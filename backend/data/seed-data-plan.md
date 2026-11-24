# Module 3 (AI Copilot): tables, seed data and where each piece lives

Sources: "Grameenphone Demo .docx" Section 5 (CP1 to CP12, Script A, ready check), plus what has been built and seeded.

Labels used in the "Basis" columns:
- **Doc**: stated in the playbook.
- **Public**: copied from an official public GP page. The source URL is stored with the item.
- **Mock**: invented so the demo can run. It must be called a mock on screen.
- **Sample**: the playbook itself marks it "(sample)".

**Status (2026-11-24).** The six DynamoDB tables exist in region `ap-south-1` and hold 31 seeded items. The app reads and writes them. If DynamoDB cannot be reached, it answers from the local seed files instead (see section 5). `GET /api/health` shows which one is in use.

## 1. Where each kind of data lives

| Kind | What | Where |
|---|---|---|
| Reference data | Customers, network status per area, packs, GP Centres | DynamoDB |
| Runtime records | Tickets, calls | DynamoDB, written by the app |
| Rules and word lists | Compliance rules, next-best-action rules, mood and network keywords, small-talk phrases | JSON files in `backend/data/mock` |
| Prompts | The four instructions given to the model | Markdown files in `backend/data/prompts` |
| Knowledge articles | Approved answers with sources | Markdown files in `backend/data/knowledge/articles` now, S3 behind the shared knowledge base later |
| Files | Call recordings, transcripts, exported PDFs | S3 (bucket not set yet) |

## 2. The six DynamoDB tables

Table names are the prefix plus the short name. The prefix is `gp-copilot-` and comes from `DYNAMODB_TABLE_PREFIX` in `config/.env`. All tables use on-demand capacity and have no secondary indexes.

| Table | Partition key | Sort key | Items now | What it holds |
|---|---|---|---|---|
| `gp-copilot-customers` | `mobile_number` | none | 5 | The customer card: plan, balance, pack, recent recharges, bill |
| `gp-copilot-network-areas` | `area` | none | 7 | Network status for each area, and the names the agent may type for it |
| `gp-copilot-tickets` | `customer_id` | `ticket_id` | 3 | Support tickets: Karim's earlier ones now, new ones from the app. A ticket raised with no confirmed customer is filed under `unidentified`. |
| `gp-copilot-calls` | `call_id` | none | 0 | One record per call, written by the app |
| `gp-copilot-packs` | `pack_id` | none | 5 | Pack catalog: price, validity, activation code |
| `gp-copilot-gp-centres` | `area` | `name` | 11 | GP Centres by area, for the lost-SIM case |

All keys are Strings. `name` is a reserved word in DynamoDB expressions, so any query on `gp-copilot-gp-centres` must use a placeholder for it.

### 2.1 `gp-copilot-customers`

Key: `mobile_number`, stored as `01XXXXXXXXX`. The app converts `+880…` input to that form before looking it up.

| Attribute | Meaning |
|---|---|
| `mobile_number` | Key. The number the agent confirms with the customer. |
| `id` | Short id (`rahim`, `nusrat`, `karim`, `sumon`, `mitu`). Ties tickets and calls to the customer. |
| `name`, `type`, `tier` | Name, Prepaid or Postpaid, and customer tier |
| `area` | The registered area. It matches one row of `network-areas`. |
| `balance_bdt` | Main balance in taka |
| `pack` | `{name, data_left_gb, valid_until}` |
| `recent_recharges` | List of `{amount_bdt, date}` |
| `usage_pattern` | A sentence the suggestions use (for example, he runs out of data early) |
| `bill`, `charges` | Nusrat only. `bill` is `{last_month_bdt, this_month_bdt, due_date}`, and `charges` is a list of `{item, amount_bdt}`. |
| `source` | `mock` |
| `team_phone` | `true` on an extra copy made for a verified team phone (see below) |

Not stored here: open tickets (they live in `tickets`) and a separate `msisdn` (the key replaces it).

| Customer (id) | Mobile number | Type, tier | Area | Current pack | Balance | Recent recharges | Used for |
|---|---|---|---|---|---|---|---|
| Rahim Uddin (`rahim`) | 01200000001 | Prepaid, Standard | Mirpur | 25 GB, 30 days, 18 GB left, until 05 Dec | Tk 120 | Tk 200 (20 Nov), Tk 100 (10 Nov) | CP1, CP3, CP4, CP5, Script A. Used up his pack early in each of the last 3 months. |
| Nusrat Jahan (`nusrat`) | 01200000002 | Postpaid, Gold | Dhanmondi | Postpaid 20 GB, 6 GB left, until 30 Nov | Tk 0 | none | CP2. Bill 500 to 850, due 07 Dec. Extra internet Tk 250, and the value-added service "Cricket Alert (sample)" Tk 100 she did not activate. |
| Karim Hossain (`karim`) | 01200000003 | Prepaid, Standard | Uttara | 10 GB, 30 days, 2 GB left, until 01 Dec | Tk 45 | Tk 100 (18 Nov) | The angry-caller case. Three earlier complaints. |
| Sumon Ahmed (`sumon`) | 01200000004 | Postpaid, Platinum | Gulshan | Postpaid 40 GB, 22 GB left, until 30 Nov | Tk 0 | none | CP10 roaming. Going to Malaysia next week, roaming not activated. |
| Mitu Akter (`mitu`) | 01200000005 | Prepaid, Standard | Chattogram | 8 GB, 7 days, 5 GB left, until 29 Nov | Tk 80 | Tk 199 (22 Nov) | CP9 lost SIM. |

Basis:
- **Doc:** the five names, Rahim's 25 GB pack with 18 GB left, and Nusrat's 500 to 850 bill with the Tk 100 service charge.
- **Mock:** everything else, including all balances, recharges, dates and areas.
- **Mitu:** the playbook names her only in the ready check and gives no scenario. The lost SIM is an assumption.

Mobile numbers and team phones:
- The numbers are invented. The `012` prefix is not used by any mobile operator, so they cannot belong to a real person.
- A verified team phone counts as that customer's number. List it in `DEMO_PHONE_MAP` (`+8801XXXXXXXXX:rahim`) and run the seed again. That adds an extra item keyed by the team phone, with `team_phone: true` and the same `id`.
- Dates such as "20 Nov" and "05 Dec" are fixed text, written for the demo date of 26 Nov 2026. Change them if the date moves.

### 2.2 `gp-copilot-network-areas`

Key: `area`. This table answers two questions: what is the network status at the customer's registered area, and at the area they are in right now.

| Attribute | Meaning |
|---|---|
| `area` | Key. The canonical area name. |
| `aliases` | The names the agent may type, in English and Bangla |
| `notice` | `{status, message, until}`, or null when no notice is on file |
| `source` | `mock` |

| Area | Aliases | Notice |
|---|---|---|
| Mirpur | mirpur, মিরপুর | Maintenance: "Network maintenance in the Mirpur area", until 6:00 PM today (Doc: Rahim's area, until 6 pm) |
| Uttara | uttara, উত্তরা | Scheduled: "Planned capacity upgrade in the Uttara area", until 2:00 AM tonight |
| Gazipur | gazipur, joydebpur, গাজীপুর | Outage: "Mobile data outage after a fibre cut in the Gazipur area", until 9:00 PM today |
| Dhanmondi | dhanmondi, ধানমন্ডি | none |
| Gulshan | gulshan, গুলশান | none |
| Chattogram | chattogram, chittagong, ctg, চট্টগ্রাম, চিটাগাং | none |
| Savar | savar, সাভার | none |

- Status values are `maintenance`, `scheduled` and `outage`. When `notice` is null the app shows `no_notice`. That means no notice is on file, never that the network is fine.
- Mirpur is Doc. Every other notice is Mock. The `until` text says "today", so edit it to match the demo day.
- Editing a row in the table changes the status the next time that area is checked.

### 2.3 `gp-copilot-tickets`

Key: `customer_id` (partition) and `ticket_id` (sort). The customer card's open tickets are a query on `customer_id`.

| Attribute | Meaning |
|---|---|
| `customer_id`, `ticket_id` | Keys, for example `karim` and `TKT-10101` |
| `category` | Network, Billing, Pack, SIM, Roaming or Other |
| `subject`, `problem` | Short text |
| `status` | `open` or `closed` |
| `created_at` | ISO date and time |
| `source` | `seed` on seeded rows |
| `checked`, `outcome`, `follow_up`, `call_sid` | Added by tickets the app creates (CP7) |

Seeded: TKT-10101, TKT-10144 and TKT-10190, all for Karim. All three are Network, "Slow internet", open, created 9, 5 and 2 days before the day the seed ran. Re-running the seed resets those dates. New tickets are numbered from `TICKET_START_NUMBER` (TKT-10234). Basis: Doc ("three earlier complaints"). The dates and topic are Mock.

### 2.4 `gp-copilot-calls`

Key: `call_id`. Empty on purpose. The app writes one item when a call ends, and again when a ticket is created. Each item is far below the 400 KB item limit.

| Attribute | Meaning |
|---|---|
| `call_id` | Key (today the code calls it `call_sid`) |
| `customer_id`, `identified_at` | Who the agent confirmed, and when. Null if never confirmed. |
| `current_location` | The network status of the area the customer said they were in |
| `started_at`, `ended_at` | Times |
| `turns` | List of `{id, speaker, text, ts}`. This is the transcript. |
| `mood_history` | The customer's mood after each analysis |
| `suggestions` | Next-best-actions shown, with whether each was used |
| `answers` | Automatic lookups: question, answer, sources, confidence, whether customer data was used |
| `compliance` | State of each compliance rule (idle, red, pending, green) |
| `summary` | `{problem, checked, outcome, follow_up, category, subject}` |
| `ticket`, `recording_url` | The ticket created, and where the recording is |

It feeds the adoption dashboard (CP12) and the hand-off to the quality module (Q11). The dashboard's sample figures come from `dashboard_seed.json`, so no calls are seeded.

### 2.5 `gp-copilot-packs`

Key: `pack_id`.

| Attribute | Meaning |
|---|---|
| `name`, `category` | Pack name, and `bundle` or `roaming` |
| `data_gb`, `minutes` | Allowance. `minutes` is null for data-only. |
| `price_bdt`, `validity_days` | Price and validity |
| `activation_code` | USSD code. Null when none is published. Codes are never invented. |
| `countries` | Roaming packs only |
| `type`, `source_url`, `fetched` | `public`, where it came from, and when |
| `verify_before_demo` | `true` on every item |
| `sample_offer` | `true` on the pack used as "the bigger 30-day pack (sample)" |
| `notes` | Caveats |

| `pack_id` | Pack | Price | Validity | Code | Basis |
|---|---|---|---|---|---|
| `bundle-5gb-100min-7d` | 5 GB + 100 minutes | Tk 169 | 7 days | none published | Public |
| `bundle-8gb-150min-7d` | 8 GB + 150 minutes | Tk 199 | 7 days | none published | Public |
| `bundle-30gb-300min-30d` | 30 GB + 300 minutes | Tk 598 | 30 days | none published | Public. Used as the "bigger 30-day pack (sample)" for CP3. |
| `bundle-60gb-1500min-30d` | 60 GB + 1500 minutes | Tk 999 | 30 days | none published | Public |
| `roaming-travel-10gb-10d` | 10 Days Travel Pack: 10 GB, valid in Thailand, Malaysia, Singapore and India | Tk 1599 | 10 days | `*121*1599#` | Public. For Sumon (CP10). |

- Rahim's current 25 GB pack is not in the catalog, because it has no published price.
- The bundle prices come from a public snapshot, and prices change often. Recheck all of them on the live page.
- The roaming site forbids automated access, and a script read it before that was noticed. A person must confirm that pack on the live page.

### 2.6 `gp-copilot-gp-centres`

Key: `area` (partition) and `name` (sort). Used for "the nearest GP Centre (sample)" in the lost-SIM case (CP9).

| Attribute | Meaning |
|---|---|
| `area`, `name` | Keys |
| `kind` | `GP Centre` |
| `address`, `hours` | As published. `hours` is null on the sample row. |
| `type`, `source_url`, `fetched` | `public` or `sample`, the store-locator page, and the date read (2026-11-23) |

| Area | Name | Hours | Type |
|---|---|---|---|
| Mirpur | Grameenphone Center Mirpur-1 | 11:00 AM- 7:00 PM | public |
| Mirpur | Grameenphone Center Mirpur-11 | 10 AM- 6 PM | public |
| Uttara | Grameenphone Center Uttara | 9 AM- 7 PM | public |
| Uttara | Grameenphone Center Sonargaon Janapath | 10 AM- 6 PM | public |
| Uttara | Grameenphone Center Airport Departure Dhaka | 24 Hours Open | public |
| Dhanmondi | Grameenphone Center Hatirpul | 11:00 AM- 7:00 PM | public |
| Dhanmondi | Grameenphone Center Rapa Plaza | 11:00 AM- 7:00 PM | public |
| Dhanmondi | Grameenphone Center Shimanto Square | 11:00 AM- 7:00 PM | public |
| Gulshan | Grameenphone Experience, Gulshan | 9 AM- 7 PM | public |
| Savar | Grameenphone Center Savar | 10 AM- 6 PM | public |
| Chattogram | GP Centre, Chattogram (sample) | none | sample |

The store-locator page lists no GP Centre for Chattogram, and Mitu (CP9) lives there. The Chattogram row is a labelled placeholder with no real address. Gazipur has no GP Centre on that page either, so it has no row.

## 3. Which scenario uses what

| CP | Tables read or written | Files and articles |
|---|---|---|
| CP1 Internet not working | customers, network-areas (registered and current location), tickets | `internet-troubleshooting`, `network-outage-goodwill`, `nba-network-apology` |
| CP2 Bill dispute | customers (bill, charges) | `vas-refund-policy`, `vas-unsubscribe`, `postpaid-bill-payment` |
| CP3 Right offer | customers (usage pattern), packs | `nba-bigger-pack` |
| CP4 Compliance | packs (price, validity) | `compliance_rules.json` |
| CP5 Mood | none | `mood_keywords.json` |
| CP6 Ask Copilot | none | knowledge articles |
| CP7 Summary and ticket | tickets (write), calls (write) | `summary.md` prompt |
| CP8 Carry-forward | none | `carry-forward-sample` article. It is a rule, not a table. |
| CP9 Lost SIM | customers (Mitu), gp-centres | `sim-replacement`, `nba-sim-block` |
| CP10 Roaming | customers (Sumon), packs | `roaming-*` articles, `nba-roaming-pack` |
| CP11 English recharge | customers (recent recharges) | `recharge-terms` |
| CP12 Dashboard | calls | `dashboard_seed.json` |

## 4. Data that stays in files, not in DynamoDB

These are app logic or content that should be reviewed and versioned in git.

| File | What it holds |
|---|---|
| `mock/customers.json`, `network_areas.json`, `packs.json`, `gp_centres.json` | The seed source for the tables above. Edit them, then run the seed again. |
| `mock/compliance_rules.json` | 2 rules: `verify_identity` and `read_price_validity` |
| `mock/nba_rules.json` | 5 next-best-actions: `nba-network-apology`, `nba-bigger-pack`, `nba-vas-refund`, `nba-roaming-pack`, `nba-sim-block` |
| `mock/mood_keywords.json` | Words that signal an angry or frustrated customer, used when the model is unavailable |
| `mock/network_keywords.json` | Words that signal a network problem, which prompts the agent to ask the location |
| `mock/smalltalk.json` | Greetings and acknowledgements that skip the automatic lookup |
| `mock/dashboard_seed.json` | Sample dashboard figures, labelled as sample |
| `prompts/analyze_turn.md`, `live_assist.md`, `summary.md`, `ask_copilot.md` | The instructions given to the model |

### Knowledge articles (15, English only)

Public: `bundle-packs`, `customer-contacts`, `internet-packs-general`, `internet-troubleshooting`, `postpaid-bill-payment`, `recharge-terms`, `roaming-activation`, `roaming-basics`, `roaming-malaysia-pack`, `roaming-travelsure`, `sim-replacement`, `vas-unsubscribe`.

Mock: `carry-forward-sample`, `network-outage-goodwill`, `vas-refund-policy`.

Bangla versions are not written yet. Each article carries its source URL, fetch date and public or mock label.

## 5. When data is read or written during a call

| Moment | What happens | Table or file |
|---|---|---|
| Startup | All 7 areas are loaded, so typed names (English or Bangla) can be matched | network-areas |
| Call connects | Nothing about the customer is loaded. The caller ID only pre-fills the mobile number box. | none |
| Agent confirms the mobile number | One read of the customer by number. Then, in parallel, the customer's open tickets and the network status of the registered area. The card is built from these. | customers, tickets, network-areas |
| Customer reports a network problem | The panel prompts the agent to ask where the customer is | `network_keywords.json` |
| Agent enters the current location | The area name is matched, then its status is read fresh from the table, so an edited notice shows on the next check | network-areas |
| Each customer sentence | Matching articles, the pack catalog, the GP Centres of the customer's areas, the card and both area statuses go into one model call | articles, packs, gp-centres |
| Suggestion shown: bigger pack or roaming pack (CP3, CP4, CP10) | The suggestion carries the pack's name, price, validity and activation code | packs |
| Suggestion shown: lost SIM (CP9) | The suggestion carries up to 3 GP Centres per area, for the registered area and the current location | gp-centres |
| Agent submits the ticket | The next free number (from `TICKET_START_NUMBER`) is saved with a check that it does not already exist. A second ticket created at the same moment takes the next number. | tickets |
| Call ends, or a ticket is created | The whole call record is saved. A copy goes to S3 only if `S3_BUCKET` is set. | calls |
| Dashboard | Totals over the saved calls, plus the sample figures | calls, `dashboard_seed.json` |
| Reset | Deletes the tickets the app created and all calls. Seeded tickets stay. | tickets, calls |

**If DynamoDB cannot be reached** (for example the AWS credentials expired), every read above is answered from the local seed files and every write goes to local files. DynamoDB is tried again after `DATA_RETRY_AFTER_S` seconds. Whatever was written meanwhile is not copied to the tables later. `GET /api/health` shows `fallback_active` and the reason.

**Speed, measured from a laptop to `ap-south-1`:** one read takes about 31 to 39 ms once connected. The identity-confirm reads together took about 250 ms the first time, which opens the connections, and 50 to 63 ms afterwards. It has not been measured from EC2 or during a call.

## 6. Seeding

Run from the `backend` folder:

```
python -m scripts.seed_dynamodb --dry-run     show what would be written, change nothing
python -m scripts.seed_dynamodb               write the data, then read every item back and compare
```

- The items are built by `copilot/repo/seed.py`, the same code the local-file fallback uses, so the tables and the fallback always hold the same data.
- It is safe to repeat. It overwrites items and never deletes anything.
- It touches only the six tables named by `DYNAMODB_TABLE_PREFIX`. It checks that each exists with the right keys before it writes anything.
- Credentials and region come from `config/.env`. They are temporary credentials, so an expired-token error means refreshing the `AWS_*` values.
- To change the data, edit the JSON files in `data/mock` and run it again.

## 7. Open items and checks before the demo

- Confirm Mitu's scenario with the demo lead. The lost SIM is an assumption.
- A person must confirm every pack price and the roaming pack on the live GP pages.
- The Chattogram GP Centre is a placeholder. Replace it if GP gives a real one.
- Adjust the "today" and "tonight" times in the network notices, and the fixed dates in the customer data, to the demo day.
- Provide the verified team phones for `DEMO_PHONE_MAP`, then seed again.
- Choose the carry-forward rule to show: GP's public rule, or the playbook's 3-day sample.
- Two public pages disagree on the charge after a pack ends (Tk 5.825 or Tk 6.95). Choose one.
- Confirm who owns the shared knowledge base and what its pack will include.
- Set `S3_BUCKET` for recordings, transcripts and the exported PDFs.
- Section 8 of the playbook (Q&A answers) is missing from the document.

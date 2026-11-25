# CP1-CP12 Data Mapping

Documentation only. No application code, DynamoDB record, Knowledge Base document or prompt was changed to produce this file.

How this was verified (2026-11-25, Wednesday):
- Read the project code, prompts, mock JSON files and `backend/data/knowledge/articles/*.md`.
- Ran read-only DynamoDB scans of the six `gp-copilot-*` tables in `ap-south-1`, using the project's own settings.
- Ran read-only `Retrieve` calls against the Bedrock Knowledge Base configured in `config/.env` (the KB id is set; it is not copied here).
- The four CSV exports you mentioned are not in the workspace (no `.csv` file exists in the project). The live tables were scanned instead. If the CSVs differ from the tables, send them and this file should be rechecked.

Label used for every fact below:
- **DDB** = read from the live DynamoDB table on 2026-11-25.
- **KB** = retrievable from the Bedrock Knowledge Base, or present in the article files that feed it.
- **CODE** = defined in a rule file, prompt or service in the repository.
- **MOCK / SAMPLE** = invented for the demo. Say so on screen.

---

## Decisions applied in this revision (2026-11-25)

| Topic | Decision | Effect on the data mapping |
|---|---|---|
| CP8 | Show public GP information and the mock carry-forward policy separately, each with its source. Never present the mock policy as GP policy. | `internet-packs-general` (public) and `carry-forward-sample` (mock) are mapped as two separate sources. |
| CP9 | Mitu's lost-SIM scenario is unconfirmed and hypothetical. It is not tied to her real record. | Mitu's card is a stand-in profile only. The seed text "Reported a lost SIM." in her `usage_pattern` is treated as the same unconfirmed assumption, not evidence. |
| Postpaid identity | Last recharge is not used for postpaid customers. The approved procedure is missing. No protected information is shared and no service is activated without verification. | Nusrat (CP2) and Sumon (CP10) are marked "verification procedure MISSING". |
| Cricket Alert | If no stop code exists the expected Copilot response is "Not found in approved knowledge." No code is invented. | Marked MISSING. No article in the KB or the PDFs I retrieved holds it. |
| Prices | Catalog values are preserved. Every `verify_before_demo = true` value is UNVERIFIED until a person confirms it. Sample offers are not live offers. | Status column added to the pack table. |
| KB privacy | Investigate why `copilot/calls/` transcripts are retrieved. Identify the data source configuration and recommend isolation. No AWS change. | See section 2.5. |
| CP12 | Separate real saved-call metrics from fixed demo metrics. State the effect of test calls and test tickets. | See the CP12 tables at the end. |

Status vocabulary used from here on: **UNVERIFIED** = catalog or article value that a person has not confirmed on the live GP page. **MISSING** = no approved source exists. **MOCK / SAMPLE / HYPOTHETICAL** = demo content.

---
## 1. What is actually in DynamoDB (live scan)

| Table | Keys | Items found | Notes |
|---|---|---|---|
| `gp-copilot-customers` | `mobile_number` (HASH) | 6 | Five seeded customers plus one extra item (see 1.1) |
| `gp-copilot-network-areas` | `area` (HASH) | 7 | Same as `mock/network_areas.json` |
| `gp-copilot-tickets` | `customer_id` (HASH), `ticket_id` (RANGE) | 4 | Karim's 3 seeded tickets plus TKT-10234 (see 1.2) |
| `gp-copilot-calls` | `call_id` (HASH) | 9 | Test calls from earlier rehearsals (see 1.3). They count in the dashboard. |
| `gp-copilot-packs` | `pack_id` (HASH) | 5 | Same as `mock/packs.json` |
| `gp-copilot-gp-centres` | `area` (HASH), `name` (RANGE) | 11 | Same as `mock/gp_centres.json` |

`DescribeTable` showed older item counts (5, 3, 3 for customers, tickets, calls) because DynamoDB refreshes that number only about every 6 hours. The scan numbers above are the real ones.

The AWS account also holds many unrelated tables (other projects). They were not read.

### 1.1 Customers (DDB, key = `mobile_number`)

| Customer | `mobile_number` | Type, tier | Area | Balance | Pack | Recent recharges | Other fields |
|---|---|---|---|---|---|---|---|
| Rahim Uddin (`rahim`) | 01200000001 | Prepaid, Standard | Mirpur | Tk 120 | 25 GB, 30 days, 18 GB left, until 05 Dec | Tk 200 (20 Nov), Tk 100 (10 Nov) | usage_pattern: "Used up the pack before validity ended in each of the last 3 months." |
| Nusrat Jahan (`nusrat`) | 01200000002 | Postpaid, Gold | Dhanmondi | Tk 0 | Postpaid 20 GB, 6 GB left, until 30 Nov | none | bill: last month Tk 500, this month Tk 850, due 07 Dec. charges: "Extra internet use" Tk 250, "Value-added service: Cricket Alert (sample)" Tk 100. usage_pattern: "Did not knowingly activate any value-added service." |
| Karim Hossain (`karim`) | 01200000003 | Prepaid, Standard | Uttara | Tk 45 | 10 GB, 30 days, 2 GB left, until 01 Dec | Tk 100 (18 Nov) | usage_pattern: "Three earlier complaints about the same issue." Open tickets come from the tickets table (1.2). |
| Sumon Ahmed (`sumon`) | 01200000004 | Postpaid, Platinum | Gulshan | Tk 0 | Postpaid 40 GB, 22 GB left, until 30 Nov | none | usage_pattern: "Travelling to Malaysia next week. Roaming not activated." |
| Mitu Akter (`mitu`) | 01200000005 | Prepaid, Standard | Chattogram | Tk 80 | 8 GB, 7 days, 5 GB left, until 29 Nov | Tk 199 (22 Nov) | usage_pattern: "Reported a lost SIM." |

All five have `source = mock`. The mobile numbers use the `012` prefix, which no real operator uses.

Fields that do NOT exist on any customer: roaming type, roaming status, SIM or Skitto flag, NID status, pack price, pack activation date, bill payment status, VAS activation date, last-bill breakdown beyond the two `charges` lines, language preference.

**1.1.1 Extra item (not in the seed files).** One more customer item exists: key `919500799295`, id `adbul`, name "Abdul Rahim", Prepaid Standard, Mirpur, with the same balance, pack and recharges as Rahim and no `team_phone` flag. It looks like a team-phone test copy. It is not used in any script. See the Data Gaps file.

### 1.2 Tickets (DDB, keys `customer_id` + `ticket_id`)

| customer_id | ticket_id | Category | Subject | Status | created_at | Source |
|---|---|---|---|---|---|---|
| karim | TKT-10101 | Network | Slow internet | open | 2026-11-15T09:42:18+00:00 | seed |
| karim | TKT-10144 | Network | Slow internet | open | 2026-11-19T09:42:18+00:00 | seed |
| karim | TKT-10190 | Network | Slow internet | open | 2026-11-22T09:42:18+00:00 | seed |
| adbul | TKT-10234 | Network | Network issue in Mirpur + balance enquiry - maintenance ongoing | open | 2026-11-25T06:37:42+00:00 | app (test) |

The customer card shows ticket age as "N days" computed when the card loads. On 25 Nov the three Karim tickets show about 10, 6 and 3 days; on 26 Nov they show 11, 7 and 4 days. Do not hard-code the ages in a spoken script.

New tickets get the next free number after the highest existing one. With TKT-10234 present, the next one is expected to be TKT-10235, but the number is assigned by the system when the agent presses Submit. Scripts below never state a new ticket number.

### 1.3 Calls (DDB, key `call_id`)

Nine saved call records (2 for the extra customer `adbul`, 7 with no customer). Between them they hold 6 suggestions shown and 1 used, and 106 mood readings (89 neutral, 17 frustrated, 0 angry). These feed the dashboard (CP12).

### 1.4 Network areas (DDB, key `area`)

| Area | Notice status | Message | Until |
|---|---|---|---|
| Mirpur | maintenance | Network maintenance in the Mirpur area | 6:00 PM today |
| Uttara | scheduled | Planned capacity upgrade in the Uttara area | 2:00 AM tonight |
| Gazipur | outage | Mobile data outage after a fibre cut in the Gazipur area | 9:00 PM today |
| Dhanmondi | none | | |
| Gulshan | none | | |
| Chattogram | none | | |
| Savar | none | | |

A `none` notice means "no notice on file", never "network is fine". The "today" and "tonight" words are fixed text, not dates.

### 1.5 Pack catalog (DDB, key `pack_id`). All five rows have `verify_before_demo = true`, so **every price, validity and code below is UNVERIFIED until a person confirms it on the live GP page.** The values are the catalog values, unchanged.

| pack_id | Name | Price | Validity | Activation code | Flags |
|---|---|---|---|---|---|
| bundle-5gb-100min-7d | 5 GB + 100 minutes | Tk 169 | 7 days | none published | public snapshot |
| bundle-8gb-150min-7d | 8 GB + 150 minutes | Tk 199 | 7 days | none published | public snapshot |
| bundle-30gb-300min-30d | 30 GB + 300 minutes | Tk 598 | 30 days | none published | **sample_offer = true**: a demo sample offer, not a live GP commercial offer. UNVERIFIED. |
| bundle-60gb-1500min-30d | 60 GB + 1500 minutes | Tk 999 | 30 days | none published | public snapshot |
| roaming-travel-10gb-10d | 10 Days Travel Pack: 10 GB, Thailand, Malaysia, Singapore, India | Tk 1599 | 10 days | `*121*1599#` | public. The roaming site's robots.txt forbids automated access and a script read it before that was noticed, so a person must confirm it on the live page. |

Rahim's own pack (25 GB, 30 days) and Mitu's pack (8 GB, 7 days) are not in the catalog and have no recorded price.

### 1.6 GP Centres (DDB, keys `area` + `name`)

| Area | Centres | Real or placeholder |
|---|---|---|
| Mirpur | Mirpur-1 (11 AM-7 PM), Mirpur-11 (10 AM-6 PM) | real (public store locator, fetched 2026-11-23) |
| Uttara | Uttara (9 AM-7 PM), Sonargaon Janapath (10 AM-6 PM), Airport Departure Dhaka (24 hours) | real |
| Dhanmondi | Hatirpul, Rapa Plaza, Shimanto Square (all 11 AM-7 PM) | real |
| Gulshan | Grameenphone Experience, Gulshan (9 AM-7 PM) | real |
| Savar | Grameenphone Center Savar (10 AM-6 PM) | real |
| Chattogram | "GP Centre, Chattogram (sample)", address "Sample entry. Not a real GP Centre address.", hours none | **placeholder** |
| Gazipur | no row | none |

---

## 2. What is in the Knowledge Base

### 2.1 How it is built and searched

- Source files: 15 hand-written articles in `backend/data/knowledge/articles/*.md`, plus 58 PDF exports of public grameenphone.com pages (43 English, 15 Bangla) described in `exported-pdfs.md`.
- `scripts/upload_kb.py` uploads them to the S3 bucket under `en/articles/`, `en/pdf/` and `bn/pdf/`, each with a `.metadata.json` sidecar (`doc_id`, `title`, `type`, `category`, `source_url`, `fetched`, `language`, `source_format`).
- Retrieval: `KnowledgeRetriever.search()` calls Bedrock `Retrieve` (top 5 chunks, no `RetrieveAndGenerate`), groups chunks by `doc_id`, then merges the result with a local keyword search over the 15 article files (reciprocal rank fusion). If the KB errors or takes longer than 4 s, the local keyword search answers and the KB is skipped for 30 s. `BEDROCK_KB_REWRITE_QUERY` is off, so the customer's sentence is searched as spoken.
- Model calls use the Bedrock Converse API with inference-profile ARNs (a "fast" one for live analysis and Ask Copilot, a "summary" one for the after-call note). Temperature 0.1. The model names behind the ARNs were not checked.

### 2.2 The 15 articles (KB)

| Article id | Type | Topic | Used by |
|---|---|---|---|
| internet-troubleshooting | public | Data on, balance check `*121*1*4#`, Android APN `gpinternet`, iPhone network reset, check area notice | CP1, CP5, CP11 |
| network-outage-goodwill | **mock** | Apologise, give cause and fix time; goodwill data bonus if outage longer than 24 hours "if policy allows"; raise network ticket if still failing after fix time | CP1, CP5 |
| internet-packs-general | public | Auto-renew codes, balance check, cancel `*121*3041#`, charge after volume ends (Tk 1.165/MB up to Tk 5.825), carry-forward rule (same pack within validity), Skitto excluded. Conflict flagged (Tk 5.825 vs Tk 6.95). | CP3, CP8, CP11 |
| bundle-packs | public snapshot | The four bundle packs and prices (same as the catalog) | CP3 |
| carry-forward-sample | **mock** | SAMPLE rule: same pack bought within 3 days after expiry keeps leftover data; later loses it | CP8 |
| vas-refund-policy | **mock** | Confirm not activated knowingly, deactivate, raise refund request, record ticket number | CP2 |
| vas-unsubscribe | public | STOP keywords for Missed Call Alert, News, Study Line, Call Block. **Cricket Alert is not listed.** | CP2, CP6 |
| postpaid-bill-payment | public | Pay in MyGP or channels (prefix 78 for some), e-bill within 24 h after cycle, MyGP Account > Usage History (last 30 days) | CP2 |
| recharge-terms | public | Recharge rules, 30-minute wait, `*566#`, limits, what to do if a recharge does not show | CP11 |
| sim-replacement | public | Fee Tk 300 (free for Gold, Platinum, Signature Star only), NID plus fingerprint, online delivery only selected metro areas, Skitto excluded, compliance order, "no public block procedure, do not invent one" | CP9 |
| customer-contacts | public | 121 (Tk 0.50/min), 158 (free complaints), email, live chat, hotlines | CP9 |
| roaming-basics | public | Roaming types, how to buy a roaming pack, migrate to Standard `*66*1*2#` | CP6, CP10 |
| roaming-activation | public | Three ways to activate (online with USD card, agent support, in person), prepaid roaming documents. **Flagged "check before use".** | CP6, CP10 |
| roaming-malaysia-pack | public | 10 Days Travel Pack Tk 1599, 10 GB, 10 days, `*121*1599#`, plus other packs and rules. **Flagged "check before use".** | CP6, CP10 |
| roaming-travelsure | public | IR status `*121*6*6*1#`, Usage Alarm `*121*6*6*5*1#`, data limit options, set via `*121*6*6*3#`. **Flagged "check before use".** | CP10 |

### 2.3 PDFs in the KB (confirmed by retrieval)

Retrieval returned these PDF documents (`source_format = pdf`): handset-settings, campaign-faq, sim-replacement, sim-ownership-transfer, sim-services, esim, bundle-packs, prime-help-faq (English and Bangla), vas-overview (Bangla), vas-missed-call-alert, vas-call-block, postpaid-bill-payment (Bangla), recharge-terms, roaming-bonus-packs. The full list of 58 is in `exported-pdfs.md`; the ones not returned by my 12 test queries were not individually confirmed.

Not exported (robots.txt): roaming activation and roaming bill-payment pages. Roaming rates, roaming FAQ, per-country Malaysia page: "to collect" or refused (403) according to `GP-public-documents.md`.

### 2.4 Rules that are NOT in the Knowledge Base (they live in code)

| Item | File | Content |
|---|---|---|
| Compliance rules (2) | `mock/compliance_rules.json` | `verify_identity` and `read_price_validity` (details in 3.1) |
| Next-best-actions (5) | `mock/nba_rules.json` | nba-network-apology, nba-bigger-pack, nba-vas-refund, nba-roaming-pack, nba-sim-block |
| Mood fallback words | `mock/mood_keywords.json` | used only if the Bedrock analysis call fails |
| Network-problem words | `mock/network_keywords.json` | triggers the "ask where the customer is" prompt |
| Small talk list | `mock/smalltalk.json` | greetings and acknowledgements skip the lookup |
| Dashboard sample numbers | `mock/dashboard_seed.json` | labelled "Sample data for the demo" |

There is no identity-verification policy article, no escalation policy article, no goodwill amount, no refund timeline and no Bangla article in the KB.

### 2.5 Investigation: why call transcripts are retrieved as knowledge articles

**Finding.** Saved call records are retrieved because the Knowledge Base data source indexes the whole S3 bucket, and the application writes call records into that same bucket.

Evidence (read-only checks on 2026-11-25; nothing was changed):

| Check | Result |
|---|---|
| Knowledge Base | "Grameenphone-KB", status ACTIVE, embedding model Amazon Titan Text Embeddings v2 |
| Data sources | One: "knowledge-base-quick-start-S3", type S3 |
| Bucket of that data source | The same bucket as the application's `S3_BUCKET` setting |
| `inclusionPrefixes` | **Not set.** With no prefix filter the data source covers every object in the bucket. |
| Parsing | Foundation-model parsing. Chunking left at the default (not set). |
| Data deletion policy | DELETE (a document removed from S3 is removed from the index at the next sync) |
| Bucket layout | Three top-level prefixes: `en/` (articles 30 objects = 15 files + 15 metadata sidecars; PDFs 86 = 43 + 43), `bn/` (PDFs 30 = 15 + 15) and **`copilot/calls/` (19 call JSON files, 2026-11-24 13:33 to 2026-11-25 08:46)** |
| Who writes `copilot/calls/` | `CallStorage._upload` in `copilot/services/storage.py`. Every time a call is saved (when it ends, and again when a ticket is created) it uploads the whole call record to `<S3_PREFIX>/<call_id>.json`. `S3_PREFIX` is `copilot/calls`. |
| What a call file contains | The full transcript (every line, including numbers the customer spoke), customer id, mood history, suggestions, lookup answers, summary and ticket. |
| Ingestion history (the latest three jobs listed) | 24 Nov 13:21: 73 documents indexed. 24 Nov 12:56: 73 failed (an earlier attempt). **24 Nov 17:17: 79 scanned, 6 new, 1 modified**, which is consistent with call files being picked up. Call files written after that job (up to 25 Nov 08:46) are not yet indexed. The next sync will index them. |
| Retrieval tests | Four of my twelve test queries returned chunks whose S3 location was `copilot/calls/...json`. Three different call files appeared (two with `local-` ids, one UUID). |
| Metadata | The call files have no `.metadata.json` sidecar, so they have no `doc_id`. |

How the application then treats them (`copilot/services/kb_retriever.py`):
- A chunk with no `doc_id` is grouped under its S3 address and turned into an "article". Its title becomes that address, its type defaults to **public**, and its source link is empty.
- The chunk text (transcript JSON) is passed to the model as article text, in both the live lookup and Ask Copilot.
- So a past caller's conversation can be quoted in the answer for a different caller, labelled as a public source.
- The local keyword fallback is not affected. It reads only the 15 article files.

Other facts that make it worse:
- No application code deletes S3 objects. "Reset demo data" clears DynamoDB, not the S3 copies. That is why S3 holds 19 call files while DynamoDB holds 9 call records.
- Spoken mobile numbers and names are inside the transcripts.

**Recommended isolation (not applied; each is a change for the AWS or project owner):**

1. Store call records in a **separate bucket** (or at least a separate bucket from the Knowledge Base), with its own access policy. This is the strongest option.
2. If the bucket stays shared, set the data source `inclusionPrefixes` to `en/` and `bn/` only, then run a sync. Documents outside the filter are expected to leave the index (deletion policy DELETE). Confirm with a test query afterwards.
3. Review and remove or move the existing 19 call files and decide a retention period. Transcripts are personal data.
4. Restrict the Knowledge Base service role to read only the knowledge prefixes, and the application role to write only the call-records location.
5. Defence in depth in code (a later change, not done): ignore any retrieved chunk without a `doc_id` and without type metadata, instead of defaulting it to "public".
6. Until 1 or 2 is done, treat every Ask Copilot and live lookup answer as possibly containing text from a previous call, and do not demo with real customer data.

---
## 3. How context reaches the model

### 3.1 Compliance rules (CODE, plain keyword matching, case-insensitive substring)

| Rule | Starts when | Turns green when | Message |
|---|---|---|---|
| `verify_identity` | the **agent** says any of: details, account er (also Bangla-script forms) -> state red | the **agent** says any of: "last recharge", "recharge koto" (also Bangla-script forms). Green is allowed even before the trigger. | "Verify identity first (for example, ask for the last recharge amount)." |
| `read_price_validity` | the **customer** says: "chalu kore", "activate" (also Bangla-script forms) -> state pending | the **agent** says any of: price, validity, taka (also Bangla-script forms). Only counts after the trigger. | "Read the price and validity to the customer before activating." |

The words "dam" and "meyad" (Banglish for price and validity) are NOT in the list. The agent must say "price", "validity" or "taka". The engine checks that the agent asked, not that the customer answered correctly.

**Postpaid customers.** The only keyword that clears `verify_identity` is "last recharge" or "recharge koto". The client has decided it is not an approved check for postpaid customers, and no other approved method exists. So for Nusrat and Sumon the reminder can go red but must not be cleared. The approved postpaid verification procedure is **MISSING**. The panel loads the customer card as soon as the number is confirmed, before any verification, so protecting the data depends on the agent not reading it out.

### 3.2 Prompts and what each receives

| Prompt | Purpose | Gets customer card | Gets network | Gets packs and centres | Gets KB articles | Output |
|---|---|---|---|---|---|---|
| `live_assist.md` | Lookup per customer sentence | yes (null until number confirmed) | yes (registered and current) | yes | yes (top 4) | answer (English, 1-3 sentences), sources, confidence, clarifying question |
| `analyze_turn.md` | Mood, agent mood, tip, next-best-action | yes | yes | no | no | customer_mood, agent_mood, tip, suggestion_id |
| `ask_copilot.md` | Ask Copilot | **no** | **no** | **no** | yes (top 4) | answer (always English), sources |
| `summary.md` | After-call note | yes | no | no | no | problem, checked, outcome, follow_up, category, subject |
| `kb_query.md` | Rewrite sentence to English query | no | no | no | no | query (switched off) |

Rules inside `live_assist.md`: if identity is not green the answer must start with "After verifying identity:"; never say the network is working when the notice is `no_notice`; quote pack price and validity exactly; "none published" means no code; say "sample offer" when flagged; a centre marked sample is "a placeholder, not a real address"; articles marked mock must be called demo policies; never invent codes, prices, dates, policies or account details.

### 3.3 Order of events in a call

1. Call starts. No customer is loaded. Caller ID only pre-fills a box.
2. The agent confirms the mobile number (typed, or spoken and picked up by the number detector). One DynamoDB read of the customer, then in parallel the open tickets and the registered area's notice.
3. A network complaint triggers the "ask where the customer is" prompt. The agent (or the customer's reply) sets the current area and the notice is read fresh from `network-areas`.
4. Every customer sentence that is not small talk starts one lookup: KB search + packs + centres, then one model call.
5. Every customer or agent turn re-runs the mood and next-best-action analysis.
6. Agent speech updates the compliance rules and marks a suggestion "used" when a rule keyword is spoken.
7. When the call ends, the summary is written, and the whole call record is saved to `gp-copilot-calls` (and to S3 if `S3_BUCKET` is set).
8. The agent edits the draft and presses Submit to create a ticket (`POST /api/tickets`). Nothing is created automatically.

What the code does NOT do: it has no separate intent classifier (intent is implicit in the lookup, the suggestion id and the ticket category), it does not activate packs, block SIMs or refund anything, and it does not check that the customer's answer to the identity question is right.

---

## 4. Mapping per scenario

| CP | Customer (DDB key) | DynamoDB records used | Knowledge sources | Rules and prompts | Missing for this CP |
|---|---|---|---|---|---|
| CP1 Internet not working | Rahim Uddin (01200000001) | customers: Mirpur, Tk 120, 25 GB with 18 GB left until 05 Dec, recharges. network-areas: Mirpur maintenance until 6:00 PM today. tickets: none open for Rahim. | internet-troubleshooting, network-outage-goodwill (mock), internet-packs-general | nba-network-apology, verify_identity, live_assist, analyze_turn | Outage start time and cause detail; goodwill amount; Bangla article; whether the customer's phone settings are correct |
| CP2 Bill dispute | Nusrat Jahan (01200000002) | customers: Postpaid Gold, bill 500 to 850, due 07 Dec, charges Tk 250 and Tk 100. **Not to be read to the customer until a postpaid verification procedure exists.** | vas-refund-policy (mock), vas-unsubscribe (no Cricket Alert), postpaid-bill-payment | nba-vas-refund (not to be acted on before verification), verify_identity stays red | **Postpaid verification procedure (MISSING)**; Cricket Alert stop code (answer "Not found in approved knowledge."); VAS activation date; refund time; reason for the Tk 250 |
| CP3 Right offer | Rahim Uddin | customers: usage_pattern, pack. packs: bundle-30gb-300min-30d (sample_offer, Tk 598, 30 days, no code, verify_before_demo) | bundle-packs, internet-packs-general | nba-bigger-pack, read_price_validity | Activation code; Rahim's current pack price; what happens to his 18 GB if he switches pack |
| CP4 Compliance reminder | Rahim Uddin | customers (card); packs (price and validity read out) | none needed | verify_identity, read_price_validity | Rule for SIM replacement is described in the plan but is not in `compliance_rules.json` (see CP9) |
| CP5 Customer turns angry | Karim Hossain (01200000003) | customers: Uttara, Tk 45, 10 GB with 2 GB left until 01 Dec. tickets: TKT-10101, TKT-10144, TKT-10190 (Network, Slow internet, open). network-areas: Uttara scheduled upgrade until 2:00 AM tonight | network-outage-goodwill (mock), internet-troubleshooting | analyze_turn tip, nba-network-apology, mood fallback words | Escalation or supervisor policy; callback commitments; what caused the three earlier tickets |
| CP6 Ask Copilot | any caller (Ask uses no customer data) | none | roaming-basics, roaming-malaysia-pack, roaming-activation; vas-unsubscribe for the "not found" case | ask_copilot, KB retrieval | Roaming FAQ and rate pages never collected; roaming articles need manual verification |
| CP7 Summary and ticket | Karim Hossain (from the CP5 call) | calls (written at end), tickets (written on Submit) | none | summary.md, ticket categories Network, Billing, Pack, SIM, Roaming, Other | Refund or SLA time field does not exist on a ticket; no real ticketing API (mock only) |
| CP8 Unused data carry-forward | Mitu Akter (01200000005) | customers: 8 GB, 7 days, 5 GB left until 29 Nov, recharge Tk 199 on 22 Nov | **Source A, public:** internet-packs-general. **Source B, mock:** carry-forward-sample. Shown separately. | live_assist, ask_copilot | What the public page says after validity ends (nothing); Mitu's pack name vs catalog is an inference; no customer has an already-expired pack |
| CP9 Lost SIM (**HYPOTHETICAL, unconfirmed**) | Mitu Akter used only as a stand-in prepaid Standard profile (01200000005). The lost SIM is not a fact about her. | customers: Chattogram, Standard, Tk 80, recharge Tk 199 on 22 Nov. Her seed text "Reported a lost SIM." is not used as evidence. gp-centres: Chattogram placeholder only | sim-replacement, customer-contacts | nba-sim-block (show_centres), verify_identity (prepaid) | Block procedure (MISSING, answer "Not found in approved knowledge."); real Chattogram centre; online-delivery eligibility; Skitto flag; data transfer to new SIM; no SIM compliance rule; scenario confirmation |
| CP10 Roaming (Malaysia) | Sumon Ahmed (01200000004) | customers: Postpaid Platinum, Gulshan, usage_pattern about Malaysia (not read out before verification). packs: roaming-travel-10gb-10d, **UNVERIFIED** | roaming-malaysia-pack, roaming-basics, roaming-activation, roaming-travelsure (all UNVERIFIED) | nba-roaming-pack, read_price_validity | **Postpaid verification procedure (MISSING)**; roaming type; how a postpaid pack is billed; Malaysia call rates; TravelSure limit vs pack; manual price check |
| CP11 English customer | Karim Hossain | customers: Tk 100 recharge on 18 Nov, Tk 45 balance, 2 GB left. tickets: 3 open slow-internet | recharge-terms, internet-packs-general, internet-troubleshooting | live_assist, verify_identity | No per-transaction recharge record (channel, status, deductions) |
| CP12 Adoption dashboard | none | calls table: 9 saved rehearsal calls (real records, not real customers). Fixed baseline from the seed file | none | `dashboard.py`, `dashboard_seed.json` | Handling time saved, mood trend and top suggestions are fixed demo numbers. Test calls inflate the totals. |

CP12 metrics, real saved-call part against fixed demo part (values on 2026-11-25):

| Metric on screen | Fixed demo baseline | Real saved-call part | Total shown | Source of each part |
|---|---|---|---|---|
| Calls that used Copilot | 128 | 9 (all rehearsal) | 137 | `dashboard_seed.json`; DDB `gp-copilot-calls` |
| Suggestions shown | 412 | 6 | 418 | same |
| Suggestions accepted | 301 | 1 | 302, shown as 72 percent (real part alone: 1 of 6) | same |
| Handling time saved per call | 48 s | none: no code measures it | 48 s | `dashboard_seed.json` only |
| Mood trend Mon-Fri | fixed rows | 106 readings (89 neutral, 17 frustrated, 0 angry) are returned by the API but not drawn | fixed rows | `dashboard_seed.json`; `live.moods` in the API |
| Most used suggestions | fixed list (96, 88, 81, 54) | none | fixed list | `dashboard_seed.json` only |
| Footer | label text | "Live calls on this machine so far: 9" | | seed label; DDB count |

Effect of test data on dashboard reporting:
- **Test calls (9).** Each adds 1 to calls and its suggestions to shown and accepted. Seven have no identified customer and some have one or two lines, but all count. Two belong to the extra customer `adbul`.
- **Test ticket TKT-10234.** No effect on any dashboard figure. The dashboard reads only the calls table. It does change the next ticket number and appears in the ticket list.
- **Reset demo data** clears all saved calls and the app-created tickets. It would return the page to 128, 412 and 301 (73 percent), but it does not delete the S3 copies of the calls.
# CP1-CP12 Data Gaps

Documentation only. Nothing was changed in the project, DynamoDB or the Knowledge Base.
Checked on 2026-11-25 against the live `gp-copilot-*` tables, read-only Bedrock Knowledge Base retrievals and the repository files.

Legend: **AVAILABLE** = verified and safe to use in a script. **PARTIAL** = exists but is mock, a sample, or needs a person to confirm it. **MISSING** = not in any source I could read, so no script states it.

---

## 0. Decisions applied in this revision (2026-11-25)

| Topic | Decision | Where it shows up |
|---|---|---|
| CP8 | Public GP information and the mock carry-forward policy are shown separately, with a source for each. The mock policy is never called GP policy. | Section 4, scripts CP8 |
| CP9 | Mitu's lost-SIM scenario is unconfirmed and hypothetical. It is not tied to her record. | Section 4, scripts CP9 |
| Postpaid identity | Last recharge is not used. The approved procedure is MISSING. No protected information is shared and no service is activated without verification. | Sections 3 and 7, scripts CP2 and CP10 |
| Cricket Alert | Expected response is "Not found in approved knowledge." No invented code. Escalate or check the official GP process. | Section 3, scripts CP2 |
| Prices | Catalog values kept. Every `verify_before_demo` value is UNVERIFIED until a person confirms it. Sample offers are not live offers. | Section 4 |
| KB privacy | Cause identified. Isolation recommended, not applied. | Section 8 and the mapping file, 2.5 |
| CP12 | Real saved-call metrics and fixed demo metrics are separated. | Section 9 and the mapping file |

---
## 1. Available (verified)

| Item | Status | Where |
|---|---|---|
| Five customer cards (Rahim, Nusrat, Karim, Sumon, Mitu): type, tier, area, balance, pack, data left, expiry, recharges | AVAILABLE (mock data) | DDB `customers` |
| Nusrat's bill 500 to 850, due 07 Dec, two charges (Tk 250, Tk 100) | AVAILABLE (mock, Cricket Alert marked sample) | DDB `customers` |
| Karim's three open "Slow internet" tickets (TKT-10101, TKT-10144, TKT-10190) | AVAILABLE | DDB `tickets` |
| Network notices for Mirpur (maintenance, 6:00 PM today), Uttara (scheduled upgrade, 2:00 AM tonight), Gazipur (outage, 9:00 PM today) | AVAILABLE (mock) | DDB `network-areas` |
| Pack catalog: four bundles and one roaming pack, with sample_offer and verify_before_demo flags | AVAILABLE, but every row needs a person to recheck the price | DDB `packs` |
| GP Centres for Mirpur, Uttara, Dhanmondi, Gulshan, Savar | AVAILABLE (public store locator, fetched 2026-11-23) | DDB `gp-centres` |
| Internet troubleshooting steps (data on, `*121*1*4#`, APN `gpinternet`, iPhone reset) | AVAILABLE (public) | KB `internet-troubleshooting` |
| Postpaid bill payment, e-bill timing, MyGP Usage History | AVAILABLE (public) | KB `postpaid-bill-payment` |
| Recharge rules and what to do when a recharge does not show | AVAILABLE (public) | KB `recharge-terms` |
| SIM replacement fee, NID, fingerprint, online delivery limit, Skitto limit, compliance order | AVAILABLE (public, with a tier-list discrepancy to confirm) | KB `sim-replacement` |
| Roaming activation, packs, TravelSure | AVAILABLE but flagged "check before use" | KB `roaming-*` |
| Customer service contacts (121, 158, email, live chat) | AVAILABLE (public) | KB `customer-contacts` |
| Two compliance rules, five next-best-actions, mood, mood fallback words, small talk list | AVAILABLE | repository `data/mock` |
| Summary and ticket fields, ticket categories, mock ticket numbering | AVAILABLE | code |
| Dashboard: calls saved, suggestions shown and used, live mood counts | AVAILABLE | DDB `calls` |

---

## 2. Missing or only partial customer data

| Gap | Affects | What the script does instead |
|---|---|---|
| **Roaming type** (Standard, Basic, Data, SMS) and roaming status for Sumon. Only the sentence "Roaming not activated" exists. | CP10 | Agent says "I need to check your roaming type". The Copilot cannot name it. |
| **SIM type (Skitto or not), SIM lost flag, block status, NID on file** for Mitu | CP9 | Agent asks. The "lost SIM" case itself is a team assumption. |
| **Postpaid identity check.** Nusrat and Sumon have no recharge history, and "last recharge" is not approved for postpaid. The approved procedure is **MISSING**. | CP2, CP10 | Agent shares no account or bill information and activates nothing. The Copilot answers a "how do I verify a postpaid customer" question with "Not found in approved knowledge." |
| **Pack prices** for Rahim's 25 GB pack and Mitu's 8 GB pack | CP3, CP8 | Not quoted. Mitu's pack is only said to be "possibly" the Tk 199 bundle. |
| **When and how Cricket Alert was activated** (date, channel), and the list of services the customer holds | CP2 | Agent asks the customer. The card only says she did not knowingly activate it. |
| **What the Tk 250 "Extra internet use" was for.** The card also shows 6 GB left, which does not explain it. | CP2 | Agent points to MyGP Usage History. The Copilot gives no reason. |
| **Bill payment status** (paid or unpaid) | CP2 | Only the due date (07 Dec) is used. |
| **Per-transaction recharge record** (channel, status, deductions) | CP11 | The Copilot says it cannot tell why the balance is Tk 45. |
| **Outage start time or duration** for any notice. Notices have only "until". | CP1, CP5 | The goodwill bonus (over 24 hours) is not promised. |
| **Closing notes for Karim's earlier tickets** (cause, what was tried) | CP5 | The script does not claim the earlier tickets had the same cause. |
| **A customer whose pack has already expired with data left** | CP8 | Mitu is used with a pack that expires on 29 Nov. |
| **Language preference** for any customer | all | Not used. |
| **Roaming call rates** for Malaysia | CP6, CP10 | Shown as "not found". |
| **Hours for the Chattogram GP Centre** | CP9 | Placeholder says no hours. |

---

## 3. Missing policies and knowledge articles

| Gap | Affects | Detail |
|---|---|---|
| **Postpaid identity verification procedure (MISSING).** No article, rule or field defines it. The only configured rule is the prepaid-style "last recharge" keyword, which is not approved for postpaid. For prepaid customers the same rule is configured but the client has not confirmed it is sufficient. | CP2, CP10 (and CP1, CP3-CP5, CP8, CP9, CP11 for the prepaid check) | Do not invent a method (security question, OTP, NID, date of birth). Needs a decision from GP compliance. The panel loads the card before any verification, so protection depends on the agent not reading it out. |
| **How to block a lost SIM.** The article says "Do not invent one." | CP9 | Copilot gives only the public contacts. The block is done in GP's own system. |
| **SIM-replacement compliance rule.** The project plan lists "verify identity and block the SIM before replacement", but `compliance_rules.json` has only two rules. | CP9 | No separate "block first" reminder appears. |
| **Stop keyword or code for Cricket Alert (MISSING).** `vas-unsubscribe` lists only four example services. A 28-page `vas-overview` PDF is in the KB and may hold it, but retrieval did not surface it. | CP2 | Expected Copilot response: "Not found in approved knowledge." Then escalate or check the official GP process. No USSD code, SMS keyword or short code is given, and codes of other services must not be reused. The current build words this differently (see 7.3). |
| **Refund timeline and approval rules** (the MOCK article says "the expected time is shown in the ticket", but tickets have no such field) | CP2 | The agent says the time is not known. |
| **Goodwill amount.** The MOCK policy says "a small goodwill data bonus if policy allows" and gives no figure. | CP1, CP5 | Never quoted. |
| **Escalation, supervisor transfer and callback policy** for repeat complaints | CP5 | The script uses only: apologise, give the fix time, raise a ticket. |
| **Roaming rates page, roaming FAQ, Malaysia country page.** Listed as "to collect" or refused (403). | CP6, CP10 | Questions about rates return "not found". |
| **How a postpaid roaming pack is charged** when the balance is Tk 0 | CP10 | Not stated. |
| **How the TravelSure default 5 MB limit works with a 10 GB roaming pack** | CP10 | Not stated. Agent must not promise. |
| **What happens to existing data when the customer switches to a different pack** | CP3 | Only the same-pack carry-forward rule exists. |
| **What happens to remaining data on a replacement SIM** | CP9 | Not covered. |
| **Which cities count as "selected metropolitan city areas"** for online SIM delivery | CP9 | Chattogram not confirmed either way. |
| **Bangla knowledge articles.** All 15 articles are English. Only 15 Bangla PDFs of public pages are in the KB. Copilot answers, tips and summaries are always English. | all | The scripts keep the customer in Banglish and every Copilot output in English. |
| **Playbook section 8 (Q&A answers)** is missing from the source document, as noted in `seed-data-plan.md`. | CP6 | Not used. |
| **Intent label.** There is no intent classifier or intent field. Intent shows only through the lookup answer, the suggestion id and the ticket category. | all | Scripts describe intent that way. |
| **Agent coaching beyond one tip.** The tip is only given when the customer is frustrated or angry. There is no scoring or training content. | CP5 | Shown as is. |
| **Real ticketing API and real pack activation.** The ticket system is a mock. The Copilot never activates a pack, blocks a SIM or raises a refund. | CP2, CP3, CP7, CP9, CP10 | Scripts say the agent does these in their own tools. |

---

## 4. Conflicts and items a person must confirm before the demo

| Item | Detail | Decision needed |
|---|---|---|
| **Carry-forward rule** | The public page (`internet-packs-general`) says unused data carries forward if the same pack is bought within the active validity or auto-renew succeeds. It does not say what happens after validity ends. The mock article (`carry-forward-sample`) allows 3 days after expiry. | **Decided:** show them separately, each with its source. The mock rule is a demo sample and never GP policy. Remaining risk: the live lookup may retrieve both and blend them (see section 9). |
| **Charge after a pack ends** | `internet-packs-general` says Tk 1.165/MB up to Tk 5.825. The recharge terms and the 30 GB Tk 599 offer page say Tk 6.95. | Pick one before any script states it. No script states it. |
| **30 GB bundle price** | The catalog says Tk 598. `GP-public-documents.md` also cites a separate GP page called "30 GB Tk 599". It is not confirmed whether that is the same product. | Check the live page. The scripts use the catalog value (Tk 598), always as a sample offer. |
| **All pack prices and the roaming pack** | Catalog values are kept unchanged. Every row has `verify_before_demo = true`, so **all are UNVERIFIED until a person confirms them on the live GP pages.** The 30 GB pack is a demo sample offer, not a live commercial offer. | A person opens the live pages and records a date and name for each confirmation. Until then say "unverified" aloud. |
| **Roaming articles** (`roaming-activation`, `roaming-malaysia-pack`, `roaming-travelsure`) | Text was read by a script from roaming.grameenphone.com, whose robots.txt forbids automated access. Each article says "check before use". | A person confirms in a browser. |
| **SIM replacement tier list** | The article lists Gold, Platinum and Signature Star as free. GP's Star page says Platinum Plus and Platinum. Mitu is Standard, so the fee applies either way. | Confirm the list on the live page. |
| **Mitu's lost SIM scenario** | **Decided:** unconfirmed and hypothetical. Mitu's record is a stand-in prepaid Standard Chattogram profile. The seed text "Reported a lost SIM." in her `usage_pattern` is the same assumption and is not evidence. | The script is labelled hypothetical and no ticket is submitted. The call record is still saved against her id when the call ends (see section 8). The client may confirm or replace the scenario. |
| **Chattogram GP Centre** | Placeholder only. The public store locator lists none for Chattogram. | Replace with a real centre if GP provides one, or keep the Copilot's "placeholder" wording. |
| **Mock policies** | `network-outage-goodwill`, `vas-refund-policy`, `carry-forward-sample`, the Cricket Alert charge, and every customer record are mock. | Say "mock" or "sample" on screen. |
| **Dates and times** | "6:00 PM today", "2:00 AM tonight", "9:00 PM today" are fixed text. Customer dates (05 Dec, 01 Dec, 29 Nov, 30 Nov, 07 Dec) are fixed text written for a 26 Nov demo. Ticket ages are computed at call time. | Edit the notices and dates if the demo day moves, then re-seed (a data change, not done here). |

---

## 5. Findings in the live environment (action is yours, nothing was changed)

1. **Extra customer item.** `gp-copilot-customers` holds a sixth item: key `919500799295`, id `adbul`, name "Abdul Rahim", with Rahim's data. It is not in `customers.json` and has no `team_phone` flag. It was not used in any script.
2. **Test tickets and calls are in the live tables.** Ticket TKT-10234 (customer `adbul`, 2026-11-25) and 9 saved rehearsal calls. Effect on the dashboard: the calls add 9 to calls, 6 to suggestions shown and 1 to accepted, so the page shows 137, 418 and 302 instead of 128, 412 and 301, and its footer says 9 live calls. The ticket has no dashboard effect (the dashboard reads only the calls table). It moves the next ticket number to about TKT-10235 and appears in the ticket list. "Reset demo data" would delete the app-created ticket and all saved calls, but not the S3 copies. Nothing was reset.
3. **Call records are inside the Knowledge Base index.** Root cause: the data source covers the whole bucket (no inclusion prefix) and the app writes call records into the same bucket under `copilot/calls/`. Full investigation and recommended isolation: mapping file, section 2.5, and section 8 below. Nothing was changed.
4. **Voice calls: speaker tag for the human agent.** The default `VOBIZ_OUTBOUND_TRACK_SPEAKER=unknown` marks the agent's speech as "unknown", and unknown speech is **never used for compliance rules, suggestions or analysis**. For a live voice demo the setting must be `human_agent` (verified on one short call only, per `M3-LIVE-VOICE.md`). For a typed demo the speaker is chosen by the person typing, so this does not apply.
5. **Credentials are temporary.** If the AWS token expires, the app silently answers from the local seed files and writes tickets and calls to local files only. The local files do not have TKT-10234 or the 9 test calls, and the knowledge search falls back to the 15 local articles (no PDFs). `GET /api/health` shows `fallback_active`. Check it before every run.
6. **KB search depends on `BEDROCK_KB_REWRITE_QUERY=false`.** Banglish sentences are searched as spoken, merged with a local keyword search. The keyword lists are English and Banglish. Banglish spellings outside the keyword lists may not match; test each CP line once.
7. **Compliance keywords are narrow.** The price reminder is satisfied only by price, validity or taka. The identity reminder is satisfied only by "last recharge" or "recharge koto". Agents who say "dam", "meyad" or "ager recharge er poriman" will not clear the reminder.
8. **Stale table counts.** DynamoDB `DescribeTable` shows older item counts than a scan. Use a scan, not the console summary, when checking.
9. **Item counts differ from the seed plan.** The plan says 31 seeded items. The live tables hold 33 outside the calls table (the 31 plus the extra customer and TKT-10234) and 9 call records.
10. **Dashboard sample content.** Handle-time saved (48 s), the Mon-Fri mood trend and the top-suggestions list are fixed sample numbers. The list mixes compliance reminders with next-best-actions.

---

## 6. Dependencies

| Dependency | Needed for | Status found |
|---|---|---|
| DynamoDB `ap-south-1`, six `gp-copilot-*` tables | all CPs | Reachable on 2026-11-25 |
| Valid AWS credentials (temporary) | all live reads and writes | Valid at check time, will expire |
| Bedrock Knowledge Base id, S3 bucket | KB retrieval | Set and answering |
| Bedrock inference profiles (fast and summary) | live lookup, mood, Ask Copilot, summary | ARNs are set; model calls were not run in this analysis |
| `ASSIST_ENABLED=true` | automatic lookup | Default true |
| Login (`COPILOT_SESSION_SECRET`, users file) | opening the panel | Not checked |
| `DEMO_CONTROLS_ENABLED=true` | customer picker and Reset demo data buttons | Not checked |
| Voice stack (Vobiz, Sarvam STT, voice agent, `INTERNAL_API_TOKEN`) | live voice demos only | Not tested here |
| Sarvam STT quality on Bangladeshi Banglish | live voice demos only | Open question in the plan; test the 12 CP scripts aloud |

---

## 7. Missing client confirmations

7.1 Needed before the matching CP can be presented as more than a labelled demo:

| # | Confirmation | CP | Why |
|---|---|---|---|
| 1 | Approved identity verification procedure for postpaid customers | CP2, CP10 | None exists. Without it the agent cannot share bill or account information or activate anything. |
| 2 | Confirmation that the last-recharge question is sufficient for prepaid customers | CP1, CP3-CP5, CP8, CP9, CP11 | It is a configured rule only. |
| 3 | The official Cricket Alert stop method, or agreement that "Not found in approved knowledge." is the expected answer | CP2 | No source exists. |
| 4 | Manual confirmation of every catalog price, validity and code: 5 GB (Tk 169), 8 GB (Tk 199), 30 GB (Tk 598), 60 GB (Tk 999) and the 10 Days Travel Pack (Tk 1599, `*121*1599#`) | CP3, CP4, CP8, CP10 | All flagged `verify_before_demo`. |
| 5 | Manual confirmation of the roaming articles and TravelSure codes | CP6, CP10 | Read by script from a site that forbids automated access. |
| 6 | Whether the 30 GB sample offer may be shown to the audience at all, and how it is labelled | CP3, CP4 | It is not a live GP offer. |
| 7 | Whether the hypothetical lost-SIM scenario (CP9) stays in the demo, and whether it may be saved against Mitu's id | CP9 | Unconfirmed. |
| 8 | The official SIM block procedure, a real Chattogram GP Centre, online-delivery cities, and whether data moves to a replacement SIM | CP9 | None of these is in the KB. |
| 9 | The confirmed SIM replacement tier list (Gold, Platinum, Signature Star versus Platinum Plus, Platinum) | CP9 | Two GP pages disagree. |
| 10 | The wording for "not found": keep the build's own sentences or change them to "Not found in approved knowledge." | CP2, CP6, CP9, CP10 | A developer or prompt change, not made here. |
| 11 | Sumon's roaming type, and how a postpaid roaming pack is charged | CP10 | Not stored, not in the KB. |
| 12 | Refund time and approval rules, goodwill amount, escalation and callback policy | CP1, CP2, CP5 | None exists. The mock policies say "if policy allows". |
| 13 | The charge after a pack ends (Tk 5.825 or Tk 6.95) and the 30 GB price (Tk 598 or Tk 599) | CP3 | Public pages disagree. No script states them. |
| 14 | The demo date, so notice times and customer dates can be aligned | CP1, CP5, CP11 | Fixed text such as "6:00 PM today". |
| 15 | Whether the CSV exports differ from the live tables | all | No CSV was in the workspace. |
| 16 | Owner decision on the extra customer `adbul`, ticket TKT-10234 and the 9 test calls | CP12 | They change reporting. |

7.2 Needed from the owner of the AWS resources (not done by these documents): the Knowledge Base isolation in section 8.

7.3 Wording variance: the build's fixed "not found" sentences differ from the required "Not found in approved knowledge." (live lookup: "Not found in the approved knowledge or the customer's data. Check with a supervisor."; Ask Copilot: "I could not find this in the approved knowledge. Please escalate to a supervisor."). The prompts were not changed.

---

## 8. Knowledge Base privacy risks

Cause (details in the mapping file, section 2.5): the data source "knowledge-base-quick-start-S3" has no `inclusionPrefixes`, so it covers the entire bucket. The application writes every saved call to the same bucket at `copilot/calls/<call_id>.json`. At the time of checking, 19 call files exist, and an earlier sync (24 Nov 17:17, 6 new documents) is consistent with some already being indexed. Files written later are indexed at the next sync.

| # | Risk | Detail | Recommended isolation (not applied) |
|---|---|---|---|
| P1 | Cross-caller leak | A previous caller's transcript can be retrieved and quoted in an answer about another caller. | Separate bucket for call records, or `inclusionPrefixes` limited to `en/` and `bn/`, then sync and retest. |
| P2 | Wrong trust label | Call chunks have no metadata, so the code gives them type "public" and an empty source link. A transcript can be shown as a public GP source. | Isolation as above. Later code change: drop chunks with no `doc_id`. |
| P3 | Personal data inside an index | Transcripts hold spoken mobile numbers, names, and bill or balance talk. | Same isolation, retention period, and removal of the 19 existing files after review. |
| P4 | Index keeps growing | Every call end and ticket creation writes or rewrites a file. Each sync indexes more. | Isolation before the next sync. |
| P5 | Reset does not clean it | "Reset demo data" does not delete S3 copies (no delete code path exists). | Manual cleanup or a lifecycle rule on the call-records location. |
| P6 | Broad permissions | One bucket serves the app writer and the KB reader. | Separate roles: KB reads knowledge prefixes only, the app writes call records only. |
| P7 | Answer quality | Retrieved call text competes with real articles for the top 5 slots (four of my twelve test queries returned call chunks). | Isolation as above. |
| P8 | Demo risk | An answer on stage could quote a rehearsal conversation. | Do not run the demo until P1 is closed, or confirm with test queries that no `copilot/calls/` chunk is returned. |

---

## 9. Data accuracy risks

| # | Risk | Effect | Control used in the documents |
|---|---|---|---|
| A1 | Catalog prices and validity are unverified snapshots | A wrong number said on stage | Quoted unchanged, called "unverified" aloud, manual confirmation required |
| A2 | Roaming figures and codes came from a site that forbids automated access, and the catalog row and the article come from the same source | Agreement between them is not independent proof | Marked UNVERIFIED |
| A3 | Sample offer shown as a real offer | Misleading the audience | Called "demo sample offer, not a live GP offer" |
| A4 | Mock policies (goodwill, refund, carry-forward) taken for GP policy | False policy statements | Labelled mock each time |
| A5 | The live lookup retrieves the public and the mock carry-forward articles together and blends them | CP8 answer mixes sources | Two separate Ask questions, and blending is logged as a defect |
| A6 | The mock article contains a sentence about GP forfeiting leftover data | A mock article makes a claim about GP | Treated as unconfirmed, not repeated |
| A7 | Seed text "Reported a lost SIM." on Mitu's record | A hypothetical shown as fact | Treated as an assumption, not evidence |
| A8 | The card loads before any verification | Protected data visible and possibly read out | Scripts forbid reading it; postpaid scripts stay red |
| A9 | The model fills gaps (Cricket Alert code, block steps, rates, refund time) | Invented codes or promises | Expected "Not found in approved knowledge." and explicit "must not" lines |
| A10 | Fixed text such as "today" and "tonight", and fixed customer dates | Wrong on another day | Align with the demo date |
| A11 | Dashboard totals mix a fixed baseline with test calls | Overstated adoption (72 percent on screen against 1 of 6 real) | Real and fixed parts shown separately |
| A12 | Identity keyword matching | A wrong word leaves a reminder red or green when it should not | Keyword table in the scripts |
| A13 | Fallback to local files if AWS credentials expire | Different data and no PDFs | Check `/api/health` before each run |
| A14 | Extra customer item `adbul` and a second ticket owner | Odd data on stage | Not used in any script |

---

## 10. Recommended removals from the demo if not confirmed

If the confirmations in 7.1 items 1, 3 and 4 are not received: present CP2 only as the "verification missing" demonstration, present CP10 only as general information with unverified prices, and keep CP9 clearly hypothetical or leave it out.
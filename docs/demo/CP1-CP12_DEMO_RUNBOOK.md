# CP1-CP12 Demo Runbook

Documentation only. Read it with `CP1-CP12_BANGLISH_SCRIPTS.md` (the exact lines) and `CP1-CP12_DATA_GAPS.md` (what is missing).
Section names on screen are taken from the front-end component names (customer, location, signals, actions, answer, ask, summary and ticket, network status, transcript, call controls). The labels you see may differ slightly.

## 0. Before you start

### 0.1 Pre-flight checklist

1. Open `GET /api/health`. Confirm the data source is DynamoDB and `fallback_active` is false for both data and knowledge. If it is true, the AWS token has probably expired. Refresh the `AWS_*` values in `backend/config/.env`, restart the backend, and check again. Do not demo from the fallback: it has no test ticket, no PDFs and writes only to local files.
2. Confirm the notice times. Mirpur says "6:00 PM today" and Uttara says "2:00 AM tonight". Run CP1 and CP5 at a time when those words are true, or tell the audience they are fixed demo text.
3. **Prices are UNVERIFIED until a person has confirmed them.** Open each pack on the live GP page and confirm the price and validity for the 30 GB bundle (Tk 598, 30 days), the 8 GB bundle (Tk 199, 7 days) and the 10 Days Travel Pack (Tk 1599, 10 days, `*121*1599#`). The catalog flags every row `verify_before_demo`. Do the same for the roaming and TravelSure articles in a browser.
4. CP8 is decided: the public GP information and the mock carry-forward policy are shown separately, each with its source. The mock policy is never called GP policy.
4a. **Knowledge Base isolation check.** The Knowledge Base currently indexes the whole bucket, including saved call transcripts (see the Data Gaps file, section 8). Before any demo, send a few test questions through Ask Copilot (for example about internet problems and about carry-forward) and confirm that no source shows an S3 address or a transcript. If one does, stop and ask the AWS owner to isolate the call records.
4b. **Postpaid verification is MISSING.** For Nusrat and Sumon the agent shares no account or bill information and activates nothing. Tell the presenters.
4c. CP9 is a **hypothetical** scenario. Do not submit a ticket for it.
5. Decide what to do with the test data: the extra customer `adbul`, ticket TKT-10234 and 9 saved calls. If you keep them, the dashboard shows 137 calls, 418 suggestions shown and 72 percent accepted. If you reset them first (the "Reset demo data" control, needs `DEMO_CONTROLS_ENABLED=true`), it shows 128, 412 and 73 percent. That control deletes the tickets the app created and **all** saved calls, so run it only if you mean to.
6. Check the sign-in works and the Live call and Adoption dashboard tabs open. These are the only two tabs I found.
7. Voice demo only: set `VOBIZ_OUTBOUND_TRACK_SPEAKER=human_agent` on the voice agent, otherwise the agent's speech is tagged "unknown" and no compliance reminder or suggestion will fire.
8. Rehearse every script once end to end, typed, before any voice rehearsal.

### 0.2 Two ways to run a call

| Mode | How | When to use |
|---|---|---|
| Typed | The panel (or `POST /api/transcript`) accepts one line with a chosen speaker (customer, human agent or AI agent). Each line takes the same path as live speech. A call starts by itself on the first line. | Rehearsals and backup. The Copilot behaves the same every run for compliance and keywords. |
| Live voice | Customer calls the Vobiz number, the AI agent answers, the human agent takes over by phone, the voice agent forwards the transcript. | The real demo. Test the speech-to-text on every number line first, because a wrong digit loads the wrong customer or none. |

Language note: the scripts are Banglish. Speech-to-text may return Bangla script or Roman letters. The matching words (compliance and next-best-action) are listed in both forms, but test each key line.

### 0.3 Things to say out loud (updated)

- Every price, validity and activation code is **unverified** until a person confirms it. The 30 GB pack is a **demo sample offer**, not a live GP offer.
- For postpaid customers, "the approved verification procedure is not available yet, so the agent shares nothing and activates nothing".
- For Cricket Alert, "the approved knowledge has no stop method, so the Copilot says Not found and we check the official GP process".
- CP9 is a hypothetical case. CP8 shows public GP information and a separate mock policy.
- The dashboard totals mix a fixed baseline with rehearsal calls.

The older list follows.

- The customers, bills, balances, network notices, tickets and the Cricket Alert charge are **mock demo data**.
- The refund policy, the goodwill policy and the carry-forward rule are **mock or sample policies**, not GP policy.
- Pack prices and roaming details are **public snapshots to be confirmed**, and the 30 GB pack is a **sample offer**.
- The ticket system is a **mock**. The Copilot suggests; the agent acts. It never activates a pack, blocks a SIM or raises a refund.
- Copilot answers are in English even when the customer speaks Banglish.

### 0.4 What the supervisor can see

I found no separate supervisor live screen. A supervisor either opens the panel in a second browser (updates are broadcast to every open panel, and the plan lists "panel reload from a second tab works" as a check) or opens the **Adoption dashboard** tab. Supervisors also see tickets and saved call records after the call, through the ticket list and the calls table.

### 0.5 Generic steps used in every call CP

1. Start the call (typed: send the first line; voice: the call connects).
2. Run the lines in order. After each line pause 2 to 4 seconds. The automatic lookup and mood analysis take a moment.
3. Confirm the mobile number by speaking the digits (the panel picks them up) or by using the identity input.
4. When the panel asks where the customer is, say the area (or set it in the location section).
5. Watch four places: transcript, customer card with network notice, signals (mood and compliance), actions (suggestions and the lookup answer).
6. End the call, check the summary, edit it if you want, and press Submit only when the script says a ticket is raised.

---

## CP1 - Internet not working (Rahim Uddin)

| Item | Detail |
|---|---|
| Goal | Show the card, the area notice and the apology suggestion appear as the customer speaks. |
| Setup | No card loaded. Mirpur notice present. Compliance idle. |
| Run | Script CP1, exchanges 1 to 4. |
| Agent sees | After line 2: the "where are you now" prompt and a lookup that asks for the mobile number. After the number: Rahim's card, "maintenance until 6:00 PM today", suggestion "Apologise and give the fix time". After lines 9 and 10: compliance red then green. After line 12: suggestion ticked, mood easing. |
| Supervisor sees | The same panel. Later, the call record with frustrated moods and no ticket. |
| Pass checks | Card shows Rahim, Mirpur, Tk 120, 25 GB with 18 GB left. Notice text matches. The first answer begins "After verifying identity:" and later ones do not. No answer says the network is fine. No ticket exists. |
| If it goes wrong | No card: say the number again, one digit word at a time. Slow answer: wait for the ticking spinner, or use the manual look-up control for the latest customer sentence. |

## CP2 - Bill dispute (Nusrat Jahan): the postpaid verification gap

| Item | Detail |
|---|---|
| Goal | Show that the Copilot and the agent hold back bill information when no approved postpaid verification exists, and that the Copilot answers "Not found in approved knowledge." instead of inventing a Cricket Alert stop code. |
| Setup | Nusrat's number ready. Agent must not say "last recharge" or "recharge koto". |
| Run | Script CP2, exchanges 1 to 4. Branch B only if the client has confirmed a postpaid procedure. |
| Agent sees | The card with the bill and charges as soon as the number is confirmed (the system does not wait for verification). A RED identity reminder after the agent says "details", which stays red. A "Not found in approved knowledge." answer for the verification question and for the Cricket Alert question. |
| Supervisor sees | The same panel. After the call, a Billing summary that says identity was not verified and no refund was raised. |
| Pass checks | The agent reads no figure from the card. The reminder stays red. No code of any kind appears for Cricket Alert. No refund time appears. No ticket is submitted. |
| Fail signs | The Copilot suggests a last recharge, a date of birth, an NID check or an OTP. A USSD or SMS code appears for Cricket Alert, including codes copied from other services. The Copilot confirms the Tk 100 charge to the agent as something to tell the customer before verification. |
| Say out loud | "The approved postpaid verification procedure is missing. This is the correct and safe behaviour until it is confirmed." |

## CP3 - Right offer at the right time (Rahim Uddin)

| Item | Detail |
|---|---|
| Goal | Show that the offer waits until the customer's own question is answered, then appears with its details. |
| Run | Script CP3. |
| Agent sees | No offer after exchange 1. After line 7: "Offer the bigger 30-day pack (sample)" with 30 GB + 300 minutes, Tk 598, 30 days, code "none published", marked sample. After line 11 amber, after line 12 green. |
| Pass checks | The panel and the agent call it a demo sample offer. Tk 598 and 30 days are spoken as "unverified catalog values". No activation code. The "what happens to my 18 GB" answer says not covered. |
| Say out loud | "This is a demo sample offer, not a live GP offer, and the price has not been verified. The Copilot does not activate it." |
## CP4 - Compliance reminder (Rahim Uddin)

| Item | Detail |
|---|---|
| Goal | Show a missed step being flagged and then cleared. |
| Run | Script CP4. Use the exact words: "details", "last recharge", "price", "validity", "taka". |
| Agent sees | Red identity reminder after line 2, green after line 4. Amber price reminder after line 1, still amber after line 7, green after line 8. |
| Supervisor sees | The compliance state saved in the call record: both rules green. |
| Pass checks | Line 7 leaves the amber reminder untouched. Line 8 clears it. |
| If it goes wrong | If the agent says "dam" or "meyad" the reminder will not clear. Use "price", "validity" or "taka". |

## CP5 - Customer mood turns angry (Karim Hossain)

| Item | Detail |
|---|---|
| Goal | Show the mood bar, the coaching tip and the absence of a sales offer. |
| Run | Script CP5. Say line 6 with real irritation, and use the words "prottek bar", "kono kaj hoy na", "officer", "complain". |
| Agent sees | Card with three open tickets and their ages. Uttara notice. Mood bar red after line 6, with the tip to acknowledge the repeated complaint, give a ticket number and a clear time. No bigger-pack suggestion. After lines 7 to 10 the mood eases. |
| Supervisor sees | Mood history with frustrated or angry readings in the saved call. |
| Pass checks | The three ticket numbers the agent reads match the card. The fix time is 2:00 AM tonight. No pack is offered. |
| Say out loud | "The tip is only generated when the customer is frustrated or angry." |

## CP6 - Ask Copilot (Sumon Ahmed as caller)

| Item | Detail |
|---|---|
| Goal | Show a question answered only from approved knowledge, with a clickable source, and a refusal when the knowledge is silent. |
| Run | Script CP6. Type each Ask question into the ask box. |
| Agent sees | A short English answer with source chips (roaming-basics, roaming-malaysia-pack). For the rate question: a "could not find this, escalate" reply and no number. |
| Supervisor sees | The sources and whether they are public or mock. |
| Pass checks | Codes match the article text (`*66*1*2#`). No Malaysia call rate appears. |
| Fallback | Ask Copilot depends on the Bedrock model. If it errors the endpoint returns a failure message. Have a screenshot ready. |

## CP7 - Summary and ticket (Karim Hossain, from the CP5 call)

| Item | Detail |
|---|---|
| Goal | Show the after-call note and a one-click ticket. |
| Run | Finish CP5, then script CP7: end the call, wait for the draft, edit, Submit. |
| Agent sees | A draft with problem, checked, outcome, follow-up, category Network and a short subject. After Submit, the new ticket number from the panel. |
| Supervisor sees | A new row in the ticket list (customer `karim`, status open) and the saved call with summary and ticket. |
| Pass checks | The draft contains only facts from the call. No refund, bonus or callback time. The ticket number is read from the panel, never from the script. |
| Say out loud | "This is a mock ticket system." |
| If it goes wrong | If the summary stays empty, the model call failed. The agent fills the fields by hand. |

## CP8 - Unused data carry-forward (Mitu Akter): public GP information and mock policy, separately

| Item | Detail |
|---|---|
| Goal | Show two sources kept apart, with a label on each. |
| Run | Script CP8. Ask the Copilot **two separate questions** in the ask box: first the public rule, then the demo sample rule. |
| Agent sees | Answer 1 from `internet-packs-general`, type public, with its grameenphone.com link. Answer 2 from `carry-forward-sample`, type mock, with the words sample and not GP policy. |
| Supervisor sees | Two source chips with different types, public and mock. |
| Pass checks | Answer 1 does not mention the 3-day rule. Answer 2 is never called GP policy. The "after validity ends" gap in the public page is stated, not filled. The pack name is not stated as fact. |
| Fail signs | One answer that blends both rules. The mock rule described as GP's rule. A statement that GP forfeits leftover data (it comes only from a mock article and is unconfirmed). |
| Say out loud | "The first answer is public GP information. The second is a demo sample policy that GP has not adopted." |

## CP9 - Lost SIM and replacement: HYPOTHETICAL scenario

| Item | Detail |
|---|---|
| Goal | Show the verify, block, replace order from the Knowledge Base, and the limits of that knowledge. |
| Status | **Hypothetical and unconfirmed.** Mitu's card is a stand-in. The seed text "Reported a lost SIM." on her record is not evidence. Say "hypothetical" before and after the call. |
| Run | Script CP9. Do **not** press Submit. |
| Agent sees | Card loaded from the number. Suggestion "Verify identity, then block the SIM" with the Chattogram placeholder centre. Red then green identity (prepaid, last recharge). The replacement facts: fee Tk 300, NID, fingerprint. |
| Pass checks | No block procedure is invented ("Not found in approved knowledge." is the expected answer). The Chattogram centre is called a sample placeholder with no address. Data on a new SIM is "not found". The summary does not state a lost SIM as a fact and does not say the SIM was blocked. |
| Caution | The call record is still saved to the calls table and to S3 against Mitu's id when the call ends. If that is not acceptable, ask the client before running CP9. |

## CP10 - International roaming, Malaysia (Sumon Ahmed): general information only

| Item | Detail |
|---|---|
| Goal | Show roaming guidance and a pack suggestion without sharing account data or activating anything. |
| Status | Postpaid verification is MISSING. All roaming figures and codes are UNVERIFIED. |
| Run | Script CP10. The agent never says "last recharge". |
| Agent sees | Card loaded (not read out). RED identity reminder that stays red. "Not found in approved knowledge." for the verification question. A general roaming answer, the 10 Days Travel Pack (Tk 1599, 10 days, `*121*1599#`, marked unverified), TravelSure codes. Amber then green price reminder after the agent reads price and validity. |
| Pass checks | The agent says nothing from the card, including "roaming not activated". Nothing is activated. Green means "price read out", not "pack activated". No promise that the bill cannot rise. No claim about how a postpaid pack is billed. |
| Say out loud | "These roaming figures are being confirmed against GP's live page. We are not activating anything because postpaid verification is not approved yet." |
## CP11 - English-speaking customer (Karim Hossain)

| Item | Detail |
|---|---|
| Goal | Show the same Copilot working in English with no setting changed. |
| Run | Script CP11, spoken in English, digits in English words. |
| Agent sees | Card, a green identity reminder after line 4, a recharge answer from the recharge terms, and the Uttara notice after the customer names the area. |
| Supervisor sees | An English transcript and an English summary. |
| Pass checks | The Tk 100 recharge on 18 Nov and the Tk 45 balance match the card. The answer does not explain why the balance is Tk 45. The network is not called fine. |

## CP12 - Adoption dashboard: real and fixed metrics

| Item | Detail |
|---|---|
| Goal | Show adoption numbers and be explicit about which part is real and which is fixed. |
| Run | Open the Adoption dashboard tab last. Use script CP12. Take a screenshot before and after one call. |
| Real saved-call part | Calls, suggestions shown and accepted from the `calls` table. Today: 9 rehearsal calls, 6 shown, 1 accepted. The footer shows the call count. |
| Fixed demo part | The baseline of 128 calls, 412 shown and 301 accepted, the 48 s handling time, the Mon-Fri mood trend and the top-suggestions list. |
| Totals on screen today | 137 calls, 418 shown, 72 percent accepted. Real part alone: 1 of 6 accepted. |
| Effect of test data | The 9 test calls inflate calls, shown and accepted. Test ticket TKT-10234 has no dashboard effect. Reset demo data would restore 128, 412 and 301 but not delete S3 copies. Do not reset without the owner's decision. |
| Pass checks | Calls rises by 1 after one live call, shown by the suggestions shown, accepted by the ones marked used. |
| Say out loud | "Handling time, mood trend and top suggestions are fixed demo numbers. The totals include rehearsal calls." |
| Not available | Real handling-time measurement, a mood trend from saved calls, a top-suggestions list from usage, per-agent or per-day filters. |

---
## 1. After the demo

1. Check the ticket list and the saved calls. Each demo adds rows to `gp-copilot-tickets` and `gp-copilot-calls`, and the saved calls are copied into the S3 location that the Knowledge Base may index (see the Data Gaps file, finding 3).
2. Decide whether to clear the demo records. Clearing is your choice and is not done by these documents.

## 2. Readiness, confirmations and execution order

### A. Scenario readiness

| CP | Readiness | Blocking items |
|---|---|---|
| CP1 Internet not working | Ready (mock data labelled) | Confirm notice time matches the demo day |
| CP2 Bill dispute | Ready only as the "verification missing" demonstration. The bill-breakdown branch is blocked. | Postpaid verification procedure; Cricket Alert stop method |
| CP3 Right offer | Ready with labels | Price confirmation; sample offer approval |
| CP4 Compliance reminder | Ready | Price confirmation for the figures read out |
| CP5 Angry customer | Ready | Notice time; confirm ticket ages shown |
| CP6 Ask Copilot | Ready with caveats | Roaming article confirmation; "not found" wording; KB isolation check |
| CP7 Summary and ticket | Ready (mock ticket system) | None beyond CP5 |
| CP8 Carry-forward | Ready with labels | KB isolation check; blending risk |
| CP9 Lost SIM | **Hypothetical only. Not ready as a factual scenario.** | Scenario confirmation; block procedure; Chattogram centre |
| CP10 Roaming | Partial: general information only | Postpaid verification; price and roaming confirmation |
| CP11 English customer | Ready | None |
| CP12 Dashboard | Ready with caveats | Decision on test data; fixed metrics labelled |
| All | **Gate:** Knowledge Base isolation | Without it any answer may quote a past call |

### B. Missing client confirmations, C. Knowledge Base privacy risks, D. Data accuracy risks

Listed in full in the Data Gaps file: section 7 (B), section 8 (C) and section 9 (D).

### E. Recommended execution order for the live demo

1. Pre-flight (section 0), including the Knowledge Base isolation check, health check, notice times, and price confirmation status.
2. CP1 Internet not working (Rahim). End the call.
3. CP4 Compliance reminder (Rahim, new call).
4. CP3 Right offer (Rahim, new call). Say "demo sample offer, unverified price".
5. CP5 Angry customer (Karim).
6. CP7 Summary and ticket, straight after CP5 while the call is fresh.
7. CP11 English customer (Karim, new call).
8. CP6 Ask Copilot, including the "Not found" question.
9. CP8 Carry-forward (Mitu), public first, mock second.
10. CP10 Roaming, general information only, only if the roaming figures are at least flagged unverified on screen.
11. CP2 Verification gap (Nusrat), as the closing "safe behaviour" example, or skip if the client prefers not to show a gap.
12. CP9 Hypothetical lost SIM, last and optional, with no ticket.
13. CP12 Dashboard, last, with the real and fixed parts read apart.

Reasoning: prepaid scenarios with verified-by-rule identity and the least unverified data come first. Scenarios that depend on unconfirmed items come late, so a problem there does not stop the main story. The dashboard comes last so its numbers include the calls just made.
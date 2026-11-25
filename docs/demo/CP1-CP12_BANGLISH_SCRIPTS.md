# CP1-CP12 Banglish Demo Scripts

Documentation only. Every customer fact comes from the live `gp-copilot-*` DynamoDB tables (scanned 2026-11-25). Every policy, code and price comes from the Knowledge Base articles or the pack catalog. Anything marked MOCK or SAMPLE is demo content and must be called that on screen.

All Bangla is written in English letters (Banglish). Every line has an English meaning. No Bengali script is used.

## Decisions applied in this revision (2026-11-25)

| Topic | Rule used in every script below |
|---|---|
| Postpaid identity (Nusrat, Sumon) | The last-recharge question is **not** used. The approved postpaid verification procedure is **MISSING (client confirmation needed)**. Until it exists, the agent shares no account or bill information and activates nothing. |
| Prepaid identity (Rahim, Karim, Mitu) | The only configured check is the last recharge amount. It is a configured rule, not a client-confirmed procedure. |
| Cricket Alert stop code | Not in approved knowledge. Expected Copilot response: **"Not found in approved knowledge."** plus a suggestion to escalate or check the official GP process. No code of any kind is given. |
| Prices, validity, activation codes | The catalog values are quoted unchanged, but every row with `verify_before_demo = true` is **UNVERIFIED until a person confirms it on the live GP page**. Say "unverified" when speaking them. |
| Sample offers | The 30 GB + 300 minutes pack is a **demo sample offer**, not a live GP commercial offer. |
| CP8 | Public GP information and the mock policy are shown separately, each with its source. The mock policy is never called GP policy. |
| CP9 | A **hypothetical, unconfirmed** scenario. It is not tied to Mitu's real record. |
| CP12 | Real saved-call metrics and fixed demo metrics are shown separately. |

**Wording variance to report:** the required "Not found in approved knowledge." is the behaviour expected by the client. The current build has its own fixed sentences ("Not found in the approved knowledge or the customer's data. Check with a supervisor." in the live lookup and "I could not find this in the approved knowledge. Please escalate to a supervisor." in Ask Copilot). The meaning is the same, the wording is not. Prompts were not changed. A developer decision is needed.
## How to read these scripts

- **Agent** is the Grameenphone customer support agent. The name "Tania" is the agent name used in the project plan. **Customer** is the caller.
- "Expected Copilot" blocks describe what the panel should show. The AI wording will vary from run to run. The **facts, sources and states** must match. Treat any difference in a fact as a bug to report.
- "Context (DDB)" is what the customer card or the network table supplies. "KB" is what the knowledge search should return. When a field is missing, the script says so instead of filling it in.
- Ticket numbers: the system assigns them when the agent presses Submit. No script states a new ticket number.

## Speaking rules that make the demo work

**Mobile number.** The panel loads the customer card when it hears an 11-digit number starting with 01, spoken as digits or digit words. Digit words that work: zero/shunno, ek/one, dui/two, tin/three, char/four, pach/five. Say all 11 digits in a row with no other word between them.

| Customer | Digits | Say it like this |
|---|---|---|
| Rahim Uddin | 01200000001 | shunno ek dui shunno shunno shunno shunno shunno shunno shunno ek |
| Nusrat Jahan | 01200000002 | shunno ek dui shunno shunno shunno shunno shunno shunno shunno dui |
| Karim Hossain | 01200000003 | shunno ek dui shunno shunno shunno shunno shunno shunno shunno tin |
| Sumon Ahmed | 01200000004 | shunno ek dui shunno shunno shunno shunno shunno shunno shunno char |
| Mitu Akter | 01200000005 | shunno ek dui shunno shunno shunno shunno shunno shunno shunno pach |

**Compliance keywords (plain substring match, not AI).**
- Red reminder starts when the agent says "details" or "account er".
- It turns green when the agent says "last recharge" or "recharge koto". If one line has both, it goes straight to green.
- The price reminder starts (amber) when the customer says "chalu kore" or "activate".
- It turns green when the agent says "price", "validity" or "taka". The Banglish words "dam" and "meyad" do not count.
- **Postpaid customers:** the agent must not say "last recharge" or "recharge koto". Those words would turn the reminder green, but that is not an approved check for postpaid. Saying "details" or "account er" turns the reminder red, and in the postpaid scripts it correctly stays red because no approved method can clear it.

**Network complaint.** A customer sentence with a subject word (net, internet, network, signal, data) plus a problem phrase (kaj korche na, slow, nai, problem) makes the panel ask the agent where the customer is right now.

**Next-best-action "used" ticks** when the agent says the rule's words: maintenance or "thik hobe" (apology), "boro pack" or "bigger pack" (offer), deactivate or refund (VAS refund), "roaming pack" (roaming), block (SIM).

**Language of the Copilot.** All Copilot answers, tips and summaries are in English, even for Banglish questions.

---

## CP1 - Internet not working

Customer: **Rahim Uddin**, 01200000001, Prepaid Standard, Mirpur.
Context (DDB): balance Tk 120. Pack 25 GB, 30 days, 18 GB left, until 05 Dec. Recharges Tk 200 (20 Nov), Tk 100 (10 Nov). No open tickets. Mirpur notice: maintenance, "Network maintenance in the Mirpur area", until 6:00 PM today.
KB: internet-troubleshooting (public), network-outage-goodwill (MOCK policy).
Real-time detections: live transcription, speaker tagging, network-problem prompt, card load, notice lookup, mood, compliance, next-best-action, tip.

### Exchange 1 - the complaint

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 1 | Agent | Assalamu alaikum, Grameenphone customer care theke Tania bolchi. Bolun, ki shahajjo korte pari? | Peace be with you, this is Tania from Grameenphone customer care. Tell me, how can I help? |
| 2 | Customer | Walaikum assalam. Apu, amar net 2 din dhore kaj korche na. Khub problem hocche. | Peace be with you too. Madam, my internet has not worked for 2 days. It is a big problem. |

Expected Copilot:
- Transcript shows both lines with the right speaker tags.
- Network complaint detected ("net" + "kaj korche na"). The panel asks the agent to confirm where the customer is. Nothing about the network is claimed yet.
- Customer card is still empty (no number confirmed). The lookup answer should tell the agent to confirm the mobile number first, and may show internet-troubleshooting as a source.
- Mood: frustrated ("khub", "problem", "2 din").

### Exchange 2 - number and card

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 3 | Agent | Ji, bujhte parchi, eta khub birokto korar moto. Apnar mobile number ta bolben? | Yes, I understand, this is really annoying. Please tell me your mobile number. |
| 4 | Customer | Ji. Shunno ek dui shunno shunno shunno shunno shunno shunno shunno ek. | Yes. Zero one two zero zero zero zero zero zero zero one. |
| 5 | Agent | Dhonnobad. Ami number ta pore shunchi: shunno ek dui shunno shunno shunno shunno shunno shunno shunno ek. Thik ache? | Thank you. I will read the number back: zero one two zero zero zero zero zero zero zero one. Is that right? |
| 6 | Customer | Ji, thik ache. | Yes, that is right. |

Expected Copilot:
- Number detected and customer card loads: Rahim Uddin, Prepaid, Standard, Mirpur, Tk 120, pack 25 GB with 18 GB left until 05 Dec, two recharges.
- The registered-area notice appears: maintenance in Mirpur until 6:00 PM today.
- The network lookup re-runs by itself now that the card is known. Expected answer (wording varies): starts with "After verifying identity:" (identity not green yet), says Mirpur has maintenance until 6:00 PM today, tells the agent to apologise, give the fix time and check where the customer is now. Sources: network-outage-goodwill (mock) and/or internet-troubleshooting.
- Suggestion appears: "Apologise and give the fix time" (nba-network-apology).

### Exchange 3 - location and identity

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 7 | Agent | Rahim bhai, apni ki ekhon Mirpur e achen? | Brother Rahim, are you in Mirpur right now? |
| 8 | Customer | Ji, ami Mirpur er basa tei achi. | Yes, I am at my home in Mirpur. |
| 9 | Agent | Apnar account er details ektu dekhchi. | I am looking at your account details. |
| 10 | Agent | Tar age bolun to, apnar last recharge koto chilo? | Before that, tell me, how much was your last recharge? |
| 11 | Customer | 200 taka, 20 November e korechilam. | 200 taka, I did it on 20 November. |

Expected Copilot:
- Current location set to Mirpur. Both statuses show the same maintenance notice.
- After line 9: compliance shows RED "Verify identity first".
- After line 10: it turns GREEN "Identity checked". The customer's answer matches the card (Tk 200, 20 Nov). The Copilot does not compare it; the agent does.
- Later answers no longer start with "After verifying identity:".

### Exchange 4 - fix time and next steps

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 12 | Agent | Dhonnobad Rahim bhai. Mirpur e ekhon network maintenance cholche, ata 6 tar moddhe thik hobe. Shomoshyar jonno amra dukkhito. | Thank you, brother Rahim. Network maintenance is going on in Mirpur now, and it will be fixed by 6 o'clock. We are sorry for the trouble. |
| 13 | Agent | Ektu dekhben, mobile data on ache kina, ar *121*1*4# dial kore internet balance ta check korben. | Please check that mobile data is on, and dial *121*1*4# to check your internet balance. |
| 14 | Customer | Balance to ache dekhlam, 18 GB. Thik ache, 6 tar por try korbo. Kintu tokhono na challe? | I see the balance is there, 18 GB. Okay, I will try after 6. But what if it still does not work? |
| 15 | Agent | Tahole abar call korben. 6 tar por o shomoshya thakle ami ticket raise korbo. | Then call again. If the problem is still there after 6, I will raise a ticket. |

Expected Copilot:
- Suggestion "Apologise and give the fix time" is ticked as used (agent said "maintenance" and "thik hobe").
- Customer mood moves from frustrated towards neutral or calm.
- Tip or answer references the MOCK goodwill policy only as a demo policy: a goodwill data bonus applies only to outages longer than 24 hours "if policy allows". The notice does not say how long the outage has lasted, so the agent must not promise a bonus.
- The 18 GB figure in line 14 equals the card, so no data-finished explanation applies. The Copilot must not say the network is fine.
- No ticket is created yet. The mock policy raises one only if the problem continues after the fix time.

---

## CP2 - Bill dispute

Customer: **Nusrat Jahan**, 01200000002, Postpaid Gold, Dhanmondi.
Context (DDB): Postpaid 20 GB, 6 GB left, until 30 Nov. Bill: last month Tk 500, this month Tk 850, due 07 Dec. Charges: "Extra internet use" Tk 250 and "Value-added service: Cricket Alert (sample)" Tk 100. Note: "Did not knowingly activate any value-added service." No recharge history. Dhanmondi has no network notice.
KB: vas-refund-policy (MOCK), vas-unsubscribe (public), postpaid-bill-payment (public).
**Identity: postpaid. The approved verification procedure is MISSING.** Until it is confirmed, the agent reads out no figure from the card and starts no refund or service change. The branch with the full bill breakdown (Branch B at the end of this CP) is **not for use** until the client confirms the procedure.
Detections: billing intent (via lookup and ticket category), card load, identity reminder, next-best-action, refund steps.

### Exchange 1 - the dispute

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 1 | Customer | Hello, ami Nusrat Jahan bolchi. Amar ei mashe bill eshechhe 850 taka, ager mashe to 500 chhilo. Eta ki kore hoy? | Hello, this is Nusrat Jahan. My bill this month is 850 taka, last month it was 500. How can this be? |
| 2 | Agent | Assalamu alaikum Nusrat apu, ami Tania. Ami ekhoni dekhchi. Apnar mobile number ta bolben? | Peace be with you, sister Nusrat, I am Tania. I will look at it right now. Please tell me your mobile number. |
| 3 | Customer | Shunno ek dui shunno shunno shunno shunno shunno shunno shunno dui. | Zero one two zero zero zero zero zero zero zero two. |

Expected Copilot: card loads for Nusrat (Postpaid, Gold, Dhanmondi, bill 500 to 850, due 07 Dec, two charges). The card becomes visible to the agent as soon as the number is confirmed. The system does not wait for verification (see Data Gaps). The agent must not read any figure from it to the customer yet. The customer has quoted 850 and 500 herself, so those two numbers are hers, not the agent's. Mood: neutral to frustrated.

### Exchange 2 - verification is missing

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 4 | Agent | Nusrat apu, apnar account er details dekhar age amake porichoy verify korte hobe. Kintu postpaid customer er verification er approved niyom ta ekhono amar system e nei. | Sister Nusrat, before I look at your account details I must verify who you are. But the approved verification method for postpaid customers is not in my system yet. |
| 5 | Agent -> Copilot (typed) | Postpaid customer ke verify korbo kivabe? | How do I verify a postpaid customer? |
| 6 | Customer | Ami to ei bill niye khub birokto. Apni ki bill er break-up ta bolte parben na? | I am very upset about this bill. Can you not tell me the breakdown? |
| 7 | Agent | Verification chhara ami bill er kono tottho bolte parbo na. Ami supervisor er sathe verification er niyom ta confirm kore nichchi. | Without verification I cannot share any bill information. I am confirming the verification rule with my supervisor. |

Expected Copilot:
- Line 4 turns the identity reminder RED ("details"). It **stays red**. No approved way to clear it exists for postpaid, and the agent does not say "last recharge".
- Line 5 answer: **"Not found in approved knowledge."** with a suggestion to escalate to a supervisor or check the official GP process. It must not suggest a last recharge question, a date of birth, an NID check, an OTP or any other method.
- Mood: frustrated. Coaching tip about acknowledging the customer is acceptable.
- Nothing on the panel may state the bill breakdown as something the agent should tell the customer yet.

### Exchange 3 - what can be said without verification

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 8 | Agent | Ei shomoy ami shudhu general ta bolte pari. Bill MyGP te dekha jay, Account, Usage History te last 30 din er usage thake. Kono value-added service er charge hole bill e service er nam thake. | For now I can only give general information. You can see the bill in MyGP, and Account, Usage History shows the last 30 days. If a value-added service is charged, the service name appears on the bill. |
| 9 | Customer | Amar bill e Cricket Alert nam e ekta service ache. Eta bondho korar code ta ki? | My bill has a service called Cricket Alert. What is the code to stop it? |

Expected Copilot:
- General answer only, from postpaid-bill-payment (MyGP Usage History) and vas-unsubscribe (find the service name on the bill). Both are public. The answer does not use the customer card (`used_customer_data` false). The customer named the service herself.
- The Copilot must not confirm that Cricket Alert, Tk 100 or any figure is on her bill.

### Exchange 4 - Cricket Alert stop code is not available

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 10 | Agent -> Copilot (typed) | Cricket Alert service bondho korar code ki? | What is the code to stop the Cricket Alert service? |
| 11 | Agent | Cricket Alert bondho korar sothik niyom ta ekhon amar kache confirmed nei. Ami official process check kore apnake janabo. | I do not have a confirmed way to stop Cricket Alert right now. I will check the official process and let you know. |
| 12 | Customer | Thik ache, tahole apnara janan. | All right, then let me know. |

Expected Copilot:
- Line 10 answer: **"Not found in approved knowledge."** Then: escalate to a supervisor or check the official GP process. The public contacts 121 and 158 may be named because the customer-contacts article lists them.
- **No USSD code, no SMS keyword and no short code.** The vas-unsubscribe article lists codes for Missed Call Alert, News Service, Study Line and Call Block. None of them is Cricket Alert, and the Copilot must not reuse one.
- Suggestion "Deactivate the service and raise a refund" may appear. The agent must not act on it: no verification, no confirmed stop method. If the word "refund" is spoken the card ticks "used". The tick is incidental.
- Summary draft, category Billing: states that identity was **not verified**, that the stop method is pending, and that no refund was raised. No ticket is submitted in this branch.

### Branch B - only after the client confirms a postpaid verification procedure

Use these lines **only** once the procedure exists. Replace "[APPROVED CHECK]" with the real procedure. The refund policy is a **MOCK demo policy**, not GP policy.

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| B1 | Agent | Apnar porichoy [APPROVED CHECK] diye verify hoyeche. Ager bill chhilo 500 taka, ei bar 850 taka, mane 350 taka beshi. Extra internet use 250 taka, ar Cricket Alert nam er ekta value-added service 100 taka. | Your identity is verified through [APPROVED CHECK]. Your last bill was 500 taka, this time 850, 350 taka more. Extra internet use is 250 taka, and a value-added service called Cricket Alert is 100 taka. |
| B2 | Customer | Cricket Alert? Ami kokhono chalu kori ni! Amar to 6 GB baki ache, extra internet 250 taka keno? | Cricket Alert? I never activated it! I still have 6 GB left, why 250 taka extra internet? |
| B3 | Agent | Apni ki kokhono Cricket Alert chalu korechilen? | Did you ever activate Cricket Alert? |
| B4 | Customer | Na apu, kokhono na. | No madam, never. |
| B5 | Agent | Thik ache. Stop code confirm hole ami service ta deactivate kore 100 taka er jonno refund request raise korbo. Refund er shomoy ta ami ekhon bolte parchi na. | All right. Once the stop method is confirmed I will deactivate the service and raise a refund request for 100 taka. I cannot give the refund time now. |

Expected Copilot (Branch B): card figures match (500, 850, 350 = 250 + 100, due 07 Dec). Refund steps from vas-refund-policy, labelled MOCK. The Tk 250 extra internet is not explained by any record. No refund time. The ticket (category Billing) is submitted only after the verification has really happened.

---
## CP3 - Right offer at the right time

Customer: **Rahim Uddin**, 01200000001. A separate call from CP1.
Context (DDB): usage_pattern "Used up the pack before validity ended in each of the last 3 months." Pack 25 GB, 18 GB left, until 05 Dec. Pack catalog row `bundle-30gb-300min-30d`: 30 GB + 300 minutes, Tk 598, 30 days, activation code none published, `sample_offer = true`, `verify_before_demo = true`.
KB: bundle-packs (public snapshot), internet-packs-general (carry-forward rule).
Rule: the offer appears only after the customer's own question is answered (the analysis prompt forbids a sales offer while the problem is open or the customer is angry).
**Price status: UNVERIFIED.** Tk 598 and 30 days are the catalog values, kept unchanged, with `verify_before_demo = true`. The pack is a **demo sample offer**, not a live GP commercial offer.

### Exchange 1 - the plain question

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 1 | Customer | Hello, ami Rahim. Amar pack e ekhon koto data baki ache, bolben? | Hello, I am Rahim. Can you tell me how much data is left in my pack? |
| 2 | Agent | Assalamu alaikum Rahim bhai. Apnar mobile number ta bolben? | Peace be with you, brother Rahim. Please tell me your mobile number. |
| 3 | Customer | Shunno ek dui shunno shunno shunno shunno shunno shunno shunno ek. | Zero one two zero zero zero zero zero zero zero one. |
| 4 | Agent | Dhonnobad. Account er details dekhar age bolun, last recharge koto chilo? | Thank you. Before I look at the account details, tell me, how much was your last recharge? |
| 5 | Customer | 200 taka, 20 November. | 200 taka, 20 November. |

Expected Copilot: card loads. Compliance goes straight to GREEN (line 4 has both "details" and "last recharge"). No offer is shown yet.

### Exchange 2 - the pattern shows

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 6 | Agent | Apnar 25 GB pack e ekhon 18 GB baki, meyad 05 December porjonto. | Your 25 GB pack has 18 GB left, valid until 05 December. |
| 7 | Customer | 18 GB? Hmm. Prottek mashe amar data meyad shesh howar age e shesh hoye jay. Ei bar o hobe mone hocche. | 18 GB? Hmm. Every month my data finishes before the validity ends. I think it will happen this time too. |

Expected Copilot:
- The card's usage pattern matches. Suggestion appears: **"Offer the bigger 30-day pack (sample)"** with the pack detail: 30 GB + 300 minutes, Tk 598, 30 days, activation code "none published", marked sample offer.
- Source: bundle-packs. The panel must not show an activation code (none exists in the catalog). The panel shows "sample offer". The Tk 598 and 30 days values are catalog values and stay UNVERIFIED until a person confirms them.
- Mood: neutral or frustrated (mild).

### Exchange 3 - the offer and a question the KB cannot answer

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 8 | Agent | Apni chaile ekta boro pack ta nite paren. Eta ekta demo sample offer, GP er live offer noy, ar price ekhono verify kora noy: 30 GB ar 300 minute, price 598 taka, meyad 30 din. | If you like, you can take a bigger pack. This is a demo sample offer, not a live GP offer, and the price is not yet verified: 30 GB and 300 minutes, price 598 taka, validity 30 days. |
| 9 | Customer | Ekhon er 18 GB ta ki hobe, ei pack nile? | What will happen to my current 18 GB if I take this pack? |
| 10 | Agent | Eta ami check kore bolchi, ektu dhorun. | I will check that and tell you, please hold on. |

Expected Copilot:
- Suggestion ticked as used (agent said "boro pack").
- For line 9 the lookup answer should say the approved information does not cover this case. The public rule only says unused data carries forward when the **same** pack is bought within the validity period. The 30 GB pack is a different pack. Confidence low, and a prompt to check with a supervisor. It must not promise that the 18 GB carries over.

### Exchange 4 - activation and the price reminder

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 11 | Customer | 598 taka ektu beshi lagche, kintu 30 GB hole thik ache. Pack ta chalu kore den. | 598 taka feels a bit much, but 30 GB is fine. Please activate the pack. |
| 12 | Agent | Ji Rahim bhai, price 598 taka, validity 30 din. Apni confirm korle ami chalu kore dibo. | Yes brother Rahim, price 598 taka, validity 30 days. If you confirm I will activate it. |
| 13 | Customer | Ji, confirm. Dhonnobad. | Yes, confirmed. Thank you. |

Expected Copilot:
- After line 11: amber "Read the price and validity to the customer before activating."
- After line 12: GREEN "Price and validity read out."
- Line 13 is small talk, so no lookup runs.
- The Copilot does not activate the pack. The agent does it in the real back-office tool, which is outside this project. No activation code is given because the catalog has none.

---

## CP4 - Compliance reminder

Customer: **Rahim Uddin**, 01200000001.
Context (DDB): same card as CP3. Pack for the price read-out: `bundle-30gb-300min-30d`, Tk 598, 30 days (sample offer, verify before demo).
**Price status: UNVERIFIED** (Tk 598 and 30 days are catalog values, `verify_before_demo = true`, demo sample offer). Identity: prepaid customer, so the last-recharge question is the configured check.
Rules (CODE): `verify_identity` and `read_price_validity`. This scenario shows both turning red or amber, one being missed for a moment, and both ending green.

### Exchange 1 - the agent starts without verifying

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 1 | Customer | Hello, ami Rahim. Amake ekta pack chalu kore dite hobe, taratari. | Hello, I am Rahim. I need a pack activated, quickly. |
| 2 | Agent | Assalamu alaikum Rahim bhai. Apnar account er details ami ekhon dekhchi. | Peace be with you, brother Rahim. I am looking at your account details now. |

Expected Copilot: after line 2 the compliance panel shows RED "Verify identity first (for example, ask for the last recharge amount)." The customer card is still empty because no number was given. After line 1 the amber price reminder also appears, because "chalu kore" was said (the customer asked to activate). Both reminders are visible.

### Exchange 2 - the agent corrects

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 3 | Customer | Ami ekhon ektu byasto, taratari korun. | I am a bit busy right now, please be quick. |
| 4 | Agent | Ji, shudhu ekta jinis. Apnar mobile number ar last recharge koto chilo, bolben? | Yes, just one thing. Please tell me your mobile number and how much your last recharge was. |
| 5 | Customer | Number shunno ek dui shunno shunno shunno shunno shunno shunno shunno ek. Last recharge 200 taka, 20 November. | The number is zero one two zero zero zero zero zero zero zero one. The last recharge was 200 taka, 20 November. |

Expected Copilot: identity reminder turns GREEN "Identity checked" after line 4. The card for Rahim loads after line 5. The price reminder is still amber.

### Exchange 3 - the price is not read yet

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 6 | Customer | 30 GB er pack ta chalu kore den. | Please activate the 30 GB pack. |
| 7 | Agent | Ji Rahim bhai, ami ekhoni kore dichhi. | Yes brother Rahim, I am doing it right now. |

Expected Copilot: the amber reminder "Read the price and validity to the customer before activating." stays on screen. Line 7 contains none of the words price, validity or taka, so nothing changes. If the agent had said "dam" or "meyad" instead, it would also stay amber.

### Exchange 4 - the agent reads price and validity

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 8 | Agent | Ek minute Rahim bhai, age price ar validity bole nei. Price 598 taka, validity 30 din. Chalu korbo? | One minute, brother Rahim, let me first tell you the price and validity. Price 598 taka, validity 30 days. Shall I activate it? |
| 9 | Customer | Ji korun. | Yes, do it. |

Expected Copilot: amber turns GREEN "Price and validity read out." Both rules are green. The 598 taka and 30 days read out match the catalog row and the pack card. This is the line to point at in the demo: the Copilot caught a missed step in real time.
---

## CP5 - Customer mood turns angry

Customer: **Karim Hossain**, 01200000003, Prepaid Standard, Uttara.
Context (DDB): balance Tk 45. Pack 10 GB, 30 days, 2 GB left, until 01 Dec. Recharge Tk 100 (18 Nov). Three open tickets, all Network, "Slow internet": TKT-10101, TKT-10144, TKT-10190 (the card shows their ages, computed on the day of the call). Uttara notice: scheduled, "Planned capacity upgrade in the Uttara area", until 2:00 AM tonight.
KB: network-outage-goodwill (MOCK policy), internet-troubleshooting.
Detections: mood change, coaching tip, no sales offer while angry, repeat-complaint context, network notice.

### Exchange 1 - the call starts annoyed

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 1 | Customer | Hello! Ami Karim bolchi. Amar net abar slow! Eta niye koybar call korlam! | Hello! This is Karim. My internet is slow again! How many times have I called about this! |
| 2 | Agent | Assalamu alaikum Karim bhai, ami Tania. Apnar mobile number ta bolben? | Peace be with you, brother Karim, I am Tania. Please tell me your mobile number. |
| 3 | Customer | Shunno ek dui shunno shunno shunno shunno shunno shunno shunno tin. | Zero one two zero zero zero zero zero zero zero three. |
| 4 | Agent | Dhonnobad. Account er details dekhar age, apnar last recharge koto chilo? | Thank you. Before I look at the account details, how much was your last recharge? |
| 5 | Customer | 100 taka, 18 November. Eta die ki hobe, net to cholche na! | 100 taka, 18 November. What good is that, the internet is not working! |

Expected Copilot:
- Network complaint detected, so the "where are you now" prompt shows.
- Card loads: Karim, Uttara, Tk 45, 2 GB left, and **three open tickets** with their ages. Uttara notice appears.
- Compliance GREEN after line 4.
- Mood: frustrated.
- The lookup answer should say a planned capacity upgrade in Uttara runs until 2:00 AM tonight.

### Exchange 2 - the customer turns angry

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 6 | Customer | Prottek bar eki kotha! Kono kaj hoy na! Ami officer er sathe kotha bolbo, complain korbo! | Every time the same words! Nothing ever gets done! I will talk to an officer, I will complain! |

Expected Copilot:
- Mood bar turns **red (angry)**. If the model call fails, the keyword fallback also gives angry ("prottek bar", "kono kaj hoy na", "officer", "complain").
- Coaching tip (English): acknowledge the repeated complaint, give a ticket number and a clear time.
- **No sales suggestion appears.** The analysis rules forbid an offer while the customer is angry or the main problem is open. Suggestion "Apologise and give the fix time" may appear.

### Exchange 3 - acknowledgement and the location

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 7 | Agent | Karim bhai, apnar kotha ami bujhchi. Apni tinbar complain korechen, ami TKT-10101, TKT-10144 ar TKT-10190 dekhte pachchi. Eta amader bhul, ami dukkhito. | Brother Karim, I understand you. You have complained three times, and I can see TKT-10101, TKT-10144 and TKT-10190. This is our fault, I am sorry. |
| 8 | Agent | Apni ki ekhon Uttara te achen? | Are you in Uttara right now? |
| 9 | Customer | Ha, Uttara tei. Ager 3 bar o to ekta kothai bolsilo! | Yes, in Uttara. The previous three times also said the same thing! |

Expected Copilot:
- The agent's ticket numbers match the card (all three are real rows in the tickets table).
- Current location set to Uttara. The scheduled upgrade notice shows for both registered and current location.
- Customer mood stays angry or frustrated. Agent mood: empathetic or calm.

### Exchange 4 - a clear time and a ticket

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 10 | Agent | Uttara te ekta planned capacity upgrade cholche, ata ajke rat 2 tar moddhe thik hobe. Apnar jonno ami notun ekta ticket raise korchi, ager 3 ta complaint er reference soho. | A planned capacity upgrade is going on in Uttara, and it will be fixed tonight by 2 AM. I am raising a new ticket for you, with the earlier 3 complaints as reference. |
| 11 | Agent | Rat 2 tar por o jodi slow thake, ei ticket number niye abar call korben. | If it is still slow after 2 AM, call again with this ticket number. |
| 12 | Customer | Hmm. Thik ache. Ticket number ta den. | Hmm. All right. Give me the ticket number. |

Expected Copilot:
- The fix time (2:00 AM tonight) matches the notice. Suggestion "Apologise and give the fix time" is ticked as used (the agent said "thik hobe").
- Mood moves from angry towards neutral.
- The new ticket is created only when the agent presses Submit. The agent then reads out the number the panel shows. Do not read a number before that.
- The KB has no escalation or callback policy. The Copilot must not invent a supervisor transfer or a callback time. The MOCK goodwill bonus (outage over 24 hours) does not apply to a one-evening planned upgrade.

---

## CP6 - Ask Copilot (Knowledge Base question)

Caller: **Sumon Ahmed** (Postpaid Platinum, Gulshan). The card is not needed here. **Ask Copilot receives only the question and the knowledge articles. It never sees the customer card, the network notice or the pack catalog.**
KB: roaming-basics (public), roaming-malaysia-pack (public), roaming-activation (public). All three are flagged "check before use" because the roaming site forbids automated access. A person must confirm them on the live page before the demo.

### Exchange 1 - the customer asks, the agent is unsure

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 1 | Customer | Hello, ami Sumon bolchi. Ami roaming e data pack nite chai. Eta kivabe chalu hoy? | Hello, this is Sumon. I want to take a data pack on roaming. How is it activated? |
| 2 | Agent | Assalamu alaikum Sumon bhai. Ektu dhorun, ami ei bepare ta niche ekbar confirm kore nichchi. | Peace be with you, brother Sumon. Please hold on, let me confirm this once. |

Expected Copilot: the automatic lookup runs on line 1. With no confirmed number, the card is empty, so it answers from the roaming articles and tells the agent to confirm the mobile number first for account-specific steps.

### Exchange 2 - the agent asks Copilot (internal question)

The agent types this into the Ask Copilot box. It is not spoken to the customer.

| # | Speaker | Text typed (Banglish) | English meaning |
|---|---|---|---|
| 3 | Agent -> Copilot | Roaming e data pack kivabe chalu korte hoy? Postpaid customer. | How do I activate a data pack on roaming? Postpaid customer. |

Expected Copilot answer (English, short steps; wording varies):
1. Check the customer's roaming type. Standard and Data roaming customers can buy internet packs. Basic roaming customers can buy voice packs only. SMS roaming customers cannot buy roaming packs.
2. If the customer is on Basic or Data roaming, migrate to Standard roaming by dialling `*66*1*2#` or using MyGP.
3. The customer opens the My GP app, Menu, Roaming, to activate roaming and buy a pack. Each roaming pack also has its own USSD code on the roaming pack page.
4. The pack price comes from the account balance. Validity counts from the time of purchase. Roaming packs do not renew automatically, and buying a new roaming pack clears the benefits of the previous one.

All roaming figures and codes in the answer are **unverified** until a person confirms them on the live roaming page. Sources shown: **Roaming activation and packs** (roaming-basics, public) and possibly **Roaming packs for Malaysia and nearby countries** (roaming-malaysia-pack, public), each with its grameenphone.com link. The codes must match the article text exactly.

### Exchange 3 - the agent asks something the KB does not hold

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 4 | Customer | Malaysia te call korle minute e koto taka lagbe? | How much per minute will a call from Malaysia cost? |
| 5 | Agent -> Copilot | Malaysia roaming call rate per minute koto? | What is the Malaysia roaming call rate per minute? |

Expected Copilot answer: **"Not found in approved knowledge."** followed by a suggestion to escalate or check the official GP roaming page. (The current build uses its own sentence, "I could not find this in the approved knowledge. Please escalate to a supervisor.". See the wording variance above.) The articles only say roaming calls use a 60-second pulse and pay-as-you-go charges apply after the bundle. No number appears. If a rate appears, treat it as a failure: it would be invented (the roaming rates page was never collected).

### Exchange 4 - the agent relays

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 6 | Agent | Sumon bhai, pack nite hole age apnar roaming type ta dekhte hobe. Standard ba Data roaming hole pack kena jay. Call er rate ta ami confirm kore apnake janabo. | Brother Sumon, to take a pack I first need to check your roaming type. With Standard or Data roaming you can buy a pack. I will confirm the call rate and tell you. |
| 7 | Customer | Thik ache, dhonnobad. | All right, thank you. |

Expected Copilot: line 7 is small talk, so no lookup. The Ask history shows both questions with their sources (or the "not found" reply). The agent's promise to call back is the agent's, not the Copilot's.

---

## CP7 - Automatic call summary and ticket creation

Call: the **Karim Hossain** call from CP5, from the closing lines to the ticket.
Context (DDB): same card as CP5. The summary uses only the transcript and the card.
Fields (CODE): summary has `problem`, `checked`, `outcome`, `follow_up`, `category`, `subject`. Allowed categories: Network, Billing, Pack, SIM, Roaming, Other. Subject must stay under 80 characters in the draft.

### Exchange 1 - the call closes

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 1 | Agent | Karim bhai, ar kono shahajjo lagbe? | Brother Karim, do you need any other help? |
| 2 | Customer | Na. Rat 2 tar por dekhi. Dhonnobad. | No. I will see after 2 AM. Thank you. |
| 3 | Agent | Dhonnobad Karim bhai. Allah hafez. | Thank you, brother Karim. God keep you. |

Expected Copilot: small talk, no lookup. Then the agent ends the call (End call button).

### Exchange 2 - the summary is written

No speech. After End call the system writes the summary in the background and saves the call record to the `calls` table. Expected draft (illustrative, the wording varies, the facts must match the call):

| Field | Expected content |
|---|---|
| problem | Customer reported slow internet again and was angry about repeated complaints. Three earlier Network tickets are still open (TKT-10101, TKT-10144, TKT-10190). |
| checked | Agent confirmed the mobile number, asked the last recharge amount, confirmed the customer is in Uttara and checked the network notice: planned capacity upgrade until 2:00 AM tonight. |
| outcome | Agent apologised, gave the fix time from the notice and raised a new ticket referencing the earlier complaints. Customer calmed down. |
| follow_up | If the internet is still slow after 2:00 AM, the customer will call back with the new ticket number. |
| category | Network |
| subject | Repeat slow internet in Uttara, 3 earlier open tickets (about 50 characters) |

What must NOT appear: a refund, a goodwill bonus, a supervisor transfer, a named callback time or any fact not said in the call.

### Exchange 3 - the agent reviews and submits

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 4 | Agent (to supervisor) | Summary ta thik ache. Ami shudhu subject ta ektu choto kore Submit korchi. | The summary is fine. I am only shortening the subject a little and submitting. |

Expected Copilot: the draft is editable. On Submit the panel sends the ticket (`POST /api/tickets`) with: `call_sid`, `customer_id` = karim, `category` = Network, `subject`, `problem`, `checked`, `outcome`, `follow_up`.

### Exchange 4 - the ticket exists

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 5 | Agent (to supervisor) | Ticket toiri hoye geche, number ta panel e dekhachhe. | The ticket has been created, the panel shows the number. |

Expected Copilot / system:
- The system saves a new row in `gp-copilot-tickets` for `karim` with a system-assigned `TKT-` number (next free number after the highest existing one), `status = open`, `created_at`, `source = app`, plus the submitted fields. The number is not scripted because it is assigned at that moment.
- The call record in `gp-copilot-calls` now holds the transcript, mood history, suggestions shown and used, answers, compliance state, summary and ticket.
- This is a **mock ticket system**. Say "mock ticket" on screen. No real GP ticketing API is called.
- The ticket has no field for a refund time or a callback time.

---

## CP8 - Unused data carry-forward (public GP information and mock policy, kept apart)

Customer: **Mitu Akter**, 01200000005, Prepaid Standard, Chattogram. Her record is used only for the facts below. This call has nothing to do with CP9.
Context (DDB): balance Tk 80. Pack "8 GB, 7 days", 5 GB left, valid until 29 Nov. Recharge Tk 199 (22 Nov).
Identity: prepaid, so the last-recharge question is the configured check.

Two sources, never merged:

| | Source A: public GP information | Source B: MOCK policy |
|---|---|---|
| Article | `internet-packs-general` | `carry-forward-sample` |
| Type tag in the KB | public | **mock** |
| Origin | grameenphone.com/general-information-about-internet-packs, fetched 2026-11 | `mock://demo/carry-forward-sample`, written by the project team |
| What it says | Unused data is carried forward if the customer buys the **same pack within the active validity period**, or if auto-renew succeeds. It says nothing about what happens to leftover data after validity ends. | SAMPLE: if the same pack is bought again within **3 days after** expiry, leftover data carries forward. After 3 days it is lost. |
| May be said as | "GP's published page says..." | "This is a demo sample rule. It is **not GP policy**." |

The mock article also contains a sentence saying GP's recharge terms forfeit leftover data after validity ends. That sentence sits inside a mock article and was not read from a public page, so the script treats it as **unconfirmed** and the agent does not repeat it as GP policy.

How to keep them separate on screen: ask the Copilot **two separate questions** in the ask box (exchange 3 and exchange 4). The live lookup may retrieve both articles at once. If it blends them into one answer, treat that as a defect to report, not as something to say to the customer.

### Exchange 1 - the question

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 1 | Customer | Hello, ami Mitu. Amar pack e 5 GB baki ache, kintu pack to 29 tarikh e shesh hoye jabe. Shesh hole ei 5 GB ta ki jabe? | Hello, I am Mitu. I have 5 GB left, but the pack ends on the 29th. What happens to this 5 GB when it ends? |
| 2 | Agent | Assalamu alaikum Mitu apu. Apnar mobile number ta bolben? | Peace be with you, sister Mitu. Please tell me your mobile number. |
| 3 | Customer | Shunno ek dui shunno shunno shunno shunno shunno shunno shunno pach. | Zero one two zero zero zero zero zero zero zero five. |
| 4 | Agent | Dhonnobad. Account er details dekhar age, last recharge koto chilo? | Thank you. Before I look at the account details, how much was your last recharge? |
| 5 | Customer | 199 taka, 22 November. | 199 taka, 22 November. |

Expected Copilot: card loads (Mitu, Chattogram, 5 GB left, valid until 29 Nov, recharge Tk 199 on 22 Nov). Compliance GREEN. The lookup may retrieve both carry-forward articles.

### Exchange 2 - Source A, the public GP information

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 6 | Agent -> Copilot (typed) | GP er public niyom e unused data carry forward kivabe hoy? | How does carry-forward of unused data work under GP's public rule? |
| 7 | Agent | Apnar 8 GB, 7 din er pack e 5 GB baki, meyad 29 November porjonto. GP er public page ta bolche: meyad cholar moddhe same pack abar kinle, ba auto-renew hole, baki data jog hoy. | Your 8 GB, 7-day pack has 5 GB left, valid until 29 November. GP's public page says: if you buy the same pack again while the validity is active, or if auto-renew succeeds, the leftover data is added. |

Expected Copilot: an answer built from `internet-packs-general`, source type **public**, with its grameenphone.com link. The deadline that follows from the rule and the card is "before the pack's validity ends (29 Nov)". The answer must not mention the 3-day rule.

### Exchange 3 - what the public page does not say

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 8 | Customer | Ar meyad shesh hoye gele ki hobe? 3 din er moddhe kinle ki thakbe? | And what if the validity has ended? If I buy within 3 days, will it stay? |
| 9 | Agent | Public page e meyad shesh hoye gele ki hobe ta ami ekhon dekhte pachchi na, ami check kore janabo. | I cannot see what the public page says about after validity ends. I will check and tell you. |

Expected Copilot: confidence low or medium. It says the public article does not cover the case after validity ends. It must not fill the gap with the sample rule.

### Exchange 4 - Source B, the mock policy, labelled

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 10 | Agent -> Copilot (typed) | Demo sample carry-forward rule ta ki? | What is the demo sample carry-forward rule? |
| 11 | Agent | Alada ekta demo sample niyom ache, eta GP er policy noy: meyad shesh hobar 3 din er moddhe same pack kinle baki data thake. Demo te eta 29 November er 3 din por, mane 2 December porjonto. | There is a separate demo sample rule, which is not GP policy: if you buy the same pack within 3 days of expiry, the leftover data stays. In the demo that means up to 2 December, 3 days after 29 November. |
| 12 | Customer | Eta ki GP er asol niyom? | Is this GP's real rule? |
| 13 | Agent | Na, eta shudhu demo sample. GP er asol niyom ager ta, jeta public page e ache. Kon pack ta abar kinbo ta ami system e dekhe nichchi. | No, this is only a demo sample. GP's actual rule is the earlier one, from the public page. I will check in the system which pack to buy again. |

Expected Copilot:
- Answer from `carry-forward-sample`, source type **mock**, with the words "sample" and "not GP policy". The date 02 Dec is calculated from the card (29 Nov) and the sample rule. It is not stored anywhere.
- The pack to buy again is not stated as a fact. The catalog row `bundle-8gb-150min-7d` (Tk 199, 7 days, **UNVERIFIED**, no activation code) is consistent with her Tk 199 recharge, but her record does not name her pack. The agent confirms it in GP's system.
- If either party says "chalu kore", the price reminder starts and needs "price", "validity" or "taka" from the agent.

---
## CP9 - Lost SIM and replacement (HYPOTHETICAL scenario, unconfirmed)

> **HYPOTHETICAL.** No customer has confirmed losing a SIM. This scenario is a project assumption, shown only to demonstrate the Knowledge Base steps for a lost SIM. Say "this is a hypothetical case" before the call and again after it.

How Mitu's record is used:
- Her prepaid Standard Chattogram card is a **stand-in profile**. The demo needs a prepaid Standard customer in Chattogram, and she is the only one.
- The assumption is **not a fact about Mitu**. Her `usage_pattern` field contains the seed text "Reported a lost SIM." That text is the same unconfirmed assumption. The script does not treat it as evidence. If the Copilot repeats it, the presenter says it is seed text.
- Nothing from this call is submitted as a ticket and nothing is saved as a real event for her. The summary is shown for illustration only.

Context (DDB, facts only): Prepaid, Standard, Chattogram, balance Tk 80, pack 8 GB / 7 days, 5 GB left, until 29 Nov, recharge Tk 199 (22 Nov). The record holds no SIM-lost event, no block status, no SIM type (Skitto or not) and no NID.
Identity: prepaid, so the last-recharge question is the configured check (client confirmation pending).
GP Centres: Chattogram has one row, "GP Centre, Chattogram (sample)", with the address "Sample entry. Not a real GP Centre address." and no hours.
KB: sim-replacement (public), customer-contacts (public). Next-best-action: nba-sim-block, "Verify identity, then block the SIM".
What the KB says: order is verify identity, block the SIM, then explain replacement. Fee Tk 300, free only for Gold, Platinum and Signature Star (public page to be confirmed). NID required, fingerprint must match. Online delivery only in selected metropolitan city areas. Skitto SIMs cannot be replaced through the online shop.
What the KB does not say: **how to block a lost SIM.** The article says "Do not invent one." Public contacts are 121, 158 and live chat.

### Exchange 1 - the hypothetical report

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 1 | Customer | Hello, dhoro amar SIM ta hariye gese, ami onno number theke call korchi. | Hello, suppose my SIM is lost and I am calling from another number. |
| 2 | Agent | Assalamu alaikum. Eta ekta hypothetical case. Je GP number ta hariyeche dhorchi, sheta bolben? | Peace be with you. This is a hypothetical case. Please tell me the GP number we are supposing was lost. |
| 3 | Customer | Shunno ek dui shunno shunno shunno shunno shunno shunno shunno pach. | Zero one two zero zero zero zero zero zero zero five. |

Expected Copilot:
- The card loads from the number. The agent reads nothing from it yet.
- Suggestion "Verify identity, then block the SIM" appears with only the Chattogram placeholder centre.
- The lookup answer starts with "After verifying identity:" and reminds the agent of the order verify, block, replace. Source: sim-replacement.
- The Copilot must not state as fact that this customer's SIM is lost.

### Exchange 2 - verify, then block

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 4 | Agent | Apnar account er details dekhar age ektu verify kore nei. | Before I look at your account details, let me verify. |
| 5 | Agent | Apnar last recharge koto chilo, bolben? | How much was your last recharge? |
| 6 | Customer | 199 taka, 22 November. | 199 taka, 22 November. |
| 7 | Agent | Dhonnobad. Ami SIM block korar step ta confirm kore nichchi, ektu hold e thakun. | Thank you. I am confirming the steps to block the SIM, please hold on. |

Expected Copilot:
- RED after line 4, GREEN after line 5.
- The suggestion ticks "used" after line 7 because the agent said "block". The tick means the word was said. It does not mean a block happened.
- There is **no approved block procedure** to show. If the agent asks, the answer is "Not found in approved knowledge." with a pointer to the official GP process and the public contacts 121 and 158. The block is done in GP's own system, outside this project.
- Only two compliance rules exist. A "verify and block before replacement" rule appears in the project plan but is not in `compliance_rules.json`, so no separate reminder shows.

### Exchange 3 - replacement details

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 8 | Agent | Block howar por notun SIM nite hole NID lagbe, ar fingerprint milte hobe. Replacement fee 300 taka. Gold, Platinum ba Signature Star customer hole free, kintu ei profile er tier Standard. | After the block, a new SIM needs the NID and a matching fingerprint. The replacement fee is 300 taka. It is free for Gold, Platinum or Signature Star customers, but this profile's tier is Standard. |
| 9 | Customer | Chattogram e kothay jabo? Ar online e SIM pathano jabe? | Where do I go in Chattogram? And can the SIM be delivered online? |
| 10 | Agent | Chattogram er GP Centre er thikana ami confirm kore bolchi. Online delivery shudhu kichu boro shohor er elaka te ache. | I will confirm the address of the GP Centre in Chattogram. Online delivery is available only in some big-city areas. |
| 11 | Customer | Amar 5 GB data ta ki notun SIM e pabo? | Will I get my 5 GB data on the new SIM? |
| 12 | Agent | Eta ami check kore janachchi. | I will check that and let you know. |

Expected Copilot:
- Fee, NID and fingerprint match sim-replacement. The article notes a discrepancy with another GP page (Platinum Plus and Platinum). The tier list must be confirmed on the live page. A Standard customer pays the fee under both lists.
- GP Centre: the Copilot says the Chattogram entry is a **sample placeholder with no real address**. It gives no address and no hours.
- Online delivery: the article says "selected metropolitan city areas" and does not say whether Chattogram is one. Confirm.
- Data on a new SIM: not covered. Answer "Not found in approved knowledge."
- The card has no SIM-type field. The agent asks whether the SIM is Skitto.

### Exchange 4 - close, nothing submitted

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 13 | Customer | Bujhlam. NID niye jabo. Dhonnobad. | I understand. I will take my NID. Thank you. |
| 14 | Agent | Dhonnobad. Eta ekta hypothetical case chhilo, tai kono ticket raise korchi na. | Thank you. This was a hypothetical case, so I am not raising a ticket. |

Expected Copilot:
- Summary draft (category SIM) is shown only to illustrate the format. It states only what was said. It must not say the SIM was blocked unless the agent said the block was done, and must not state a lost SIM as a fact about the customer.
- The agent does **not** press Submit. The saved call record still goes to the `calls` table and to S3 when the call ends. Tell the client this, or use the "no customer" path (do not give the number) if a record against Mitu is not wanted. See the Data Gaps file.

---
## CP10 - International roaming, Malaysia (general information only until postpaid verification exists)

Customer: **Sumon Ahmed**, 01200000004, Postpaid Platinum, Gulshan.
Context (DDB): Postpaid 40 GB, 22 GB left, until 30 Nov. Balance Tk 0. No recharge history. usage_pattern "Travelling to Malaysia next week. Roaming not activated." Gulshan has no notice.
**Identity: postpaid. The approved verification procedure is MISSING.** The agent does not use "last recharge", does not read anything from the card to the customer, and activates nothing. General roaming information is allowed.
Pack (DDB `packs`, `roaming-travel-10gb-10d`): 10 Days Travel Pack, 10 GB, Thailand, Malaysia, Singapore, India, **Tk 1599, 10 days, code `*121*1599#`** (catalog values kept unchanged). `verify_before_demo = true`, so **UNVERIFIED until a person confirms it on the live roaming page**.
KB: roaming-malaysia-pack, roaming-basics, roaming-activation, roaming-travelsure (all public, all flagged "check before use").
Cross-check done: the catalog row and the roaming-malaysia-pack article agree on price, validity, code and the four countries including Malaysia. Both were read from the same roaming site, which forbids automated access, so agreement between them is not independent confirmation.
Missing: Sumon's roaming type is not stored.

### Exchange 1 - the trip

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 1 | Customer | Hello, ami Sumon Ahmed. Ami agami shoptahe Malaysia jachchi. Amar phone ta wahan e kaj korbe to? | Hello, I am Sumon Ahmed. I am going to Malaysia next week. Will my phone work there? |
| 2 | Agent | Assalamu alaikum Sumon bhai. Apnar mobile number ta bolben? | Peace be with you, brother Sumon. Please tell me your mobile number. |
| 3 | Customer | Shunno ek dui shunno shunno shunno shunno shunno shunno shunno char. | Zero one two zero zero zero zero zero zero zero four. |

Expected Copilot: the card loads and the roaming suggestion appears with the 10 Days Travel Pack details marked unverified. The agent does not read the card's "Roaming not activated" note to the customer.

### Exchange 2 - verification is missing

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 4 | Agent | Sumon bhai, apnar account er details dekhar age verification lagbe. Kintu postpaid er approved verification niyom ta ekhono amar kache nei, tai ami account er kono tottho bolbo na, ar kono service o chalu korbo na. | Brother Sumon, I need verification before looking at your account details. But the approved verification method for postpaid is not with me yet, so I will not share any account information or activate any service. |
| 5 | Agent -> Copilot (typed) | Postpaid customer ke verify korbo kivabe? | How do I verify a postpaid customer? |
| 6 | Customer | Thik ache. Tahole general ta bolen, Malaysia gele ki korte hobe? | All right. Then tell me the general things, what do I need to do for Malaysia? |
| 7 | Agent | Roaming chalu korte MyGP app e Menu, tarpor Roaming e jete hoy. Ami ekta roaming pack er kotha general bhabe bolte pari. | Roaming is activated in the MyGP app under Menu, then Roaming. I can talk about a roaming pack in general terms. |

Expected Copilot:
- Line 4 turns the identity reminder RED. It **stays red**.
- Line 5 answer: **"Not found in approved knowledge."** with a suggestion to escalate or check the official GP process. No last-recharge question, no other invented method.
- Line 7 answer from roaming-basics and roaming-activation, general only: roaming types (Standard recommended), Standard and Data roaming can buy internet packs, Basic cannot, migrate with `*66*1*2#` or MyGP, other ways to activate (online with a USD-enabled card, agent support, GP store). All marked unverified. The answer must not use the customer's card and must not tell the agent the customer's roaming type.

### Exchange 3 - general pack information and bill control

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 8 | Customer | Malaysia er jonno kon pack, koto taka, koto din? | Which pack for Malaysia, how much, and for how many days? |
| 9 | Agent | General tottho bolchi, price ekhono verify kora noy: 10 Days Travel Pack, 10 GB, price 1599 taka, validity 10 din, Malaysia, Thailand, Singapore ar India te. | General information, the price is not yet verified: the 10 Days Travel Pack, 10 GB, price 1599 taka, validity 10 days, in Malaysia, Thailand, Singapore and India. |
| 10 | Customer | Ami 7 din thakbo. Roaming e bill beshi ashar bhoy lage. Kono limit deya jay? | I will stay 7 days. I am afraid of a high roaming bill. Can a limit be set? |
| 11 | Agent | TravelSure e limit deya jay, *121*6*6*3# dial kore limit thik korte hoy. Postpaid hole usage alarm er jonno *121*6*6*5*1# dial korle SMS alert ashe. Eta public tottho, ami ekhono live page e confirm kori ni. | You can set a limit with TravelSure by dialling *121*6*6*3#. For postpaid, dialling *121*6*6*5*1# gives SMS alerts for usage. This is public information I have not yet confirmed on the live page. |

Expected Copilot:
- Pack figures equal the catalog and roaming-malaysia-pack exactly, and the panel marks them **unverified**.
- TravelSure answer equals roaming-travelsure: limit options 5 MB (default), 20 MB, 50 MB, 200 MB or unlimited; set with `*121*6*6*3#`; Usage Alarm for postpaid `*121*6*6*5*1#`; IR status check for postpaid `*121*6*6*1#`.
- The article says postpaid roaming bills can exceed the limit because charging is offline. It does not say how the 5 MB default limit works with a 10 GB pack. The Copilot must not promise that the pack removes the limit or that the bill cannot rise.
- The 7-day stay is the customer's own statement.

### Exchange 4 - activation request is declined until verification

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 12 | Customer | Thik ache. Pack ta ekhoni chalu kore den. | All right. Please activate the pack right now. |
| 13 | Agent | Sumon bhai, price 1599 taka, validity 10 din, kintu verification chhara ami kono pack chalu korte parbo na. Pack er validity kena r shomoy theke shuru hoy, tai jawar kachakachi nijer MyGP login diye kinle bhalo. | Brother Sumon, price 1599 taka, validity 10 days, but without verification I cannot activate any pack. The validity starts from the time of purchase, so it is better to buy it close to your trip through your own MyGP login. |
| 14 | Customer | Accha, ami jawar age nijei MyGP theke kine nibo. | All right, I will buy it myself from MyGP before I leave. |

Expected Copilot:
- After line 12: amber price reminder. After line 13: GREEN (the agent said price, validity and taka).
- Green means the price was read out. It does **not** mean the pack was activated. Nothing is activated.
- The validity-from-purchase advice comes from the roaming articles. Packs do not renew automatically, and a new roaming pack clears the benefits of the previous one.
- Not covered by the KB, so not stated: how a postpaid pack is charged with a Tk 0 balance.
- Summary (category Roaming): states that identity was **not verified**, that general information was given, that nothing was activated, and that the customer will buy via MyGP. No ticket is needed.

---
## CP11 - English-speaking customer

Customer: **Karim Hossain**, 01200000003, Prepaid Standard, Uttara. A new call, English throughout.
Context (DDB): balance Tk 45. Recharge Tk 100 on 18 Nov. Pack 10 GB, 2 GB left, until 01 Dec. Three open Network tickets (Slow internet). Uttara notice: scheduled capacity upgrade until 2:00 AM tonight.
KB: recharge-terms (public), internet-troubleshooting (public).
Point of the scenario: the same transcript, card, compliance, lookup and summary work for English with no setting changed. For this CP the "Banglish" column is the English words themselves, and the meaning column is repeated only where the English is not obvious.

### Exchange 1 - the question

| # | Speaker | Dialogue | Meaning |
|---|---|---|---|
| 1 | Customer | Hello, good afternoon. I recharged 100 taka on the 18th of November and I want to check that it reached my balance. | Same as spoken. |
| 2 | Agent | Good afternoon, thank you for calling Grameenphone, this is Tania. May I have your mobile number please? | Same as spoken. |
| 3 | Customer | Sure. Zero one two zero zero zero zero zero zero zero three. | Same as spoken. |
| 4 | Agent | Thank you. Before I open the account details, could you tell me your last recharge amount? | Same as spoken. |
| 5 | Customer | It was 100 taka, around the 18th. | Same as spoken. |

Expected Copilot: digits spoken in English are recognised and Karim's card loads. Line 4 has both "details" and "last recharge", so compliance goes straight to GREEN. The recharge answer matches the card (Tk 100, 18 Nov). All Copilot text is in English as always.

### Exchange 2 - the balance question

| # | Speaker | Dialogue | Meaning |
|---|---|---|---|
| 6 | Agent | I can see a Tk 100 recharge on 18 November on your record, and your main balance now is Tk 45. | Same as spoken. |
| 7 | Customer | Only 45? I expected more. Why is it not 100? | Same as spoken. |

Expected Copilot (sources: recharge-terms, public):
- Amounts recharged beyond a pack price go to the main balance. For an online pack purchase, any earlier subscription fee or emergency balance already used is deducted first, and the pack may not activate. The customer may need to wait up to 30 minutes for the confirmation.
- Check the main balance with `*566#`.
- The record has no breakdown, no channel and no deductions, so the Copilot must say it cannot tell why the balance is Tk 45. It must not invent a reason.

### Exchange 3 - internet left and slow internet

| # | Speaker | Dialogue | Meaning |
|---|---|---|---|
| 8 | Agent | I cannot see a breakdown here. You can check your main balance by dialling star 566 hash. | Same as spoken. |
| 9 | Customer | OK. How much internet do I have left? It has also been slow lately. | Same as spoken. |
| 10 | Agent | You have 2 GB left, valid until 01 December, and you can check it with star 121 star 1 star 4 hash. Are you in Uttara right now? | Same as spoken. |
| 11 | Customer | Yes, I am in Uttara. | Same as spoken. |

Expected Copilot:
- After line 9 the "where are you now" prompt appears (internet + slow). After line 11 the current location is Uttara.
- Card data used: 2 GB left, valid until 01 Dec. Source for the dial code: recharge-terms or internet-troubleshooting (`*121*1*4#`).
- Network notice: planned capacity upgrade in Uttara until 2:00 AM tonight. The card also shows three open "Slow internet" tickets (ages computed that day). The Copilot should point the agent to these and must not say the network is fine.
- Mood: neutral.

### Exchange 4 - close

| # | Speaker | Dialogue | Meaning |
|---|---|---|---|
| 12 | Agent | I can see the three earlier tickets about slow internet and the planned upgrade in Uttara until 2 AM tonight. Would you like me to add a note? | Same as spoken. |
| 13 | Customer | No, that is clear. Thanks. | Same as spoken. |

Expected Copilot: line 13 is small talk, so no lookup. Summary (English), category **Billing** or **Network**: balance and recharge enquiry answered from the record, internet balance given, open tickets and Uttara notice mentioned, no new ticket requested. The agent decides whether to raise one.

---

## CP12 - Adoption dashboard (real saved-call metrics and fixed demo metrics, kept apart)

No customer call. This is a short supervisor and presenter conversation at the Adoption dashboard tab.

The dashboard adds a fixed baseline to the real saved calls and shows one total. The presenter reads them apart:

| Metric | Fixed demo baseline (`dashboard_seed.json`) | Real saved-call part (DynamoDB `calls`, 2026-11-25) | Total on screen today |
|---|---|---|---|
| Calls that used Copilot | 128 | 9 (all rehearsal calls) | 137 |
| Suggestions shown | 412 | 6 | 418 |
| Suggestions accepted | 301 | 1 | 302 (shown as 72 percent) |
| Handling time saved per call | 48 s, fixed | none, nothing measures it | 48 s |
| Mood trend Mon-Fri | fixed sample rows | 106 readings: 89 neutral, 17 frustrated, 0 angry (returned by the API, not drawn) | fixed sample rows only |
| Most used suggestions | fixed sample list (96, 88, 81, 54) | none | fixed sample list |

Real-only acceptance is 1 of 6, about 17 percent. The 72 percent on screen is dominated by the fixed baseline (301 of 412 is 73 percent).

### Exchange 1 - how many calls

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 1 | Supervisor | Ei dashboard e koyta call e Copilot use hoyeche? | How many calls on this dashboard used the Copilot? |
| 2 | Presenter | Screen e 137 dekhachhe. 128 ta fixed demo baseline, ar 9 ta ei system e save kora rehearsal call, jegulo asol customer call noy. | The screen shows 137. 128 is a fixed demo baseline, and 9 are rehearsal calls saved in this system, not real customer calls. |

### Exchange 2 - suggestions

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 3 | Supervisor | Suggestion koyta dekhano hoyeche, koyta agent nilo? | How many suggestions were shown and how many did agents take? |
| 4 | Presenter | 418 ta, 72 shotangsho. Kintu er moddhe 412 ar 301 fixed baseline. Saved call theke shudhu 6 ta dekhano, 1 ta neya hoyeche. | 418, and 72 percent. But 412 and 301 of those are the fixed baseline. From saved calls only 6 were shown and 1 was taken. |

### Exchange 3 - what is fixed

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 5 | Supervisor | Handle time 48 second kom hoyeche, mood trend o ki real? | Handle time is 48 seconds lower, and is the mood trend real? |
| 6 | Presenter | Na, duitai fixed demo number. Footer e "Sample data for the demo" lekha ache. Real mood data save hoy, kintu screen e dekhano hoy na. | No, both are fixed demo numbers. The footer says "Sample data for the demo". Real mood data is saved but not drawn on screen. |

### Exchange 4 - which suggestions

| # | Speaker | Banglish | English meaning |
|---|---|---|---|
| 7 | Supervisor | Kon suggestion shobcheye beshi use hoy? | Which suggestion is used the most? |
| 8 | Presenter | Eta fixed sample list: Apologise and give the fix time 96, Read price and validity 88, Verify identity first 81, bigger 30-day pack 54. Real call theke ei list toiri hoy na. | This is a fixed sample list: Apologise and give the fix time 96, Read price and validity 88, Verify identity first 81, bigger 30-day pack 54. This list is not built from real calls. |

Notes:
- Two of the four list names are compliance reminders in the real system, not next-best-action suggestions, so the sample list mixes two kinds of items.
- **Effect of the test data:** the 9 saved test calls add 9 to calls, 6 to shown and 1 to accepted, and make the footer say 9 live calls. Seven of the 9 have no identified customer, and some have only 1 or 2 turns, but each counts as a "call that used Copilot". The test ticket TKT-10234 does **not** affect any dashboard figure, because the dashboard reads only the calls table. It changes the next ticket number and appears in the ticket list.
- Every call made during the demo changes the numbers: +1 call, plus the suggestions shown and used in it.
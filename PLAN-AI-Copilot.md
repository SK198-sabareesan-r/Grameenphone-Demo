# AI Copilot (Use Case 3) – Build Plan

Source: "Grameenphone Demo.docx", Section 5 (Module 3, AI Copilot / Real-Time Agent Assist).
Demo slot: 10:00–10:15 on 26/Nov/2026. Target: working end to end today.

Stack decided: AWS (Amazon Bedrock, S3, DynamoDB, EC2), Twilio free trial for telephony, Sarvam for speech-to-text (STT) and text-to-speech (TTS).

---

## 1. What we are building

A side panel next to a live call. It shows the live transcript, the customer card, a mood bar, compliance reminders, next-best-action, an "Ask Copilot" box, and a one-click call summary and ticket. The agent decides; Copilot only suggests.

### Scope for the 15 minutes

| ID | Scenario | Priority |
|---|---|---|
| Script A | Main live call (Rahim, about 5 min) | Must |
| CP1 | Internet not working: transcript, customer card, network notice | Must |
| CP3 | Offer the bigger pack | Must |
| CP4 | Compliance reminders (verify identity, read price and validity) | Must |
| CP5 | Mood turns angry | Must |
| CP6 | Ask Copilot (roaming question, with source) | Must |
| CP7 | Summary and ticket in one click | Must |
| CP12 | Adoption dashboard | Should |
| CP2 | Bill dispute (Nusrat) | Should |
| CP8–CP11 | Carry-forward, lost SIM, roaming call, English call | Only if time allows |

### Script A line-to-panel mapping (acceptance test)

| Speaker | Line (Banglish) | Panel must show |
|---|---|---|
| Agent | "Assalamu alaikum, Grameenphone theke ami Tania bolchi…" | Transcript starts |
| Customer | "Bhai, amar net 2 din dhore kaj korche na." | Rahim's card, network notice (maintenance until 6 pm) |
| Agent | "Apnar account er details bolchi…" | Red reminder: verify identity first |
| Agent | "Age apnar last recharge koto chilo, bolben?" | Reminder turns green |
| Customer | "Prottek bar eki kotha, kono kaj hoy na!" | Mood bar red, tip to acknowledge |
| Agent | "Ami dukkhito… 6 tar moddhe thik hobe." | Mood moves toward calm |
| Agent | "Apni protimash e data shesh hoye jay, boro pack ta nite paren." | Next-best-action ticked as used |
| Customer | "Thik ache, chalu kore den." | Reminder: read price and validity |

---

## 2. Architecture

```
Customer phone ──► Twilio number ──TwiML──►  <Start><Stream track="both_tracks" url="wss://…/twilio"/>
(verified number)                             <Dial><Client>agent</Client></Dial>
                                                   │ mulaw 8 kHz, 20 ms frames, one track per speaker
Agent browser (Twilio Voice JS SDK softphone) ◄────┤
                                                   ▼
                    Copilot backend (FastAPI, Python) on AWS EC2 (Caddy for TLS)
                    ├─ Sarvam realtime STT: 2 WebSockets per call (agent track, customer track)
                    ├─ Orchestrator: debounce, context window, rule checks
                    ├─ Amazon Bedrock (Converse API): suggestions JSON, summary, Ask Copilot
                    ├─ Mock GP API (customers, network notices, tickets)  ← FastAPI routes, DynamoDB
                    ├─ Knowledge store: S3 (markdown) + retrieval
                    ├─ S3: call recordings, transcripts, summaries (AQM picks these up)
                    └─ WebSocket /panel ──► Agent panel UI (React or plain HTML + JS)
```

### Key design decisions

| Decision | Choice | Reason |
|---|---|---|
| Speaker separation | Twilio `both_tracks` gives agent and customer as separate audio | No diarization needed. Each track goes to its own Sarvam socket. |
| Audio format to STT | Pass Twilio mulaw 8 kHz straight to Sarvam (`encoding=mulaw`, `sample_rate=8000`) | Sarvam realtime STT accepts `mulaw` and 8000 Hz. No resampling. |
| Hosting | Local + ngrok for development, then one EC2 instance with Caddy for the demo | Twilio needs a public `wss://` URL. Keep it simple. |
| Retrieval | Start with the small knowledge pack in the prompt. Add embeddings only if it outgrows that. | Fewer moving parts today. |
| LLM calls | One structured call per customer turn (mood, tip, suggestion, compliance flags). Separate calls for Ask Copilot and the summary. | Keeps the live path fast. |
| Bedrock models | Fast Claude Haiku-class model for live turns, Sonnet-class for summary | Confirm which models are enabled in your region and account. |

---

## 3. Sarvam plan: STT and TTS

### 3.1 STT (the live transcript, Must)

Docs: [Saaras model](https://docs.sarvam.ai/api/api-guides-tutorials/models/saaras), [Realtime streaming API](https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/realtime-streaming).

| Setting | Value | Note |
|---|---|---|
| Endpoint | `wss://api.sarvam.ai/speech-to-text-realtime/ws` | Auth header `api-subscription-key` |
| Model | `saaras:v4` | Optimised for 8 kHz telephony and code-mixed speech |
| `encoding` / `sample_rate` | `mulaw` / `8000` | Matches Twilio Media Streams |
| `stream_type` | `fast` | Gives `transcript.partial` events for the live feel |
| `endpointing` | `vad` | Tune `silence_duration_ms` (start at 600–800 instead of the 1000 default) |
| `language_code` | Test `bn-IN` vs `auto` on real Banglish audio, then pick one | Sarvam has no separate Bangladesh code |
| `mode` | Test `transcribe` vs `codemix` | Applies to final transcripts only. Partials are always plain transcription. |
| `keyterms` | Up to 50 GP terms, 64 chars each | e.g. Grameenphone, MyGP, Skitto, bKash, Flexiplan, GP Star, roaming, Prime, Rahim, Nusrat, Tania |

Implementation notes:
- One socket per track. Tag every event with `speaker=agent|customer`.
- The socket closes after 60 s with no audio. Send `ping` events while the other side is silent.
- On `fast`, each `audio_input` frame can carry at most 500 ms of audio. Forward Twilio's 20 ms frames, or batch them to about 100 ms.
- Use `transcript.partial` for the grey live text and `transcript.final` to trigger the LLM.
- Write a fallback: if STT drops, reconnect with backoff and show "reconnecting" in the panel.

Open question to test in the first hour: how well `saaras:v4` handles Bangladeshi Bangla and Banglish spoken in Roman words. Record 10 sample sentences (the Script A lines) from a real Bangladeshi speaker on a phone call and measure. The panel can display romanised text from `mode=translit` if Bangla script output looks wrong to the audience. Decide based on what GP will see best.

### 3.2 TTS (Bulbul, used as test and backup tooling, not in the live path)

Docs: [Bulbul model](https://docs.sarvam.ai/api/api-guides-tutorials/models/bulbul).

The Copilot never speaks to the customer, so TTS is not needed live. It is still useful today:

| Use | How |
|---|---|
| Pipeline testing without people | Generate the Script A customer and agent lines as `mulaw` 8 kHz audio and replay them into the Twilio stream endpoint |
| Backup material | Audio for the backup videos of Script A, CP6 and CP7 if live audio fails |
| AQM sample calls (Q1–Q12, 20–30 calls) | Generate synthetic calls with two voices. Save to S3 for the AQM owner. |
| Rehearsal | Anyone can rehearse the panel alone |

Settings: `model=bulbul:v4-flash`, `language_code=bn-IN` (or `en-IN`), `speech_sample_rate=8000`, `output_audio_codec=mulaw`, pick one male and one female persona from the [voice catalog](https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/voices#bulbul-v4-flash-voices).

Limits to respect:
- Bulbul quality drops on romanised input. Write the Bangla words in Bengali script. Keep a script-conversion step (Bedrock prompt) that turns the Banglish scripts into Bengali script plus English words.
- No SSML. Use `pace` and sentence splitting.
- The voices are Indian Bengali (`bn-IN`). They are fine for test audio. Do not present them as Bangladeshi accent. That claim belongs to the Voice Bot module.

---

## 4. Twilio free trial plan

Docs: [Trial account guide](https://www.twilio.com/docs/usage/tutorials/how-to-use-your-free-trial-account), [Try out Voice](https://www.twilio.com/docs/usage/trials/try-out-voice), [Sarvam + Twilio guide](https://docs.sarvam.ai/api/integration/build-voice-agent-with-twilio), [Media Streams](https://www.twilio.com/docs/voice/twiml/stream).

### Trial limits that affect us (from Twilio docs)

| Limit | Impact |
|---|---|
| 75 free voice minutes, account expires after 30 days | Budget: about 5 min per rehearsal gives roughly 12 full runs. Use TTS-replayed test audio for most testing, and save live calls for rehearsals. |
| Calls only to and from verified numbers (up to 5 per account) | Verify the team phone(s) that will play Rahim. |
| Voice limited to your sign-up country | The phone that plays the customer must be from the sign-up country. Check which country your Twilio account was created with. |
| Inbound calls to trial numbers must come from a verified caller ID | Calls from unverified phones are rejected (error 21264) |
| Trial numbers are assigned in the console flow | Use the number the console gives you |

Things to verify in the first hour, because the docs do not settle them:
1. Whether a trial inbound call plays a trial-account announcement before connecting to our TwiML, and how long it is.
2. Whether `<Start><Stream track="both_tracks">` works on a trial account as documented.
3. Whether the Voice JS SDK browser client can connect to the trial number without extra limits.

### Call flow

1. Customer (team member on a verified phone) dials the Twilio number.
2. Twilio requests our webhook. We return TwiML: `<Start><Stream url="wss://…/twilio" track="both_tracks"/></Start><Dial><Client>agent</Client></Dial>`.
3. The agent answers in the browser softphone (Twilio Voice JS SDK, needs an API key, a TwiML App and an access token endpoint).
4. Audio flows to our backend over the WebSocket, one message per 20 ms frame with a `track` field.

### Fallbacks (decide now, not during the demo)

| Plan | When |
|---|---|
| A: real phone calls the Twilio number | Default for the demo |
| B: customer also uses a browser client (Client-to-Client call) | If the trial announcement or number rules block A. Avoids PSTN limits. |
| C: replay TTS audio into the stream | Rehearsal and automated tests |
| D: recorded video of Script A | Always prepared, per the playbook |

---

## 5. Knowledge base plan

### 5.1 What is publicly available (fetched and checked)

Public Grameenphone content is enough for the packs, internet rules, bill, SIM replacement and roaming topics. The scenario-specific items (network notices, customer cards, refund policy for value-added services, compliance rules) have no public source and must be mock data written by us.

| Topic | Public source | What it gives us | Used in |
|---|---|---|---|
| Internet pack rules | [General Information about Internet Packs](https://www.grameenphone.com/general-information-about-internet-packs) | Auto-renew on/off codes, balance check code, cancel code, charges after volume ends, **carry-forward rule** | CP1, CP3, CP8 |
| Internet packs and prices | [Internet Packages](https://www.grameenphone.com/personal/plans-offers/internet-packages) | Pack list | CP3 |
| Bundle packs (data + minutes) | [Bundle packs](https://www.grameenphone.com/personal/plans-offers/internet-packages/bundle-packs) | Prices, validity, minutes | CP3 |
| MyGP app how-tos, APN settings, terms | [Useful Information](https://www.grameenphone.com/useful-information) | Bill and recharge in MyGP, usage history, APN `gpinternet`, network reset steps (good for CP1 troubleshooting) | CP1, CP2 |
| SIM replacement | [SIM Replacement](https://www.grameenphone.com/shop/sim-replacement) | Fee Tk 300, free for Gold/Platinum/Signature Star, NID needed, fingerprint match, delivery in selected metro areas, Skitto excluded | CP9 |
| Roaming packs and rules | [Roaming site](https://roaming.grameenphone.com/personal/services/roaming/popular-offers/20-gb-internet) | Roaming types (Standard, Basic, Data, SMS), activation via MyGP, pack terms | CP6, CP10 |
| Roaming internet packages | [Internet Packages (roaming)](https://roaming.grameenphone.com/personal/plans-offers/internet-packages) | Cancel code and pack info | CP6 |
| Postpaid bill payment | [Postpaid Bill Payment](https://roaming.grameenphone.com/personal/services/vas-others/postpaid-bill-payment) | How to pay (prefix 78 + number) | CP2 |
| E-bill | [eBill](https://www.grameenphone.com/personal/services/vas-others/ebill) | When the e-bill arrives | CP2 |
| Value-added service example | [News Service](https://www.grameenphone.com/personal/services/vas/news-service) | Example of how a VAS is unsubscribed (SMS or USSD) | CP2 |
| Prime postpaid FAQ | [Prime Help and FAQ](https://www.grameenphone.com/personal/plans-offers/prepaid-postpaid-packages/prime/help-and-faq) | Migration rules | Extra |
| Notices | [Notice board example](https://www.grameenphone.com/notices-board-345) | Connection deactivation notices | Extra |
| Online customer service | [Online Customer Service](https://www.grameenphone.com/personal/customer-service/online-customer-service) | Contact channels | Extra |
| Main site and sitemap | [grameenphone.com](https://www.grameenphone.com), [sitemap](https://www.grameenphone.com/sitemap.xml) | Find more pages (e.g. roaming country pages, value-added services) | As needed |

Notes:
- Many pages return partial text when fetched. Prices and pack lists change often. Record the fetch date on every article and say "sample public GP content" in the demo.
- `robots.txt` sets `Crawl-delay: 10` and blocks a few paths (for example `/search/`, `/dashboard`, `/personal/plans-offers/internet-packages/1.5-gb`, `/personal/plans-offers/offers/recharge-validity-matrix`). Fetch by hand or with a 10-second delay, only a few dozen pages, and skip blocked paths.
- The public carry-forward rule says unused data carries forward if the same pack is bought within the active validity. The playbook's CP8 example says "within 3 days (sample rule)". Pick one. Recommendation: use the sample rule from the playbook, but label it clearly as a sample, as the playbook requires.
- GP's fees, codes and prices in these pages may have changed since we fetched them. A GP reviewer will notice if a number is wrong, so spot-check the three or four numbers shown on screen (the pack for CP3, the SIM fee, the dial codes).

### 5.2 What we must write ourselves (mock, label as mock on screen)

| Item | Content |
|---|---|
| Customer cards | Rahim (prepaid, 25 GB active, 18 GB left, runs out of data every month), Nusrat (bill 850 vs 500, VAS Tk 100), Karim, Sumon (roaming), Mitu |
| Network notice | "Maintenance in Rahim's area until 6 pm" |
| VAS refund policy | Confirm with customer, deactivate, raise refund (CP2) |
| Compliance rules (2–3) | Verify identity before sharing account details (e.g. last recharge amount). Read price and validity before activation. Verify identity and block the SIM before replacement. |
| Next-best-action rules | Heavy user near pack end: offer bigger 30-day pack. Network outage: apologise, give fix time, goodwill data if policy allows. |
| Ticket categories | Network, Billing, Pack, SIM, Roaming |
| Goodwill policy | Small data bonus if policy allows (CP1) |

### 5.3 Structure and pipeline

```
kb/
  raw/            fetched page text + fetch date + URL
  clean/          one markdown file per article, with front matter
  mock/           customers.json, network_notices.json, compliance.yaml, nba_rules.yaml
```

Front matter on each article:

```yaml
id: internet-packs-general
title: General information about internet packs
source_url: https://www.grameenphone.com/general-information-about-internet-packs
fetched: 2026-11-DD
language: en        # add bn versions below
category: internet  # internet | bill | sim | roaming | policy-mock
type: public        # public | mock
```

Steps:
1. Collect 15–25 public pages by hand or a slow script (10-second delay). Save raw text.
2. Clean into short articles (one topic each, under about 400 words). Keep codes and numbers exactly as published.
3. Add Bangla versions of the articles used by CP1, CP2, CP6 using Bedrock (or Sarvam Mayura translation). Have a Bangla speaker review the 5–6 articles that appear on screen.
4. Add 10–15 Banglish and English test questions per topic with the expected article (see 5.4).
5. Upload to S3 (`s3://<bucket>/kb/`).
6. Retrieval: first try putting all articles that fit (about 20–30 short ones) directly into the prompt with their IDs. If answers get slow or noisy, add embeddings and retrieve the top 3–4 articles. Test a multilingual embedding model on Banglish queries before relying on it (for example a Cohere multilingual embedding model on Bedrock, if enabled). Bedrock Knowledge Bases is the managed option. Skip it today because it adds setup and a vector store to run.
7. Every Ask Copilot answer returns the article ID and `source_url`. The panel shows it as a link.

### 5.4 Retrieval test set

| Query (as the agent types it) | Expected article |
|---|---|
| "Roaming e data pack kivabe chalu hoy?" | Roaming activation and packs |
| "How to activate a data pack in roaming" | Same |
| "pack expire hoye gese, data ferot pabo?" | Internet pack rules (carry-forward) |
| "SIM hariye gese ki korbo" | SIM replacement |
| "internet balance check korbo kivabe" | Internet pack rules (balance code) |
| "auto renew bondho korbo kivabe" | Internet pack rules (auto-renew codes) |
| "bill kivabe pay korbo" | Postpaid bill payment |

Pass mark: correct article in the top 3 for at least 90% of these, with no invented codes.

---

## 6. Data model (kept small)

DynamoDB tables (or JSON files while prototyping):

| Table | Key | Fields |
|---|---|---|
| `customers` | `msisdn` | name, type, plan, balance, pack, data_left, recent_recharges, open_tickets, tier |
| `network_notices` | `area` | message, until |
| `tickets` | `ticket_id` | customer, category, summary, status, created_at |
| `calls` | `call_sid` | started_at, transcript (list), events (mood, suggestions, compliance), summary, ticket_id, recording_key |

Mapping the incoming Twilio caller number to a customer: keep a lookup from the verified team phone number to "Rahim" (and others for other scripts). Make this switchable from a small admin page so one phone can play several customers.

---

## 7. Panel events (backend → UI)

| Event | Payload |
|---|---|
| `transcript.partial` / `transcript.final` | speaker, text, ts |
| `customer.card` | customer object, network notice |
| `mood` | customer_mood (calm/neutral/frustrated/angry), agent_mood, tip |
| `suggestion` | id, type (nba/knowledge), text, source |
| `compliance` | rule_id, status (pending/red/green), text |
| `call.ended` | trigger summary generation |
| `summary` | problem, checked, outcome, follow_up, ticket draft (category) |
| `ticket.created` | ticket_id |

Bedrock structured output for each customer turn (use the Converse API with a JSON schema or tool use):

```json
{
  "customer_mood": "calm|neutral|frustrated|angry",
  "agent_mood": "calm|neutral|rushed|empathetic",
  "tip": "string or null",
  "suggestion": {"id": "nba-bigger-pack", "text": "..."} ,
  "compliance": [{"rule_id": "verify_identity", "state": "pending|red|green"}]
}
```

Compliance logic is hybrid: a keyword and intent check decides most states (identity question asked, price read), and the LLM breaks ties. That keeps the red/green behaviour predictable in the demo.

Latency target: transcript text in about 1–2 s, panel update within about 3 s of the end of a customer turn.

---

## 8. Build plan for today

Durations are estimates. Run blocks 2, 3 and 5 in parallel if more than one person is working.

| # | Block | Work | Done when |
|---|---|---|---|
| 0 | Setup (about 1 h) | Twilio number, verified phones, API key, TwiML App. Sarvam key. Bedrock model access. S3 bucket. Repo skeleton, ngrok. | A test call reaches the backend and frames are logged. Trial-limit questions in section 4 answered. |
| 1 | Live transcript (about 2 h, Priority 1) | Twilio stream handler, two Sarvam sockets, WebSocket to panel, keyterms, VAD tuning. Test with TTS-replayed audio, then a real call. | Banglish speech shows as text within about 2 s. Speakers labelled. |
| 2 | Mock data and knowledge pack (about 1.5 h, parallel) | Section 5. Mock GP API with five customers, notice, tickets. | Retrieval test set passes. Mock ticket endpoint returns a number. |
| 3 | Side panel (about 2.5 h, Priority 2) | UI: transcript, card, mood bar, compliance, NBA. Bedrock prompt per turn. Tune on Script A. | Every Script A line triggers the right change (section 1 table). |
| 4 | Summary and ticket (about 1 h, Priority 3) | Call-end summary, editable draft, Submit to mock ticket API, recording and transcript to S3 for AQM. | One click creates a ticket number. Recording is in S3. |
| 5 | Ask Copilot (about 1 h, Priority 4) | Question box, retrieval, answer with source link. | CP6 returns roaming steps with a working source link. |
| 6 | Dashboard (about 45 min, Priority 5) | Seeded data: calls using Copilot, suggestions accepted, mood trend. | Page loads with numbers. |
| 7 | Safety net (about 1.5 h, Priority 6) | Rehearse Script A three times. Record backup videos (Script A, CP6, CP7). Reset-data button. | Videos saved, reset works. |

Cut order if time runs short: CP8–CP11, dashboard, then Bangla UI polish. Never cut the backup videos.

### Deploy to AWS

1. EC2 instance (t3.small or larger), Python 3.11+, Caddy for automatic TLS on a domain or free dynamic DNS name.
2. IAM role on the instance with access to Bedrock `InvokeModel`/`Converse`, one S3 bucket, and the DynamoDB tables. No keys in the repo.
3. Secrets (Sarvam key, Twilio auth token) in AWS Secrets Manager or SSM Parameter Store, or environment variables for today.
4. Point the Twilio webhook and the `wss://` URL at the instance. Test a full call from a phone.

---

## 9. Test checklist (maps to the playbook's Ready check)

- [ ] A softphone call reaches Copilot and the transcript appears within a couple of seconds
- [ ] Customer cards for Rahim, Nusrat, Karim, Sumon and Mitu match the demo data
- [ ] Knowledge pack covers roaming, value-added service refunds, SIM replacement and the sample carry-forward rule
- [ ] Mock ticket system returns a ticket number
- [ ] Call recording is saved and AQM can score it straight after
- [ ] Backup video of Script A, CP6 and CP7 is ready
- [ ] Script A rehearsed at least three times with the same two people
- [ ] Panel reload from a second tab works
- [ ] Demo data reset button works

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Sarvam STT accuracy on Bangladeshi Banglish over 8 kHz phone audio | Test in block 1 with a real speaker. Use keyterms. Rehearse with the actors' real voices. Keep backup video. |
| Twilio trial restrictions (verified numbers, sign-up country, announcement, minutes) | Section 4 checks in block 0. Plan B (browser-to-browser call). Conserve minutes with TTS replay. |
| Trial expires in 30 days or minutes run out | Create the account as late as possible before the demo, and watch the Free units tracker. |
| LLM output varies between runs | Fixed prompts and schema, low temperature, rule-based compliance, rehearse the exact script. |
| Public knowledge out of date or wrong on screen | Record fetch dates, spot-check shown numbers, say it is a sample of public content. |
| Panel freezes during the demo | Second browser tab ready, then backup video. |
| Missing Section 8 of the playbook (Q&A answers) | The document ends after Module 4. Ask the demo lead for the Q&A section. |

---

## 11. Open questions

1. Which country was the Twilio account created in, and which phones are verified?
2. Which AWS region and account? Is Bedrock model access already enabled for a Claude Haiku-class and Sonnet-class model?
3. Who plays Rahim and Tania, and can one of them speak Bangladeshi Bangla? (This matters most for STT quality.)
4. What format does the AQM owner expect for the call recording and transcript hand-off?
5. Is a domain or a dynamic DNS name available for the EC2 TLS endpoint?

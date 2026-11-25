# Grameenphone AI Copilot (demo)

```
backend/    Copilot FastAPI service: sign-in, live panel events, Amazon Bedrock analysis, mock GP data
  config/     settings.py and .env (all credentials live here, git-ignored)
  copilot/    api/ (routes), services/, models/, utils/, container.py, main.py
  data/       knowledge articles, mock customers, prompts
  requirements.txt
backend/voice_agent/   Voice Bot service (Vobiz calls, Sarvam STT/TTS, Bedrock replies). Separate app, own settings
frontend/   React (Vite) agent panel in the Grameenphone theme
PLAN-AI-Copilot.md   build plan
```

## Run

Backend (from `backend/`):

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config\.env.example config\.env      # then fill in the values (COPILOT_SESSION_SECRET is required)
python -m scripts.hash_password --username tania --name "Tania" --role agent   # creates config/users.json
uvicorn copilot.main:app --host 0.0.0.0 --port 8000
pytest                                    # Copilot tests: no network, no provider calls
```

Sign-in:

- Agents sign in with a username and password from `config/users.json` (git-ignored, scrypt hashes).
  Roles: `agent`, `supervisor` (supervisor includes agent). Restart the backend after editing users.
- Every `/api` route except `GET /api/health` and `POST /api/auth/login` needs `Authorization: Bearer <token>`.
- The panel WebSocket uses a one-time ticket from `POST /api/auth/ws-ticket` (`/api/panel/stream?ticket=...`).
- The customer picker and "Reset demo data" work only with `DEMO_CONTROLS_ENABLED=true`.
- `/docs` and `/openapi.json` are off unless `COPILOT_API_DOCS_ENABLED=true`.

Frontend (from `frontend/`):

```
npm install
npm run dev          # http://localhost:5173, proxies /api to the backend
npm run build        # output in frontend/dist, served by the backend when present
```

## Data and credentials

- **Credentials live only in `.env` files.** `backend/config/.env`, `backend/voice_agent/config/.env` and `frontend/.env.local` are git-ignored. Only the `.env.example` files are committed. Nothing in the code holds a key, token or table name that is not read from `.env` (the table prefix is `DYNAMODB_TABLE_PREFIX`).
- **The Copilot's data is in DynamoDB**, in six tables named `<prefix>customers`, `network-areas`, `tickets`, `calls`, `packs` and `gp-centres`. What each table holds is in `backend/data/seed-data-plan.md`.
- **Seed the tables** (once, and again after editing the JSON in `backend/data/mock`):

```
cd backend
python -m scripts.seed_dynamodb --dry-run     # show what would be written
python -m scripts.seed_dynamodb               # write it, then read every item back
```

- **If DynamoDB cannot be reached** (for example the AWS credentials expired), the Copilot keeps working from the seed data, read-only, and keeps what it writes in memory only (nothing is written to the local disk). It tries DynamoDB again after `DATA_RETRY_AFTER_S` seconds. Anything written meanwhile is lost on restart and is not copied to the tables later. `GET /api/health` shows `"fallback_active": true` and the reason. Set `DATA_FALLBACK_TO_FILES=false` to fail instead, or `DATA_BACKEND=files` to run without AWS.
- **Adding a team phone:** put `+<number>:<customer_id>` in `DEMO_PHONE_MAP` (for example `+8801711111111:rahim`) and run the seed again, so the table has an item for that number. The demo caller `9500799295` does not need this: it has its own record, Abdul Rahim (`abdul-rahim`), that carries the data for every scenario.
- The AWS keys are temporary. When they expire, replace the three `AWS_*` values in `backend/config/.env`.

## Export official pages as PDFs (for Bedrock Data Automation)

```
cd backend
pip install -r requirements.txt
playwright install chromium            # one time
python -m scripts.export_pdfs --dry-run          # list pages and check robots.txt, no browser
python -m scripts.export_pdfs                    # render every page (waits 10 s between pages)
python -m scripts.export_pdfs --lang bangla      # only the Bangla pages
python -m scripts.export_pdfs --only roaming-activation sim-replacement
python -m scripts.export_pdfs --upload           # also copy to S3 (needs S3_BUCKET in config/.env)
```

- The page list is `backend/data/knowledge/sources.json`. Add or remove pages there.
- Output: `backend/data/knowledge/pdf/english/<id>.pdf`, `.../bangla/<id>.pdf`, a `<id>.txt` with the exact page text, and `index.json` with the source URL, fetch date, status and checksum for every page.
- Every PDF starts with a line giving the title, source URL and retrieval date.
- The script honours `robots.txt`, keeps at least a 10-second gap between pages, and reports pages the site refuses (403 and similar) without retrying them.
- On Linux servers, install a Bengali font (for example Noto Sans Bengali) before rendering, or Bangla text will not display.

## Voice agent service (`backend/voice_agent/`)

A separate FastAPI service for the Voice Bot module. It has its own `app/` package, tests, requirements and
settings, and runs on its own port. It does not import anything from the Copilot (`backend/copilot/`).

```
cd backend/voice_agent
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config\.env.example config\.env          # then fill in the values
uvicorn app.main:app --reload --port 8100     # 8100 so it does not clash with the Copilot on 8000
pytest                                        # no network, no provider calls
```

- Credentials: `backend/voice_agent/config/.env` (git-ignored). It is separate from `backend/config/.env` on purpose,
  because variables such as `SARVAM_STT_MODE` and `SARVAM_STT_STREAM_TYPE` mean different things in the two services.
- Full details are in `backend/voice_agent/README.md`.

## Telephony

Phone calls are handled by the voice agent (Vobiz), which forwards each call and its live transcript to the Copilot
(see [M3-LIVE-VOICE.md](M3-LIVE-VOICE.md)). The panel only shows calls that arrive this way. It has no box for typing
transcript lines or starting a call.

## API (Copilot)

All routes need a signed-in agent except where marked public.

| Group | Method | Path | Purpose |
|---|---|---|---|
| System | GET | `/api/health` | Health check (public), and whether the data comes from DynamoDB or the fallback files |
| System | POST | `/api/data/reset` | Clear the current call, the tickets the app created and all saved calls. Seeded data stays (demo controls only) |
| Auth | POST | `/api/auth/login` | Sign in, returns a bearer token (public) |
| Auth | GET | `/api/auth/me` | Current user and whether demo controls are on |
| Auth | POST | `/api/auth/ws-ticket` | One-time ticket for the panel WebSocket |
| Call | WS | `/api/panel/stream?ticket=` | Live events to the panel |
| Call | GET | `/api/calls/current` | Current call state |
| Call | POST | `/api/calls/current/end` | End the call on screen |
| Transcript | GET | `/api/transcript` | Turns so far |
| Transcript | POST | `/api/transcript` | Add one line through the API (same steps as live speech). The panel has no box for this |
| Analysis | POST | `/api/analyze` | Run mood and next-best-action analysis now |
| Answers | GET | `/api/answers` | Automatic lookups made so far for the customer's sentences |
| Answers | POST | `/api/answers` | Look up the customer's latest sentence now |
| Analysis | GET | `/api/mood` | Current mood and history |
| Compliance | GET | `/api/compliance` | Current reminders |
| Compliance | GET | `/api/compliance/rules` | Rule list |
| Suggestions | GET | `/api/suggestions` | Suggestions shown so far |
| Suggestions | POST | `/api/suggestions/{id}/use` | Mark one as used |
| Summary | GET | `/api/summary` | Summary of the finished call |
| Summary | POST | `/api/summary` | Write or rewrite the summary |
| Tickets | GET | `/api/tickets/categories` | Categories |
| Tickets | GET | `/api/tickets` | Tickets in the mock system |
| Tickets | POST | `/api/tickets` | Create a ticket |
| Knowledge | POST | `/api/ask` | Ask Copilot. Body: `question` and optional `history` (up to 12 earlier chat messages as role and content). Answers in Markdown, with sources, and `searched_for` when the chat changed the question |
| Knowledge | GET | `/api/knowledge/articles` | Articles in the knowledge pack |
| Identity | GET | `/api/identity` | Whether a customer is identified, the caller ID, and the card |
| Identity | POST | `/api/identity` | Typed fallback. On a call the number is picked up from the transcript and loads the card by itself |
| Identity | DELETE | `/api/identity` | Wrong customer: hide the card again |
| Location | GET | `/api/locations` | Areas the agent can pick from |
| Location | GET | `/api/location` | Registered and current location status |
| Location | PUT | `/api/location` | The customer's current location. Checks the network status there |
| Dashboard | GET | `/api/dashboard` | Adoption numbers counted from the saved calls: no sample data |

## Live voice calls (M3)

The voice agent forwards live transcripts and call events to the Copilot, which stores them as JSON in the private call-transcripts bucket (`S3_TRANSCRIPT_BUCKET`, under `live/<call id>/`) and shows them live. The AI can hand a call to a human agent's phone and keeps assisting in the background. Setup, API, storage limits, hold/resume and speaker attribution: see [M3-LIVE-VOICE.md](M3-LIVE-VOICE.md).

## How the panel fills in by itself

The agent types nothing to start the Copilot. Everything is read from the live transcript:

1. **Caller ID, then the mobile number.** When a call reaches the human agent, the caller ID is looked up at once and the customer card appears, marked **Not confirmed yet**. The customer's network status shows with it. The number is confirmed in one of two ways: the customer says it on the call (or says yes after the agent reads it back), which is picked up automatically, or the agent confirms or types it in the box on the card. A different number loads that customer instead. Until the number is confirmed, the answer tells the agent to confirm it first and does not quote balance, bill or ticket details. Bangladesh numbers have 11 digits (`01XXXXXXXXX`) and Indian numbers have 10 (`9500799295`, also read as `+91...`). If no customer has the number, the panel says so and keeps listening. Speech from an unverified speaker is never used. `Not this customer` hides the card and ignores that number until another is heard. The panel has no box for starting a test call or typing transcript lines: calls reach it from the voice agent. The routes `POST /api/calls` (start a call with a caller ID) and `POST /api/transcript` (add a line) stay in the API for tests and scripts.
2. **Location.** The card shows the network status of the registered area straight away, read from the 
etwork-areas table. When the customer reports a network problem, the panel asks the agent to confirm the area. The customer's reply sets the location: a place name, or a yes after the agent named the area. The status of that area is read from the table and the answer is looked up again.
3. **One panel.** The customer card, location, answer, next best action, mood and compliance are sections of one AI Copilot card. Ask Copilot is the bot button in the bottom right corner (see below).

The code is in \copilot/services/capture.py\ and \spoken_number.py\; the panel is \rontend/src/components/copilot/\.

## Where calls and tickets are stored

Nothing is written to the local disk. Leave COPILOT_RUNTIME_DIR and COPILOT_CONVERSATIONS_DIR empty (only the tests set them).

| What | Where |
|---|---|
| The finished call: transcript, suggestions, answers, mood, compliance, summary, ticket, and the customer's **original mobile number** | `s3://<S3_TRANSCRIPT_BUCKET>/calls/<mobile number>/<call id>.json`. A call with no known number goes under `unidentified`. |
| A voice call while it runs (transcript events, call events, customer and agent notes) | `s3://<S3_TRANSCRIPT_BUCKET>/live/<call id>/`, with the original caller number in `customer_context.json` |
| The same call record, for the dashboard | DynamoDB table `gp-copilot-calls` |
| Tickets | DynamoDB table `gp-copilot-tickets`, with the customer's mobile number and name |

The bucket holds personal data. Create it once with `python -m scripts.create_transcript_bucket`: it blocks all public access and turns encryption on.

## Tickets tab

The **Tickets** tab lists every ticket (`GET /api/tickets`). Filters: mobile number (full or part, any format such as `+91...`), ticket number (full or part), a **From** and **To** date (calendar pickers, inclusive, Bangladesh dates) and the status (open, in progress, resolved, closed or all). Without a status filter only **open** tickets are listed. Click a ticket to see its problem, what was checked, the outcome and the follow-up.

## Ask Copilot chat

The bot button in the bottom right corner opens the Ask Copilot chat on every tab. It answers from the approved knowledge, with the source, and writes the answer in Markdown (lists, bold, code, tables).

- **Memory.** The chat remembers what was said earlier, so a follow-up such as "ar eta koto din cholbe?" is understood. Each question is sent with the last 6 messages, and the question is first rewritten into a standalone English question using them. That rewritten question is what is searched, and it is shown under the answer as "Searched for". A first question has no rewrite step, so it is a little faster.
- **Short-lived.** The chat lives in the browser page only. It is not stored in the browser storage or on the server, so refreshing the page clears it. Closing the window or changing tab keeps it. "New chat" clears it at once.
- **Where.** `frontend/src/components/chat/` (the window and the button), `frontend/src/state/chatMemory.js` (what is remembered and sent), `backend/copilot/services/ask.py` (the rewrite and the answer), and the prompts `backend/data/prompts/ask_condense.md` and `ask_copilot.md`.

## Where the knowledge comes from

With `BEDROCK_KB_ID` set, the Bedrock Knowledge Base is the **only** source of knowledge, for Ask Copilot and for the live answer panel. Local article files are not mixed in and do not answer when the Knowledge Base fails.

- **Only uploaded documents are searched.** The search is filtered to documents tagged `language` = `en` or `bn` (the metadata that `scripts/upload_kb.py` writes). Anything else that sits in the same bucket, such as saved call records, is never returned as knowledge.
- **If the Knowledge Base cannot be reached** (expired AWS keys, a timeout, a wrong ID), Ask Copilot says "The knowledge base cannot be reached right now" (`"unavailable": true`) and gives no answer. The live answer panel carries on with the customer card and the network status only, and says the knowledge is not available. The Knowledge Base is retried after `DATA_RETRY_AFTER_S` seconds.
- **`GET /api/health`** shows `knowledge`: `backend` (`bedrock_kb` or `local_articles`), `available`, and `last_error`.
- **Without `BEDROCK_KB_ID`** (development with no AWS) the local article files in `backend/data/knowledge/articles` are used, and `backend` says `local_articles`.
- **Sources.** An answer lists only the documents it used. An answer that says "not in the knowledge" lists none.

## Adoption dashboard

Every number on the dashboard is counted from the saved calls (the `gp-copilot-calls` table) when the page is opened or refreshed. Nothing is seeded, estimated or labelled as a sample.

| Figure | How it is counted |
|---|---|
| Calls that used Copilot | Saved calls with at least one transcript line |
| Suggestions shown, accepted | The suggestions on those calls, and how many the agent used (said, or marked used) |
| Tickets raised from calls | Calls that have a ticket |
| Customer mood, last 7 days | Per Bangladesh day, each call counted once by the worst customer mood seen: angry, then frustrated, else calm. The bar is the share of that day's calls with a mood reading |
| Most shown suggestions | Suggestions by title, with how many times each was shown and used |

There is no "handling time saved" figure: the app does not measure it, so it is not shown. If the saved calls cannot be read, the page says so and shows only the calls made since the app started. The calls from testing count too: **Reset demo data** (a demo control) clears them.

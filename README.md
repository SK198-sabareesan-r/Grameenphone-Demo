# Grameenphone AI Copilot (demo)

```
backend/    FastAPI service: Twilio media stream, Sarvam speech-to-text, Amazon Bedrock, mock GP data
  config/     settings.py and .env (all credentials live here, git-ignored)
  copilot/    api/ (routes), services/, models/, utils/, container.py, main.py
  data/       knowledge articles, mock customers, prompts
  requirements.txt
frontend/   React (Vite) agent panel in the Grameenphone theme
PLAN-AI-Copilot.md   build plan
```

## Run

Backend (from `backend/`):

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config\.env.example config\.env      # then fill in the values
uvicorn copilot.main:app --host 0.0.0.0 --port 8000
```

Frontend (from `frontend/`):

```
npm install
npm run dev          # http://localhost:5173, proxies /api to the backend
npm run build        # output in frontend/dist, served by the backend when present
```

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

## Twilio setup

- Voice webhook (TwiML App and phone number): `POST {PUBLIC_BASE_URL}/api/twilio/voice`
- The backend returns TwiML that streams both call legs to `wss://{host}/api/twilio/stream` and rings the browser softphone.

## API

| Group | Method | Path | Purpose |
|---|---|---|---|
| System | GET | `/api/health` | Health check |
| System | GET | `/api/softphone/token` | Browser softphone access token |
| System | POST | `/api/data/reset` | Clear the current call, tickets and saved calls |
| Twilio | POST | `/api/twilio/voice` | Voice webhook |
| Twilio | POST | `/api/twilio/recording` | Recording callback |
| Twilio | WS | `/api/twilio/stream` | Media stream |
| Call | WS | `/api/panel/stream` | Live events to the panel |
| Call | GET | `/api/calls/current` | Current call state |
| Call | POST | `/api/calls/current/end` | End the call on screen |
| Transcript | GET | `/api/transcript` | Turns so far |
| Transcript | POST | `/api/transcript` | Add one typed line (same steps as live speech) |
| Analysis | POST | `/api/analyze` | Run mood and next-best-action analysis now |
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
| Knowledge | POST | `/api/ask` | Ask Copilot, answers with sources |
| Knowledge | GET | `/api/knowledge/articles` | Articles in the knowledge pack |
| Customers | GET | `/api/customers` | Mock customers |
| Customers | GET | `/api/customers/{id}` | One customer record |
| Customers | GET / PUT | `/api/customers/active` | Customer used for the next call |
| Dashboard | GET | `/api/dashboard` | Adoption numbers |

# Grameenphone AI Voice Agent

FastAPI backend for an AI voice agent on phone calls. Vobiz carries the call, Sarvam
transcribes and speaks, and Amazon Bedrock generates the replies.

```
Caller <-> Vobiz  --(WebSocket, L16 audio)-->  this app
                                                 |-- Sarvam realtime STT  (speech to text)
                                                 |-- Amazon Bedrock       (reply text, Converse API)
                                                 `-- Sarvam TTS           (text to speech)
                                                        |
Caller <-- Vobiz <--(playAudio / checkpoint / clearAudio)
```

Final transcripts are also published through an internal event pipeline (logging, plus an
optional Copilot webhook). No database is used; call state is in memory in a single process.

## Features

- Outbound calls via the Vobiz API (`POST /api/v1/vobiz/calls`).
- Vobiz answer and hangup webhooks, with HMAC signature validation. The answer webhook returns
  VobizXML that starts a bidirectional `<Stream>`.
- WebSocket audio streams (`/ws/vobiz/stream/{call_uuid}`, plus `/ws/vobiz/monitor/{call_uuid}`
  for per-track monitoring), with a signed per-call token.
- Playback to the caller in 20 ms chunks, with checkpoints and `clearAudio` for barge-in.
- Sarvam realtime STT, one session per call. Off by default.
- AI conversation: greeting, then STT -> Bedrock -> Sarvam TTS -> playback. Off by default.
- Diagnostics endpoints for Vobiz and Sarvam configuration. They never return secrets.

## Project layout

```
app/
  api/routes/        HTTP and WebSocket routes (health, vobiz, vobiz_ws, vobiz_config, sarvam_config, conversation)
  core/              Settings (config.py) and logging
  integrations/vobiz/  Vobiz client, auth, webhooks, VobizXML, stream events/sender, call control
  publishers/        Transcript event pipeline and publishers
  schemas/           Pydantic models (sarvam, vobiz)
  services/
    audio/           PCM/WAV helpers, byte order, format conversion
    conversation/    Conversation manager, prompts, per-call state
    llm/             LLM interface and Amazon Bedrock client
    sarvam/          Realtime STT, TTS, diagnostics, speech check
    voice/           Call state, media stream handler, audio consumers, playback, track routing
  utils/             Reserved, currently empty
config/              .env (git-ignored) and .env.example
scripts/             Development tools (see below)
tests/               Automated tests (no network, no provider calls)
tests_live/          Manual live-provider checks (opt-in, billed)
```

## Setup (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config\.env.example config\.env   # then fill in values
```

## Run

```powershell
uvicorn app.main:app --reload --port 8000
```

Check: `curl http://localhost:8000/health` returns `{"status":"ok"}`.

## Test

```powershell
pytest
```

The suite in `tests/` uses fakes and makes no calls to Vobiz, Sarvam or AWS.

## Connecting a real call

1. Run the app and expose it over HTTPS (for example `ngrok http 8000`).
2. In `config/.env`, set `PUBLIC_BASE_URL` to that HTTPS URL (no trailing slash), plus `VOBIZ_AUTH_ID`,
   `VOBIZ_AUTH_TOKEN`, `VOBIZ_CALLER_ID` and `INTERNAL_API_TOKEN`. Restart the app.
3. Call `GET /api/v1/vobiz/diagnostics` to see the answer and hangup URLs the app will use.
4. Place a call with `POST /api/v1/vobiz/calls` and the header `X-Internal-Token`. Vobiz then
   calls the answer webhook, which points the stream at `wss://<host>/ws/vobiz/stream/<call_uuid>`.
5. To enable speech recognition set `VOBIZ_SARVAM_STT_ENABLED=true` and `SARVAM_API_KEY`. For the
   AI conversation also set `AI_CONVERSATION_ENABLED=true` and `BEDROCK_INFERENCE_PROFILE_ARN`.
   AWS credentials come from the standard AWS credential chain, never from `config/.env`.

Real calls use paid Vobiz, Sarvam and AWS services.

## Main routes

| Route | Purpose |
|---|---|
| `GET /health` | Liveness |
| `POST /api/v1/vobiz/calls` | Place an outbound call (internal token) |
| `POST /api/v1/vobiz/answer`, `/hangup` | Vobiz webhooks (paths configurable) |
| `WS /ws/vobiz/stream/{call_uuid}`, `/ws/vobiz/monitor/{call_uuid}` | Audio streams |
| `GET /api/v1/vobiz/streams` | Active streams (internal token) |
| `POST /api/v1/vobiz/streams/{call_uuid}/test-tone`, `/clear` | Playback and clearAudio checks (internal token) |
| `GET /api/v1/vobiz/diagnostics`, `/api/v1/sarvam/diagnostics` | Effective config; `verify=true` tests credentials |
| `GET /api/v1/ai/conversations` | AI conversation status (internal token) |

## Scripts and live tests

- `scripts/simulate_vobiz_stream.py`: simulated Vobiz client for the stream endpoint, no real call.
- `scripts/analyze_vobiz_capture.py`: analyzes a development audio capture (byte order check).
- `scripts/validate_sarvam_realtime_stt.py`: live Sarvam STT check. Billed.
- `tests_live/`: live Sarvam tests, skipped unless `RUN_LIVE_SARVAM=1`. Billed.

## Configuration

See `config/.env.example`. Secrets are read from the environment or `config/.env` only and are held as
`SecretStr`, so they are not logged.

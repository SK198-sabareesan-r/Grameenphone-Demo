# Integration plan: one backend, one frontend, Vobiz instead of Twilio

## 1. What exists today

| Piece | What it does | Telephony |
|---|---|---|
| `backend/copilot` (port 8000) | Agent-assist panel: live transcript, customer card, mood, compliance, next-best-action, Ask Copilot, summary, tickets, dashboard. Two Sarvam STT sockets per call (agent and customer tracks). | Twilio: TwiML webhook, media stream `/api/twilio/stream`, browser softphone token |
| `grameenphone-ai-voice-agent` (root, has real `.env`) | AI bot that answers the call: Vobiz call and stream -> Sarvam STT -> Bedrock -> Sarvam TTS -> playback. Transcript event pipeline (`app/publishers`). Optional `COPILOT_TRANSCRIPT_WEBHOOK_URL`. | Vobiz |
| `backend/voice_agent` | Older, near-identical copy of the voice agent. 9 files differ in content: it uses `/api/vobiz/...` paths and `config/.env`; the root copy uses `/api/v1/vobiz/...`. | Vobiz |
| `frontend` (Vite + React) | Panel UI. `useSoftphone.js` and `Softphone.jsx` use `@twilio/voice-sdk`. | Twilio |

Problems to solve: two servers, three `.env` files, duplicate voice-agent code, Twilio spread across 8 files, and several env names that mean different things in each app.

## 2. Target layout

```
backend/
  config/            settings.py + .env + .env.example   (the only env file)
  copilot/
    main.py          single FastAPI app (uvicorn copilot.main:app, port 8000)
    api/             existing routes; twilio_routes.py removed
    services/        existing; twilio_helpers.py removed; pipeline.py made telephony-neutral
    voice/           moved from grameenphone-ai-voice-agent/app (Vobiz, Sarvam STT/TTS, Bedrock bot)
      api/ integrations/vobiz/ services/ publishers/ schemas/ core/
  tests/             copilot tests + voice tests (moved from voice agent)
frontend/            same app, softphone removed, "Live call" source switched to Vobiz
```

Deleted at the end: `backend/voice_agent/`, `grameenphone-ai-voice-agent/` (after its `.env` values are merged), Twilio code, `twilio` and `@twilio/voice-sdk` dependencies.

Source of truth for voice code: the root `grameenphone-ai-voice-agent` copy (it matches its README and `.env`). Take `config/.env` loading from the `backend/voice_agent` copy, but pointed at the shared settings (section 4).

## 3. Call flow after the merge

```
Customer -> Vobiz number -> POST /api/v1/vobiz/answer (VobizXML <Stream>)
          -> WS /ws/vobiz/stream/{call_uuid}
               |- Sarvam realtime STT (customer track)
               |- Bedrock bot reply -> Sarvam TTS -> playAudio back to caller
               `- transcript events (customer final, bot reply)
                     |  in-process call, no HTTP webhook
                     v
             copilot CallPipeline.ingest(speaker, kind, text)
                     v
             hub.broadcast -> WS /api/panel/stream -> React panel
```

Changes that make this work:

1. **Bridge in `copilot/voice/publishers`**: a new `CopilotSubscriber` registered with `pipeline.add_global_subscriber(...)`. On `session.begin` it calls `CallPipeline.begin(call_sid=call_uuid, caller, live_audio=False)`. On `transcript.partial/final` it calls `ingest("customer", ...)`. On `session.end` it calls `finish()`. This replaces `COPILOT_TRANSCRIPT_WEBHOOK_URL`.
2. **Bot speech as the "agent" turn**: `ConversationManager` calls `ingest("agent", "final", reply_text)` after each reply, so the panel shows both sides, and compliance and next-best-action keep working.
3. **`CallPipeline`**: remove the Twilio `handle()` and `_on_media()` code, the two `SarvamStream` sockets, and `TRACK_SPEAKER`. STT now lives in the voice module. Keep `begin`, `ingest`, `finish`.
4. **Caller number to customer card**: Vobiz `start`/answer payload carries the caller; pass it to `begin(caller=...)` so `CustomerDirectory.resolve` and `DEMO_PHONE_MAP` still work.
5. **Recording**: Twilio `/api/twilio/recording` is dropped. `recording_url` stays empty for now (optional later: Vobiz recording callback).
6. **Browser softphone**: with Vobiz the AI bot answers, so there is no agent softphone. Remove `useSoftphone.js`, `/api/softphone/token`, and the Twilio parts of `Softphone.jsx`. Keep a small call-status bar with "End call and summarise".
7. **Starting a call**: add a panel button "Place demo call" that hits `POST /api/v1/vobiz/calls` through a copilot proxy route (so the browser never holds `INTERNAL_API_TOKEN`).
8. **Single app wiring**: `copilot/main.py` calls a `voice.install(app, settings)` function (the body of today's `create_app`: routers, call manager, STT sink factory, conversation) inside `lifespan`, and shares one `PanelHub`/container. Shutdown order is kept from the voice agent.
9. **Frontend**: `endpoints.js` drops `softphoneToken`; adds `placeCall` and `voiceStatus`. `vite.config.js` proxies `/api` and `/ws` to port 8000. Typed lines and demo controls keep working (they use `ensure_pipeline`).

## 4. Merged `.env` (`backend/config/.env`)

Shared keys, defined once:

| Key | Used by |
|---|---|
| `APP_HOST`, `APP_PORT`, `LOG_LEVEL`, `PUBLIC_BASE_URL`, `CORS_ORIGINS` | both |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | both (the voice agent says to use the AWS credential chain; blank keys still work) |
| `SARVAM_API_KEY`, `SARVAM_BASE_URL` | both |
| `BEDROCK_INFERENCE_PROFILE_ARN_FAST` | copilot analysis and Ask |
| `BEDROCK_INFERENCE_PROFILE_ARN_SUMMARY` | copilot summary |
| `BEDROCK_INFERENCE_PROFILE_ARN` (voice bot reply) | voice bot. Default to `_FAST` when empty. |

Name collisions, where the two apps use the same name with different values:

| Name | Copilot value | Voice agent value | Resolution |
|---|---|---|---|
| `SARVAM_STT_MODE` | `codemix` | `transcribe` | Keep one key, used only by the voice module now that the copilot's own STT is gone. Value: `transcribe` (from the voice agent's `.env`). |
| `SARVAM_STT_STREAM_TYPE` | `fast` | `balanced` | Same: keep the voice agent's. |
| `SARVAM_STT_MODEL` | empty | `saaras:v4` | Keep `saaras:v4`. |
| `SARVAM_STT_LANGUAGE` vs `SARVAM_STT_LANGUAGE_CODE` | `bn-IN` | `auto` | Keep `SARVAM_STT_LANGUAGE_CODE`. |
| `SARVAM_TTS_MODEL`, `SARVAM_TTS_LANGUAGE` | empty, `bn-IN` | `bulbul:v4-flash`, `bn-IN` | Keep the voice agent's. |

Because the copilot's own Sarvam STT socket code is removed, these collisions disappear instead of needing prefixes. Copilot-only STT keys (`SARVAM_STT_URL`, `SARVAM_STT_SILENCE_MS`, `SARVAM_STT_KEYTERMS`, `SARVAM_STT_MAX_RETRIES`, `SARVAM_TTS_SPEAKER_AGENT/CUSTOMER`) stay only if still used by `scripts/` (check before deleting).

Removed (Twilio): `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET`, `TWILIO_TWIML_APP_SID`, `TWILIO_AGENT_IDENTITY`, `TWILIO_RECORD_CALLS`, `TWILIO_VALIDATE_SIGNATURE`.

Added (Vobiz, from the voice agent): `VOBIZ_AUTH_ID`, `VOBIZ_AUTH_TOKEN`, `VOBIZ_BASE_URL`, `VOBIZ_CALLER_ID`, `VOBIZ_VALIDATE_SIGNATURE`, `VOBIZ_ANSWER_WEBHOOK_PATH`, `VOBIZ_HANGUP_WEBHOOK_PATH`, `VOBIZ_STREAM_WS_PATH`, `VOBIZ_INBOUND_CONTENT_TYPE`, `VOBIZ_WS_*`, `VOBIZ_PLAYBACK_CHUNK_MS`, `VOBIZ_SARVAM_STT_ENABLED`, `AI_CONVERSATION_ENABLED`, `AI_*`, `BEDROCK_*` tuning, `TRANSCRIPT_*`, `INTERNAL_API_TOKEN`, `APP_ENV`, `VOBIZ_DEV_CAPTURE_*`.

Removed: `COPILOT_TRANSCRIPT_WEBHOOK_URL` (replaced by the in-process bridge).

`config/settings.py` gains the voice fields (the voice agent's dataclass `Settings` is folded into the pydantic `Settings` so there is one loader and one `.env`). Secrets stay `SecretStr`.

## 5. Steps

1. **Backup and baseline.** Commit current state. Run `pytest` in both projects to record what passes now.
2. **Move voice code** into `backend/copilot/voice/`, rewrite imports `app.` -> `copilot.voice.`, move tests to `backend/tests/voice/`. Fix `pytest.ini`.
3. **Fold settings.** Merge voice settings into `config/settings.py`; update `.env.example`; build the real `config/.env` from the two existing `.env` files (I will not print secrets).
4. **Wire one app.** `voice.install(app, settings)` in `copilot/main.py`; keep voice routes at `/api/v1/vobiz/...` and `/ws/vobiz/...` so Vobiz console URLs do not change. Keep `/health` and `/api/health`.
5. **Add the bridge** (section 3, points 1 to 4) and the agent-turn hook in `ConversationManager`.
6. **Remove Twilio**: `twilio_routes.py`, `twilio_helpers.py`, `verify_twilio_signature`, softphone route, Twilio settings and container checks, `twilio` in `requirements.txt`.
7. **Merge requirements**: one `backend/requirements.txt` (add `pytest-asyncio`, `python-dotenv` only if still needed).
8. **Frontend**: remove `@twilio/voice-sdk`, `useSoftphone`, rework `Softphone.jsx` into a call bar with "Place demo call" and "End call and summarise"; update `endpoints.js`, `vite.config.js`.
9. **Docs**: rewrite `README.md` (Run section, API table, replace "Twilio setup" with "Vobiz setup": answer URL `{PUBLIC_BASE_URL}/api/v1/vobiz/answer`, hangup URL, stream URL).
10. **Verify**: backend tests, `npm run build`, start one server, `/health`, simulate a call with `scripts/simulate_vobiz_stream.py` and confirm the panel gets transcript, card, mood, compliance and summary. A real Vobiz call needs your credentials, a public URL and costs money, so that last check is yours.
11. **Cleanup**: delete `backend/voice_agent/` and `grameenphone-ai-voice-agent/` only after you confirm.

## 6. Decisions needed from you

1. **Who talks to the customer?** This plan assumes the AI bot answers the call and the panel shows the bot-customer conversation live (so the Copilot assists a supervisor or shows the bot's reasoning). If you still want a human agent speaking on the call, Vobiz needs a different path (dial the agent's phone, with both audio tracks streamed), and the demo script (Script A, agent "Tania") would work that way. Which do you want?
2. **Which voice copy wins?** I propose the root `grameenphone-ai-voice-agent` (paths `/api/v1/vobiz/...`). Confirm that those are the URLs set in your Vobiz console.
3. **Call recording**: drop it, or add a Vobiz recording callback later?
4. **Delete the old folders** at the end, or keep them for reference?

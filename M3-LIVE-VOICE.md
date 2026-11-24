# M3: live voice calls in the Copilot

The voice agent (`backend/voice_agent`, port of your choice, for example 8001) forwards live call events to the
Copilot (`backend`, port 8000). The Copilot stores them as JSON files and shows them live to the human agent.
No database and no knowledge base are involved.

```
Customer <-> Vobiz <-> voice agent                              Copilot                      Human agent
                         |  Sarvam STT (per track)                |                              |
                         |  Bedrock reply -> Sarvam TTS           |                              |
                         |-- CopilotEventForwarder --HTTP-------> /api/v1/internal/*             |
                                  (own bounded buffer, retries)     |-> data/conversations/{call_id}/*.json
                                                                    |-> PanelHub -> React panel --->| (browser: reads only)
```

The human agent talks to the customer **on their own phone**. The Copilot screen only reads. The AI never speaks
to the customer after the hand-off.

## Configuration

| Where | Variable | Meaning |
|---|---|---|
| both | `INTERNAL_API_TOKEN` | Same value in `backend/config/.env` and `backend/voice_agent/config/.env`. Sent as `X-Internal-Token`. Empty on the Copilot = internal routes answer 503. |
| voice agent | `COPILOT_TRANSCRIPT_WEBHOOK_URL` | `http://localhost:8000/api/v1/internal/transcripts/events`. Empty = forwarding off. The lifecycle route `.../calls/events` is derived from it. |
| voice agent | `COPILOT_FORWARD_*` | Timeout, attempts, back-off, buffer size (defaults are in `.env.example`). |
| voice agent | `HUMAN_HANDOFF_ENABLED`, `HUMAN_AGENT_PHONE` | Turn the hand-off on and set the human agent's phone (E.164; several numbers separated by commas ring together, max 5). Without a valid number the customer is told no transfer is possible. |
| voice agent | `HUMAN_HANDOFF_PHRASES`, `HUMAN_HANDOFF_ANNOUNCEMENT` | Extra phrases that also start a hand-off (the built-in rules need none), and the sentence spoken before the transfer. |
| voice agent | `HOLD_AUDIO_URL`, `HOLD_DTMF`, `RESUME_DTMF` | Hold audio and the digits the human agent presses on their phone. |
| voice agent | `VOBIZ_OUTBOUND_TRACK_SPEAKER` | `unknown` (default) or `human_agent`. See "Speaker attribution". |
| Copilot | `COPILOT_CONVERSATIONS_DIR` | Where the JSON is written. Default `backend/data/conversations/` (git-ignored). |

## Transcript event flow

1. Vobiz streams the caller's audio to the voice agent. Sarvam realtime STT produces partial and final transcripts.
2. They enter the existing `TranscriptEventPipeline`. `CopilotEventForwarder` is one more subscriber of that pipeline,
   so it runs in its own task and cannot slow the Sarvam receive loop.
3. The forwarder gives every event a per-call `sequence_number` and a fixed `event_id`, decides the speaker, and
   queues it in a bounded buffer. One worker per call sends the buffer in order.
4. The AI agent's replies (greeting and answers) are reported by `ConversationManager` through a callback, as
   `ai_agent` final lines. Call lifecycle events come from the answer webhook, the stream listener, the hand-off
   service and the hang-up path.
5. The Copilot validates, stores (`ConversationStore`), then updates the panel (`CallPipeline`). A repeated
   `event_id` is acknowledged as a duplicate and not stored or shown again.

Reliability: 5xx, 408, 429 and network errors are retried (6 attempts, back-off 0.5 s doubling to 8 s; partials get one
retry) with the same `event_id`; 401/403/409/422 are not retried. A full buffer drops the oldest partial first; finals
and lifecycle events are only dropped at twice the buffer size, and that is logged at ERROR. Events that cannot be
delivered within that budget are lost: this is bounded buffering, not a durable queue.

## API contract

`POST /api/v1/internal/transcripts/events`, header `X-Internal-Token: <INTERNAL_API_TOKEN>`

```json
{
  "event_id": "5401fd2e-...-t2", "call_id": "5401fd2e-...", "session_id": "sess-5401fd2e-...",
  "customer_id": null, "speaker": "customer", "speaker_basis": "primary_stream_inbound_track",
  "transcript_text": "I need help with my mobile recharge.", "transcript_type": "final",
  "timestamp": "2026-11-24T10:30:00Z", "sequence_number": 2,
  "call_status": "in_progress", "agent_mode": "ai_agent", "language": null
}
```

`speaker`: `customer | ai_agent | human_agent | unknown`. `call_status`: `in_progress | handoff_requested |
dialing_human | with_human | on_hold | human_disconnected | ended`. `agent_mode`: `ai_agent | human_agent`.
`customer_id` is optional: the Copilot fills it in from `call_started`.

`POST /api/v1/internal/calls/events` takes `{event_id, call_id, session_id, type, timestamp, source, data}` with `type` one of
`call_started, ai_agent_connected, human_handoff_requested, human_agent_dialing, human_agent_connected,
human_handoff_failed, customer_put_on_hold, customer_resumed, ai_agent_assist_activated, human_agent_disconnected,
call_ended`. `call_started` carries `data.from_number`, used once to find the customer and never stored (only the last 4 digits).

Status codes: `201` stored, `200` duplicate (safe to retry), `401` wrong token, `409` different `session_id` for that call or a
`sequence_number` already used by another event, `422` invalid body, `503` token not configured or the file could not be
written (the response has `Retry-After`).

Agent-facing (normal sign-in): `GET /api/calls/{call_id}/context` (hand-off package) and `POST /api/calls/{call_id}/notes`.

Voice-agent control routes (header `X-Internal-Token`): `POST /api/vobiz/calls/{id}/handoff` (`{"reason", "pending_actions"}`),
`GET /api/vobiz/calls/{id}/handoff`, `POST /api/vobiz/calls/{id}/hold`, `POST /api/vobiz/calls/{id}/resume`.
Vobiz-facing (signed webhooks): `POST /api/vobiz/handoff/{id}/dial`, `/dial-events`, `/dial-result`.

## Local JSON storage

```
backend/data/conversations/{call_id}/
  transcript.json          every partial and final event, ordered by sequence_number
  customer_context.json    customer id and details, initial intent, references to the customer's turns
  ai_agent_context.json    AI messages, actions, hand-off reason / time / pending actions
  human_agent_context.json masked agent reference, transcript references, notes, hold/resume, hand-off times
  call_events.json         lifecycle events in chronological order
```

All five carry the same `call_id`, `session_id` and `customer_id`. A recorded sample is in
`backend/tests/fixtures/conversations/SAMPLE_CALL_001/`. Rules: atomic writes (temp file, fsync, rename), one lock per call, no duplicate
`event_id`, ordered insert, a corrupt file is renamed `*.corrupt-<time>` and the call continues, call ids are validated before
they become a path, and no tokens or full phone numbers are written. The folder is git-ignored.

### Limitations and database migration

* One backend process only: the locks are in-process. Two workers would race on the same files.
* Every event rewrites its whole file. Fine for a call of a few thousand events; use append-only storage for more.
* Partials are kept (as requested), so `transcript.json` grows with speech time.
* No retention, encryption at rest or access control beyond the file system. Transcripts are personal data.
* The panel shows one call at a time (the existing `SessionRegistry`); a second simultaneous call replaces it on screen
  (the JSON for both is complete).
* Migration: `ConversationStore` is the only code that touches these files. Replace its six methods with PostgreSQL
  tables (`calls`, `transcript_events` unique on `(call_id, event_id)` and `(call_id, sequence_number)`, `call_events`,
  and JSONB columns or tables for the three contexts). The HTTP contract and status codes stay the same, and the unique
  constraints give the same duplicate and conflict behaviour.

## Human hand-off (what is built, from the Vobiz docs)

1. The hand-off starts when the customer asks for a person, or from `POST .../handoff`. Detection (`handoff_intent.py`) is
   rule-based and looks only at the customer's words, before the LLM sees the sentence: request verbs near a target
   ("speak to a human", "connect me to customer care", "agent er sathe kotha bolbo", Bangla script) plus `HUMAN_HANDOFF_PHRASES`;
   negations ("I don't want an agent") and information questions ("what is the customer care number", "how much does
   it cost") do not match. It is not an LLM decision: the reply the AI generates is never used to trigger a transfer.
   The AI then says the announcement, waits (bounded) until it was played, and stays silent (customer speech ignored).
   The Vobiz Transfer API then redirects the call's A-leg to our `/api/vobiz/handoff/{id}/dial`. Repeated requests
   (also the same sentence delivered twice) never start a second transfer. If no transfer is possible (disabled, no or
   invalid number) the customer hears that honestly, the Copilot gets `human_handoff_requested` + `human_handoff_failed`.
2. That URL returns VobizXML: `<Stream audioTrack="both" bidirectional="false" keepCallAlive="false">` to our monitor
   WebSocket, followed by `<Dial>` to the human agent. The stream does not hold the call, so the dial starts at once and the
   stream keeps forking audio. Sarvam keeps transcribing and the Copilot keeps analysing: that is the background AI assist.
3. Dial `callbackUrl` events become Copilot events (`DialConnected` -> `human_agent_connected`, `DialHangup` ->
   `human_agent_disconnected`). If the human does not answer (`DialStatus` busy, no-answer, failed, timeout, cancel), `action`
   returns the AI stream again and the AI says it could not reach a colleague (`human_handoff_failed`).
4. Customer hang-up during the transfer arrives through the existing hang-up webhook (`call_ended`).

## Hold and resume

Real signals only, never silence:

* The human agent presses `HOLD_DTMF` (default `*1`) or `RESUME_DTMF` (`*2`) on their own phone. Vobiz reports digits pressed on the
  B-leg as `DialDigitsMatch` with `DialDigitsPressedBy=bleg`. Digits pressed by the customer are ignored.
* Or `POST .../hold` / `.../resume` is called.
* While on hold the configured `HOLD_AUDIO_URL` is played to the caller with the Vobiz Play API (`mix=false`, looped) and stopped on
  resume. The event records `hold_audio: playing | stopped | failed | not_configured` so a failure is visible.

**Not possible:** Vobiz documents no event for the human's handset hold button, so that cannot be detected. Hold is only
recognised through the digits above or the API.

## Speaker attribution

`TranscriptEvent.speaker` is not used: the STT sink leaves it at a default. The forwarder decides from the media track:

| Source | Speaker | Basis |
|---|---|---|
| Primary AI stream (`audioTrack="inbound"`) | customer | `primary_stream_inbound_track`. Confirmed with real speech in M4.2. |
| Monitor stream, `inbound` track | customer | `monitor_inbound_track`. Same provider tag; documented, not yet confirmed on a transferred call. |
| Monitor stream, `outbound` track | `unknown`, or `human_agent` if `VOBIZ_OUTBOUND_TRACK_SPEAKER=human_agent` and a human is handling the call | `outbound_track_unverified` / `monitor_outbound_track_configured` |
| AI replies | ai_agent | reported by the conversation manager, not by STT |

Vobiz does not document what the outbound track carries on a bridged call, so the code default stays `unknown`.

**Real call of 2026-11-24 (call `95fd27b8-...`, human answered, 22 s bridged):** the stored events show the outbound track
carrying a person answering ("I mean, hello.", 3 s after the bridge, before the caller said anything) and the inbound track carrying
"Hello, ma'am." and "Thank you." No caller sentence appeared on the outbound track and no answering sentence on the
inbound track, so there was no cross-talk in either direction. Together with the Vobiz definition (inbound = audio from the caller,
outbound = audio sent to the caller) this verifies inbound = customer and outbound = human agent for this setup. It rests on one short call:
if a later call shows customer words on the outbound track, set `VOBIZ_OUTBOUND_TRACK_SPEAKER=unknown` again. The mapping is only
applied after `human_agent_connected`; outbound audio while the phone is still ringing stays "unknown". Lines the STT produces
at the instant the call ends can be spurious (two "Yes." lines appeared on both tracks within 80 ms of the hang-up).

## Manual test (costs money: real Vobiz and Sarvam calls)

1. Set the same `INTERNAL_API_TOKEN` in both `.env` files. Voice agent: `COPILOT_TRANSCRIPT_WEBHOOK_URL`, `AI_CONVERSATION_ENABLED=true`,
   `VOBIZ_SARVAM_STT_ENABLED=true`, `HUMAN_HANDOFF_ENABLED=true`, `HUMAN_AGENT_PHONE=<your test phone>`, `PUBLIC_BASE_URL` (https), Vobiz keys.
2. Start the Copilot (8000) and the voice agent (8001, `uvicorn app.main:app --port 8001`); open the Copilot and sign in.
3. Call the Vobiz number. Check the panel: the call appears, your speech shows as Customer, the AI as AI agent.
4. `curl -X POST http://localhost:8001/api/vobiz/calls/<call_uuid>/handoff -H "X-Internal-Token: ..." -H "Content-Type: application/json" -d '{"reason":"test"}'`
   (the call uuid is in the panel header). The status bar goes Handing over, Calling, With the human agent.
5. Answer the human phone, talk, press `*1` and `*2`. Check the status and `human_agent_context.json`.
6. Hang up. Check `backend/data/conversations/<call_uuid>/` and the summary on the panel.

## If nothing appears in the Copilot

1. Start the voice agent and read its first log lines. `copilot_forwarding` must say `enabled: true`. `copilot_forwarding_disabled` names the missing setting (usually `COPILOT_TRANSCRIPT_WEBHOOK_URL` or `INTERNAL_API_TOKEN` empty).
2. Both `.env` files need the **same** `INTERNAL_API_TOKEN`. The Copilot answers 503 (token missing) or 401 (token differs); the voice agent logs `copilot_forward_rejected` / `copilot_forward_gave_up` with the status.
3. Restart both processes after editing `.env`. Settings are read once at start.
4. Run the voice agent and the Copilot on different ports (for example 8001 and 8000). The Vite proxy points at the Copilot.
5. `cd backend/voice_agent; python -m scripts.check_copilot_link --copilot http://localhost:8000` sends one synthetic call through the real forwarder with your real settings (no Vobiz or Sarvam call). Add `--panel-user` and `--panel-password` to also list what the panel WebSocket receives. It leaves a `DIAG-*` folder under `data/conversations/`.
6. `GET /api/vobiz/streams` on the voice agent (internal token) shows `copilot_forwarding` with the last HTTP status per call.

## The AI never reads out contact details

The first real calls showed the model answering "call 121 ... or 01711594594". Two layers now prevent it: the system prompt forbids numbers,
codes, emails and websites, and `contains_contact_details()` replaces any reply that still contains a phone number, USSD code,
email or web address with a fixed sentence telling the customer to ask for a human agent. A customer asking for a human is checked
first (rules in `handoff_intent.py`, clause by clause, so a negation in another sentence cannot hide the request) and never
reaches the model.

## AI-first call routing (M3.3)

Every call is answered by the AI agent: the answer webhook returns one bidirectional AI stream (never a `<Dial>`), the AI greets, and the
call is reported to the Copilot as `agent_mode=ai_agent`. The human agent's number is only used by the transfer service, which refuses to run
unless the call has a live AI stream.

| Variable | Meaning |
|---|---|
| `CALL_ROUTING_MODE=ai_first` | The only mode. Unknown values stop startup. |
| `AI_AGENT_ENABLED` | Another name for the existing `AI_CONVERSATION_ENABLED`. Set one; if both are set they must agree. `CALL_ROUTING_MODE` set explicitly with the AI disabled is a startup error. |
| `AI_AGENT_AUTO_ANSWER=true` | The AI greets by itself. `false` is a startup error in ai_first (the greeting cannot be skipped). |
| `HUMAN_HANDOFF_ENABLED`, `HUMAN_AGENT_PHONE` | As before: whether a transfer is possible and where to. |
| `HUMAN_HANDOFF_MODE` | `disabled`, `on_request` (only when the customer asks) or `on_request_or_unresolved`. |
| `AI_AUTO_HANDOFF_ON_UNRESOLVED=true` | Allow the AI to transfer an unresolved call. Needs mode `on_request_or_unresolved`. |
| `AI_MAX_RESOLUTION_ATTEMPTS=2` | Customer reports of "still not working" (1-5) before the AI is asked whether to escalate. |
| `AI_BACKGROUND_ASSIST_ENABLED=true` | Fork both audio tracks after the transfer. `false` = no transcription after the transfer. |

When a human is requested: **explicit request** (customer's words, `handoff_intent.py`) transfers at once. **Unresolved** (`resolution.py`): only the
customer's own words count, and only two things: "it still does not work" after the AI has already replied (one attempt each), and asking for something the
assistant cannot do (balance, bill, refund, ticket: the AI has no account access). Telling the AI about a problem is not a signal. Once the attempts are used
up (or on a capability gap) the model is asked once for a structured decision (`{"escalate", "reason", "next_step_available"}`, strict schema, contradictory
or invalid answers rejected). `escalate=true` starts the transfer. If the model says another step exists, the AI carries on; the next eligible moment escalates
without asking again. If the answer is missing, late or invalid the call escalates, because the customer has already said twice that it is not working.
Nothing is escalated from the text the AI generated.

Copilot statuses: AI agent handling, AI resolution attempt, Human handoff requested, Human agent dialing, Human agent connected · AI background assist,
Human handoff failed (AI handling again), Call completed. `call_ended` carries `resolution` (`ai_resolved`, `human_handled`, `unresolved`, `no_interaction`),
stored in `ai_agent_context.json`.

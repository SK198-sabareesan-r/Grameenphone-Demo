"""Application configuration loaded from environment variables / config/.env."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr

# Credentials live in config/.env (next to .env.example), not in the project root.
ENV_FILE = Path(__file__).resolve().parents[2] / "config" / ".env"


class Settings(BaseModel):
    """Typed settings. Secrets are wrapped in SecretStr so they never print in logs."""

    app_name: str = "grameenphone-ai-voice-agent"
    log_level: str = "INFO"

    sarvam_api_key: SecretStr = SecretStr("")
    sarvam_model: str = "saaras:v4"
    sarvam_language: str = "auto"
    sarvam_stream_type: str = "fast"
    sarvam_base_url: str = "https://api.sarvam.ai"
    sarvam_realtime_ws_url: str = "wss://api.sarvam.ai/speech-to-text-realtime/ws"
    sarvam_request_timeout_s: float = 30.0
    # --- Realtime STT (wss .../speech-to-text-realtime/ws). Model = sarvam_model, language =
    # sarvam_language (SARVAM_STT_LANGUAGE_CODE). The older sarvam_stream_type is kept as-is.
    sarvam_stt_mode: str = "transcribe"
    sarvam_stt_stream_type: str = "balanced"
    sarvam_stt_encoding: str = "linear16"
    sarvam_stt_sample_rate: int = 16000
    sarvam_stt_endpointing: str = "vad"
    sarvam_stt_return_timestamps: bool = True
    sarvam_stt_chunk_ms: int = 100  # audio_input size: 100 ms = 3,200 bytes at 16 kHz linear16
    sarvam_stt_connect_timeout_s: float = 10.0  # handshake + session.begin
    sarvam_stt_keepalive_interval_s: float = 20.0  # app-level {"event":"ping"} when idle (server idles out at 60 s)
    sarvam_stt_max_reconnect_attempts: int = 3
    sarvam_tts_model: str = "bulbul:v4-flash"
    sarvam_tts_language: str = "bn-IN"
    sarvam_tts_speaker: str = ""  # empty = let Sarvam use the model's default speaker
    sarvam_tts_sample_rate: int = 8000

    vobiz_auth_id: str = ""
    vobiz_auth_token: SecretStr = SecretStr("")
    vobiz_base_url: str = "https://api.vobiz.ai/api/v1"
    vobiz_caller_id: str = ""  # outbound `from`: an E.164 number owned by this Vobiz account
    vobiz_request_timeout_s: float = 15.0
    # True = reject Vobiz webhooks whose HMAC signature is missing or invalid.
    # False = skip the check (local debugging only; a warning is logged on every webhook).
    vobiz_validate_signature: bool = True
    # Paths are joined to PUBLIC_BASE_URL to build the URLs Vobiz calls back.
    vobiz_answer_webhook_path: str = "/api/vobiz/answer"
    vobiz_hangup_webhook_path: str = "/api/vobiz/hangup"
    vobiz_stream_ws_path: str = "/ws/vobiz/stream"  # WebSocket server arrives in Milestone 3
    # M5: WebSocket path for MONITORED streams (opt-in multi-stream, per-track routing). Only used when a
    # stream is attached to this path; the legacy stream path above is unchanged.
    vobiz_monitor_ws_path: str = "/ws/vobiz/monitor"
    # Inbound audio format requested from Vobiz: audio/x-l16;rate=8000|16000 or audio/x-mulaw;rate=8000
    vobiz_inbound_content_type: str = "audio/x-l16;rate=16000"

    # --- Vobiz WebSocket server (Milestone 3) ---
    # "query": token in the stream URL (?token=...). "start_event": token sent via <Stream extraHeaders>
    # and checked on the `start` event. Switch if Vobiz does not preserve query strings.
    vobiz_ws_auth_mode: str = "query"
    vobiz_ws_max_connections: int = 100
    vobiz_ws_queue_frames: int = 500  # per stream; 500 x 20 ms = 10 s of inbound audio
    vobiz_ws_max_message_bytes: int = 262_144
    vobiz_ws_start_timeout_s: float = 10.0  # max wait for the `start` event after connect
    vobiz_ws_idle_timeout_s: float = 60.0  # close if nothing arrives (Vobiz sends ~50 frames/s)
    vobiz_playback_chunk_ms: int = 20  # outbound playAudio chunk size; documented range 20-60
    vobiz_checkpoint_timeout_s: float = 30.0

    # --- Vobiz -> Sarvam realtime STT (M4.3). Off by default: no Sarvam connection is ever made. ---
    vobiz_sarvam_stt_enabled: bool = False
    # Max wait for Sarvam's final transcript (session.end) after a call's stream ends.
    vobiz_sarvam_stt_finalize_timeout_s: float = 2.0
    # Time already-buffered inbound frames get to reach the sink when a stream ends.
    vobiz_audio_drain_timeout_s: float = 0.5

    # --- Internal transcript event pipeline (M4.4) ---
    transcript_subscriber_queue_size: int = 256  # per subscriber, per call
    transcript_final_reserve: int = 64  # extra headroom only finals/VAD/session/error events may use
    transcript_subscriber_timeout_s: float = 5.0  # max time for one subscriber callback
    transcript_drain_timeout_s: float = 2.0  # time subscribers get to drain when a call closes
    transcript_call_idle_close_s: float = 30.0  # close a call's session this long after its last stream ends
    transcript_max_subscribers_per_call: int = 16

    # --- Development-only inbound audio capture (M4.2 byte-order verification) ---
    # Active only when APP_ENV=development AND VOBIZ_DEV_CAPTURE_ENABLED=true AND a test destination
    # is set. Only calls placed through POST /api/vobiz/calls to that destination are captured.
    app_env: str = "production"
    vobiz_dev_capture_enabled: bool = False
    vobiz_dev_capture_destination: str = ""  # E.164 number of YOUR test phone
    vobiz_dev_capture_max_seconds: int = 10  # hard cap 30
    vobiz_dev_capture_dir: str = ""  # empty = per-user app-data dir, outside the project
    vobiz_dev_capture_retention_hours: float = 24.0

    # --- Inbound AI voice conversation (greeting + Bedrock LLM + Sarvam TTS). Off by default. ---
    ai_conversation_enabled: bool = False
    # AWS credentials come from the standard boto3 chain (env, shared config/SSO, IAM role); none are
    # stored here. Region: AWS_REGION / AWS_DEFAULT_REGION, else taken from the inference profile ARN.
    aws_region: str = ""
    bedrock_inference_profile_arn: str = ""  # passed as `modelId` to the Bedrock Converse API
    bedrock_max_tokens: int = 200  # spoken replies are short
    bedrock_temperature: float = 0.3
    ai_llm_timeout_s: float = 12.0  # per Bedrock request (connect + read)
    ai_tts_timeout_s: float = 10.0  # per Sarvam TTS request
    ai_history_max_messages: int = 20  # messages kept per call and sent to the model
    ai_barge_in_enabled: bool = True
    ai_barge_in_grace_ms: int = 500  # ignore speech-start this soon after assistant audio starts (echo/late VAD)

    # Shared secret for our own protected routes (e.g. POST /api/vobiz/calls). Empty = routes disabled.
    internal_api_token: SecretStr = SecretStr("")

    public_base_url: str = ""
    copilot_transcript_webhook_url: str = Field(default="")

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from the process environment (after loading .env)."""
        load_dotenv(ENV_FILE)
        env = os.environ

        def get(name: str, default: str = "") -> str:
            # Treat empty values (as in .env.example) the same as unset.
            return env.get(name) or default

        return cls(
            log_level=get("LOG_LEVEL", "INFO").upper(),
            sarvam_api_key=SecretStr(get("SARVAM_API_KEY")),
            # SARVAM_STT_MODEL / SARVAM_DEFAULT_LANGUAGE are the current names;
            # the older SARVAM_MODEL / SARVAM_LANGUAGE are still honoured.
            sarvam_model=get("SARVAM_STT_MODEL") or get("SARVAM_MODEL", "saaras:v4"),
            sarvam_language=get("SARVAM_STT_LANGUAGE_CODE")
            or get("SARVAM_DEFAULT_LANGUAGE")
            or get("SARVAM_LANGUAGE", "auto"),
            sarvam_stt_mode=get("SARVAM_STT_MODE", "transcribe"),
            sarvam_stt_stream_type=get("SARVAM_STT_STREAM_TYPE", "balanced"),
            sarvam_stt_encoding=get("SARVAM_STT_ENCODING", "linear16"),
            sarvam_stt_sample_rate=int(get("SARVAM_STT_SAMPLE_RATE", "16000")),
            sarvam_stt_endpointing=get("SARVAM_STT_ENDPOINTING", "vad"),
            sarvam_stt_return_timestamps=get("SARVAM_STT_RETURN_TIMESTAMPS", "true").lower()
            not in ("0", "false", "no"),
            sarvam_stt_chunk_ms=int(get("SARVAM_STT_CHUNK_MS", "100")),
            sarvam_stt_connect_timeout_s=float(get("SARVAM_STT_CONNECT_TIMEOUT_S", "10")),
            sarvam_stt_keepalive_interval_s=float(get("SARVAM_STT_KEEPALIVE_INTERVAL_S", "20")),
            sarvam_stt_max_reconnect_attempts=int(get("SARVAM_STT_MAX_RECONNECT_ATTEMPTS", "3")),
            sarvam_stream_type=get("SARVAM_STREAM_TYPE", "fast"),
            sarvam_base_url=get("SARVAM_BASE_URL", "https://api.sarvam.ai").rstrip("/"),
            sarvam_realtime_ws_url=get(
                "SARVAM_REALTIME_WS_URL", "wss://api.sarvam.ai/speech-to-text-realtime/ws"
            ),
            sarvam_request_timeout_s=float(get("SARVAM_REQUEST_TIMEOUT_S", "30")),
            sarvam_tts_model=get("SARVAM_TTS_MODEL", "bulbul:v4-flash"),
            sarvam_tts_language=get("SARVAM_TTS_LANGUAGE", "bn-IN"),
            sarvam_tts_speaker=get("SARVAM_TTS_SPEAKER"),
            sarvam_tts_sample_rate=int(get("SARVAM_TTS_SAMPLE_RATE", "8000")),
            vobiz_auth_id=get("VOBIZ_AUTH_ID"),
            vobiz_auth_token=SecretStr(get("VOBIZ_AUTH_TOKEN")),
            vobiz_base_url=get("VOBIZ_BASE_URL", "https://api.vobiz.ai/api/v1").rstrip("/"),
            vobiz_caller_id=get("VOBIZ_CALLER_ID"),
            vobiz_request_timeout_s=float(get("VOBIZ_REQUEST_TIMEOUT_S", "15")),
            vobiz_validate_signature=get("VOBIZ_VALIDATE_SIGNATURE", "true").lower()
            not in ("0", "false", "no"),
            vobiz_answer_webhook_path=get("VOBIZ_ANSWER_WEBHOOK_PATH", "/api/vobiz/answer"),
            vobiz_hangup_webhook_path=get("VOBIZ_HANGUP_WEBHOOK_PATH", "/api/vobiz/hangup"),
            vobiz_stream_ws_path=get("VOBIZ_STREAM_WS_PATH", "/ws/vobiz/stream"),
            vobiz_monitor_ws_path=get("VOBIZ_MONITOR_WS_PATH", "/ws/vobiz/monitor"),
            vobiz_inbound_content_type=get(
                "VOBIZ_INBOUND_CONTENT_TYPE", "audio/x-l16;rate=16000"
            ),
            vobiz_ws_auth_mode=get("VOBIZ_WS_AUTH_MODE", "query").lower(),
            vobiz_ws_max_connections=int(get("VOBIZ_WS_MAX_CONNECTIONS", "100")),
            vobiz_ws_queue_frames=int(get("VOBIZ_WS_QUEUE_FRAMES", "500")),
            vobiz_ws_max_message_bytes=int(get("VOBIZ_WS_MAX_MESSAGE_BYTES", "262144")),
            vobiz_ws_start_timeout_s=float(get("VOBIZ_WS_START_TIMEOUT_S", "10")),
            vobiz_ws_idle_timeout_s=float(get("VOBIZ_WS_IDLE_TIMEOUT_S", "60")),
            vobiz_playback_chunk_ms=int(get("VOBIZ_PLAYBACK_CHUNK_MS", "20")),
            vobiz_checkpoint_timeout_s=float(get("VOBIZ_CHECKPOINT_TIMEOUT_S", "30")),
            vobiz_sarvam_stt_enabled=get("VOBIZ_SARVAM_STT_ENABLED", "false").lower() in ("1", "true", "yes"),
            vobiz_sarvam_stt_finalize_timeout_s=float(get("VOBIZ_SARVAM_STT_FINALIZE_TIMEOUT_S", "2.0")),
            vobiz_audio_drain_timeout_s=float(get("VOBIZ_AUDIO_DRAIN_TIMEOUT_S", "0.5")),
            transcript_subscriber_queue_size=int(get("TRANSCRIPT_SUBSCRIBER_QUEUE_SIZE", "256")),
            transcript_final_reserve=int(get("TRANSCRIPT_FINAL_RESERVE", "64")),
            transcript_subscriber_timeout_s=float(get("TRANSCRIPT_SUBSCRIBER_TIMEOUT_S", "5")),
            transcript_drain_timeout_s=float(get("TRANSCRIPT_DRAIN_TIMEOUT_S", "2")),
            transcript_call_idle_close_s=float(get("TRANSCRIPT_CALL_IDLE_CLOSE_S", "30")),
            transcript_max_subscribers_per_call=int(get("TRANSCRIPT_MAX_SUBSCRIBERS_PER_CALL", "16")),
            app_env=get("APP_ENV", "production").lower(),
            vobiz_dev_capture_enabled=get("VOBIZ_DEV_CAPTURE_ENABLED", "false").lower() in ("1", "true", "yes"),
            vobiz_dev_capture_destination=get("VOBIZ_DEV_CAPTURE_DESTINATION"),
            vobiz_dev_capture_max_seconds=int(get("VOBIZ_DEV_CAPTURE_MAX_SECONDS", "10")),
            vobiz_dev_capture_dir=get("VOBIZ_DEV_CAPTURE_DIR"),
            vobiz_dev_capture_retention_hours=float(get("VOBIZ_DEV_CAPTURE_RETENTION_HOURS", "24")),
            ai_conversation_enabled=get("AI_CONVERSATION_ENABLED", "false").lower() in ("1", "true", "yes"),
            aws_region=get("AWS_REGION") or get("AWS_DEFAULT_REGION"),
            bedrock_inference_profile_arn=get("BEDROCK_INFERENCE_PROFILE_ARN"),
            bedrock_max_tokens=int(get("BEDROCK_MAX_TOKENS", "200")),
            bedrock_temperature=float(get("BEDROCK_TEMPERATURE", "0.3")),
            ai_llm_timeout_s=float(get("AI_LLM_TIMEOUT_S", "12")),
            ai_tts_timeout_s=float(get("AI_TTS_TIMEOUT_S", "10")),
            ai_history_max_messages=int(get("AI_HISTORY_MAX_MESSAGES", "20")),
            ai_barge_in_enabled=get("AI_BARGE_IN_ENABLED", "true").lower() not in ("0", "false", "no"),
            ai_barge_in_grace_ms=int(get("AI_BARGE_IN_GRACE_MS", "500")),
            internal_api_token=SecretStr(get("INTERNAL_API_TOKEN")),
            public_base_url=get("PUBLIC_BASE_URL").rstrip("/"),
            copilot_transcript_webhook_url=get("COPILOT_TRANSCRIPT_WEBHOOK_URL"),
        )


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings.from_env()

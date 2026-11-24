"""Application settings. All credentials come from config/.env, never from code."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).resolve().parent
BASE_DIR = CONFIG_DIR.parent
ENV_FILE = CONFIG_DIR / ".env"


def _normalize_phone(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit() or ch == "+")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="ignore"
    )

    # Server
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    public_base_url: str = ""
    # Only needed when the frontend is served from a different origin than the backend.
    cors_origins: str = ""

    # AWS
    aws_region: str = ""
    aws_access_key_id: SecretStr = SecretStr("")
    aws_secret_access_key: SecretStr = SecretStr("")
    aws_session_token: SecretStr = SecretStr("")
    s3_bucket: str = ""
    s3_prefix: str = "copilot/calls"
    s3_knowledge_prefix: str = "knowledge/pdf"
    dynamodb_table_prefix: str = "gp-copilot-"
    dynamodb_timeout_s: float = 4.0

    # Where the Copilot's data lives
    data_backend: str = "dynamodb"  # dynamodb | files
    data_fallback_to_files: bool = True
    data_retry_after_s: float = 30.0
    bedrock_inference_profile_arn_fast: str = ""
    bedrock_inference_profile_arn_summary: str = ""

    # Sarvam
    sarvam_api_key: SecretStr = SecretStr("")
    sarvam_stt_url: str = "wss://api.sarvam.ai/speech-to-text-realtime/ws"
    sarvam_stt_model: str = ""
    sarvam_stt_language: str = "bn-IN"
    sarvam_stt_mode: str = "codemix"
    sarvam_stt_stream_type: str = "fast"
    sarvam_stt_silence_ms: int = 700
    sarvam_stt_keyterms: str = ""
    sarvam_stt_max_retries: int = 3
    sarvam_tts_url: str = "https://api.sarvam.ai/text-to-speech"
    sarvam_tts_model: str = ""
    sarvam_tts_language: str = "bn-IN"
    sarvam_tts_speaker_agent: str = ""
    sarvam_tts_speaker_customer: str = ""

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: SecretStr = SecretStr("")
    twilio_twiml_app_sid: str = ""
    twilio_agent_identity: str = "agent"
    twilio_record_calls: bool = True
    twilio_validate_signature: bool = False

    # Automatic lookup for each customer sentence
    assist_enabled: bool = True
    assist_timeout_s: float = 8.0
    assist_max_articles: int = 4

    # Demo behaviour
    demo_phone_map: str = ""
    ticket_start_number: int = 10234

    @property
    def data_dir(self) -> Path:
        return BASE_DIR / "data"

    @property
    def runtime_dir(self) -> Path:
        path = self.data_dir / "runtime"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def frontend_dist_dir(self) -> Path:
        """Built React app (frontend/dist). Served by the backend only when it exists."""
        return BASE_DIR.parent / "frontend" / "dist"

    @property
    def public_stream_url(self) -> str:
        base = self.public_base_url.rstrip("/")
        return base.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/twilio/stream"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def stt_keyterms(self) -> list[str]:
        terms = [t.strip() for t in self.sarvam_stt_keyterms.split(",") if t.strip()]
        return [t[:64] for t in terms][:50]

    @property
    def phone_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for pair in self.demo_phone_map.split(","):
            phone, _, customer_id = pair.partition(":")
            if phone.strip() and customer_id.strip():
                mapping[_normalize_phone(phone)] = customer_id.strip()
        return mapping

    def missing(self, *names: str) -> list[str]:
        """Return the env var names (upper case) that are empty."""
        missing = []
        for name in names:
            value = getattr(self, name)
            raw = value.get_secret_value() if isinstance(value, SecretStr) else value
            if not raw:
                missing.append(name.upper())
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()

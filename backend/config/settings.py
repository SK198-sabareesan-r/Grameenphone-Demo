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
    # Only needed when the frontend is served from a different origin than the backend.
    cors_origins: str = ""
    # FastAPI /docs and /openapi.json. Off by default: they would be reachable without login.
    copilot_api_docs_enabled: bool = False

    # Authentication (see copilot/services/auth.py)
    # HMAC key for session tokens. Required: login is refused while it is empty.
    copilot_session_secret: SecretStr = SecretStr("")
    # Local users file (scrypt password hashes). Empty = config/users.json.
    copilot_users_file: str = ""
    copilot_session_ttl_minutes: int = 480
    copilot_ws_ticket_ttl_s: int = 60
    # Customer picker and data reset in the panel. Off unless explicitly enabled.
    demo_controls_enabled: bool = False

    # Shared secret for internal service-to-service routes (/api/v1/internal/*). The voice agent sends
    # it in the X-Internal-Token header (same variable name as in the voice agent's config).
    # Empty = those routes answer 503 (fail closed).
    internal_api_token: SecretStr = SecretStr("")

    # AWS
    aws_region: str = ""
    aws_access_key_id: SecretStr = SecretStr("")
    aws_secret_access_key: SecretStr = SecretStr("")
    aws_session_token: SecretStr = SecretStr("")
    s3_bucket: str = ""
    # Call conversations and call records. A private bucket; it holds customers' original mobile numbers.
    s3_transcript_bucket: str = ""
    s3_transcript_prefix: str = "calls"  # <prefix>/<mobile number>/<call id>.json, the finished call record
    s3_live_prefix: str = "live"  # <prefix>/<call id>/<document>.json, voice calls while they are running
    s3_knowledge_prefix: str = "knowledge/pdf"
    dynamodb_table_prefix: str = "gp-copilot-"
    dynamodb_timeout_s: float = 4.0

    # Where the Copilot's data lives
    data_backend: str = "dynamodb"  # dynamodb | files
    data_fallback_to_files: bool = True
    data_retry_after_s: float = 30.0
    bedrock_inference_profile_arn_fast: str = ""
    bedrock_inference_profile_arn_summary: str = ""
    # Bedrock Knowledge Base (retrieval). Empty means the app keeps using the local article search.
    bedrock_kb_id: str = ""
    bedrock_kb_top_k: int = 5
    startup_warm_up: bool = True  # make the first AWS calls at startup so the first live lookup is not slow
    bedrock_kb_timeout_s: float = 4.0
    # Off by default: the extra model call costs 1 to 3 s. The keyword list covers Banglish instead.
    bedrock_kb_rewrite_query: bool = False
    bedrock_kb_rewrite_timeout_s: float = 3.0

    # Automatic lookup for each customer sentence
    assist_enabled: bool = True
    assist_timeout_s: float = 8.0
    # Ask Copilot: how long to wait when it rewrites a follow-up question using the chat so far.
    ask_condense_timeout_s: float = 4.0
    assist_max_articles: int = 4

    # Demo behaviour
    demo_phone_map: str = ""
    ticket_start_number: int = 10234

    # Folders for call records, tickets and conversation files. Leave both EMPTY: nothing is then written to the
    # local disk (conversations go to the call-transcripts bucket, tickets and calls to DynamoDB). Tests set them
    # to a temporary folder.
    copilot_runtime_dir: str = ""
    copilot_conversations_dir: str = ""

    @property
    def data_dir(self) -> Path:
        return BASE_DIR / "data"

    @property
    def conversations_dir(self) -> Path | None:
        """The folder for conversation files, only when one is set on purpose (tests). None means no local files."""
        return Path(self.copilot_conversations_dir) if self.copilot_conversations_dir else None

    @property
    def runtime_dir(self) -> Path | None:
        """The folder for call records and tickets, only when one is set on purpose (tests). None means memory only."""
        return Path(self.copilot_runtime_dir) if self.copilot_runtime_dir else None

    @property
    def frontend_dist_dir(self) -> Path:
        """Built React app (frontend/dist). Served by the backend only when it exists."""
        return BASE_DIR.parent / "frontend" / "dist"

    @property
    def users_file(self) -> Path:
        return Path(self.copilot_users_file) if self.copilot_users_file else CONFIG_DIR / "users.json"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

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

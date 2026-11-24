"""Configuration diagnostics for the Sarvam integration."""

from __future__ import annotations

from app.core.config import Settings
from app.schemas.sarvam import AuthCheck, SarvamDiagnostics, SttConfigInfo, TtsConfigInfo
from app.services.sarvam import constants as c
from app.services.sarvam.errors import SarvamConfigError
from app.services.sarvam.realtime_protocol import RealtimeSttConfig


def _realtime_params(s: Settings) -> dict[str, str]:
    """Realtime connection query parameters exactly as they would be sent (no secrets in them)."""
    return RealtimeSttConfig(
        url=s.sarvam_realtime_ws_url, model=s.sarvam_model, language_code=s.sarvam_language,
        mode=s.sarvam_stt_mode, stream_type=s.sarvam_stt_stream_type, encoding=s.sarvam_stt_encoding,
        sample_rate=s.sarvam_stt_sample_rate, endpointing=s.sarvam_stt_endpointing,
        return_timestamps=s.sarvam_stt_return_timestamps,
    ).query_params()


def _realtime_config_error(s: Settings) -> str | None:
    try:
        RealtimeSttConfig.from_settings(s)
    except SarvamConfigError as exc:
        return exc.message
    return None


def collect_warnings(s: Settings) -> list[str]:
    """Flag settings that differ from the documented values. Nothing is changed or replaced."""
    w: list[str] = []
    if not s.sarvam_api_key.get_secret_value():
        w.append("SARVAM_API_KEY is not set.")
    if s.sarvam_model not in c.STT_MODELS:
        w.append(f"SARVAM_STT_MODEL '{s.sarvam_model}' is not a documented STT model {sorted(c.STT_MODELS)}.")
    if s.sarvam_model not in c.REALTIME_STT_MODELS:
        w.append(f"SARVAM_STT_MODEL '{s.sarvam_model}' is not accepted by the realtime STT endpoint (saaras:v4 only).")
    if s.sarvam_stream_type not in c.STT_STREAM_TYPES:
        w.append(f"SARVAM_STREAM_TYPE '{s.sarvam_stream_type}' is not one of {sorted(c.STT_STREAM_TYPES)}.")
    error = _realtime_config_error(s)
    if error:
        w.append(error)
    if s.sarvam_language not in c.REALTIME_STT_LANGUAGES:
        w.append(f"SARVAM_DEFAULT_LANGUAGE '{s.sarvam_language}' is not a documented realtime STT language code.")
    if s.sarvam_language == "auto":
        w.append(
            "SARVAM_DEFAULT_LANGUAGE=auto is valid for realtime STT; for REST STT, "
            "language auto-detection means omitting language_code (or sending 'unknown')."
        )
    if s.sarvam_tts_model not in c.TTS_MODELS:
        w.append(f"SARVAM_TTS_MODEL '{s.sarvam_tts_model}' is not a documented TTS model {sorted(c.TTS_MODELS)}.")
    if s.sarvam_tts_language not in c.TTS_LANGUAGES:
        w.append(f"SARVAM_TTS_LANGUAGE '{s.sarvam_tts_language}' is not a documented TTS language code.")
    if s.sarvam_tts_sample_rate not in c.TTS_REST_SAMPLE_RATES:
        w.append(
            f"SARVAM_TTS_SAMPLE_RATE {s.sarvam_tts_sample_rate} is not supported by REST TTS "
            f"{sorted(c.TTS_REST_SAMPLE_RATES)}."
        )
    if not s.sarvam_tts_speaker:
        w.append("SARVAM_TTS_SPEAKER is empty; Sarvam will use the model's default speaker.")
    return w


def build_diagnostics(s: Settings, auth: AuthCheck | None = None) -> SarvamDiagnostics:
    return SarvamDiagnostics(
        api_key_configured=bool(s.sarvam_api_key.get_secret_value()),
        stt=SttConfigInfo(
            model=s.sarvam_model,
            default_language=s.sarvam_language,
            stream_type=s.sarvam_stream_type,
            rest_endpoint=f"{s.sarvam_base_url}{c.STT_REST_PATH}",
            realtime_endpoint=s.sarvam_realtime_ws_url,
            realtime_params=_realtime_params(s),
            realtime_config_valid=_realtime_config_error(s) is None,
        ),
        tts=TtsConfigInfo(
            model=s.sarvam_tts_model,
            language_code=s.sarvam_tts_language,
            speaker=s.sarvam_tts_speaker or None,
            sample_rate=s.sarvam_tts_sample_rate,
        ),
        warnings=collect_warnings(s),
        auth=auth or AuthCheck(checked=False, note="Pass ?verify=true to test the API key."),
    )

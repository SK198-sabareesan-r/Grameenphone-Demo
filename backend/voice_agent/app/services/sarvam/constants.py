"""Values taken from the official Sarvam docs (docs.sarvam.ai). Re-verify when upgrading models.

These are used only for *warnings* in diagnostics. Model names stay configurable and unknown
values are never silently replaced; Sarvam itself is the authority and its errors are surfaced.
"""

from __future__ import annotations

AUTH_HEADER = "api-subscription-key"

# REST STT: POST /speech-to-text (multipart). Realtime: wss .../speech-to-text-realtime/ws
STT_REST_PATH = "/speech-to-text"
STT_MODELS = frozenset({"saaras:v4", "saaras:v3"})
REALTIME_STT_MODELS = frozenset({"saaras:v4"})  # saaras:v3-realtime is legacy
STT_STREAM_TYPES = frozenset({"fast", "balanced", "simulated"})

# Realtime STT language_code values (also "auto").
REALTIME_STT_LANGUAGES = frozenset(
    {
        "auto", "en-IN", "hi-IN", "bn-IN", "kn-IN", "ml-IN", "mr-IN", "or-IN", "pa-IN",
        "ta-IN", "te-IN", "gu-IN", "ur-IN", "ne-IN", "kok-IN", "ks-IN", "sd-IN", "sa-IN",
        "sat-IN", "mni-IN", "brx-IN", "mai-IN", "doi-IN", "as-IN",
    }
)  # fmt: skip

# Realtime STT connection-parameter enums (docs: Realtime Streaming API reference).
REALTIME_STT_MODES = frozenset({"transcribe", "translate", "verbatim", "translit", "codemix"})
REALTIME_STT_ENCODINGS = frozenset({"linear16", "linear32", "mulaw", "alaw"})
REALTIME_STT_SAMPLE_RATES = frozenset({8000, 16000})
REALTIME_STT_ENDPOINTING = frozenset({"vad", "manual"})
# Server rejects a single audio_input above 500 ms of audio (16,000 bytes at 16 kHz linear16).
REALTIME_STT_MAX_CHUNK_MS = 500
# WebSocket close codes documented for the realtime endpoint.
REALTIME_CLOSE_AUTH_OR_QUOTA = 1003  # invalid key, rate limit, or quota exhausted
REALTIME_CLOSE_INACTIVITY = 1008  # no audio for 60 s
REALTIME_CLOSE_SERVER_ERROR = 1011  # retry with backoff
REALTIME_CLOSE_INVALID_REQUEST = 4000  # bad model / language / parameter, or account not enabled

# TTS: POST /text-to-speech
TTS_PATH = "/text-to-speech"
TTS_MODELS = frozenset({"bulbul:v4-flash", "bulbul:v3"})
TTS_LANGUAGES = frozenset(
    {"hi-IN", "en-IN", "bn-IN", "gu-IN", "kn-IN", "mr-IN", "pa-IN", "ta-IN", "te-IN", "ml-IN", "od-IN"}
)
TTS_REST_SAMPLE_RATES = frozenset({8000, 16000, 22050, 24000, 32000, 44100, 48000})
TTS_MAX_CHARS = 2500

# Cheap authenticated text endpoint used only to verify the API key.
AUTH_PROBE_PATH = "/text-lid"

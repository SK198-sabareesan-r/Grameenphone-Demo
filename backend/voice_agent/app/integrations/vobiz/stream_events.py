"""Parsing of Vobiz -> application WebSocket events.

Event shapes follow the Vobiz Stream docs: start, media, playedStream, clearedAudio, dtmf, and
incorrectPayload (sent when Vobiz rejects a command). There is no inbound `stop`. Parsing is pure
and never raises anything except StreamProtocolError, so a bad message cannot crash the handler.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.services.audio.formats import AudioFormat, AudioFormatError, inbound_format

MAX_ID_LEN = 128
MAX_NAME_LEN = 256


class StreamProtocolError(ValueError):
    """A message was malformed. `reason` is a short machine-readable code safe to log."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class UnsupportedStreamFormat(StreamProtocolError):
    """start.mediaFormat names a format we cannot decode."""


@dataclass(frozen=True)
class StartEvent:
    sequence_number: int | None
    call_id: str
    stream_id: str
    account_id: str | None
    tracks: tuple[str, ...]
    media_format: AudioFormat
    extra_headers: str | None = field(default=None, repr=False)  # may carry our auth token


@dataclass(frozen=True)
class MediaEvent:
    sequence_number: int | None
    stream_id: str
    track: str
    timestamp_ms: int
    chunk: int
    payload: str = field(repr=False)


@dataclass(frozen=True)
class PlayedStreamEvent:
    name: str  # playedStream carries no streamId


@dataclass(frozen=True)
class ClearedAudioEvent:
    stream_id: str | None
    sequence_number: int | None


@dataclass(frozen=True)
class DtmfEvent:
    stream_id: str | None
    track: str | None
    timestamp_ms: int | None
    digit: str | None = field(default=None, repr=False)  # may be a PIN: never log it


@dataclass(frozen=True)
class IncorrectPayloadEvent:
    stream_id: str | None
    payload_length: int


@dataclass(frozen=True)
class UnknownEvent:
    name: str


StreamEvent = (
    StartEvent
    | MediaEvent
    | PlayedStreamEvent
    | ClearedAudioEvent
    | DtmfEvent
    | IncorrectPayloadEvent
    | UnknownEvent
)


def _req_str(d: dict, key: str, where: str, max_len: int = MAX_ID_LEN) -> str:
    value = d.get(key)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise StreamProtocolError("invalid_field", f"{where}.{key}")
    if any(ch.isspace() or ord(ch) < 32 for ch in value) and key != "name":
        raise StreamProtocolError("invalid_field", f"{where}.{key}")
    return value


def _opt_str(d: dict, key: str, max_len: int = MAX_ID_LEN) -> str | None:
    value = d.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_len:
        raise StreamProtocolError("invalid_field", key)
    return value


def _opt_int(d: dict, key: str) -> int | None:
    value = d.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise StreamProtocolError("invalid_field", key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit() and len(value) <= 20:
        return int(value)
    raise StreamProtocolError("invalid_field", key)


def _req_int(d: dict, key: str, where: str) -> int:
    value = _opt_int(d, key)
    if value is None or value < 0:
        raise StreamProtocolError("invalid_field", f"{where}.{key}")
    return value


def _obj(d: dict, key: str) -> dict:
    value = d.get(key)
    if not isinstance(value, dict):
        raise StreamProtocolError("invalid_field", key)
    return value


def parse_message(text: str) -> StreamEvent:
    """Parse one text frame from Vobiz into a typed event."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise StreamProtocolError("invalid_json") from exc
    if not isinstance(data, dict):
        raise StreamProtocolError("not_an_object")
    name = data.get("event")
    if not isinstance(name, str) or not name:
        raise StreamProtocolError("missing_event")

    seq = _opt_int(data, "sequenceNumber") if "sequenceNumber" in data else None

    if name == "start":
        start = _obj(data, "start")
        media_format = _obj(start, "mediaFormat")
        tracks = start.get("tracks", [])
        if not isinstance(tracks, list) or not all(isinstance(t, str) for t in tracks):
            raise StreamProtocolError("invalid_field", "start.tracks")
        account = start.get("accountId")
        try:
            fmt = inbound_format(media_format.get("encoding"), media_format.get("sampleRate"))
        except AudioFormatError as exc:
            raise UnsupportedStreamFormat("unsupported_format", str(exc)) from exc
        extra = data.get("extra_headers")
        return StartEvent(
            sequence_number=seq,
            call_id=_req_str(start, "callId", "start"),
            stream_id=_req_str(start, "streamId", "start"),
            account_id=str(account) if isinstance(account, (str, int)) and not isinstance(account, bool) else None,
            tracks=tuple(tracks),
            media_format=fmt,
            extra_headers=extra if isinstance(extra, str) else None,
        )

    if name == "media":
        media = _obj(data, "media")
        return MediaEvent(
            sequence_number=seq,
            stream_id=_req_str(data, "streamId", "media"),
            track=_req_str(media, "track", "media"),
            timestamp_ms=_req_int(media, "timestamp", "media"),
            chunk=_req_int(media, "chunk", "media"),
            payload=media.get("payload") if isinstance(media.get("payload"), str) else "",
        )

    if name == "playedStream":
        return PlayedStreamEvent(name=_req_str(data, "name", "playedStream", MAX_NAME_LEN))

    if name == "clearedAudio":
        return ClearedAudioEvent(stream_id=_opt_str(data, "streamId"), sequence_number=seq)

    if name == "dtmf":
        dtmf = data.get("dtmf") if isinstance(data.get("dtmf"), dict) else {}
        return DtmfEvent(
            stream_id=_opt_str(data, "streamId"),
            track=_opt_str(dtmf, "track", 32),
            timestamp_ms=_opt_int(dtmf, "timestamp"),
            digit=_opt_str(dtmf, "digit", 1),
        )

    if name == "incorrectPayload":
        payload = data.get("payload")
        return IncorrectPayloadEvent(
            stream_id=_opt_str(data, "streamId"),
            payload_length=len(payload) if isinstance(payload, str) else 0,
        )

    return UnknownEvent(name=name[:64])


_EXTRA_TOKEN_RE = re.compile(r"(?:X-VH-)?token\s*[:=]\s*([A-Za-z0-9]+)", re.IGNORECASE)


def token_from_extra_headers(extra_headers: str | None) -> str | None:
    """Extract our token from the start event's `extra_headers`.

    Vobiz documents the string as NOT JSON, e.g. "{X-VH-agentType: sales}" (each key gets an
    X-VH- prefix). We also tolerate JSON and "token=..." forms, because the exact format for our
    key has not been observed on a real call.
    """
    if not extra_headers:
        return None
    try:
        parsed = json.loads(extra_headers)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        for key, value in parsed.items():
            if isinstance(key, str) and key.lower().removeprefix("x-vh-") == "token" and isinstance(value, str):
                return value
    match = _EXTRA_TOKEN_RE.search(extra_headers)
    return match.group(1) if match else None

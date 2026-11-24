"""Speech-check utility (M4.2). MOCKED Sarvam: proves the measuring logic, not live transcription."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys

import pytest

from app.services.sarvam.speech_check import (
    FRAME_BYTES,
    normalize_words,
    run_speech_check,
    summarize,
    word_error_rate,
)
from tests.test_sarvam_realtime_stt import FakeConnect, FakeWS, Sleeps, config, session_begin

KEY = "sk-test-SECRET-key-123"


async def no_pace(_: float) -> None:
    return None


def scripted_server(final_text: str = "My internet is not working"):
    """Answers like Sarvam: VAD + partial after the first chunk, final + session.end on end."""
    state = {"chunks": 0}

    def on_send(ws: FakeWS, message: dict) -> None:
        if message["event"] == "audio_input":
            state["chunks"] += 1
            if state["chunks"] == 1:
                ws.push({"event": "vad.speech_start", "utterance_idx": 0})
                ws.push({"event": "transcript.partial", "utterance_idx": 0, "text": "My inter", "language": "en-IN"})
        elif message["event"] == "end":
            ws.push({"event": "vad.speech_end", "utterance_idx": 0})
            ws.push({"event": "transcript.final", "utterance_idx": 0, "text": final_text, "language": "en-IN",
                     "language_confidence": "0.96", "start_s": "0.10", "end_s": "1.90"})
            ws.push({"event": "session.end", "request_id": "req-1", "audio_duration_s": "3.5", "total_utterances": 1})
            ws.push_close(1000, "")

    return on_send


async def test_speech_check_feeds_vobiz_sized_frames_into_3200_byte_chunks() -> None:
    ws = FakeWS([session_begin("req-1")], on_send=scripted_server())
    pcm = os.urandom(32000 * 2)  # 2 s
    result = await run_speech_check(pcm, config(), KEY, connect_fn=FakeConnect(ws), pace=False,
                                    trailing_silence_s=0.5, client_sleep=Sleeps())
    audio = [base64.b64decode(json.loads(t)["audio"]) for t in ws.sent if json.loads(t)["event"] == "audio_input"]
    assert FRAME_BYTES == 640
    assert all(len(a) == 3200 for a in audio)  # 2.5 s = 80,000 bytes = exactly 25 chunks
    assert len(audio) == 25 and result.chunks_sent == 25 and result.bytes_sent == 80000
    assert b"".join(audio)[: len(pcm)] == pcm  # speech unchanged and in order; padding is zeros after it
    assert set(b"".join(audio)[len(pcm):]) == {0}
    assert json.loads(ws.sent[-1]) == {"event": "end"}


async def test_speech_check_records_partials_finals_latency_and_wer() -> None:
    ws = FakeWS([session_begin("req-1")], on_send=scripted_server("My internet is not working"))
    result = await run_speech_check(os.urandom(16000), config(), KEY, expected="my internet is not working.",
                                    connect_fn=FakeConnect(ws), pace=False, client_sleep=Sleeps())
    assert result.request_id == "req-1"
    assert [p.text for p in result.partials] == ["My inter"]
    assert len(result.finals) == 1 and result.finals[0].language == "en-IN"
    assert result.finals[0].language_confidence == 0.96 and result.finals[0].start_s == 0.10
    assert result.transcript == "My internet is not working"
    assert result.word_error_rate == 0.0  # case and punctuation ignored
    assert result.first_partial_ms is not None and result.first_final_ms >= result.first_partial_ms
    assert len(result.final_after_speech_end_ms) == 1
    assert result.billed_audio_s == 3.5 and result.final_state == "closed"
    lines = "\n".join(summarize(result))
    assert "TRANSCRIPT          My internet is not working" in lines and "0.00%" in lines
    assert KEY not in lines


async def test_speech_check_with_real_time_pacing_uses_absolute_schedule() -> None:
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    ws = FakeWS([session_begin()], on_send=scripted_server())
    await run_speech_check(b"\x00\x00" * 1600, config(), KEY, connect_fn=FakeConnect(ws), pace_sleep=record,
                           trailing_silence_s=0.0, client_sleep=Sleeps())
    # 5 x 20 ms frames. The fake sleep does not advance the clock, so an ABSOLUTE schedule asks for
    # ~20, 40, 60, 80, 100 ms (a drifting relative schedule would ask for 20 ms every time).
    assert len(delays) == 5
    assert [round(d, 2) for d in delays] == [0.02, 0.04, 0.06, 0.08, 0.1]


async def test_speech_check_without_finals_reports_none() -> None:
    def server(ws, message):
        if message["event"] == "end":
            ws.push({"event": "session.end", "request_id": "r", "audio_duration_s": "1.0"})

    ws = FakeWS([session_begin()], on_send=server)
    result = await run_speech_check(b"\x00\x00" * 16000, config(end_timeout_s=0.2), KEY, connect_fn=FakeConnect(ws),
                                    pace=False, client_sleep=Sleeps(), expected="hello")
    assert result.finals == [] and result.transcript == "" and result.word_error_rate == 1.0
    assert "(none)" in "\n".join(summarize(result))


async def test_speech_check_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        await run_speech_check(b"\x00", config(), KEY, connect_fn=FakeConnect())
    from dataclasses import replace

    with pytest.raises(ValueError):
        await run_speech_check(b"\x00\x00", replace(config(), encoding="mulaw"), KEY, connect_fn=FakeConnect())


@pytest.mark.parametrize(
    ("ref", "hyp", "wer"),
    [("my internet is not working", "my internet is not working", 0.0),
     ("my internet is not working", "my internet not working", 0.2),
     ("my internet is not working", "", 1.0),
     ("আমার ইন্টারনেট কাজ করছে না।", "আমার ইন্টারনেট কাজ করছে না", 0.0),
     ("আমার ইন্টারনেট কাজ করছে না", "আমার ইন্টারনেট কাজ করে না", 0.2),
     ("", "", 0.0)],
)
def test_word_error_rate(ref: str, hyp: str, wer: float) -> None:
    assert word_error_rate(ref, hyp) == pytest.approx(wer)


def test_normalize_words_handles_bengali_and_punctuation() -> None:
    assert normalize_words("Hello, WORLD!") == ["hello", "world"]
    assert normalize_words("আপনার internet connection check করছি। একটু অপেক্ষা করুন।") == [
        "আপনার", "internet", "connection", "check", "করছি", "একটু", "অপেক্ষা", "করুন"]


# --- the CLI scripts -------------------------------------------------------------------------


def run_script(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=full_env, timeout=60,
                          encoding="utf-8", errors="replace")


def test_validation_script_rejects_unusable_wav_without_contacting_sarvam(tmp_path) -> None:
    import wave

    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00" * 6400)
    proc = run_script("scripts/validate_sarvam_realtime_stt.py", "--wav", str(path), env={"SARVAM_API_KEY": "dummy-key"})
    assert proc.returncode != 0 and "2 channels" in (proc.stdout + proc.stderr)
    assert "dummy-key" not in proc.stdout + proc.stderr


def test_validation_script_requires_an_audio_source() -> None:
    proc = run_script("scripts/validate_sarvam_realtime_stt.py")
    assert proc.returncode != 0


def test_analysis_script_reports_and_writes_both_listening_wavs(tmp_path) -> None:
    from tests.test_audio_wav_byte_order import pack, speech_like

    capture = tmp_path / "cap.l16"
    capture.write_bytes(pack(speech_like(), "little"))
    (tmp_path / "cap.json").write_text(json.dumps({"encoding": "audio/x-l16", "sample_rate": 16000}))
    proc = run_script("scripts/analyze_vobiz_capture.py", str(capture))
    assert proc.returncode == 0, proc.stderr
    assert "suggestion: little (strong)" in proc.stdout
    assert (tmp_path / "cap.as_little.wav").exists() and (tmp_path / "cap.as_big.wav").exists()


def test_analysis_script_reports_ambiguous_for_silence(tmp_path) -> None:
    capture = tmp_path / "silence.l16"
    capture.write_bytes(b"\x00\x00" * 32000)
    proc = run_script("scripts/analyze_vobiz_capture.py", str(capture), "--no-wav")
    assert proc.returncode == 0 and "AMBIGUOUS" in proc.stdout

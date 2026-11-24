"""DEVELOPMENT-ONLY live check of Sarvam Realtime STT (not part of the API, never mounted).

Reads SARVAM_API_KEY and the SARVAM_STT_* settings from the environment / .env, connects to the
realtime endpoint, validates session.begin, streams a KNOWN little-endian 16 kHz mono 16-bit speech
recording in real time (640-byte frames, aggregated into 3,200-byte / 100 ms chunks), prints partial
and final transcripts with timing, sends {"event":"end"}, waits for session.end and closes.
It never prints the API key or audio.

Input must already be 16 kHz, mono, 16-bit PCM; nothing is resampled or byte-swapped:
  --wav PATH          WAV (RIFF/PCM). WAV PCM is little-endian by specification. Header is stripped.
  --pcm PATH          raw headerless s16le (little-endian) PCM.
  --handshake-only    connect, check session.begin, send 1 s of digital silence, end. Proves auth and
                      parameters only; it is NOT a transcription test.
  --expected TEXT     the phrase actually spoken; prints the word error rate.

Keep recordings OUT of the project (or in samples/, which is git-ignored). Do not use other
people's voices without consent.

Examples:
  python scripts/validate_sarvam_realtime_stt.py --handshake-only
  python scripts/validate_sarvam_realtime_stt.py --wav C:\\recordings\\en_16k.wav --language en-IN ^
      --expected "My internet connection is not working"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import Settings  # noqa: E402
from app.services.audio.wav import WavFormatError, read_pcm16_mono  # noqa: E402
from app.services.sarvam.errors import SarvamError  # noqa: E402
from app.services.sarvam.realtime_protocol import RealtimeSttConfig, SttEvent  # noqa: E402
from app.services.sarvam.speech_check import run_speech_check, summarize  # noqa: E402

SAMPLE_RATE = 16000


def load_audio(args: argparse.Namespace) -> tuple[bytes, str]:
    if args.handshake_only:
        return b"\x00\x00" * SAMPLE_RATE, "1 s digital silence (handshake only, not a transcription test)"
    if args.wav:
        try:
            pcm, info = read_pcm16_mono(args.wav, sample_rate=SAMPLE_RATE)
        except WavFormatError as exc:
            sys.exit(str(exc))
        return pcm, f"WAV {os.path.basename(args.wav)} ({info.duration_s:.2f} s)"
    with open(args.pcm, "rb") as fh:
        data = fh.read()
    if not data or len(data) % 2:
        sys.exit("PCM file is empty or not a whole number of 16-bit samples.")
    return data, f"raw s16le {os.path.basename(args.pcm)}"


def printer(started: float):
    def on_event(event: SttEvent) -> None:
        t = f"+{time.monotonic() - started:6.2f}s"
        if event.type == "session.begin":
            print(f"{t} session.begin request_id={event.request_id} config={event.session_config}")
        elif event.type == "transcript.partial":
            print(f"{t} partial  [{event.language or '-'}] {event.text}")
        elif event.type == "transcript.final":
            span = f" ({event.start_s:.2f}-{event.end_s:.2f}s)" if event.start_s is not None and event.end_s is not None else ""
            conf = f" conf={event.language_confidence}" if event.language_confidence is not None else ""
            print(f"{t} FINAL    [{event.language or '-'}{conf}]{span} {event.text}")
        elif event.type == "error" and event.error:
            print(f"{t} error    code={event.error.code} fatal={event.error.is_fatal} "
                  f"status={event.error.status_code} message={event.error.message}")
        elif event.type == "session.end":
            print(f"{t} session.end audio_duration_s={event.audio_duration_s} utterances={event.total_utterances}")
        else:
            print(f"{t} {event.type}")
    return on_event


async def main(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if not settings.sarvam_api_key.get_secret_value():
        print("SARVAM_API_KEY is not set (environment or .env).")
        return 2
    config = RealtimeSttConfig.from_settings(settings)
    if args.language:
        config = replace(config, language_code=args.language).validated()
    audio, label = load_audio(args)
    print(f"params: {config.query_params()}")
    print(f"audio:  {label}")
    try:
        result = await run_speech_check(audio, config, settings.sarvam_api_key, expected=args.expected,
                                        on_event=printer(time.monotonic()))
    except SarvamError as exc:
        print(f"FAILED: {exc.code}: {exc.message}")
        return 1
    print("\n".join(["", "--- summary ---", *summarize(result)]))
    if args.handshake_only:
        return 0 if result.request_id else 1
    return 0 if result.finals else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--wav")
    source.add_argument("--pcm")
    source.add_argument("--handshake-only", action="store_true")
    parser.add_argument("--language", help="override SARVAM_STT_LANGUAGE_CODE, e.g. en-IN, bn-IN, auto")
    parser.add_argument("--expected", help="the phrase actually spoken, for a word error rate")
    sys.exit(asyncio.run(main(parser.parse_args())))

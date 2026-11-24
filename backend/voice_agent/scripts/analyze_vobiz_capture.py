"""DEVELOPMENT-ONLY analysis of a Vobiz inbound capture (from app/services/voice/dev_capture.py).

Steps:
  1. Reads the raw .l16 capture and its .json sidecar (bytes exactly as Vobiz sent them).
  2. Computes objective metrics for BOTH byte-order readings and prints a suggestion.
  3. Writes two WAVs next to the capture, `<name>.as_little.wav` and `<name>.as_big.wav`, so you
     can LISTEN to both. Exactly one should sound like your voice; the other like loud static.
  4. With --transcribe ORDER, converts explicitly from ORDER to little-endian (no-op for "little",
     one byte swap for "big"; never resampled) and runs it through Sarvam, comparing the transcript
     with --expected (the phrase you actually said on the call).

The byte order counts as VERIFIED only if the metrics, your listening test and the transcript all
agree. Otherwise treat it as unverified.

Usage:
  python scripts/analyze_vobiz_capture.py <capture.l16>
  python scripts/analyze_vobiz_capture.py <capture.l16> --transcribe little --language en-IN ^
      --expected "My internet connection is not working"
  python scripts/analyze_vobiz_capture.py --purge         (delete all captures and derived WAVs)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import Settings  # noqa: E402
from app.services.audio.byte_order import compare_byte_orders, to_little_endian  # noqa: E402
from app.services.audio.wav import write_pcm16_mono  # noqa: E402
from app.services.sarvam.errors import SarvamError  # noqa: E402
from app.services.sarvam.realtime_protocol import RealtimeSttConfig  # noqa: E402
from app.services.sarvam.speech_check import run_speech_check, summarize  # noqa: E402
from app.services.voice.dev_capture import purge_old_captures, resolve_capture_dir  # noqa: E402


def load_capture(path: Path) -> tuple[bytes, dict]:
    raw = path.read_bytes()
    meta_path = path.with_suffix(".json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if meta.get("encoding", "audio/x-l16") != "audio/x-l16":
        sys.exit(f"capture encoding is {meta.get('encoding')}; byte order only applies to L16")
    return raw, meta


def print_metrics(raw: bytes, meta: dict) -> str | None:
    rate = int(meta.get("sample_rate", 16000))
    print(f"capture: {len(raw)} bytes, {len(raw) / (2 * rate):.2f} s at {rate} Hz, meta={ {k: meta.get(k) for k in ('encoding', 'sample_rate', 'frames_captured', 'end_reason')} }")
    verdict = compare_byte_orders(raw)
    print(f"{'metric':<22}{'little-endian':>16}{'big-endian':>16}")
    for label, attr, fmt in (("lag-1 autocorrelation", "lag1_autocorr", "{:.3f}"),
                             ("zero-crossing rate", "zero_crossing_rate", "{:.3f}"),
                             ("RMS (dBFS)", "rms_dbfs", "{:.1f}"), ("peak", "peak", "{}"),
                             ("clipped fraction", "clip_fraction", "{:.4f}"),
                             ("near-silent fraction", "near_silent_fraction", "{:.3f}")):
        print(f"{label:<22}{fmt.format(getattr(verdict.little, attr)):>16}{fmt.format(getattr(verdict.big, attr)):>16}")
    print(f"suggestion: {verdict.suggested or 'AMBIGUOUS'} ({verdict.strength})")
    for reason in verdict.reasons:
        print(f"  - {reason}")
    return verdict.suggested


async def transcribe(raw: bytes, order: str, args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if not settings.sarvam_api_key.get_secret_value():
        print("SARVAM_API_KEY is not set.")
        return 2
    config = RealtimeSttConfig.from_settings(settings)
    if args.language:
        config = replace(config, language_code=args.language).validated()
    pcm_le = to_little_endian(raw, order)  # explicit; "little" leaves the bytes untouched
    print(f"\ntranscribing as {order}-endian ({'unchanged' if order == 'little' else 'byte-swapped'}), "
          f"params {config.query_params()}")
    try:
        result = await run_speech_check(pcm_le, config, settings.sarvam_api_key, expected=args.expected)
    except SarvamError as exc:
        print(f"FAILED: {exc.code}: {exc.message}")
        return 1
    print("\n".join(summarize(result)))
    for p in result.partials:
        print(f"  partial +{p.at_ms:.0f} ms [{p.language or '-'}] {p.text}")
    for f in result.finals:
        print(f"  FINAL   +{f.at_ms:.0f} ms [{f.language or '-'}] {f.text}")
    return 0 if result.finals else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("capture", nargs="?", help="path to a .l16 capture file")
    parser.add_argument("--transcribe", choices=["little", "big"], help="byte order to send to Sarvam")
    parser.add_argument("--language", help="e.g. en-IN, bn-IN, auto")
    parser.add_argument("--expected", help="the phrase you actually spoke on the call")
    parser.add_argument("--no-wav", action="store_true", help="do not write the two listening WAVs")
    parser.add_argument("--purge", action="store_true", help="delete all captures and derived WAVs")
    args = parser.parse_args()

    if args.purge:
        directory = resolve_capture_dir(Settings.from_env().vobiz_dev_capture_dir)
        print(f"removed {purge_old_captures(directory, max_age_s=-1)} files from {directory}")
        return 0
    if not args.capture:
        parser.error("give a capture file or --purge")
    path = Path(args.capture)
    raw, meta = load_capture(path)
    print_metrics(raw, meta)
    if not args.no_wav:
        rate = int(meta.get("sample_rate", 16000))
        for order in ("little", "big"):
            out = path.with_name(f"{path.stem}.as_{order}.wav")
            write_pcm16_mono(str(out), to_little_endian(raw, order), rate)
            print(f"listen: {out}")
    if args.transcribe:
        return asyncio.run(transcribe(raw, args.transcribe, args))
    return 0


if __name__ == "__main__":
    sys.exit(main())

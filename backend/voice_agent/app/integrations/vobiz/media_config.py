"""Vobiz stream media settings, recorded in one place.

"Verified" below means observed on a real Vobiz call (Indian outbound call, 2026-11-23), not only
read in the docs. Byte order is stated explicitly and never inferred from the host platform.
"""

from __future__ import annotations

from typing import Literal

from app.services.audio.formats import ENC_L16, ENC_MULAW, AudioFormat

ByteOrder = Literal["little", "big"]

# Inbound (Vobiz -> us). Verified: start.mediaFormat = audio/x-l16 @ 16000 Hz, 640-byte 20 ms frames.
VOBIZ_INBOUND_FORMAT = AudioFormat(ENC_L16, 16000)
VOBIZ_INBOUND_CONTENT_TYPE = "audio/x-l16;rate=16000"  # value sent in <Stream contentType>
# Verified in M4.2 (2026-11-23) from a development capture of real speech on a controlled Vobiz call:
# read little-endian the capture is speech-like (lag-1 autocorrelation 0.986, -23.4 dBFS, no
# clipping); read big-endian it is noise (0.126, clipped). Confirmed by listening and by a Sarvam
# transcript of the UNCHANGED bytes. So inbound bytes can go to linear16 consumers without swapping.
VOBIZ_INBOUND_L16_BYTE_ORDER: ByteOrder = "little"

# Outbound (us -> Vobiz playAudio). Both verified audible on a real call.
VOBIZ_OUTBOUND_L16_FORMAT = AudioFormat(ENC_L16, 16000)
VOBIZ_OUTBOUND_L16_BYTE_ORDER: ByteOrder = "little"  # verified: "big" produced noise, "little" a clean tone
VOBIZ_OUTBOUND_MULAW_FORMAT = AudioFormat(ENC_MULAW, 8000)


def inbound_byte_order(fmt: AudioFormat) -> ByteOrder | None:
    """Byte order of inbound Vobiz samples; None for 1-byte formats (mu-law has no byte order)."""
    return VOBIZ_INBOUND_L16_BYTE_ORDER if fmt.bytes_per_sample == 2 else None


def outbound_byte_order(fmt: AudioFormat) -> ByteOrder | None:
    """Byte order to use for outbound samples; None for 1-byte formats (mu-law has no byte order)."""
    return VOBIZ_OUTBOUND_L16_BYTE_ORDER if fmt.bytes_per_sample == 2 else None

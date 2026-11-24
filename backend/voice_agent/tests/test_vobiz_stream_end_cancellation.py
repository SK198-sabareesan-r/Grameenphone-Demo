"""Regression: a WebSocket handler cancelled while it cleans up must still finish ending the stream.

Found while investigating intermittent failures of tests that wait for `vobiz_stream_ended` after the
Starlette test client closes a socket (the client cancels the app task right after the disconnect).
Before the fix, cancellation during cleanup skipped the end-of-stream log and the `on_stream_end`
listener notification.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.services.voice.call_manager import CallStateManager, wait_for_pending_cleanups
from app.services.voice.media_stream_manager import VobizMediaStreamHandler, _Connection
from tests.test_vobiz_sarvam_stt import make_session
from tests.test_vobiz_websocket import make_settings


class FakeWebSocket:
    application_state = None  # never CONNECTED: the cleanup must not try to close it


class EndListener:
    def __init__(self) -> None:
        self.ended: list[str] = []

    async def on_stream_start(self, session) -> None: ...
    async def on_dtmf(self, session, event) -> None: ...

    async def on_stream_end(self, session, reason: str) -> None:
        self.ended.append(reason)


async def test_cancelled_cleanup_still_detaches_notifies_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    calls = CallStateManager()
    listener = EndListener()
    calls.add_listener(listener)
    handler = VobizMediaStreamHandler(make_settings(), calls)
    session = make_session("call-x", "stream-x")
    await calls.attach_stream(session)
    conn = _Connection(call_uuid="call-x", session=session)

    original = calls.detach_stream

    async def slow_detach(*args, **kwargs):
        await asyncio.sleep(0.1)  # the cancellation arrives while detach is still in progress
        return await original(*args, **kwargs)

    calls.detach_stream = slow_detach  # type: ignore[method-assign]

    task = asyncio.create_task(handler._cleanup(FakeWebSocket(), conn, "socket_closed", 1000))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await wait_for_pending_cleanups(2.0)
    await asyncio.sleep(0)
    assert session.closed and calls.current_stream("call-x") is None
    assert listener.ended == ["socket_closed"]
    assert any(r.getMessage() == "vobiz_stream_ended" for r in caplog.records)

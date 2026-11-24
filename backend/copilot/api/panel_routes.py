"""Live event stream to the agent panel.

Browsers cannot send an Authorization header on a WebSocket, so the panel connects with a
one-time ticket from POST /api/auth/ws-ticket: /api/panel/stream?ticket=<ticket>.
A missing, unknown, reused or expired ticket is accepted and then closed with code 4401, so the
browser can tell an auth failure apart from a network drop.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from copilot.services.pipeline import session_view

router = APIRouter(prefix="/api", tags=["calls"])
CLOSE_UNAUTHORIZED = 4401


@router.websocket("/panel/stream")
async def panel_stream(ws: WebSocket) -> None:
    c = ws.app.state.container
    user = c.auth.consume_ws_ticket(ws.query_params.get("ticket"))
    if user is None:
        await ws.accept()
        await ws.close(code=CLOSE_UNAUTHORIZED, reason="Sign in required")
        return
    await c.hub.connect(ws)
    try:
        if c.registry.current:
            await c.hub.send(ws, "snapshot", session_view(c, c.registry.current))
        while True:
            await ws.receive_text()  # the panel only listens; this detects disconnects
    except WebSocketDisconnect:
        pass
    finally:
        c.hub.disconnect(ws)

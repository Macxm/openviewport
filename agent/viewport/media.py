"""The player's video connection, relayed through the agent.

Without this, the browser talks to go2rtc directly, so go2rtc must be reachable from the
screen. go2rtc's API then hands anyone on that network every source URL with its
credentials, serves live video with no authentication, and will open arbitrary
`src=` URLs on request. Relaying keeps go2rtc on loopback, puts video behind the same
`auth.token` as the rest of the API, and lets the agent refuse any stream the wall has
not assigned — the stream budget is enforced here, not merely advised.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from .credentials import redact

log = logging.getLogger(__name__)

MJPEG_SUFFIX = "_mjpeg"
# One go2rtc MSE message holds a frame; a 4K keyframe is a few MiB at most.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def upstream_url(go2rtc_api_url: str, stream: str) -> str:
    base = go2rtc_api_url.rstrip("/")
    scheme_rest = base.split("://", 1)
    ws_base = ("wss://" if scheme_rest[0] == "https" else "ws://") + scheme_rest[-1]
    return f"{ws_base}/api/ws?src={quote(stream, safe='')}"


def allowed_stream(stream: str, active: set[str], mjpeg_enabled: bool) -> bool:
    """Only a stream the wall currently plays (or its MJPEG variant, when enabled)."""
    if stream.endswith(MJPEG_SUFFIX):
        return mjpeg_enabled and stream[: -len(MJPEG_SUFFIX)] in active
    return stream in active


async def relay(client: WebSocket, url: str) -> None:
    """Copy messages both ways until either side closes. `client` must be accepted."""
    try:
        async with connect(url, max_size=MAX_MESSAGE_BYTES, open_timeout=5,
                           ping_interval=None, compression=None) as upstream:
            to_upstream = asyncio.create_task(_client_to_upstream(client, upstream))
            to_client = asyncio.create_task(_upstream_to_client(upstream, client))
            done, pending = await asyncio.wait({to_upstream, to_client},
                                               return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if task.exception() and not isinstance(task.exception(), ConnectionClosed):
                    log.debug("media relay ended: %s", redact(str(task.exception())))
    except (OSError, TimeoutError, InvalidHandshake, InvalidURI) as exc:
        log.warning("media relay could not reach go2rtc: %s", exc.__class__.__name__)
        await _send_error(client, "video service unreachable")
    finally:
        try:
            await client.close()
        except (RuntimeError, WebSocketDisconnect):
            # Already closed by the browser. Starlette says so with RuntimeError or, since
            # 1.0, WebSocketDisconnect, which escaped here as a traceback in the log each
            # time a tile changed stream.
            pass


async def _client_to_upstream(client: WebSocket, upstream) -> None:
    while True:
        message = await client.receive()
        if message["type"] == "websocket.disconnect":
            return
        if message.get("text") is not None:
            await upstream.send(message["text"])
        elif message.get("bytes") is not None:
            await upstream.send(message["bytes"])


async def _upstream_to_client(upstream, client: WebSocket) -> None:
    async for data in upstream:
        if isinstance(data, str):
            # go2rtc's error messages quote the source URL, password included. The browser
            # scrubs them too, but it should never receive them in the first place.
            await client.send_text(redact(data))
        else:
            await client.send_bytes(data)


async def _send_error(client: WebSocket, text: str) -> None:
    try:
        await client.send_json({"type": "error", "value": text})
    except (RuntimeError, WebSocketDisconnect):
        pass

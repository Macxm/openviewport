"""The agent's video relay: who may connect, which streams, and what gets through."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from conftest import make_camera
from test_api import FakeAdapter
from test_go2rtc import FakeGo2rtc
from viewport.api import create_app
from viewport.config import AppConfig, AuthConfig, Go2rtcConfig, SourceConfig
from viewport.go2rtc import Go2rtcClient
from viewport.media import allowed_stream, relay, upstream_url
from viewport.runtime import Runtime

TOKEN = "relay-token-123"
PASSWORD = "Sup3r&secret!"


@contextmanager
def fake_go2rtc_ws():
    """A WebSocket server on a real port, answering like go2rtc's /api/ws."""
    seen: list[dict] = []
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state: dict = {}

    async def handler(conn):
        query = parse_qs(urlparse(conn.request.path).query)
        src = query.get("src", [""])[0]
        seen.append({"src": src})
        async for message in conn:
            seen.append({"src": src, "from_client": message})
            if src == "nvr_0_broken":
                await conn.send(json.dumps({"type": "error", "value":
                    f'mse: streams: Get "http://nvr/flv?user=viewer&password={PASSWORD}": EOF'}))
            else:
                await conn.send(json.dumps({"type": "mse", "value": 'video/mp4; codecs="avc1.640029"'}))
                await conn.send(b"\x00\x00\x00\x18ftypiso5")

    async def main():
        server = await serve(handler, "127.0.0.1", 0)
        state["port"] = server.sockets[0].getsockname()[1]
        state["server"] = server
        ready.set()
        await server.serve_forever()

    thread = threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True)
    thread.start()
    ready.wait(5)
    try:
        yield state["port"], seen
    finally:
        loop.call_soon_threadsafe(state["server"].close)
        thread.join(timeout=5)


def make_client(port: int, token: str = TOKEN, mjpeg: bool = False) -> TestClient:
    config = AppConfig(auth=AuthConfig(token=token),
                       go2rtc=Go2rtcConfig(api_url=f"http://127.0.0.1:{port}", mjpeg_fallback=mjpeg),
                       sources=[SourceConfig(id="nvr", host="nvr", username="viewer", password=PASSWORD)])
    rt = Runtime(config, adapters=[FakeAdapter([make_camera(0), make_camera(1)])],
                 go2rtc=Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(FakeGo2rtc().handler)))
    rt.wall.update_cameras("nvr", [make_camera(0), make_camera(1)])
    return TestClient(create_app(config, rt, start_background=False))


def closed_with(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_text()
    return exc.value.code


# ----- the pure rules ---------------------------------------------------------

def test_upstream_url_is_go2rtc_ws_with_the_stream_quoted():
    assert upstream_url("http://go2rtc:1984/", "nvr_0_sub") == "ws://go2rtc:1984/api/ws?src=nvr_0_sub"
    assert upstream_url("https://g:1/", "a b") == "wss://g:1/api/ws?src=a%20b"


def test_only_streams_on_the_wall_are_allowed():
    active = {"nvr_0_sub"}
    assert allowed_stream("nvr_0_sub", active, mjpeg_enabled=False)
    assert not allowed_stream("nvr_0_main", active, mjpeg_enabled=False)        # not assigned
    assert not allowed_stream("rtsp://attacker/x", active, mjpeg_enabled=False)  # go2rtc would dial it
    assert not allowed_stream("nvr_0_sub_mjpeg", active, mjpeg_enabled=False)
    assert allowed_stream("nvr_0_sub_mjpeg", active, mjpeg_enabled=True)


# ----- the WebSocket route ----------------------------------------------------

def test_relay_needs_the_token():
    with fake_go2rtc_ws() as (port, seen):
        with make_client(port).websocket_connect("/api/media/ws?src=nvr_0_sub") as ws:
            assert closed_with(ws) == 1008
        assert seen == []                              # go2rtc was never contacted


def test_relay_refuses_a_stream_the_wall_did_not_assign():
    """A client cannot open main streams on its own: the budget holds server-side."""
    with fake_go2rtc_ws() as (port, seen):
        with make_client(port).websocket_connect(f"/api/media/ws?src=nvr_0_main&token={TOKEN}") as ws:
            assert closed_with(ws) == 1008
        assert seen == []


def test_relay_refuses_arbitrary_sources():
    with fake_go2rtc_ws() as (port, seen):
        client = make_client(port)
        with client.websocket_connect(f"/api/media/ws?src=rtsp%3A%2F%2Fattacker%2Fx&token={TOKEN}") as ws:
            assert closed_with(ws) == 1008
        assert seen == []


def test_relay_copies_messages_both_ways():
    with fake_go2rtc_ws() as (port, seen):
        with make_client(port).websocket_connect(f"/api/media/ws?src=nvr_0_sub&token={TOKEN}") as ws:
            ws.send_text(json.dumps({"type": "mse", "value": "avc1.640029"}))
            assert json.loads(ws.receive_text())["type"] == "mse"
            assert ws.receive_bytes() == b"\x00\x00\x00\x18ftypiso5"
    assert {"src": "nvr_0_sub", "from_client": '{"type": "mse", "value": "avc1.640029"}'} in seen


def test_relay_scrubs_credentials_from_go2rtc_errors():
    with fake_go2rtc_ws() as (port, _):
        client = make_client(port)
        client.app.state.runtime.wall.active_streams = lambda: {"nvr_0_broken"}
        with client.websocket_connect(f"/api/media/ws?src=nvr_0_broken&token={TOKEN}") as ws:
            ws.send_text(json.dumps({"type": "mse", "value": "avc1.640029"}))
            text = ws.receive_text()
    assert "error" in text
    assert PASSWORD not in text and "Sup3r" not in text


def test_relay_reports_an_unreachable_go2rtc_without_details():
    with make_client(port=1).websocket_connect(f"/api/media/ws?src=nvr_0_sub&token={TOKEN}") as ws:
        message = json.loads(ws.receive_text())
    assert message == {"type": "error", "value": "video service unreachable"}


class GoneBrowser:
    """A player that closed its socket first, as one does whenever a tile changes stream."""

    async def receive(self):
        return {"type": "websocket.disconnect"}

    async def _gone(self, *_args, **_kwargs):
        raise WebSocketDisconnect(code=1006)

    send_bytes = send_text = send_json = close = _gone


async def test_a_browser_that_already_left_is_not_an_error():
    """Starlette 1.x reports writing to a closed socket as WebSocketDisconnect, which used to
    escape the relay and print a traceback for every stream change."""
    with fake_go2rtc_ws() as (port, _):
        await relay(GoneBrowser(), f"ws://127.0.0.1:{port}/api/ws?src=nvr_0_sub")
    await relay(GoneBrowser(), "ws://127.0.0.1:1/api/ws?src=nvr_0_sub")    # go2rtc down too


def test_relay_is_open_when_auth_is_off():
    with fake_go2rtc_ws() as (port, _):
        with make_client(port, token="").websocket_connect("/api/media/ws?src=nvr_0_sub") as ws:
            ws.send_text(json.dumps({"type": "mse", "value": "avc1.640029"}))
            assert json.loads(ws.receive_text())["type"] == "mse"


def test_snapshot_tells_renderers_to_use_the_relay_by_default():
    with fake_go2rtc_ws() as (port, _):
        snapshot = make_client(port).get("/api/wall", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert snapshot["player"]["transport"] == "proxy"

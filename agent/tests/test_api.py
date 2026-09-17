from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from conftest import make_camera
from test_go2rtc import FakeGo2rtc
from viewport.adapters.base import SourceAdapter
from viewport.api import create_app
from viewport.config import AppConfig
from viewport.go2rtc import Go2rtcClient
from viewport.runtime import Runtime


class FakeAdapter(SourceAdapter):
    def __init__(self, cameras):
        self.id = "nvr"
        self.cameras = cameras
        self.closed = False

    async def discover(self):
        return self.cameras

    def stream_source(self, camera, quality):
        return f"rtsp://nvr/{camera.channel}/{quality}"

    async def close(self):
        self.closed = True


def build(views=None, mjpeg=False):
    config = AppConfig.model_validate({
        "go2rtc": {"mjpeg_fallback": mjpeg},
        "sources": [{"id": "nvr", "host": "nvr", "username": "u"}],
        **({"views": views} if views else {}),
    })
    fake = FakeGo2rtc()
    adapter = FakeAdapter([make_camera(i) for i in range(3)] + [make_camera(3, online=False)])
    runtime = Runtime(config, adapters=[adapter],
                      go2rtc=Go2rtcClient("http://go2rtc", transport=httpx.MockTransport(fake.handler)))
    app = create_app(config, runtime, start_background=False)
    return app, runtime, fake, adapter


def refresh(client, runtime):
    client.post("/api/sources/refresh")
    client.portal.call(runtime.registry.sync)


def test_wall_flow():
    app, runtime, fake, adapter = build()
    with TestClient(app) as client:
        refresh(client, runtime)
        wall = client.get("/api/wall").json()
        assert wall["layout"]["id"] == "2x2"
        assert [t["stream"] for t in wall["tiles"]] == ["nvr_0_sub", "nvr_1_sub", "nvr_2_sub", None]
        assert wall["tiles"][3]["reason"] == "offline"
        assert set(fake.streams) >= {"nvr_0_main", "nvr_0_sub", "nvr_3_sub"}

        wall = client.post("/api/wall/fullscreen", json={"tile": "t1"}).json()
        assert wall["fullscreen"] == "t1"
        assert wall["tiles"][1]["stream"] == "nvr_1_main"
        assert wall["budget"]["main_in_use"] == 1
        assert client.post("/api/wall/fullscreen", json={"tile": "nope"}).status_code == 404

        wall = client.post("/api/wall/fullscreen", json={"tile": None}).json()
        assert wall["fullscreen"] is None

        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert health["sources"][0]["cameras"] == 4

        streams = client.get("/api/streams").json()
        assert "nvr_0_sub" in streams["active"]
        assert "static_cam" not in streams["go2rtc"]
        assert client.get("/").status_code == 200
        assert client.get("/static/wall.js").status_code == 200
    assert adapter.closed


def test_websocket_commands():
    app, runtime, _, _ = build(views=[{"name": "Grid"}, {"name": "Feature", "layout": "1+5"}])
    with TestClient(app) as client:
        refresh(client, runtime)
        with client.websocket_connect("/api/wall/ws?renderer=test") as ws:
            first = ws.receive_json()
            assert first["type"] == "wall" and first["view"]["name"] == "Grid"

            ws.send_json({"type": "fullscreen", "tile": "t0"})
            snap = ws.receive_json()
            assert snap["fullscreen"] == "t0" and snap["tiles"][0]["quality"] == "main"

            ws.send_json({"type": "view", "step": 1})
            snap = ws.receive_json()
            assert snap["view"]["name"] == "Feature" and snap["fullscreen"] is None
            assert snap["layout"]["id"] == "1+5"
            assert snap["tiles"][0]["quality"] == "main"          # big tile covers 4/9 of the screen

            ws.send_json({"type": "stats", "tiles": [{"tile": "t0", "state": "playing"}]})
            ws.send_json({"type": "bogus"})
            ws.send_text("not json")
            ws.send_json({"type": "view", "index": 0})
            snap = ws.receive_json()
            assert snap["view"]["index"] == 0
        assert "test" in client.get("/api/health").json()["renderers"]


def test_mjpeg_fallback_registers_extra_streams():
    app, runtime, fake, _ = build(mjpeg=True)
    with TestClient(app) as client:
        refresh(client, runtime)
        assert fake.streams["nvr_0_sub_mjpeg"]["producers"][0]["url"] == "ffmpeg:nvr_0_sub#video=mjpeg"
        assert client.get("/api/wall").json()["player"]["mjpeg_fallback"] is True


def test_discovery_failure_marks_degraded():
    app, runtime, _, adapter = build()

    async def broken():
        raise httpx.ConnectError("no route to host")

    adapter.discover = broken
    with TestClient(app) as client:
        health = client.post("/api/sources/refresh").json()
        assert health["status"] == "degraded"
        assert health["sources"][0]["reachable"] is False
        assert "no route" in health["sources"][0]["last_error"]
        assert runtime.sources["nvr"].next_delay() < 10

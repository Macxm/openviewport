"""Token authentication on /api/* and the wall WebSocket."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import make_camera
from test_api import FakeAdapter
from test_go2rtc import FakeGo2rtc
from viewport.api import bearer_token, create_app, token_matches
from viewport.config import AppConfig, AuthConfig
from viewport.go2rtc import Go2rtcClient
from viewport.runtime import Runtime

TOKEN = "s3cret-token"
PROTECTED = ["/api/health", "/api/cameras", "/api/wall", "/api/config"]


def make_client(token: str = TOKEN) -> TestClient:
    config = AppConfig(auth=AuthConfig(token=token))
    fake = FakeGo2rtc()
    rt = Runtime(config, adapters=[FakeAdapter([make_camera(0)])],
                 go2rtc=Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(fake.handler)))
    rt.wall.update_cameras("nvr", [make_camera(0)])
    return TestClient(create_app(config, rt, start_background=False))


# ----- the token helpers ----------------------------------------------------

def test_bearer_header_is_preferred_and_query_is_the_fallback():
    assert bearer_token({"authorization": "Bearer abc"}, {}) == "abc"
    assert bearer_token({"authorization": "bearer abc"}, {}) == "abc"     # case-insensitive
    assert bearer_token({}, {"token": "abc"}) == "abc"
    assert bearer_token({}, {}) is None
    assert bearer_token({"authorization": "Basic abc"}, {"token": "q"}) == "q"


def test_blank_configured_token_disables_authentication():
    assert token_matches("", None) is True
    assert token_matches("", "anything") is True


def test_token_must_match_exactly():
    assert token_matches(TOKEN, TOKEN) is True
    assert token_matches(TOKEN, TOKEN + "x") is False
    assert token_matches(TOKEN, TOKEN[:-1]) is False
    assert token_matches(TOKEN, None) is False
    assert token_matches(TOKEN, "") is False


# ----- REST -----------------------------------------------------------------

@pytest.mark.parametrize("path", PROTECTED)
def test_api_rejects_a_missing_token(path):
    resp = make_client().get(path)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("path", PROTECTED)
def test_api_accepts_a_bearer_header(path):
    assert make_client().get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


@pytest.mark.parametrize("path", PROTECTED)
def test_api_accepts_a_query_parameter(path):
    assert make_client().get(path, params={"token": TOKEN}).status_code == 200


def test_api_rejects_a_wrong_token():
    assert make_client().get("/api/wall", params={"token": "nope"}).status_code == 401


def test_post_routes_are_protected_too():
    client = make_client()
    assert client.post("/api/wall/fullscreen", json={"tile": None}).status_code == 401
    assert client.post("/api/wall/fullscreen", json={"tile": None},
                       headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_everything_is_open_when_no_token_is_configured():
    client = make_client(token="")
    for path in PROTECTED:
        assert client.get(path).status_code == 200


# ----- the wall page and its assets stay reachable ---------------------------

def test_page_and_static_files_need_no_token():
    """The kiosk loads the page first and reads the token out of its own URL."""
    client = make_client()
    assert client.get("/").status_code == 200
    assert client.get("/static/wall.js").status_code == 200
    assert client.get("/static/player.js").status_code == 200


# ----- WebSocket ------------------------------------------------------------

def test_websocket_needs_a_token():
    """Closed with 1008 rather than refused, so the browser can tell why."""
    client = make_client()
    with client.websocket_connect("/api/wall/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 1008


def test_websocket_rejects_a_wrong_token():
    client = make_client()
    with client.websocket_connect("/api/wall/ws?token=nope") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 1008


def test_websocket_accepts_a_query_token():
    client = make_client()
    with client.websocket_connect(f"/api/wall/ws?token={TOKEN}") as ws:
        assert ws.receive_json()["type"] == "wall"


def test_websocket_is_open_when_no_token_is_configured():
    client = make_client(token="")
    with client.websocket_connect("/api/wall/ws") as ws:
        assert ws.receive_json()["type"] == "wall"


def test_admin_edit_routes_are_protected():
    client = make_client()
    body = {"views": [{"name": "x", "layout": "auto", "cameras": "all"}]}
    assert client.put("/api/config/views", json=body).status_code == 401
    assert client.put("/api/config/sources/nvr", json={"protocol": "rtsp"}).status_code == 401


def test_admin_page_itself_needs_no_token():
    """Same as the wall: the page is code, and reads its token from its own URL."""
    client = make_client()
    assert client.get("/admin").status_code == 200
    assert client.get("/static/admin.js").status_code == 200


def test_healthz_is_open_but_says_nothing_useful():
    """The container healthcheck cannot carry a token; /api/health still needs one."""
    client = make_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert set(resp.json()) == {"status"}          # no camera names, no error text
    assert client.get("/api/health").status_code == 401

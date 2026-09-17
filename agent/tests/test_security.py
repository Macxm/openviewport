"""The HTTP defences in security.py: headers, cross-site requests, and DNS rebinding."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import make_camera
from test_api import FakeAdapter
from test_go2rtc import FakeGo2rtc
from viewport.api import create_app
from viewport.config import AppConfig, AuthConfig, Go2rtcConfig, SourceConfig
from viewport.go2rtc import Go2rtcClient
from viewport.runtime import Runtime
from viewport.security import content_security_policy, from_another_site, host_allowed

from starlette.datastructures import Headers


def make_client(token="", allowed_hosts=(), go2rtc=None, base_url="http://viewport.local:8080"):
    config = AppConfig(auth=AuthConfig(token=token, allowed_hosts=list(allowed_hosts)),
                       go2rtc=go2rtc or Go2rtcConfig(),
                       sources=[SourceConfig(id="nvr", host="nvr", username="u", password="p")])
    rt = Runtime(config, adapters=[FakeAdapter([make_camera(0)])],
                 go2rtc=Go2rtcClient("http://g", transport=httpx.MockTransport(FakeGo2rtc().handler)))
    rt.wall.update_cameras("nvr", [make_camera(0)])
    return TestClient(create_app(config, rt, start_background=False), base_url=base_url)


# ----- headers ----------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/admin", "/static/admin.js", "/api/health"])
def test_every_response_carries_the_security_headers(path):
    resp = make_client().get(path)
    assert resp.status_code == 200
    policy = resp.headers["content-security-policy"]
    assert "script-src 'self'" in policy and "frame-ancestors 'none'" in policy
    assert "object-src 'none'" in policy and "base-uri 'none'" in policy
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"       # the wall's URL may hold the token


def test_api_answers_are_never_cached_but_pages_keep_their_own_rules():
    client = make_client()
    assert client.get("/api/config").headers["cache-control"] == "no-store"
    assert client.get("/static/admin.css").headers["cache-control"] == "no-cache"


def test_the_policy_lets_the_player_reach_this_origin_and_nothing_else():
    policy = make_client().get("/").headers["content-security-policy"]
    assert "connect-src 'self' ws://viewport.local:8080 wss://viewport.local:8080;" in policy
    assert "media-src 'self' blob:" in policy


def test_direct_playback_allows_go2rtc_on_the_same_host():
    client = make_client(go2rtc=Go2rtcConfig(player_transport="direct"))
    assert "ws://viewport.local:1984" in client.get("/").headers["content-security-policy"]


def test_a_host_header_cannot_write_into_the_policy():
    policy = content_security_policy("evil; script-src *", ["ws://{host}:1984"])
    assert "script-src *" not in policy and "evil" not in policy


# ----- requests from another site -----------------------------------------------------

def test_a_page_on_another_site_cannot_change_anything():
    client = make_client()
    resp = client.post("/api/wall/view/next", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403


@pytest.mark.parametrize("headers", [{"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"},
                                     {"Origin": "http://viewport.local:8080"}, {}])
def test_the_page_itself_and_non_browser_clients_can(headers):
    assert make_client().post("/api/wall/view/next", headers=headers).status_code == 200


@pytest.mark.parametrize("origin", ["http://evil.example", "null"])
def test_without_fetch_metadata_the_origin_must_match(origin):
    assert make_client().post("/api/wall/view/next", headers={"Origin": origin}).status_code == 403


def test_a_cross_site_page_may_still_link_to_the_wall():
    """Only changing requests are checked: following a link is a cross-site GET."""
    assert make_client().get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_another_site_cannot_open_the_video_relay_or_the_wall_socket():
    """No CORS rule covers WebSockets: this is the only thing stopping a web page watching
    the cameras when no token is set."""
    client = make_client()
    for path in ("/api/wall/ws", "/api/media/ws?src=nvr_0_sub"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path, headers={"Origin": "http://evil.example"}) as ws:
                ws.receive_text()
    # Starlette's test client always opens WebSockets to "testserver", whatever its base URL.
    with client.websocket_connect("/api/wall/ws", headers={"Origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] == "wall"


def test_a_reverse_proxy_that_rewrites_host_still_matches_the_forwarded_one():
    headers = Headers({"host": "agent:8080", "x-forwarded-host": "cams.example.com",
                       "origin": "https://cams.example.com"})
    assert not from_another_site({"type": "http"}, headers)


# ----- DNS rebinding --------------------------------------------------------------------

@pytest.mark.parametrize("host", ["192.168.1.20:8080", "[fe80::1]:8080", "localhost:8080", "viewport",
                                  "viewport.local", "wall.home.arpa:8080", "pi.lan", "agent:8080"])
def test_the_names_a_lan_device_is_reached_by_are_accepted(host):
    assert host_allowed(host)


@pytest.mark.parametrize("host", ["attacker.example.com", "192.168.1.20.evil.com", "", "cams.example.com:443"])
def test_a_public_name_is_refused_unless_listed(host):
    assert not host_allowed(host)


def test_listed_names_and_their_subdomains_are_accepted():
    assert host_allowed("cams.example.com", ["cams.example.com"])
    assert host_allowed("tv.cams.example.com:8443", ["*.cams.example.com"])
    assert not host_allowed("example.com.evil.net", ["*.example.com"])


def test_a_rebound_domain_gets_nothing():
    client = make_client(base_url="http://attacker.example.com")
    for path in ("/", "/api/config", "/api/health"):
        assert client.get(path).status_code == 400
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/media/ws?src=nvr_0_sub") as ws:
            ws.receive_text()


def test_allowed_hosts_can_come_from_one_environment_variable():
    assert AuthConfig(allowed_hosts="cams.example.com, *.lan.example").allowed_hosts == [
        "cams.example.com", "*.lan.example"]


# ----- the session cookie ---------------------------------------------------------------

def test_the_session_cookie_is_secure_behind_an_https_proxy(tmp_path):
    from test_admin_auth import make_client as admin_client, PASSWORD
    client = admin_client(tmp_path)
    plain = client.post("/api/admin/login", json={"username": "admin", "password": PASSWORD})
    assert "secure" not in plain.headers["set-cookie"].lower()
    proxied = client.post("/api/admin/login", json={"username": "admin", "password": PASSWORD},
                          headers={"X-Forwarded-Proto": "https"})
    assert "secure" in proxied.headers["set-cookie"].lower()

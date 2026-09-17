"""Secret handling: NAME_FILE secrets, and credentials kept out of logs and responses."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_camera
from test_api import FakeAdapter
from test_go2rtc import FakeGo2rtc
from viewport.adapters.reolink import ReolinkAdapter, ReolinkClient
from viewport.api import create_app
from viewport.config import AppConfig, AuthConfig, SourceConfig, expand_env
from viewport.credentials import env_value, redact, register_secret, safe_error
from viewport.go2rtc import Go2rtcClient, StreamRegistry
from viewport.runtime import Runtime

HERE = Path(__file__).parent
PASSWORD = "Zq9&!x@y:p/w%rd"          # every character that needs escaping somewhere
TOKEN = "tok_7f3a9c1e5b2d"


def encodings(secret: str) -> list[str]:
    once = quote(secret, safe="")
    return [secret, once, quote(once, safe=""), quote_plus(secret), json.dumps(secret)[1:-1]]


def assert_no_secret(text: str, *secrets: str) -> None:
    for secret in secrets:
        for form in encodings(secret):
            assert form not in text, f"secret leaked as {form!r} in: {text[:300]}"


# ----- redact(): the shared cases, and registered secrets ---------------------

@pytest.mark.parametrize("case", json.loads((HERE / "redaction_cases.json").read_text()),
                         ids=lambda c: c["name"])
def test_redaction_cases_shared_with_the_browser(case):
    assert redact(case["input"]) == case["expected"]


def test_a_registered_secret_is_scrubbed_in_every_encoding():
    register_secret(PASSWORD)
    for form in encodings(PASSWORD):
        assert redact(f"before {form} after") == "before *** after"


def test_registered_secret_catches_what_no_pattern_can():
    """A raw '&' inside a query value is a separator to any parser; only the value knows."""
    register_secret("k7&!mQw3vX9pLr")
    assert redact("https://nvr/flv?password=k7&!mQw3vX9pLr") == "https://nvr/flv?password=***"


def test_short_values_are_not_registered():
    register_secret("abc")
    assert redact("abc abcd") == "abc abcd"


# ----- safe_error() -----------------------------------------------------------

def test_http_status_errors_lose_the_query_string():
    request = httpx.Request("PATCH", f"http://go2rtc:1984/api/streams?name=x&src=rtsp://u:{quote(PASSWORD)}@nvr/x")
    exc = httpx.HTTPStatusError("boom", request=request, response=httpx.Response(500, request=request))
    assert safe_error(exc) == "HTTP 500 from PATCH /api/streams"


def test_other_errors_keep_their_first_line_only():
    exc = httpx.ConnectError("All connection attempts failed\nFor more information check: https://x")
    assert safe_error(exc) == "All connection attempts failed"


def test_errors_without_a_message_are_named():
    assert safe_error(ValueError()) == "ValueError"


# ----- NAME_FILE secrets ------------------------------------------------------

def test_env_value_reads_a_file(tmp_path, monkeypatch):
    secret = tmp_path / "pw"
    secret.write_text(" pass word \n")                 # trailing newline dropped, spaces kept
    monkeypatch.delenv("NVR_PASSWORD", raising=False)
    monkeypatch.setenv("NVR_PASSWORD_FILE", str(secret))
    assert env_value("NVR_PASSWORD") == " pass word "


def test_the_variable_itself_wins_over_the_file(tmp_path, monkeypatch):
    (tmp_path / "pw").write_text("from-file")
    monkeypatch.setenv("NVR_PASSWORD", "from-env")
    monkeypatch.setenv("NVR_PASSWORD_FILE", str(tmp_path / "pw"))
    assert env_value("NVR_PASSWORD") == "from-env"


def test_an_unreadable_secret_file_fails_loudly(monkeypatch):
    monkeypatch.delenv("NVR_PASSWORD", raising=False)
    monkeypatch.setenv("NVR_PASSWORD_FILE", "/nonexistent/secret")
    with pytest.raises(RuntimeError, match="NVR_PASSWORD_FILE"):
        env_value("NVR_PASSWORD")


def test_config_placeholders_accept_file_secrets(tmp_path, monkeypatch):
    (tmp_path / "token").write_text(TOKEN + "\n")
    monkeypatch.delenv("VIEWPORT_API_TOKEN", raising=False)
    monkeypatch.setenv("VIEWPORT_API_TOKEN_FILE", str(tmp_path / "token"))
    assert expand_env({"auth": {"token": "${VIEWPORT_API_TOKEN:-}"}}) == {"auth": {"token": TOKEN}}


# ----- regressions for the three leaks found during T7 ------------------------

async def test_a_failed_go2rtc_patch_does_not_leak_the_camera_password():
    """httpx quoted the PATCH URL, whose src= held the password percent-encoded."""
    def go2rtc_500(request):
        return httpx.Response(200, json={}) if request.method == "GET" else httpx.Response(500)

    registry = StreamRegistry(Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(go2rtc_500)), ["nvr"])
    registry.set_desired({"nvr_0_main": f"rtsp://viewer:{quote(PASSWORD, safe='')}@nvr:554/x"})
    assert not await registry.sync()
    assert registry.last_error == "HTTP 500 from PATCH /api/streams"


async def test_a_failed_nvr_request_does_not_leak_the_session_token():
    def nvr_502(request):
        return httpx.Response(502)

    config = AppConfig(sources=[SourceConfig(id="nvr", host="nvr", username="u", password=PASSWORD)])
    adapter = ReolinkAdapter(config.sources[0], transport=httpx.MockTransport(nvr_502))
    adapter.client._token, adapter.client._token_expires = TOKEN, float("inf")
    rt = Runtime(config, adapters=[adapter],
                 go2rtc=Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(FakeGo2rtc().handler)))
    await rt.refresh_source(rt.sources["nvr"])
    error = rt.sources["nvr"].last_error
    assert error == "HTTP 502 from POST /cgi-bin/api.cgi"
    assert TOKEN not in json.dumps(rt.health())


def test_player_errors_from_go2rtc_are_scrubbed_before_storing_or_logging(caplog):
    """go2rtc sends the source URL, password included, in its WebSocket error."""
    config = AppConfig(sources=[SourceConfig(id="nvr", host="nvr", username="u", password=PASSWORD)])
    rt = Runtime(config, adapters=[FakeAdapter([make_camera(0)])],
                 go2rtc=Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(FakeGo2rtc().handler)))
    rt.wall.update_cameras("nvr", [make_camera(0)])
    rt.registry.reachable = True
    go2rtc_error = (f'mse: streams: Get "http://nvr/flv?user=u&password={quote(PASSWORD, safe="")}": '
                    "dial tcp: connection refused")
    with caplog.at_level(logging.DEBUG):
        for _ in range(5):
            rt.record_renderer_stats("tv", {"tiles": [
                {"tile": "t0", "stream": "nvr_0_sub", "state": "error", "error": go2rtc_error}]})
    assert_no_secret(json.dumps(rt.health()), PASSWORD)
    assert_no_secret(caplog.text, PASSWORD)


# ----- a sweep of every response, with every error path triggered --------------

@pytest.fixture
def log_redaction():
    """What __main__ installs, undone afterwards so other tests see plain logging."""
    from viewport.credentials import install_log_redaction
    previous = logging.getLogRecordFactory()
    install_log_redaction()
    yield
    logging.setLogRecordFactory(previous)


def test_no_endpoint_or_log_line_ever_contains_a_secret(caplog, log_redaction):
    source = SourceConfig(id="nvr", host="nvr.local", username="viewer", password=PASSWORD)
    config = AppConfig(auth=AuthConfig(token=TOKEN), sources=[source])

    def broken_go2rtc(request):
        return httpx.Response(200, json={}) if request.method == "GET" else httpx.Response(500)

    adapter = ReolinkAdapter(source)
    rt = Runtime(config, adapters=[adapter],
                 go2rtc=Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(broken_go2rtc)))
    rt.wall.update_cameras("nvr", [make_camera(0), make_camera(1)])
    rt._update_desired_streams()
    rt.registry.reachable = True

    headers = {"Authorization": f"Bearer {TOKEN}"}
    with caplog.at_level(logging.DEBUG), TestClient(create_app(config, rt, start_background=False)) as client:
        import asyncio
        asyncio.run(rt.registry.sync())                                   # fails: 500 on PATCH
        rt.record_renderer_stats("tv", {"tiles": [{"tile": "t0", "stream": "nvr_0_sub", "state": "error",
                                                   "error": f"Get \"{adapter.stream_source(make_camera(0), 'sub')}\""}]})
        bodies = [client.get(path, headers=headers).text
                  for path in ("/api/health", "/api/cameras", "/api/wall", "/api/streams", "/api/config", "/healthz")]
        with client.websocket_connect(f"/api/wall/ws?token={TOKEN}") as ws:
            bodies.append(json.dumps(ws.receive_json()))

    for body in bodies:
        assert_no_secret(body, PASSWORD)
    assert_no_secret(caplog.text, PASSWORD, TOKEN)


# ----- the go2rtc log filter --------------------------------------------------

def test_credentials_module_filters_a_log_stream():
    module = Path(__file__).parents[1] / "viewport" / "credentials.py"
    lines = ('ERR rtmp.go:166 > error="streams: Get \\"https://nvr/flv?user=u&password=hunter22\\": EOF"\n'
             "INF go2rtc started\n")
    result = subprocess.run([sys.executable, "-u", str(module)], input=lines,
                            capture_output=True, text=True, timeout=10, check=True)
    assert "hunter22" not in result.stdout
    assert "password=***" in result.stdout
    assert "INF go2rtc started" in result.stdout


def test_log_redaction_covers_loggers_we_do_not_own(caplog, log_redaction):
    """httpx logs each request URL; the go2rtc src= parameter carries the password."""
    register_secret(PASSWORD)
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info("HTTP Request: PATCH %s", f"http://go2rtc/api?src={quote(quote(PASSWORD, safe=''), safe='')}")
    assert_no_secret(caplog.text, PASSWORD)


def test_redacting_formatter_scrubs_tracebacks():
    register_secret(PASSWORD)
    try:
        raise RuntimeError(f"failed for rtsp://u:{PASSWORD}@nvr/x")
    except RuntimeError:
        record = logging.getLogger("t").makeRecord("t", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    from viewport.credentials import RedactingFormatter
    assert_no_secret(RedactingFormatter().format(record), PASSWORD)

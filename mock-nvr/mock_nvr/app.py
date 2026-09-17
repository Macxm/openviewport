"""HTTP side of the mock NVR: a subset of the Reolink CGI API plus HTTP-FLV live streams.

Only the commands the viewport agent uses are implemented. Response shapes follow
Reolink's API as used by open-source clients; error codes are approximate.
"""

from __future__ import annotations

import asyncio
import html
import re
import secrets
import time
from collections import Counter
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from .settings import Settings

LEASE_SECONDS = 3600
FLV_STREAM = re.compile(r"^channel(\d{1,2})_(main|sub)\.bcs$")


def _error(cmd: str, rsp_code: int, detail: str) -> dict[str, Any]:
    return {"cmd": cmd, "code": 1, "error": {"detail": detail, "rspCode": rsp_code}}


def _ok(cmd: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"cmd": cmd, "code": 0, "value": value}


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Mock Reolink NVR", docs_url=None, redoc_url=None)
    tokens: dict[str, float] = {}
    active: Counter[tuple[int, str]] = Counter()
    stats: Counter[str] = Counter()
    lock = asyncio.Lock()
    go2rtc = f"http://127.0.0.1:{settings.go2rtc_api_port}"
    # (channel, Reolink AI key or "motion") -> monotonic time the detection ends.
    detections: dict[tuple[int, str], float] = {}

    def detecting(channel: int, kind: str) -> bool:
        return detections.get((channel, kind), 0.0) > time.monotonic()

    def valid_token(token: str | None) -> bool:
        now = time.monotonic()
        for t, exp in list(tokens.items()):
            if exp < now:
                tokens.pop(t, None)
        return token is not None and token in tokens

    def enc_value(channel: int) -> dict[str, Any]:
        def stream(quality: str) -> dict[str, Any]:
            p = settings.profile(quality)
            out = {
                # gop is the I-frame interval in seconds (a multiple of the frame rate): the
                # NVS8 on firmware 3.4.0.318 ships with 2 for main streams and 4 for sub.
                "bitRate": p.bitrate_kbps, "frameRate": p.fps, "gop": 2 if quality == "main" else 4,
                "height": p.height, "width": p.width, "profile": "High",
                "size": f"{p.width}*{p.height}",
            }
            # Real firmware (NVS8 3.4.0.318) reports vType for the main stream only.
            if quality == "main":
                out["vType"] = p.codec
            return out
        return {"Enc": {"audio": 0, "channel": channel,
                        "mainStream": stream("main"), "subStream": stream("sub")}}

    def run_command(item: dict[str, Any], token: str | None) -> dict[str, Any]:
        cmd = str(item.get("cmd", ""))
        param = item.get("param") or {}
        stats[f"cmd:{cmd}"] += 1
        if cmd == "Login":
            user = (param.get("User") or {})
            if user.get("userName") != settings.username or user.get("password") != settings.password:
                return _error(cmd, -7, "login failed")
            new = secrets.token_hex(8)
            tokens[new] = time.monotonic() + LEASE_SECONDS
            stats["logins"] += 1
            return _ok(cmd, {"Token": {"leaseTime": LEASE_SECONDS, "name": new}})
        if not valid_token(token):
            return _error(cmd, -6, "please login first")
        if cmd == "Logout":
            tokens.pop(token, None)
            return _ok(cmd, {"rspCode": 200})
        if cmd == "GetDevInfo":
            return _ok(cmd, {"DevInfo": {
                "channelNum": settings.channels, "model": settings.model, "name": "Mock NVR",
                "type": "NVR", "exactType": "NVR", "firmVer": "mock-0.1", "hardVer": "MOCK",
                "serial": "MOCK0000000000", "diskNum": 1, "audioNum": 0, "wifi": 0,
            }})
        if cmd == "GetChannelstatus":
            # Only these three keys: real firmware sends no typeInfo, sleep or uid.
            return _ok(cmd, {"count": settings.channels, "status": [
                {"channel": ch, "name": settings.channel_name(ch),
                 "online": 0 if settings.is_down(ch) else 1}
                for ch in range(settings.channels)
            ]})
        if cmd == "GetEnc":
            channel = int(param.get("channel", 0))
            if not 0 <= channel < settings.channels:
                return _error(cmd, -4, "param error")
            if settings.is_down(channel):
                return _error(cmd, -99, "device offline")
            return _ok(cmd, enc_value(channel))
        if cmd in ("GetMdState", "GetAiState"):
            channel = int(param.get("channel", 0))
            if not 0 <= channel < settings.channels:
                return _error(cmd, -4, "param error")
            if settings.is_down(channel):
                return _error(cmd, -99, "device offline")
            ai_keys = ("dog_cat", "face", "people", "vehicle")
            if cmd == "GetMdState":
                # A camera that sees a person has seen motion too.
                moving = detecting(channel, "motion") or any(detecting(channel, k) for k in ai_keys)
                return _ok(cmd, {"state": int(moving)})
            # Face is unsupported, as on the real NVS8's cameras.
            return _ok(cmd, {"channel": channel, **{
                key: {"alarm_state": int(detecting(channel, key)), "support": int(key != "face")}
                for key in ai_keys}})
        if cmd == "GetNetPort":
            return _ok(cmd, {"NetPort": {
                "httpEnable": int(settings.http_enabled), "httpPort": settings.http_port,
                "httpsEnable": 1, "httpsPort": 443,
                "mediaPort": 9000, "onvifEnable": 1, "onvifPort": 8000,
                "rtmpEnable": 1, "rtmpPort": 1935,
                "rtspEnable": 1, "rtspPort": settings.rtsp_port,
            }})
        return _error(cmd, -9, "not support")

    @app.post("/cgi-bin/api.cgi")
    async def api_cgi(request: Request) -> JSONResponse:
        token = request.query_params.get("token")
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse([_error(request.query_params.get("cmd", ""), -1, "bad json")])
        if not isinstance(body, list):
            body = [body]
        return JSONResponse([run_command(item, token) for item in body])

    @app.get("/flv")
    async def flv(request: Request):
        q = request.query_params
        token_ok = valid_token(q.get("token"))
        creds_ok = q.get("user") == settings.username and q.get("password") == settings.password
        if not (token_ok or creds_ok):
            return PlainTextResponse("unauthorized", status_code=401)
        m = FLV_STREAM.match(q.get("stream", ""))
        if not m or q.get("app") != "bcs":
            return PlainTextResponse("unknown stream", status_code=404)
        channel, quality = int(m.group(1)), m.group(2)
        if channel >= settings.channels or settings.is_down(channel):
            return PlainTextResponse("channel offline", status_code=404)
        if settings.profile(quality).codec == "h265":
            return PlainTextResponse("H.265 is not served over FLV by this mock; use RTSP", status_code=415)
        limit = settings.max_main_per_channel if quality == "main" else settings.max_sub_per_channel
        async with lock:
            if active[(channel, quality)] >= limit:
                stats["rejected_over_limit"] += 1
                return PlainTextResponse(f"too many {quality} viewers on channel {channel}", status_code=503)
            active[(channel, quality)] += 1
        src = settings.rtsp_path(channel, quality)

        async def body():
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=30.0)) as client:
                    async with client.stream("GET", f"{go2rtc}/api/stream.flv", params={"src": src}) as r:
                        async for chunk in r.aiter_raw():
                            yield chunk
            finally:
                async with lock:
                    active[(channel, quality)] -= 1

        return StreamingResponse(body(), media_type="video/x-flv")

    @app.post("/mock/detect")
    async def mock_detect(channel: int, kind: str = "people", seconds: float = 10.0) -> dict[str, Any]:
        """Pretend a camera sees something: kind is people, vehicle, dog_cat or motion."""
        if kind not in ("people", "vehicle", "dog_cat", "motion"):
            return JSONResponse({"error": "kind must be people, vehicle, dog_cat or motion"}, status_code=400)
        if not 0 <= channel < settings.channels:
            return JSONResponse({"error": "no such channel"}, status_code=404)
        detections[(channel, kind)] = time.monotonic() + max(0.0, min(seconds, 3600.0))
        stats[f"detect:{kind}"] += 1
        return {"channel": channel, "kind": kind, "seconds": seconds}

    @app.get("/mock/stats")
    async def mock_stats() -> dict[str, Any]:
        valid_token(None)  # expire old tokens
        return {
            "active_tokens": len(tokens),
            "flv_viewers": {f"ch{c}_{q}": n for (c, q), n in sorted(active.items()) if n},
            "limits": {"main_per_channel": settings.max_main_per_channel,
                       "sub_per_channel": settings.max_sub_per_channel},
            "counters": dict(stats),
        }

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> str:
        host = request.url.hostname or "localhost"
        rows = []
        for ch in range(settings.channels):
            state = "empty slot" if ch in settings.empty else "offline" if ch in settings.offline else "online"
            rows.append(
                f"<tr><td>{ch + 1}</td><td>{html.escape(settings.channel_name(ch))}</td><td>{state}</td>"
                f"<td><code>rtsp://USER:PASS@{host}:RTSP_PORT/{settings.rtsp_path(ch, 'main')}</code></td>"
                f"<td><code>/flv?port=1935&amp;app=bcs&amp;stream=channel{ch}_sub.bcs&amp;user=USER&amp;password=PASS</code></td></tr>"
            )
        return (
            "<!doctype html><meta charset=utf-8><title>Mock Reolink NVR</title>"
            "<style>body{font:14px system-ui;margin:24px}td,th{padding:4px 10px;text-align:left}"
            "code{font-size:12px}</style>"
            f"<h1>Mock Reolink NVR</h1><p>Model {html.escape(settings.model)} · {settings.channels} channels · "
            f"main {settings.main.width}x{settings.main.height} {settings.main.codec} · "
            f"sub {settings.sub.width}x{settings.sub.height} {settings.sub.codec}</p>"
            "<table><tr><th>Ch</th><th>Name</th><th>State</th><th>RTSP (main)</th><th>HTTP-FLV (sub)</th></tr>"
            + "".join(rows) + "</table><p><a href='/mock/stats'>Connection stats</a></p>"
        )

    return app

"""Reolink NVR / Home Hub / camera adapter.

Uses a small subset of Reolink's HTTP API (Login, GetDevInfo, GetChannelstatus,
GetEnc, Logout). Design choices that keep load on the NVR low:

* One login token, reused until it expires. Reolink devices limit concurrent sessions.
* Several commands batched into one HTTP request.
* Channel list refreshed on a slow timer (default 60 s), not per request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from ..config import SourceConfig
from ..models import Camera, Quality, StreamInfo
from .base import SourceAdapter

log = logging.getLogger(__name__)

LOGIN_REQUIRED = -6          # "please login first"
FIVE_MP = 2592 * 1944        # HTTP-FLV is recommended for H.264 up to 5 MP
PROTOCOLS = ("rtsp", "flv")  # the two transports a Reolink NVR offers, for the fallback
# GetAiState keys -> the vendor-neutral detection types in focus.DETECTION_TYPES.
AI_TYPES = {"people": "person", "vehicle": "vehicle", "dog_cat": "animal", "face": "face"}


class ReolinkError(Exception):
    def __init__(self, cmd: str, rsp_code: int | None, detail: str):
        super().__init__(f"{cmd} failed ({rsp_code}): {detail}")
        self.cmd = cmd
        self.rsp_code = rsp_code
        self.detail = detail


def _rsp_code(item: dict[str, Any]) -> int | None:
    return (item.get("error") or {}).get("rspCode")


def _raise_for_item(item: dict[str, Any]) -> None:
    if item.get("code") != 0:
        err = item.get("error") or {}
        raise ReolinkError(str(item.get("cmd")), err.get("rspCode"), str(err.get("detail", "unknown error")))


class ReolinkClient:
    """Minimal async client for the Reolink HTTP API."""

    def __init__(self, host: str, username: str, password: str, *, port: int | None = None,
                 https: bool = False, verify_tls: bool = False, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        scheme = "https" if https else "http"
        self.base_url = f"{scheme}://{host}" + (f":{port}" if port else "")
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(base_url=self.base_url, verify=verify_tls,
                                         timeout=timeout, transport=transport)
        self._token: str | None = None
        self._token_expires = 0.0
        self._lock = asyncio.Lock()
        self.logins = 0

    async def _post(self, cmd: str, body: list[dict[str, Any]], token: str | None = None) -> list[dict[str, Any]]:
        params = {"cmd": cmd}
        if token:
            params["token"] = token
        resp = await self._client.post("/cgi-bin/api.cgi", params=params, json=body)
        if resp.is_redirect and resp.headers.get("location", "").lower().startswith("https:"):
            # An NVR with HTTP switched off answers every HTTP request with a redirect to
            # the HTTPS root, dropping the path. Following it cannot reach the API anyway.
            raise ReolinkError(cmd, None, "the NVR redirects HTTP to HTTPS (its HTTP port is off); "
                                          "set https: true (NVR_HTTPS=true)")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or not data:
            raise ReolinkError(cmd, None, f"unexpected response: {str(data)[:200]}")
        return data

    async def login(self) -> None:
        body = [{"cmd": "Login", "action": 0, "param": {"User": {
            "Version": "0", "userName": self._username, "password": self._password}}}]
        item = (await self._post("Login", body))[0]
        _raise_for_item(item)
        token = item["value"]["Token"]
        self._token = token["name"]
        lease = int(token.get("leaseTime", 3600))
        self._token_expires = time.monotonic() + max(lease - 60, 30)
        self.logins += 1

    async def _get_token(self) -> str:
        async with self._lock:
            if self._token is None or time.monotonic() >= self._token_expires:
                await self.login()
            assert self._token is not None
            return self._token

    async def batch(self, commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Send several commands in one request. Returns the raw per-command results."""
        for attempt in (1, 2):
            token = await self._get_token()
            data = await self._post(commands[0]["cmd"], commands, token)
            if attempt == 1 and any(_rsp_code(i) == LOGIN_REQUIRED for i in data):
                self._token = None    # token expired or was revoked: log in again once
                continue
            return data
        return data

    async def command(self, cmd: str, param: dict[str, Any] | None = None, action: int = 0) -> dict[str, Any]:
        item = (await self.batch([{"cmd": cmd, "action": action, "param": param or {}}]))[0]
        _raise_for_item(item)
        return item.get("value") or {}

    async def logout(self) -> None:
        if self._token is None:
            return
        token, self._token = self._token, None
        try:
            await self._post("Logout", [{"cmd": "Logout", "action": 0, "param": {}}], token)
        except (httpx.HTTPError, ReolinkError, ValueError) as exc:
            log.debug("logout failed: %s", exc)

    async def close(self) -> None:
        await self.logout()
        await self._client.aclose()


def _parse_stream(raw: dict[str, Any] | None) -> StreamInfo | None:
    if not raw:
        return None
    width, height = raw.get("width"), raw.get("height")
    size = str(raw.get("size", ""))
    if (not width or not height) and "*" in size:
        try:
            width, height = (int(v) for v in size.split("*", 1))
        except ValueError:
            pass
    codec = str(raw.get("vType", "")).lower() or None
    # `gop` is the keyframe interval as a multiple of the frame rate, which is the NVR's
    # "I-frame interval: 2x" setting: a keyframe every 2 seconds.
    gop = raw.get("gop")
    keyframe = float(gop) if isinstance(gop, (int, float)) and not isinstance(gop, bool) and gop > 0 else None
    return StreamInfo(codec=codec, width=width, height=height,
                      fps=raw.get("frameRate"), bitrate_kbps=raw.get("bitRate"),
                      keyframe_seconds=keyframe)


def _is_empty_slot(status: dict[str, Any] | None) -> bool:
    """An NVR slot with no camera: offline and never named."""
    if not status:
        return False
    return not int(status.get("online", 0)) and not str(status.get("name") or "").strip()


def _available_protocols(port_item: dict[str, Any], config: SourceConfig) -> tuple[str, ...]:
    """Which transports can work, from GetNetPort. Empty when the NVR won't say.

    Reolink serves HTTP-FLV on its HTTP port only: with HTTP off, /flv on the HTTPS port
    accepts the TLS connection and then closes it without a response (NVS8, 3.4.0.318).
    So FLV needs the HTTP port enabled *and* an http:// URL, i.e. https off in our config.
    """
    if port_item.get("code") != 0:
        return ()      # e.g. a firmware that refuses GetNetPort to this user: assume both
    ports = (port_item.get("value") or {}).get("NetPort") or {}
    out = []
    if int(ports.get("rtspEnable", 1)):
        out.append("rtsp")
    if int(ports.get("httpEnable", 1)) and not config.https:
        out.append("flv")
    return tuple(out)


class ReolinkAdapter(SourceAdapter):
    def __init__(self, config: SourceConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.id = config.id
        self.config = config
        self.client = ReolinkClient(
            config.host, config.username, config.password, port=config.port,
            https=config.https, verify_tls=config.verify_tls, transport=transport,
        )
        self.model: str | None = None
        self.firmware: str | None = None
        # Transports the NVR says it serves (from GetNetPort). Empty = not known yet.
        self.available_protocols: tuple[str, ...] = ()
        # (channel, quality) -> transport pinned by the automatic fallback.
        self._forced: dict[tuple[int, Quality], str] = {}

    async def discover(self) -> list[Camera]:
        # One request for the device, its channels and which transports it serves.
        dev_item, status_item, port_item = await self.client.batch([
            {"cmd": "GetDevInfo", "action": 0, "param": {}},
            {"cmd": "GetChannelstatus", "action": 0, "param": {}},
            {"cmd": "GetNetPort", "action": 0, "param": {}},
        ])
        _raise_for_item(dev_item)
        dev = (dev_item.get("value") or {}).get("DevInfo", {})
        self.model = dev.get("model")
        self.firmware = dev.get("firmVer")
        channel_count = int(dev.get("channelNum") or 1)
        self.available_protocols = _available_protocols(port_item, self.config)

        if status_item.get("code") == 0:
            value = status_item.get("value") or {}
            statuses = {int(st["channel"]): st for st in value.get("status", [])}
        else:
            # Standalone cameras don't have GetChannelstatus.
            statuses = {0: {"channel": 0, "name": dev.get("name") or "Camera", "online": 1}}
            channel_count = 1

        channels = sorted(statuses) or list(range(channel_count))
        if self.config.channels is not None:
            channels = [c for c in channels if c in set(self.config.channels)]
        else:
            # An NVR reports every slot it has. Unused ones come back offline with no name;
            # listing them would fill the wall with dead tiles (9 of 12 on the NVS8 tested).
            # A camera that is merely down keeps its name, so it still shows as offline.
            channels = [c for c in channels if not _is_empty_slot(statuses.get(c))]

        encodings: dict[int, dict[str, Any]] = {}
        wanted = [c for c in channels if int(statuses.get(c, {}).get("online", 1))]
        if wanted:
            # Offline channels answer GetEnc with rspCode -99, so don't ask.
            results = await self.client.batch(
                [{"cmd": "GetEnc", "action": 0, "param": {"channel": c}} for c in wanted])
            for channel, item in zip(wanted, results):
                if item.get("code") == 0:
                    encodings[channel] = (item.get("value") or {}).get("Enc", {})

        cameras = []
        for channel in channels:
            status = statuses.get(channel, {})
            enc = encodings.get(channel, {})
            cameras.append(Camera(
                id=f"{self.id}:{channel}",
                source_id=self.id,
                channel=channel,
                name=str(status.get("name") or f"Channel {channel + 1}"),
                online=bool(int(status.get("online", 1))),
                model=status.get("typeInfo") or None,
                main=_parse_stream(enc.get("mainStream")),
                sub=_parse_stream(enc.get("subStream")),
            ))
        return cameras

    supports_detection = True

    async def detections(self, cameras: list[Camera]) -> dict[str, frozenset[str]]:
        """GetMdState + GetAiState for every camera, in one request.

        Both are allowed for a view-only account (NVS8, 3.4.0.318). AI types a camera does
        not support (`support: 0`) are ignored, whatever their alarm state says.
        """
        if not cameras:
            return {}
        commands = []
        for cam in cameras:
            commands.append({"cmd": "GetMdState", "action": 0, "param": {"channel": cam.channel}})
            commands.append({"cmd": "GetAiState", "action": 0, "param": {"channel": cam.channel}})
        results = await self.client.batch(commands)
        out: dict[str, frozenset[str]] = {}
        for i, cam in enumerate(cameras):
            md, ai = results[2 * i], results[2 * i + 1]
            types: set[str] = set()
            if md.get("code") == 0 and int((md.get("value") or {}).get("state", 0)):
                types.add("motion")
            if ai.get("code") == 0:
                value = ai.get("value") or {}
                for key, kind in AI_TYPES.items():
                    entry = value.get(key)
                    if isinstance(entry, dict) and int(entry.get("support", 0)) and int(entry.get("alarm_state", 0)):
                        types.add(kind)
            if types:
                out[cam.id] = frozenset(types)
        return out

    def protocol_for(self, camera: Camera, quality: Quality) -> str:
        forced = self._forced.get((camera.channel, quality))
        if forced is not None:
            return forced
        if self.config.protocol != "auto":
            return self.config.protocol
        preferred = self._preferred_protocol(camera, quality)
        if preferred not in self.available_protocols and self.available_protocols:
            # e.g. HTTP-FLV preferred for H.264, but the NVR has its HTTP port off.
            return next(p for p in PROTOCOLS if p in self.available_protocols)
        return preferred

    def _preferred_protocol(self, camera: Camera, quality: Quality) -> str:
        info = camera.main if quality == "main" else camera.sub
        if info is None or info.codec is None:
            # Unknown encoding: sub streams are H.264 on Reolink; main may be H.265.
            # (Real firmware omits the sub stream's vType; go2rtc confirms it is H.264.)
            return "flv" if quality == "sub" else "rtsp"
        if info.codec == "h264" and info.pixels <= FIVE_MP:
            return "flv"
        return "rtsp"

    def next_protocol(self, camera: Camera, quality: Quality) -> str | None:
        """Pin this stream to the other transport (RTSP <-> FLV), if that one can work."""
        if not self.config.protocol_fallback:
            return None
        current = self.protocol_for(camera, quality)
        others = [p for p in PROTOCOLS if p != current]
        if self.available_protocols:
            others = [p for p in others if p in self.available_protocols]
        if not others:
            return None       # nothing else the NVR serves: retrying the same one is the fallback
        self._forced[(camera.channel, quality)] = others[0]
        return others[0]

    def clear_protocol_overrides(self) -> None:
        self._forced.clear()

    def stream_source(self, camera: Camera, quality: Quality) -> str:
        cfg = self.config
        if self.protocol_for(camera, quality) == "rtsp":
            user = quote(cfg.username, safe="")
            password = quote(cfg.password, safe="")
            return (f"rtsp://{user}:{password}@{cfg.host}:{cfg.rtsp_port}"
                    f"/Preview_{camera.channel + 1:02d}_{quality}")
        scheme = "https" if cfg.https else "http"
        port = f":{cfg.port}" if cfg.port else ""
        query = urlencode({
            "port": 1935, "app": "bcs", "stream": f"channel{camera.channel}_{quality}.bcs",
            "user": cfg.username, "password": cfg.password,
        })
        return f"{scheme}://{cfg.host}{port}/flv?{query}"

    async def close(self) -> None:
        await self.client.close()

"""ONVIF source: the standard most IP cameras and NVRs speak.

One adapter for many brands. Written against the standard rather than a library: the four
calls needed are small, and a hand-written SOAP client keeps the agent dependency-free.

    GetSystemDateAndTime   unauthenticated; gives the device's clock
    GetDeviceInformation   manufacturer, model, firmware
    GetProfiles            one per stream a channel offers (usually main and sub)
    GetStreamUri           the RTSP URL for a profile

Authentication is WS-Security UsernameToken with a password digest. Devices check the
timestamp against their own clock, so the offset from GetSystemDateAndTime is applied
rather than trusting ours to match.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from xml.etree import ElementTree

import httpx

from ..config import SourceConfig
from ..models import Camera, Quality, StreamInfo
from .base import SourceAdapter
from .rtsp import with_credentials

log = logging.getLogger(__name__)

DEVICE = "http://www.onvif.org/ver10/device/wsdl"
MEDIA = "http://www.onvif.org/ver10/media/wsdl"
SCHEMA = "http://www.onvif.org/ver10/schema"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
BASE64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
EVENTS = "http://www.onvif.org/ver10/events/wsdl"
WSA = "http://www.w3.org/2005/08/addressing"
NS = {"s": "http://www.w3.org/2003/05/soap-envelope", "tds": DEVICE, "trt": MEDIA,
      "tt": SCHEMA, "tev": EVENTS, "wsa": WSA,
      "wsnt": "http://docs.oasis-open.org/wsn/b-2"}

#: ONVIF topics -> our detection types. Matched on the tail of the topic, because vendors
#: prefix them differently (tns1:, tnsaxis:, and so on).
EVENT_TOPICS = {
    "CellMotionDetector/Motion": "motion",
    "MotionAlarm": "motion",
    "MotionDetector/Motion": "motion",
    "PeopleDetect": "person",
    "PeopleDetector": "person",
    "Person": "person",
    "VehicleDetect": "vehicle",
    "VehicleDetector": "vehicle",
    "Vehicle": "vehicle",
    "DogCatDetect": "animal",
    "AnimalDetect": "animal",
    "FaceDetect": "face",
    "Face": "face",
}
#: Ask for this long a subscription, and pull with this timeout each poll.
SUBSCRIPTION = "PT300S"
PULL_TIMEOUT = "PT1S"
PULL_LIMIT = 64

DEFAULT_PORT = 8000
DEVICE_PATH = "/onvif/device_service"
CLOCK_RESYNC_SECONDS = 3600.0


class OnvifError(Exception):
    pass


def _text(element: ElementTree.Element | None, path: str, default: str = "") -> str:
    if element is None:
        return default
    found = element.find(path, NS)
    return (found.text or default) if found is not None else default


class OnvifClient:
    """Just enough SOAP to ask a device what it streams."""

    def __init__(self, base_url: str, username: str, password: str, *, verify_tls: bool = False,
                 timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(verify=verify_tls, timeout=timeout, transport=transport)
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._clock_offset = 0.0

    def _security_header(self) -> str:
        nonce = secrets.token_bytes(16)
        created = (datetime.now(timezone.utc) + timedelta(seconds=self._clock_offset)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = hashlib.sha1(nonce + created.encode() + self._password.encode()).digest()
        return (
            f'<s:Header><Security xmlns="{WSSE}" s:mustUnderstand="1"><UsernameToken>'
            f"<Username>{_escape(self._username)}</Username>"
            f'<Password Type="{DIGEST}">{base64.b64encode(digest).decode()}</Password>'
            f'<Nonce EncodingType="{BASE64}">{base64.b64encode(nonce).decode()}</Nonce>'
            f'<Created xmlns="{WSU}">{created}</Created>'
            "</UsernameToken></Security></s:Header>"
        )

    async def call(self, url: str, body: str, *, authenticated: bool = True) -> ElementTree.Element:
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            f"{self._security_header() if authenticated else ''}"
            f"<s:Body>{body}</s:Body></s:Envelope>"
        )
        try:
            response = await self._client.post(
                url, content=envelope.encode(),
                headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        except httpx.ConnectError as exc:
            if "WRONG_VERSION_NUMBER" in str(exc) or "SSL" in str(exc):
                # `https` describes the vendor's own API; ONVIF usually listens on plain
                # HTTP on its own port even when the device's web UI is HTTPS-only.
                raise OnvifError(f"{self.base_url} answered plain HTTP, not TLS: "
                                 "set https: false (NVR_HTTPS=false) for an ONVIF source") from None
            raise
        if response.status_code >= 400:
            # A SOAP fault still carries a useful reason; the status alone does not.
            raise OnvifError(f"HTTP {response.status_code} from {_path_of(url)}"
                             f"{_fault_reason(response.text)}")
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise OnvifError(f"not SOAP from {_path_of(url)}: {exc}") from None
        fault = root.find(".//s:Fault", NS)
        if fault is not None:
            raise OnvifError(f"{_path_of(url)} refused: {_text(fault, './/s:Text', 'unknown fault')}")
        body_element = root.find("s:Body", NS)
        if body_element is None:
            raise OnvifError(f"no SOAP body from {_path_of(url)}")
        return body_element

    async def sync_clock(self) -> None:
        """ONVIF rejects a token whose timestamp is far from the device's own clock."""
        body = await self.call(f"{self.base_url}{DEVICE_PATH}",
                               f'<GetSystemDateAndTime xmlns="{DEVICE}"/>', authenticated=False)
        utc = body.find(".//tt:UTCDateTime", NS)
        if utc is None:
            return
        try:
            device_time = datetime(
                int(_text(utc, "tt:Date/tt:Year")), int(_text(utc, "tt:Date/tt:Month")),
                int(_text(utc, "tt:Date/tt:Day")), int(_text(utc, "tt:Time/tt:Hour")),
                int(_text(utc, "tt:Time/tt:Minute")), int(_text(utc, "tt:Time/tt:Second")),
                tzinfo=timezone.utc)
        except ValueError:
            return
        self._clock_offset = device_time.timestamp() - time.time()
        if abs(self._clock_offset) > 5:
            log.info("onvif device clock differs by %.0f s; adjusting the token timestamp",
                     self._clock_offset)

    async def device_information(self) -> dict[str, str]:
        body = await self.call(f"{self.base_url}{DEVICE_PATH}", f'<GetDeviceInformation xmlns="{DEVICE}"/>')
        info = body.find("tds:GetDeviceInformationResponse", NS)
        return {key.lower(): _text(info, f"tds:{key}") for key in
                ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber")}

    async def media_url(self) -> str:
        """Where the Media service lives; devices may host it on another path or port."""
        body = await self.call(f"{self.base_url}{DEVICE_PATH}",
                               f'<GetCapabilities xmlns="{DEVICE}"><Category>Media</Category></GetCapabilities>')
        url = _text(body.find(".//tt:Media", NS), "tt:XAddr")
        return url or f"{self.base_url}/onvif/media_service"

    async def profiles(self, media_url: str) -> list[dict[str, Any]]:
        body = await self.call(media_url, f'<GetProfiles xmlns="{MEDIA}"/>')
        out = []
        for profile in body.findall(".//trt:Profiles", NS):
            encoder = profile.find("tt:VideoEncoderConfiguration", NS)
            resolution = encoder.find("tt:Resolution", NS) if encoder is not None else None
            out.append({
                "token": profile.get("token", ""),
                "name": _text(profile, "tt:Name"),
                "source": _text(profile, "tt:VideoSourceConfiguration/tt:SourceToken"),
                "codec": (_text(encoder, "tt:Encoding") or "").lower() or None,
                "width": int(_text(resolution, "tt:Width", "0") or 0),
                "height": int(_text(resolution, "tt:Height", "0") or 0),
                "fps": int(float(_text(encoder, "tt:RateControl/tt:FrameRateLimit", "0") or 0)),
                "bitrate": int(float(_text(encoder, "tt:RateControl/tt:BitrateLimit", "0") or 0)),
                # Frames between keyframes; ONVIF's media service only reports it for H.264.
                "gov": int(float(_text(encoder, "tt:H264/tt:GovLength", "0") or 0)),
            })
        return out

    async def stream_uri(self, media_url: str, token: str) -> str:
        body = await self.call(media_url, (
            f'<GetStreamUri xmlns="{MEDIA}">'
            f'<StreamSetup xmlns="{MEDIA}"><Stream xmlns="{SCHEMA}">RTP-Unicast</Stream>'
            f'<Transport xmlns="{SCHEMA}"><Protocol>RTSP</Protocol></Transport></StreamSetup>'
            f"<ProfileToken>{_escape(token)}</ProfileToken></GetStreamUri>"))
        uri = _text(body.find(".//trt:MediaUri", NS), "tt:Uri")
        if not uri:
            raise OnvifError(f"no stream URL for profile {token}")
        return uri

    # ----- events ----------------------------------------------------------

    async def events_url(self) -> str:
        body = await self.call(f"{self.base_url}{DEVICE_PATH}",
                               f'<GetCapabilities xmlns="{DEVICE}"><Category>Events</Category></GetCapabilities>')
        url = _text(body.find(".//tt:Events", NS), "tt:XAddr")
        return url or f"{self.base_url}/onvif/event_service"

    async def create_pullpoint(self, events_url: str) -> str:
        """Start a pull-point subscription; returns where to pull from."""
        body = await self.call(events_url, (
            f'<CreatePullPointSubscription xmlns="{EVENTS}">'
            f"<InitialTerminationTime>{SUBSCRIPTION}</InitialTerminationTime>"
            "</CreatePullPointSubscription>"))
        address = _text(body.find(".//tev:SubscriptionReference", NS), "wsa:Address")
        if not address:
            raise OnvifError("the device did not give a subscription address")
        return address

    async def pull_messages(self, subscription_url: str) -> list[tuple[str, str, dict[str, str]]]:
        """(topic, source token, data) for each event waiting. Blocks up to PULL_TIMEOUT."""
        body = await self.call(subscription_url, (
            f'<PullMessages xmlns="{EVENTS}">'
            f"<Timeout>{PULL_TIMEOUT}</Timeout><MessageLimit>{PULL_LIMIT}</MessageLimit>"
            "</PullMessages>"))
        out = []
        for message in body.findall(".//wsnt:NotificationMessage", NS):
            topic = (message.findtext("wsnt:Topic", "", NS) or "").strip()
            source, data = "", {}
            for item in message.findall(".//tt:Source/tt:SimpleItem", NS):
                if item.get("Name") in ("VideoSourceConfigurationToken", "VideoSourceToken",
                                        "Source", "channel"):
                    source = item.get("Value", "") or source
            for item in message.findall(".//tt:Data/tt:SimpleItem", NS):
                data[item.get("Name", "")] = item.get("Value", "")
            out.append((topic, source, data))
        return out

    async def unsubscribe(self, subscription_url: str) -> None:
        try:
            await self.call(subscription_url,
                            '<Unsubscribe xmlns="http://docs.oasis-open.org/wsn/b-2"/>')
        except (OnvifError, httpx.HTTPError):
            pass       # it expires on its own; never hold up shutdown for this

    async def close(self) -> None:
        await self._client.aclose()


def detection_type(topic: str) -> str | None:
    """Map an ONVIF topic to one of our types, by its tail: vendors prefix differently."""
    tail = topic.replace("\\", "/").split(":")[-1]
    for suffix, kind in EVENT_TOPICS.items():
        if tail.endswith(suffix) or suffix in tail:
            return kind
    return None


def _is_on(data: dict[str, str]) -> bool:
    """Whether an event says something started. Absent state means it did."""
    for key in ("State", "IsMotion", "isMotion", "Value"):
        if key in data:
            return str(data[key]).strip().lower() in ("true", "1", "on")
    return True


class OnvifAdapter(SourceAdapter):
    """Any ONVIF Profile S device: a camera, or an NVR with several channels."""

    def __init__(self, config: SourceConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.id = config.id
        self.config = config
        scheme = "https" if config.https else "http"
        self.client = OnvifClient(f"{scheme}://{config.host}:{config.port or DEFAULT_PORT}",
                                  config.username, config.password,
                                  verify_tls=config.verify_tls, transport=transport)
        self.model: str | None = None
        self.firmware: str | None = None
        self.available_protocols: tuple[str, ...] = ("rtsp",)
        self._urls: dict[tuple[int, str], str] = {}
        # Discovery runs every refresh_seconds. Only the profile list is worth re-reading:
        # the rest is fixed for the life of the device, so ask once.
        self._media_url: str | None = None
        # Events: a pull-point subscription, and what each channel is currently detecting.
        self._events_url: str | None = None
        self._subscription: str | None = None
        self._active: dict[int, dict[str, bool]] = {}
        self._channel_of_source: dict[str, int] = {}
        self._stream_uris: dict[str, str] = {}
        self._clock_synced_at = float("-inf")

    async def discover(self) -> list[Camera]:
        now = time.monotonic()
        if now - self._clock_synced_at > CLOCK_RESYNC_SECONDS:
            # Devices reject a token whose timestamp drifts from their clock, and clocks
            # drift, so this is refreshed occasionally rather than only at startup.
            await self.client.sync_clock()
            self._clock_synced_at = now
        if self.model is None:
            info = await self.client.device_information()
            self.model = info.get("model") or info.get("manufacturer")
            self.firmware = info.get("firmwareversion")
        if self._media_url is None:
            self._media_url = await self.client.media_url()
        media_url = self._media_url
        profiles = await self.client.profiles(media_url)
        if not profiles:
            raise OnvifError("the device reports no media profiles")

        # Profiles belong to a video source; on an NVR each source is a channel.
        channels: dict[str, list[dict[str, Any]]] = {}
        for profile in profiles:
            channels.setdefault(profile["source"] or profile["token"], []).append(profile)

        cameras = []
        self._channel_of_source = {token: i for i, token in enumerate(sorted(channels))}
        for channel, (source_token, group) in enumerate(sorted(channels.items())):
            # Biggest is the main stream, smallest the sub; a device offering one uses it twice.
            ordered = sorted(group, key=lambda p: (p["width"] * p["height"], p["token"]), reverse=True)
            main, sub = ordered[0], ordered[-1]
            for quality, profile in (("main", main), ("sub", sub)):
                token = profile["token"]
                if token not in self._stream_uris:
                    self._stream_uris[token] = await self.client.stream_uri(media_url, token)
                self._urls[(channel, quality)] = self._stream_uris[token]
            cameras.append(Camera(
                id=f"{self.id}:{channel}", source_id=self.id, channel=channel,
                name=main["name"] or source_token or f"Channel {channel + 1}",
                online=True, model=self.model,
                main=_stream_info(main), sub=_stream_info(sub) if sub is not main else None,
            ))
        return cameras

    supports_detection = True

    async def detections(self, cameras: list[Camera]) -> dict[str, frozenset[str]]:
        """What each camera reports over ONVIF events.

        Events are edges — "motion started", "motion stopped" — so the current state is kept
        between polls. A subscription that has expired or was lost is simply made again.
        """
        if not cameras:
            return {}
        try:
            await self._pull()
        except (OnvifError, httpx.HTTPError, TimeoutError) as exc:
            self._subscription = None       # make a new one on the next poll
            raise OnvifError(f"events unavailable: {exc}") from None
        wanted = {c.channel: c.id for c in cameras}
        out = {}
        for channel, states in self._active.items():
            kinds = frozenset(kind for kind, on in states.items() if on)
            if kinds and channel in wanted:
                out[wanted[channel]] = kinds
        return out

    async def _pull(self) -> None:
        if self._events_url is None:
            self._events_url = await self.client.events_url()
        if self._subscription is None:
            self._subscription = await self.client.create_pullpoint(self._events_url)
            self._active.clear()
        for topic, source, data in await self.client.pull_messages(self._subscription):
            kind = detection_type(topic)
            if kind is None:
                continue
            channel = self._channel_of_source.get(source)
            if channel is None and source.isdigit():
                channel = int(source)
            if channel is None:
                channel = 0 if len(self._channel_of_source) <= 1 else None
            if channel is None:
                continue
            self._active.setdefault(channel, {})[kind] = _is_on(data)

    def stream_source(self, camera: Camera, quality: Quality) -> str:
        url = self._urls.get((camera.channel, quality)) or self._urls.get((camera.channel, "main"), "")
        return with_credentials(url, self.config.username, self.config.password)

    async def close(self) -> None:
        if self._subscription:
            await self.client.unsubscribe(self._subscription)
            self._subscription = None
        await self.client.close()


def _stream_info(profile: dict[str, Any]) -> StreamInfo:
    codec = {"h264": "h264", "h265": "h265", "hevc": "h265"}.get(profile["codec"] or "", profile["codec"])
    fps, gov = profile["fps"], profile.get("gov") or 0
    return StreamInfo(codec=codec, width=profile["width"] or None, height=profile["height"] or None,
                      fps=fps or None, bitrate_kbps=profile["bitrate"] or None,
                      keyframe_seconds=round(gov / fps, 2) if gov and fps else None)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def _path_of(url: str) -> str:
    return "/" + url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url


def _fault_reason(text: str) -> str:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return ""
    reason = root.find(".//s:Fault//s:Text", NS)
    return f": {reason.text}" if reason is not None and reason.text else ""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from conftest import make_camera
from viewport.adapters.reolink import ReolinkAdapter, ReolinkClient, ReolinkError
from viewport.config import SourceConfig
from viewport.models import StreamInfo


def source(**kw) -> SourceConfig:
    base = dict(id="nvr", host="nvr.local", username="admin", password="p@ss:w/rd&1")
    base.update(kw)
    return SourceConfig(**base)


async def test_discover_against_mock(mock_transport):
    adapter = ReolinkAdapter(source(), transport=mock_transport)
    cameras = await adapter.discover()
    assert adapter.model == "RLN8-410"
    assert [c.id for c in cameras] == ["nvr:0", "nvr:1", "nvr:2", "nvr:3"]
    assert [c.name for c in cameras] == ["Front Door", "Driveway", "Garden", "Garage"]
    assert [c.online for c in cameras] == [True, True, True, False]
    assert cameras[0].main == StreamInfo(codec="h264", width=1920, height=1080, fps=15, bitrate_kbps=4096,
                                         keyframe_seconds=2.0)
    assert cameras[0].sub.width == 640
    assert cameras[0].sub.keyframe_seconds == 4.0
    await adapter.close()


async def test_token_is_reused(mock_transport):
    adapter = ReolinkAdapter(source(), transport=mock_transport)
    for _ in range(3):
        await adapter.discover()
    assert adapter.client.logins == 1
    await adapter.close()


async def test_relogin_when_token_is_rejected(mock_transport):
    adapter = ReolinkAdapter(source(), transport=mock_transport)
    await adapter.discover()
    adapter.client._token = "revoked"          # simulate the NVR dropping our session
    cameras = await adapter.discover()
    assert len(cameras) == 4
    assert adapter.client.logins == 2
    await adapter.close()


async def test_channel_filter(mock_transport):
    adapter = ReolinkAdapter(source(channels=[1, 2]), transport=mock_transport)
    assert [c.channel for c in await adapter.discover()] == [1, 2]
    await adapter.close()


async def test_wrong_password(mock_transport):
    client = ReolinkClient("nvr.local", "admin", "wrong", transport=mock_transport)
    with pytest.raises(ReolinkError) as err:
        await client.command("GetDevInfo")
    assert err.value.cmd == "Login"
    await client.close()


async def test_standalone_camera_without_channel_status():
    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        out = []
        for item in body:
            cmd = item["cmd"]
            if cmd == "Login":
                out.append({"cmd": cmd, "code": 0, "value": {"Token": {"leaseTime": 3600, "name": "t"}}})
            elif cmd == "GetDevInfo":
                out.append({"cmd": cmd, "code": 0, "value": {"DevInfo": {"channelNum": 1, "name": "Porch",
                                                                         "model": "RLC-810A"}}})
            elif cmd == "GetEnc":
                out.append({"cmd": cmd, "code": 0, "value": {"Enc": {"mainStream": {
                    "size": "3840*2160", "vType": "h265", "frameRate": 20}}}})
            else:
                out.append({"cmd": cmd, "code": 1, "error": {"rspCode": -9, "detail": "not support"}})
        return httpx.Response(200, json=out)

    adapter = ReolinkAdapter(source(), transport=httpx.MockTransport(handler))
    cameras = await adapter.discover()
    assert len(cameras) == 1
    cam = cameras[0]
    assert (cam.name, cam.main.width, cam.main.codec, cam.sub) == ("Porch", 3840, "h265", None)
    assert adapter.protocol_for(cam, "main") == "rtsp"
    assert adapter.protocol_for(cam, "sub") == "flv"
    await adapter.close()


@pytest.mark.parametrize("gop,expected", [(2, 2.0), (1, 1.0), (0, None), (None, None),
                                          ("2", None), (True, None)])
def test_the_keyframe_interval_is_read_from_gop(gop, expected):
    """It is most of the wait when a tile switches to this stream, so the admin page shows it."""
    from viewport.adapters.reolink import _parse_stream
    raw = {"size": "3840*2160", "vType": "h265", "frameRate": 25}
    if gop is not None:
        raw["gop"] = gop
    assert _parse_stream(raw).keyframe_seconds == expected


def test_protocol_auto_rules():
    adapter = ReolinkAdapter(source())
    cam = make_camera(0)
    assert adapter.protocol_for(cam, "main") == "flv"         # H.264 1080p
    assert adapter.protocol_for(cam, "sub") == "flv"
    cam.main = StreamInfo(codec="h264", width=3840, height=2160)
    assert adapter.protocol_for(cam, "main") == "rtsp"        # H.264 above 5 MP
    cam.main = StreamInfo(codec="h265", width=2560, height=1440)
    assert adapter.protocol_for(cam, "main") == "rtsp"        # H.265
    cam.main = None
    assert adapter.protocol_for(cam, "main") == "rtsp"        # unknown
    assert ReolinkAdapter(source(protocol="rtsp")).protocol_for(cam, "sub") == "rtsp"


def test_rtsp_url_escapes_credentials():
    adapter = ReolinkAdapter(source(protocol="rtsp", rtsp_port=8554))
    url = adapter.stream_source(make_camera(2), "main")
    assert url == "rtsp://admin:p%40ss%3Aw%2Frd%261@nvr.local:8554/Preview_03_main"


def test_flv_url():
    adapter = ReolinkAdapter(source(protocol="flv", port=8080))
    url = urlparse(adapter.stream_source(make_camera(0), "sub"))
    assert (url.scheme, url.netloc, url.path) == ("http", "nvr.local:8080", "/flv")
    q = parse_qs(url.query)
    assert q["stream"] == ["channel0_sub.bcs"]
    assert q["app"] == ["bcs"]
    assert q["password"] == ["p@ss:w/rd&1"]


# ----- behaviour observed on real hardware (NVS8, firmware v3.4.0.318) ---------

from mock_nvr.app import create_app as create_mock_app      # noqa: E402
from mock_nvr.settings import Settings as MockSettings      # noqa: E402


def mock_adapter(**settings_kw):
    settings = MockSettings(password="p@ss:w/rd&1", **{"channels": 4, **settings_kw})
    transport = httpx.ASGITransport(app=create_mock_app(settings))
    return ReolinkAdapter(source(), transport=transport), transport


class CountingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner):
        self.inner, self.requests = inner, []

    async def handle_async_request(self, request):
        self.requests.append(__import__("json").loads(request.content))
        return await self.inner.handle_async_request(request)


async def test_unused_nvr_slots_are_not_cameras():
    """The NVS8 reports 12 slots for 3 cameras; the other 9 must not become dead tiles."""
    adapter, _ = mock_adapter(channels=6, empty=frozenset({1, 2, 5}))
    cameras = await adapter.discover()
    assert [c.channel for c in cameras] == [0, 3, 4]
    await adapter.close()


async def test_a_named_camera_that_is_down_still_shows_as_offline():
    adapter, _ = mock_adapter(offline=frozenset({2}), empty=frozenset({3}))
    cameras = {c.channel: c for c in await adapter.discover()}
    assert set(cameras) == {0, 1, 2}
    assert cameras[2].online is False and cameras[2].name == "Garden"
    await adapter.close()


async def test_an_explicitly_listed_empty_slot_is_kept():
    settings = MockSettings(channels=4, empty=frozenset({1}), password="p@ss:w/rd&1")
    adapter = ReolinkAdapter(source(channels=[0, 1]),
                             transport=httpx.ASGITransport(app=create_mock_app(settings)))
    assert [c.channel for c in await adapter.discover()] == [0, 1]
    await adapter.close()


async def test_discovery_batches_device_channels_and_ports_into_one_request():
    settings = MockSettings(channels=4, empty=frozenset({2, 3}), password="p@ss:w/rd&1")
    counting = CountingTransport(httpx.ASGITransport(app=create_mock_app(settings)))
    adapter = ReolinkAdapter(source(), transport=counting)
    await adapter.discover()
    cmds = [[item["cmd"] for item in body] for body in counting.requests]
    assert ["GetDevInfo", "GetChannelstatus", "GetNetPort"] in cmds
    # GetEnc only for the channels with a camera online, all in one request.
    assert [["GetEnc", "GetEnc"]] == [c for c in cmds if c and c[0] == "GetEnc"]
    await adapter.close()


async def test_sub_stream_without_vtype_parses_and_plays_over_flv():
    """Real firmware omits the sub stream's codec; it is H.264, so FLV is still right."""
    adapter, _ = mock_adapter()
    cam = (await adapter.discover())[0]
    assert cam.sub.codec is None and (cam.sub.width, cam.sub.height) == (640, 360)
    assert cam.main.codec == "h264"
    assert adapter.protocol_for(cam, "sub") == "flv"
    await adapter.close()


async def test_http_switched_off_makes_auto_use_rtsp_everywhere():
    """HTTP-FLV lives on the HTTP port, so with it off every stream must go over RTSP."""
    adapter, _ = mock_adapter(http_enabled=False)
    cam = (await adapter.discover())[0]                # H.264 1080p: would prefer FLV
    assert adapter.available_protocols == ("rtsp",)
    assert adapter.protocol_for(cam, "main") == "rtsp"
    assert adapter.protocol_for(cam, "sub") == "rtsp"
    assert adapter.stream_source(cam, "sub").startswith("rtsp://")
    await adapter.close()


async def test_https_config_rules_out_flv_even_with_http_on():
    """/flv on the HTTPS port closes without a response, so https:// FLV URLs cannot work."""
    adapter, transport = mock_adapter()
    adapter.config = source(https=True)
    adapter.client = ReolinkClient("nvr.local", "admin", "p@ss:w/rd&1", transport=transport)
    await adapter.discover()
    assert adapter.available_protocols == ("rtsp",)
    await adapter.close()


async def test_fallback_never_moves_to_a_transport_the_nvr_does_not_serve():
    adapter, _ = mock_adapter(http_enabled=False)
    cam = (await adapter.discover())[0]
    assert adapter.next_protocol(cam, "main") is None
    assert adapter.protocol_for(cam, "main") == "rtsp"
    await adapter.close()


async def test_unknown_ports_keep_the_old_rules():
    """A firmware that refuses GetNetPort to this user: assume both transports."""
    def handler(request):
        out = []
        for item in __import__("json").loads(request.content):
            cmd = item["cmd"]
            if cmd == "Login":
                out.append({"cmd": cmd, "code": 0, "value": {"Token": {"leaseTime": 3600, "name": "t"}}})
            elif cmd == "GetDevInfo":
                out.append({"cmd": cmd, "code": 0, "value": {"DevInfo": {"channelNum": 1}}})
            elif cmd == "GetChannelstatus":
                out.append({"cmd": cmd, "code": 0, "value": {"status": [
                    {"channel": 0, "name": "Porch", "online": 1}]}})
            elif cmd == "GetEnc":
                out.append({"cmd": cmd, "code": 0, "value": {"Enc": {
                    "mainStream": {"size": "1920*1080", "vType": "h264"}, "subStream": {"size": "640*360"}}}})
            else:
                out.append({"cmd": cmd, "code": 1, "error": {"rspCode": -26, "detail": "ability error"}})
        return httpx.Response(200, json=out)

    adapter = ReolinkAdapter(source(), transport=httpx.MockTransport(handler))
    cam = (await adapter.discover())[0]
    assert adapter.available_protocols == ()
    assert adapter.protocol_for(cam, "main") == "flv"
    await adapter.close()


async def test_http_redirect_to_https_gives_a_clear_error():
    """An NVR with HTTP off redirects to the HTTPS root; say what to change."""
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://nvr.local"})

    client = ReolinkClient("nvr.local", "admin", "p", transport=httpx.MockTransport(handler))
    with pytest.raises(ReolinkError) as err:
        await client.command("GetDevInfo")
    assert "NVR_HTTPS=true" in str(err.value)
    await client.close()


async def test_firmware_version_is_recorded():
    adapter, _ = mock_adapter()
    await adapter.discover()
    assert adapter.firmware == "mock-0.1"
    await adapter.close()

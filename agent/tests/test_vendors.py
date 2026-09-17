"""The other source adapters: plain RTSP, and ONVIF."""

from __future__ import annotations

import httpx
import pytest

from viewport.adapters import ADAPTERS, build_adapter
from viewport.adapters.onvif import OnvifAdapter, OnvifError
from viewport.adapters.rtsp import RtspAdapter, with_credentials
from viewport.config import SourceConfig

PASSWORD = "p@ss w/rd&1"

# Shapes taken from a real Reolink NVS8 (firmware v3.4.0.318) over ONVIF.
DEVICE_INFO = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>
<GetDeviceInformationResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
<Manufacturer>Reolink</Manufacturer><Model>NVS8</Model>
<FirmwareVersion>v3.4.0.318</FirmwareVersion><SerialNumber>0001</SerialNumber>
</GetDeviceInformationResponse></s:Body></s:Envelope>"""

CAPABILITIES = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>
<GetCapabilitiesResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
<Capabilities xmlns="http://www.onvif.org/ver10/schema">
<Media><XAddr>http://cam:8000/onvif/media_service</XAddr></Media>
</Capabilities></GetCapabilitiesResponse></s:Body></s:Envelope>"""

DATE_TIME = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>
<GetSystemDateAndTimeResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
<SystemDateAndTime xmlns="http://www.onvif.org/ver10/schema"><UTCDateTime>
<Time><Hour>4</Hour><Minute>5</Minute><Second>6</Second></Time>
<Date><Year>2031</Year><Month>1</Month><Day>2</Day></Date>
</UTCDateTime></SystemDateAndTime></GetSystemDateAndTimeResponse></s:Body></s:Envelope>"""


def profile(token, name, source, width, height, fps=25, codec="H264", gov=None):
    # Profiles sits in the media wsdl namespace; its children in the schema namespace.
    h264 = f"<H264><GovLength>{gov}</GovLength></H264>" if gov is not None else ""
    return f"""<trt:Profiles token="{token}" xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
 xmlns="http://www.onvif.org/ver10/schema">
<Name>{name}</Name>
<VideoSourceConfiguration><SourceToken>{source}</SourceToken></VideoSourceConfiguration>
<VideoEncoderConfiguration><Encoding>{codec}</Encoding>
<Resolution><Width>{width}</Width><Height>{height}</Height></Resolution>
<RateControl><FrameRateLimit>{fps}</FrameRateLimit><BitrateLimit>6144</BitrateLimit></RateControl>
{h264}</VideoEncoderConfiguration></trt:Profiles>"""


def profiles_response(*entries):
    return ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
            '<GetProfilesResponse xmlns="http://www.onvif.org/ver10/media/wsdl">'
            + "".join(entries) + "</GetProfilesResponse></s:Body></s:Envelope>")


def stream_uri(url):
    return f"""<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>
<GetStreamUriResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
<MediaUri><Uri xmlns="http://www.onvif.org/ver10/schema">{url}</Uri></MediaUri>
</GetStreamUriResponse></s:Body></s:Envelope>"""


DEFAULT_PROFILES = profiles_response(
    profile("p0", "Profile000_MainStream", "vs0", 3840, 2160),
    profile("p1", "Profile001_SubStream", "vs0", 640, 360, fps=10),
    profile("p2", "Profile040_MainStream", "vs1", 2560, 1920, fps=30),
    profile("p3", "Profile041_SubStream", "vs1", 640, 480, fps=10),
)


def onvif_device(profiles_xml=DEFAULT_PROFILES, fault=None):
    """A fake ONVIF device; `requests` records the SOAP actions it was asked for."""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        action = next((a for a in ("GetSystemDateAndTime", "GetDeviceInformation", "GetCapabilities",
                                   "GetProfiles", "GetStreamUri") if f"<{a}" in body), "?")
        requests.append(action)
        if fault and action == fault:
            return httpx.Response(400, text='<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                                            "<s:Body><s:Fault><s:Reason><s:Text>Not Authorized</s:Text>"
                                            "</s:Reason></s:Fault></s:Body></s:Envelope>")
        if action == "GetSystemDateAndTime":
            return httpx.Response(200, text=DATE_TIME)
        if action == "GetDeviceInformation":
            return httpx.Response(200, text=DEVICE_INFO)
        if action == "GetCapabilities":
            return httpx.Response(200, text=CAPABILITIES)
        if action == "GetProfiles":
            return httpx.Response(200, text=profiles_xml)
        token = body.split("<ProfileToken>")[1].split("</ProfileToken>")[0]
        return httpx.Response(200, text=stream_uri(f"rtsp://cam:554/stream/{token}"))

    config = SourceConfig(id="cam", type="onvif", host="cam", username="viewer", password=PASSWORD)
    return OnvifAdapter(config, transport=httpx.MockTransport(handler)), requests


# ----- the registry -----------------------------------------------------------

def test_every_source_type_builds():
    assert set(ADAPTERS) == {"reolink", "onvif", "rtsp"}
    assert isinstance(build_adapter(SourceConfig(id="a", type="onvif", host="h", username="u")), OnvifAdapter)
    assert isinstance(build_adapter(SourceConfig(
        id="b", type="rtsp", cameras_urls=[{"name": "One", "main": "rtsp://h/1"}])), RtspAdapter)


@pytest.mark.parametrize("config,message", [
    ({"id": "a", "type": "rtsp"}, "needs cameras_urls"),
    ({"id": "a", "type": "onvif", "username": "u"}, "needs a host"),
    ({"id": "a", "type": "reolink", "host": "h"}, "needs a username"),
])
def test_a_source_must_carry_what_its_type_needs(config, message):
    with pytest.raises(ValueError, match=message):
        SourceConfig(**config)


# ----- plain RTSP -------------------------------------------------------------

async def test_rtsp_source_lists_its_cameras_and_builds_urls():
    adapter = RtspAdapter(SourceConfig(id="c", type="rtsp", username="viewer", password=PASSWORD,
                                       cameras_urls=[{"name": "Gate", "main": "rtsp://h/main",
                                                      "sub": "rtsp://h/sub"},
                                                     {"name": "Shed", "main": "rtsp://h/only"}]))
    cameras = await adapter.discover()
    assert [(c.id, c.name) for c in cameras] == [("c:0", "Gate"), ("c:1", "Shed")]
    assert adapter.stream_source(cameras[0], "main") == "rtsp://viewer:p%40ss%20w%2Frd%261@h/main"
    assert adapter.stream_source(cameras[0], "sub") == "rtsp://viewer:p%40ss%20w%2Frd%261@h/sub"
    # No sub given: the main stream is used for both rather than leaving a tile black.
    assert adapter.stream_source(cameras[1], "sub").endswith("/only")
    await adapter.close()


@pytest.mark.parametrize("url,expected", [
    ("rtsp://h:554/s", "rtsp://u:p@h:554/s"),
    ("rtsp://other:pw@h/s", "rtsp://other:pw@h/s"),          # its own credentials win
    ("rtsp://h/s?x=1#f", "rtsp://u:p@h/s?x=1#f"),
])
def test_credentials_are_only_added_where_there_are_none(url, expected):
    assert with_credentials(url, "u", "p") == expected


def test_a_url_keeps_its_own_when_the_source_has_no_credentials():
    assert with_credentials("rtsp://h/s", "", "") == "rtsp://h/s"


# ----- ONVIF ------------------------------------------------------------------

async def test_onvif_discovers_channels_from_video_sources():
    adapter, requests = onvif_device()
    cameras = await adapter.discover()
    assert (adapter.model, adapter.firmware) == ("NVS8", "v3.4.0.318")
    assert [(c.id, c.channel, c.name) for c in cameras] == [
        ("cam:0", 0, "Profile000_MainStream"), ("cam:1", 1, "Profile040_MainStream")]
    assert (cameras[0].main.width, cameras[0].main.height, cameras[0].main.codec) == (3840, 2160, "h264")
    assert (cameras[0].sub.width, cameras[0].sub.fps) == (640, 10)
    assert requests[0] == "GetSystemDateAndTime"          # unauthenticated, to sync the clock
    await adapter.close()


async def test_onvif_uses_the_biggest_profile_as_main_and_the_smallest_as_sub():
    adapter, _ = onvif_device()
    cameras = await adapter.discover()
    assert adapter.stream_source(cameras[0], "main").endswith("/stream/p0")
    assert adapter.stream_source(cameras[0], "sub").endswith("/stream/p1")
    await adapter.close()


async def test_onvif_credentials_are_added_to_the_stream_url():
    adapter, _ = onvif_device()
    cameras = await adapter.discover()
    assert adapter.stream_source(cameras[0], "main").startswith("rtsp://viewer:p%40ss%20w%2Frd%261@")
    await adapter.close()


async def test_a_device_with_one_profile_per_channel_still_works():
    adapter, _ = onvif_device(profiles_response(profile("only", "Front", "vs0", 1920, 1080)))
    cameras = await adapter.discover()
    assert len(cameras) == 1 and cameras[0].sub is None
    assert adapter.stream_source(cameras[0], "sub").endswith("/stream/only")   # falls back to main
    await adapter.close()


async def test_hevc_is_reported_as_h265():
    adapter, _ = onvif_device(profiles_response(profile("p", "Cam", "vs0", 3840, 2160, codec="HEVC")))
    cameras = await adapter.discover()
    assert cameras[0].main.codec == "h265"
    await adapter.close()


async def test_the_keyframe_interval_comes_from_the_gov_length():
    adapter, _ = onvif_device(profiles_response(profile("p", "Cam", "vs0", 1920, 1080, fps=25, gov=50)))
    cameras = await adapter.discover()
    assert cameras[0].main.keyframe_seconds == 2.0
    await adapter.close()
    adapter, _ = onvif_device(profiles_response(profile("p", "Cam", "vs0", 1920, 1080)))
    assert (await adapter.discover())[0].main.keyframe_seconds is None     # not reported
    await adapter.close()


async def test_a_soap_fault_becomes_a_readable_error():
    adapter, _ = onvif_device(fault="GetDeviceInformation")
    with pytest.raises(OnvifError, match="Not Authorized"):
        await adapter.discover()
    await adapter.close()


async def test_a_device_with_no_profiles_says_so():
    adapter, _ = onvif_device(profiles_response())
    with pytest.raises(OnvifError, match="no media profiles"):
        await adapter.discover()
    await adapter.close()


async def test_https_against_a_plain_http_onvif_port_explains_itself():
    def refuse(request):
        raise httpx.ConnectError("[SSL: WRONG_VERSION_NUMBER] wrong version number")

    adapter = OnvifAdapter(SourceConfig(id="c", type="onvif", host="h", https=True, username="u"),
                           transport=httpx.MockTransport(refuse))
    with pytest.raises(OnvifError, match="NVR_HTTPS=false"):
        await adapter.discover()
    await adapter.close()


async def test_the_password_is_never_sent_in_the_clear():
    """WS-Security sends a digest of nonce + timestamp + password, never the password."""
    sent: list[str] = []

    def handler(request):
        sent.append(request.content.decode())
        return httpx.Response(200, text=DEVICE_INFO)

    adapter = OnvifAdapter(SourceConfig(id="c", type="onvif", host="h", username="viewer",
                                        password=PASSWORD), transport=httpx.MockTransport(handler))
    await adapter.client.device_information()
    assert PASSWORD not in sent[0]
    assert "PasswordDigest" in sent[0] and "<Nonce" in sent[0] and "<Created" in sent[0]
    await adapter.close()


async def test_onvif_asks_the_device_only_what_can_have_changed():
    """Discovery runs every refresh; re-reading fixed facts would be needless load."""
    adapter, requests = onvif_device()
    await adapter.discover()
    first = list(requests)
    assert first.count("GetStreamUri") == 4              # two channels, main and sub

    requests.clear()
    await adapter.discover()
    assert requests == ["GetProfiles"]                    # nothing else can have changed
    assert len(first) > len(requests)
    await adapter.close()


async def test_onvif_re_reads_the_device_clock_eventually():
    adapter, requests = onvif_device()
    await adapter.discover()
    adapter._clock_synced_at -= 4000                       # an hour later
    requests.clear()
    await adapter.discover()
    assert "GetSystemDateAndTime" in requests
    await adapter.close()


async def test_onvif_fetches_a_stream_url_for_a_profile_it_has_not_seen():
    adapter, requests = onvif_device()
    await adapter.discover()
    adapter._stream_uris.pop("p1")                         # as if a channel gained a profile
    requests.clear()
    await adapter.discover()
    assert requests.count("GetStreamUri") == 1
    await adapter.close()


# ----- ONVIF events -----------------------------------------------------------

from viewport.adapters.onvif import detection_type, _is_on     # noqa: E402


@pytest.mark.parametrize("topic,expected", [
    ("tns1:RuleEngine/CellMotionDetector/Motion", "motion"),
    ("tns1:VideoSource/MotionAlarm", "motion"),
    ("tns1:RuleEngine/MyRuleDetector/PeopleDetect", "person"),
    ("tns1:RuleEngine/MyRuleDetector/VehicleDetect", "vehicle"),
    ("tns1:RuleEngine/MyRuleDetector/DogCatDetect", "animal"),
    ("tnsaxis:CameraApplicationPlatform/ObjectAnalytics", None),
    ("tns1:Device/HardwareFailure/StorageFailure", None),
])
def test_onvif_topics_map_to_our_detection_types(topic, expected):
    """Vendors prefix topics differently, so the tail is what is matched."""
    assert detection_type(topic) == expected


@pytest.mark.parametrize("data,expected", [
    ({"State": "true"}, True), ({"State": "false"}, False),
    ({"IsMotion": "1"}, True), ({"Value": "off"}, False),
    ({}, True),                                    # no state at all: treat as "it happened"
])
def test_an_event_says_whether_it_started_or_stopped(data, expected):
    assert _is_on(data) is expected

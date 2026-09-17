"""Automatic RTSP <-> FLV fallback when one stream keeps failing."""

from __future__ import annotations

import httpx

from conftest import make_camera
from test_go2rtc import FakeGo2rtc
from viewport.adapters.reolink import ReolinkAdapter
from viewport.config import AppConfig, SourceConfig
from viewport.go2rtc import Go2rtcClient
from viewport.runtime import FAILURES_BEFORE_FALLBACK, Runtime


def make_source(**kw) -> SourceConfig:
    return SourceConfig(id="nvr", host="nvr", username="u", password="p", **kw)


def make_runtime(source: SourceConfig | None = None, cameras: int = 2):
    source = source or make_source()
    config = AppConfig(sources=[source])
    adapter = ReolinkAdapter(source)
    fake = FakeGo2rtc()
    client = Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(fake.handler))
    rt = Runtime(config, adapters=[adapter], go2rtc=client)
    rt.wall.update_cameras("nvr", [make_camera(i) for i in range(cameras)])
    rt._update_desired_streams()
    rt.registry.reachable = True                 # go2rtc is fine; the stream is not
    return rt, adapter, fake


def report(rt: Runtime, stream: str, state: str, times: int = 1, renderer: str = "r1"):
    for _ in range(times):
        rt.record_renderer_stats(renderer, {"tiles": [{"tile": "t0", "stream": stream,
                                                       "state": state, "error": "no frames"}]})


# ----- the adapter's half ---------------------------------------------------

def test_next_protocol_flips_between_rtsp_and_flv():
    adapter = ReolinkAdapter(make_source())
    cam = make_camera(0)                           # h264 1080p -> auto picks flv
    assert adapter.protocol_for(cam, "main") == "flv"
    assert adapter.next_protocol(cam, "main") == "rtsp"
    assert adapter.protocol_for(cam, "main") == "rtsp"
    assert adapter.stream_source(cam, "main").startswith("rtsp://")
    assert adapter.next_protocol(cam, "main") == "flv"   # and back again


def test_next_protocol_only_affects_the_one_stream():
    adapter = ReolinkAdapter(make_source())
    cam = make_camera(0)
    adapter.next_protocol(cam, "main")
    assert adapter.protocol_for(cam, "sub") == "flv"
    assert adapter.protocol_for(make_camera(1), "main") == "flv"


def test_pinned_protocol_still_falls_back():
    """A transport that cannot connect is no use pinned."""
    adapter = ReolinkAdapter(make_source(protocol="rtsp"))
    cam = make_camera(0)
    assert adapter.protocol_for(cam, "main") == "rtsp"
    assert adapter.next_protocol(cam, "main") == "flv"


def test_fallback_can_be_switched_off():
    adapter = ReolinkAdapter(make_source(protocol_fallback=False))
    assert adapter.next_protocol(make_camera(0), "main") is None


# ----- the runtime's half ---------------------------------------------------

async def test_repeated_failures_switch_the_transport():
    rt, adapter, _ = make_runtime()
    before = rt.registry.desired["nvr_0_sub"]
    assert before.startswith("http://")           # FLV to begin with

    report(rt, "nvr_0_sub", "error", FAILURES_BEFORE_FALLBACK)

    assert rt.registry.desired["nvr_0_sub"].startswith("rtsp://")
    assert rt.fallbacks == {"nvr_0_sub": "rtsp"}
    assert rt.health()["streams"]["fallbacks"] == {"nvr_0_sub": "rtsp"}
    assert rt.registry.desired["nvr_1_sub"] == before.replace("channel0", "channel1")


async def test_one_or_two_failures_are_tolerated():
    rt, _, _ = make_runtime()
    report(rt, "nvr_0_sub", "error", FAILURES_BEFORE_FALLBACK - 1)
    assert rt.fallbacks == {}
    assert rt.registry.desired["nvr_0_sub"].startswith("http://")


async def test_a_successful_frame_clears_the_count():
    rt, _, _ = make_runtime()
    report(rt, "nvr_0_sub", "error", FAILURES_BEFORE_FALLBACK - 1)
    report(rt, "nvr_0_sub", "playing")
    report(rt, "nvr_0_sub", "error", FAILURES_BEFORE_FALLBACK - 1)
    assert rt.fallbacks == {}


async def test_cooldown_stops_flapping_when_both_transports_fail():
    rt, _, _ = make_runtime()
    report(rt, "nvr_0_sub", "error", FAILURES_BEFORE_FALLBACK)
    assert rt.fallbacks["nvr_0_sub"] == "rtsp"
    report(rt, "nvr_0_sub", "error", FAILURES_BEFORE_FALLBACK * 3)
    assert rt.fallbacks["nvr_0_sub"] == "rtsp"    # still the first switch
    assert rt.registry.desired["nvr_0_sub"].startswith("rtsp://")


async def test_go2rtc_being_down_does_not_switch_anything():
    """Every tile fails at once then, which says nothing about a single transport."""
    rt, _, _ = make_runtime()
    rt.registry.reachable = False
    report(rt, "nvr_0_sub", "error", FAILURES_BEFORE_FALLBACK * 2)
    assert rt.fallbacks == {}


async def test_mjpeg_variant_counts_against_its_upstream():
    rt, _, _ = make_runtime()
    report(rt, "nvr_0_sub_mjpeg", "error", FAILURES_BEFORE_FALLBACK)
    assert rt.fallbacks == {"nvr_0_sub": "rtsp"}


async def test_malformed_reports_are_ignored():
    rt, _, _ = make_runtime()
    for _ in range(FAILURES_BEFORE_FALLBACK):
        rt.record_renderer_stats("r1", {"tiles": ["nonsense", {}, 42,
                                                  {"stream": "", "state": "error"},
                                                  {"state": "error"}]})
    assert rt.fallbacks == {}


async def test_a_stream_with_no_camera_is_ignored():
    """A renderer can report a stale stream name after the camera list changed."""
    rt, _, _ = make_runtime()
    report(rt, "nvr_9_sub", "error", FAILURES_BEFORE_FALLBACK * 2)
    assert rt.fallbacks == {}
    assert rt.health()["streams"]["failing"]["nvr_9_sub"] >= FAILURES_BEFORE_FALLBACK


async def test_fallback_tells_renderers_to_reconnect():
    """The stream name is unchanged, so an epoch bump is what makes a renderer retry."""
    rt, _, _ = make_runtime()
    before = next(t["epoch"] for t in rt.wall.snapshot()["tiles"] if t["stream"] == "nvr_0_sub")

    report(rt, "nvr_0_sub", "error", FAILURES_BEFORE_FALLBACK)

    tiles = {t["stream"]: t["epoch"] for t in rt.wall.snapshot()["tiles"] if t["stream"]}
    assert tiles["nvr_0_sub"] == before + 1
    assert tiles["nvr_1_sub"] == 0                # untouched streams keep their epoch


async def test_epoch_is_stable_when_nothing_falls_back():
    rt, _, _ = make_runtime()
    report(rt, "nvr_0_sub", "playing", 5)
    rt.wall.set_fullscreen("t0")
    assert all(t["epoch"] == 0 for t in rt.wall.snapshot()["tiles"])

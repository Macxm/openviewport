from __future__ import annotations

import httpx
import pytest

from viewport.go2rtc import Go2rtcClient, StreamRegistry, redact


class FakeGo2rtc:
    def __init__(self):
        self.streams: dict[str, dict] = {"static_cam": {"producers": [{"url": "rtsp://x"}], "consumers": None}}
        self.calls: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.down = False
        self.read_only_config = True     # as in docker-compose.yml

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused")
        self.requests.append(request)
        name = request.url.params.get("name")
        src = request.url.params.get("src")
        if request.method == "GET":
            return httpx.Response(200, json=self.streams)
        self.calls.append((request.method, name or src))
        if request.method == "PATCH":
            self.streams[name] = {"producers": [{"url": src}], "consumers": None}
        elif request.method == "DELETE":
            # As go2rtc 1.9.14 behaves, verified against the real binary: the stream name
            # goes in `src`; `?name=` is a silent no-op. A real delete then fails to save
            # the read-only go2rtc.yaml and answers 400 although the stream is gone.
            if src is None:
                return httpx.Response(200, json=self.streams)
            existed = self.streams.pop(src, None) is not None
            if existed and self.read_only_config:
                return httpx.Response(400, text="open /config/go2rtc.yaml: read-only file system")
        return httpx.Response(200)


def make_registry(fake):
    return StreamRegistry(Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(fake.handler)), ["nvr"])


async def test_sync_creates_updates_and_removes():
    fake = FakeGo2rtc()
    reg = make_registry(fake)
    reg.set_desired({"nvr_0_sub": "rtsp://a", "nvr_0_main": "rtsp://b"})
    assert await reg.sync()
    assert sorted(fake.calls) == [("PATCH", "nvr_0_main"), ("PATCH", "nvr_0_sub")]

    fake.calls.clear()
    assert await reg.sync()
    assert fake.calls == []                                  # nothing changed, nothing sent

    fake.streams["nvr_9_sub"] = {"producers": [{"url": "old"}]}
    reg.set_desired({"nvr_0_sub": "rtsp://a2"})
    assert await reg.sync()
    assert sorted(fake.calls) == [("DELETE", "nvr_0_main"), ("DELETE", "nvr_9_sub"), ("PATCH", "nvr_0_sub")]
    assert "static_cam" in fake.streams                       # not ours: left alone


async def test_sync_reapplies_after_go2rtc_restart():
    fake = FakeGo2rtc()
    reg = make_registry(fake)
    reg.set_desired({"nvr_0_sub": "rtsp://a"})
    await reg.sync()
    fake.streams = {}                                         # go2rtc restarted
    fake.calls.clear()
    await reg.sync()
    assert fake.calls == [("PATCH", "nvr_0_sub")]


async def test_sync_reports_unreachable():
    fake = FakeGo2rtc()
    fake.down = True
    reg = make_registry(fake)
    reg.set_desired({"nvr_0_sub": "rtsp://a"})
    assert not await reg.sync()
    assert reg.reachable is False and reg.last_error


async def test_stats_hide_credentials():
    fake = FakeGo2rtc()
    fake.streams["nvr_0_sub"] = {
        "producers": [{"url": "rtsp://admin:secret@nvr/Preview_01_sub", "remote_addr": "10.0.0.2:554"}],
        "consumers": [{"id": 1}, {"id": 2}],
    }
    stats = await make_registry(fake).stats()
    assert stats == {"nvr_0_sub": {"source": "rtsp://***@nvr/Preview_01_sub",
                                   "upstream_connected": True, "consumers": 2}}


def test_redact():
    assert redact("http://nvr/flv?stream=a&user=admin&password=p%40ss") == \
        "http://nvr/flv?stream=a&user=admin&password=***"
    assert redact("rtsp://u:p@host:554/x") == "rtsp://***@host:554/x"


async def test_delete_really_removes_the_stream():
    """Regression: `?name=` returned 200 and deleted nothing, so stale streams piled up."""
    fake = FakeGo2rtc()
    reg = make_registry(fake)
    reg.set_desired({"nvr_0_sub": "rtsp://a"})
    assert await reg.sync()
    reg.set_desired({})
    fake.requests.clear()
    assert await reg.sync()                       # the 400 from the read-only config is not a failure
    delete = next(r for r in fake.requests if r.method == "DELETE")
    assert delete.url.params.get("src") == "nvr_0_sub"
    assert "nvr_0_sub" not in fake.streams
    assert reg.reachable is True


async def test_a_400_that_left_the_stream_in_place_is_an_error():
    fake = FakeGo2rtc()
    fake.streams["nvr_0_sub"] = {"producers": [{"url": "rtsp://a"}]}
    original = fake.handler

    def refuse_deletes(request):
        if request.method == "DELETE":
            return httpx.Response(400, text="something else went wrong")
        return original(request)

    client = Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(refuse_deletes))
    with pytest.raises(httpx.HTTPStatusError):
        await client.delete_stream("nvr_0_sub")


async def test_delete_works_with_a_writable_config_too():
    fake = FakeGo2rtc()
    fake.read_only_config = False
    fake.streams["nvr_0_sub"] = {"producers": [{"url": "rtsp://a"}]}
    await make_registry(fake).client.delete_stream("nvr_0_sub")
    assert "nvr_0_sub" not in fake.streams


async def test_sync_keeps_streams_until_first_discovery():
    """A restarted agent must not tear down the streams a running go2rtc already serves."""
    fake = FakeGo2rtc()
    fake.streams["nvr_0_sub"] = {"producers": [{"url": "rtsp://a"}]}
    reg = make_registry(fake)
    assert reg.primed is False

    assert await reg.sync()                      # discovery has not finished yet
    assert fake.calls == []                      # nothing removed
    assert "nvr_0_sub" in fake.streams

    reg.set_desired({"nvr_0_sub": "rtsp://a"})   # discovery lands: same stream, no churn
    assert await reg.sync()
    assert [c for c in fake.calls if c[0] == "DELETE"] == []

    reg.set_desired({})                          # a real empty result does prune
    assert await reg.sync()
    assert ("DELETE", "nvr_0_sub") in fake.calls

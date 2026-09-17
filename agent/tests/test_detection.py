"""Detection → primary screen: adapter, wall presentation, poll loop, API."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_camera
from mock_nvr.app import create_app as create_mock_app
from mock_nvr.settings import Settings as MockSettings
from test_api import FakeAdapter
from test_go2rtc import FakeGo2rtc
from test_wall import FakeClock
from viewport.adapters.reolink import ReolinkAdapter
from viewport.api import create_app
from viewport.config import AppConfig, DetectionConfig, DeviceConfig, SourceConfig, ViewConfig
from viewport.go2rtc import Go2rtcClient
from viewport.runtime import Runtime
from viewport.store import ConfigStore
from viewport.wall import WallController


def make_wall(count=4, layout="2x2", **detection):
    config = AppConfig(
        device=DeviceConfig(main_stream_hold_seconds=0),
        detection=DetectionConfig(enabled=True, **{"hold_seconds": 10, "min_focus_seconds": 0,
                                                   "manual_override_seconds": 30, **detection}),
        views=[ViewConfig(name="All", layout=layout), ViewConfig(name="Two", layout="1x1", cameras=["nvr:1"])],
    )
    clock = FakeClock()
    wall = WallController(config, clock=clock)
    wall.update_cameras("nvr", [make_camera(i) for i in range(count)])
    return wall, clock


def see(**cams):
    return {f"nvr:{cam[1:]}": frozenset(types.split(",")) for cam, types in cams.items()}


def tile_of(wall, camera_id):
    return next(t for t in wall.snapshot()["tiles"] if (t["camera"] or {}).get("id") == camera_id)


# ----- Reolink: GetMdState + GetAiState ------------------------------------------

def reolink(mock: MockSettings):
    app = create_mock_app(mock)
    adapter = ReolinkAdapter(SourceConfig(id="nvr", host="nvr", username="admin", password=mock.password),
                             transport=httpx.ASGITransport(app=app))
    return adapter, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nvr")


async def test_reolink_reports_ai_and_motion_detections():
    adapter, mock = reolink(MockSettings(channels=3, password="mockpass"))
    cameras = await adapter.discover()
    assert await adapter.detections(cameras) == {}
    await mock.post("/mock/detect", params={"channel": 1, "kind": "people", "seconds": 30})
    await mock.post("/mock/detect", params={"channel": 2, "kind": "motion", "seconds": 30})
    assert await adapter.detections(cameras) == {"nvr:1": frozenset({"person", "motion"}),
                                                 "nvr:2": frozenset({"motion"})}
    await adapter.close()


async def test_reolink_ignores_ai_types_the_camera_does_not_support():
    def handler(request):
        import json
        out = []
        for item in json.loads(request.content):
            if item["cmd"] == "Login":
                out.append({"cmd": "Login", "code": 0, "value": {"Token": {"leaseTime": 3600, "name": "t"}}})
            elif item["cmd"] == "GetMdState":
                out.append({"cmd": "GetMdState", "code": 0, "value": {"state": 0}})
            else:
                out.append({"cmd": "GetAiState", "code": 0, "value": {
                    "face": {"alarm_state": 1, "support": 0},       # firmware noise
                    "vehicle": {"alarm_state": 1, "support": 1}}})
        return httpx.Response(200, json=out)

    adapter = ReolinkAdapter(SourceConfig(id="nvr", host="nvr", username="u", password="p"),
                             transport=httpx.MockTransport(handler))
    assert await adapter.detections([make_camera(0)]) == {"nvr:0": frozenset({"vehicle"})}
    await adapter.close()


async def test_reolink_polls_every_camera_in_one_request():
    adapter, _ = reolink(MockSettings(channels=4, password="mockpass"))
    cameras = await adapter.discover()
    seen = []
    original = adapter.client.batch

    async def counting(commands):
        seen.append([c["cmd"] for c in commands])
        return await original(commands)

    adapter.client.batch = counting
    await adapter.detections(cameras)
    assert seen == [["GetMdState", "GetAiState"] * 4]
    await adapter.close()


# ----- the wall: presentations ---------------------------------------------------

def test_fullscreen_action_uses_the_warm_fullscreen_when_the_camera_is_in_view():
    wall, _ = make_wall(action="fullscreen")
    wall.update_detections(see(c2="person"))
    snap = wall.snapshot()
    assert snap["layout"]["id"] == "2x2"                        # the grid is still there, warm
    assert snap["fullscreen"] == tile_of(wall, "nvr:2")["id"]
    assert snap["focus"] == {"camera": "nvr:2", "name": "Cam 3", "reason": "person", "presentation": "tile"}
    assert tile_of(wall, "nvr:2")["quality"] == "main"


def test_fullscreen_action_shows_a_camera_outside_the_view_on_its_own():
    wall, _ = make_wall(action="fullscreen")
    wall.set_view(1, by_user=False)                           # view "Two" shows only nvr:1
    wall.update_detections(see(c3="person"))
    snap = wall.snapshot()
    assert snap["layout"]["id"] == "1x1"
    assert [t["camera"]["id"] for t in snap["tiles"]] == ["nvr:3"]
    assert snap["focus"]["presentation"] == "layout"


def test_promote_action_puts_the_camera_in_the_big_tile_and_keeps_the_rest():
    wall, _ = make_wall(count=4, action="promote", promote_layout="1+5")
    wall.update_detections(see(c3="vehicle"))
    snap = wall.snapshot()
    assert snap["layout"]["id"] == "1+5"
    order = [(t["camera"] or {}).get("id") for t in snap["tiles"]]
    assert order[:4] == ["nvr:3", "nvr:0", "nvr:1", "nvr:2"]
    assert snap["tiles"][0]["quality"] == "main"


def test_highlight_action_changes_nothing_but_marks_the_tile():
    wall, _ = make_wall(action="highlight")
    wall.update_detections(see(c1="person"))
    snap = wall.snapshot()
    assert snap["layout"]["id"] == "2x2" and snap["fullscreen"] is None
    assert tile_of(wall, "nvr:1")["focus"] is True
    assert tile_of(wall, "nvr:1")["detections"] == ["person"]


def test_after_rapid_focus_changes_the_newest_main_stream_is_the_one_held():
    """Regression, found live: the held slot went to tile order, reopening an old stream."""
    config = AppConfig(device=DeviceConfig(main_stream_hold_seconds=30, max_main_streams=1),
                       detection=DetectionConfig(enabled=True, hold_seconds=0, min_focus_seconds=0,
                                                 manual_override_seconds=0),
                       views=[ViewConfig(name="All", layout="2x2")])
    clock = FakeClock()
    wall = WallController(config, clock=clock)
    wall.update_cameras("nvr", [make_camera(i) for i in range(4)])
    wall.update_detections(see(c1="vehicle"))       # Driveway full screen
    clock.advance(5)
    wall.update_detections(see(c3="person"))        # then Garage
    clock.advance(1)
    wall.update_detections({})                      # both quiet; both mains still held
    wall.tick()
    assert [t["camera"]["id"] for t in wall.snapshot()["tiles"] if t["quality"] == "main"] == ["nvr:3"]


def test_every_tile_reports_its_detections_even_without_focus():
    wall, _ = make_wall(triggers=["person"])
    wall.update_detections(see(c0="motion"))
    assert tile_of(wall, "nvr:0")["detections"] == ["motion"]
    assert wall.snapshot()["focus"] is None


def test_focus_ends_after_the_hold_and_the_view_returns():
    wall, clock = make_wall(hold_seconds=10)
    wall.update_detections(see(c2="person"))
    wall.update_detections({})
    clock.advance(9)
    wall.tick()
    assert wall.snapshot()["focus"] is not None
    clock.advance(2)
    wall.tick()
    snap = wall.snapshot()
    assert snap["focus"] is None and snap["fullscreen"] is None


def test_stale_detections_expire_if_the_nvr_stops_answering():
    wall, clock = make_wall(hold_seconds=5, poll_seconds=2)
    wall.update_detections(see(c2="person"))
    for _ in range(30):                                        # no more polls arrive
        clock.advance(1)
        wall.tick()
    assert wall.snapshot()["focus"] is None


# ----- the wall: people come first -------------------------------------------------

def test_a_person_pressing_escape_dismisses_the_focus_and_it_stays_away():
    wall, clock = make_wall(manual_override_seconds=30)
    wall.update_detections(see(c2="person"))
    wall.set_fullscreen(None)                                  # Escape on the remote
    assert wall.snapshot()["fullscreen"] is None
    for _ in range(15):                                        # polls keep reporting the person
        clock.advance(2)
        wall.update_detections(see(c2="person"))
        wall.tick()
        if clock.now - 1000 < 30:
            assert wall.snapshot()["fullscreen"] is None       # override holds
    assert wall.snapshot()["focus"]["camera"] == "nvr:2"      # override over: back on screen


def test_shortening_the_override_in_settings_applies_at_once():
    """Regression: the override kept its old length until it ran out."""
    wall, clock = make_wall(manual_override_seconds=60)
    wall.update_detections(see(c2="person"))
    wall.dismiss_focus()
    assert wall.override_active()
    wall.set_detection_config(wall.config.detection.model_copy(update={"manual_override_seconds": 0}))
    assert not wall.override_active()
    wall.update_detections(see(c2="person"))
    assert wall.snapshot()["focus"]["camera"] == "nvr:2"


def test_dismiss_clears_the_focus_immediately():
    wall, _ = make_wall()
    wall.update_detections(see(c2="person"))
    wall.dismiss_focus()
    assert wall.snapshot()["focus"] is None and wall.snapshot()["fullscreen"] is None


def test_toggling_the_focused_tile_closes_it():
    wall, _ = make_wall()
    wall.update_detections(see(c2="person"))
    tile = wall.snapshot()["fullscreen"]
    wall.toggle_fullscreen(tile)
    assert wall.snapshot()["fullscreen"] is None


def test_a_manual_fullscreen_returns_when_the_focus_ends():
    wall, clock = make_wall(hold_seconds=5, manual_override_seconds=10)
    wall.set_fullscreen("t0")                                  # someone watches camera 0
    clock.advance(11)                                          # ...and walks away
    wall.tick()
    wall.update_detections(see(c3="person"))
    assert wall.snapshot()["focus"]["camera"] == "nvr:3"
    wall.update_detections({})
    clock.advance(6)
    wall.tick()
    assert wall.snapshot()["fullscreen"] == "t0"               # back to what they chose


def test_unwatched_cameras_never_take_focus():
    wall, _ = make_wall(cameras=["nvr:0"])
    wall.update_detections(see(c3="person"))
    assert wall.snapshot()["focus"] is None
    assert tile_of(wall, "nvr:3")["detections"] == []


def test_automatic_view_steps_do_not_count_as_a_person():
    wall, _ = make_wall()
    wall.step_view(1, by_user=False)
    assert not wall.override_active()
    wall.step_view(1)
    assert wall.override_active()


# ----- the runtime poll loop -----------------------------------------------------------

def make_runtime(adapter, detection=None):
    config = AppConfig(detection=detection or DetectionConfig(enabled=True, poll_seconds=2))
    rt = Runtime(config, adapters=[adapter],
                 go2rtc=Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(FakeGo2rtc().handler)),
                 store=ConfigStore(""))
    rt.wall.update_cameras("nvr", [make_camera(0), make_camera(1, online=False)])
    return rt


class DetectingAdapter(FakeAdapter):
    supports_detection = True

    def __init__(self, cameras, result=None, error=None):
        super().__init__(cameras)
        self.result, self.error, self.calls = result or {}, error, []

    async def detections(self, cameras):
        self.calls.append([c.id for c in cameras])
        if self.error:
            raise self.error
        return self.result


async def test_poll_asks_only_for_online_watched_cameras_and_feeds_the_wall():
    adapter = DetectingAdapter([make_camera(0)], result={"nvr:0": frozenset({"person"})})
    rt = make_runtime(adapter)
    await rt.poll_detections()
    assert adapter.calls == [["nvr:0"]]                        # nvr:1 is offline
    assert rt.wall.snapshot()["focus"]["camera"] == "nvr:0"


async def test_poll_backs_off_after_a_failure():
    adapter = DetectingAdapter([make_camera(0)], error=httpx.ConnectError("refused"))
    rt = make_runtime(adapter)
    await rt.poll_detections()
    await rt.poll_detections()                                 # still inside the back-off
    assert len(adapter.calls) == 1
    health = rt.health()["sources"][0]["detection"]
    assert health == {"supported": True, "last_error": "refused"}


async def test_sources_without_detection_are_never_polled():
    adapter = FakeAdapter([make_camera(0)])
    rt = make_runtime(adapter)
    await rt.poll_detections()
    assert rt.wall.snapshot()["focus"] is None


# ----- API and persistence -------------------------------------------------------------

def api_client(tmp_path):
    config = AppConfig(detection=DetectionConfig(enabled=True))
    rt = Runtime(config, adapters=[FakeAdapter([make_camera(0), make_camera(1)])],
                 go2rtc=Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(FakeGo2rtc().handler)),
                 store=ConfigStore(tmp_path / "state.json"))
    rt.wall.update_cameras("nvr", [make_camera(0), make_camera(1)])
    return TestClient(create_app(config, rt, start_background=False)), rt


def test_detection_settings_are_editable_live_and_persisted(tmp_path):
    client, rt = api_client(tmp_path)
    body = client.get("/api/config").json()
    assert body["detection"]["action"] == "fullscreen"
    new = {**body["detection"], "action": "promote", "triggers": ["vehicle", "person"], "hold_seconds": 42}
    assert client.put("/api/config/detection", json=new).status_code == 200
    assert rt.config.detection.action == "promote"
    assert rt.wall.snapshot()["detection"]["action"] == "promote"
    assert ConfigStore(tmp_path / "state.json").apply(AppConfig()).detection.hold_seconds == 42


@pytest.mark.parametrize("bad", [{"triggers": []}, {"triggers": ["ghosts"]}, {"poll_seconds": 0.1},
                                 {"promote_layout": "9x9"}, {"action": "explode"}])
def test_invalid_detection_settings_are_refused(tmp_path, bad):
    client, rt = api_client(tmp_path)
    current = client.get("/api/config").json()["detection"]
    assert client.put("/api/config/detection", json={**current, **bad}).status_code == 422
    assert rt.config.detection.action == "fullscreen"


def test_the_renderer_and_the_api_can_dismiss_a_focus(tmp_path):
    client, rt = api_client(tmp_path)
    rt.wall.update_detections({"nvr:1": frozenset({"person"})})
    assert rt.wall.snapshot()["focus"] is not None
    assert client.post("/api/wall/focus/dismiss").json()["focus"] is None
    rt.wall.update_detections({"nvr:1": frozenset({"person"})})
    with client.websocket_connect("/api/wall/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "focus", "action": "dismiss"})
        assert ws.receive_json()["focus"] is None


# ----- view rotation ----------------------------------------------------------

def cycling_runtime(seconds):
    config = AppConfig(device=DeviceConfig(cycle_views_seconds=seconds),
                       views=[ViewConfig(name="A"), ViewConfig(name="B")])
    rt = Runtime(config, adapters=[FakeAdapter([make_camera(0)])], store=ConfigStore(""),
                 go2rtc=Go2rtcClient("http://g", transport=httpx.MockTransport(FakeGo2rtc().handler)))
    rt.wall.update_cameras("nvr", [make_camera(0)])
    return rt


async def test_view_rotation_can_be_switched_on_at_runtime():
    """Regression: the loop was only created at startup, so the admin setting did nothing."""
    import asyncio
    rt = cycling_runtime(0)
    await rt.start()
    try:
        rt.apply_device(rt.config.device.model_copy(update={"cycle_views_seconds": 1}))
        # Sample rather than compare endpoints: with two views and a one-second interval,
        # it is back where it started every two seconds.
        seen = set()
        for _ in range(25):
            await asyncio.sleep(0.1)
            seen.add(rt.wall.view_index)
        assert len(seen) > 1
    finally:
        await rt.stop()


async def test_view_rotation_switched_off_does_not_spin():
    """Regression: sleep(0) returned at once, flipping views tens of thousands of times."""
    import asyncio
    rt = cycling_runtime(1)
    await rt.start()
    try:
        rt.apply_device(rt.config.device.model_copy(update={"cycle_views_seconds": 0}))
        version = rt.wall.version
        await asyncio.sleep(1.2)
        assert rt.wall.version == version
    finally:
        await rt.stop()


async def test_rotation_waits_again_after_someone_uses_the_wall():
    import asyncio
    rt = cycling_runtime(2)
    await rt.start()
    try:
        rt.wall.set_fullscreen("t0")            # a person is looking at one camera
        before = rt.wall.view_index
        await asyncio.sleep(2.5)
        assert rt.wall.view_index == before     # rotation waits for them
    finally:
        await rt.stop()


def test_a_camera_that_goes_offline_gives_the_screen_back():
    """Regression: the wall stayed full screen on a black tile until the hold expired."""
    wall, _ = make_wall(hold_seconds=30)
    wall.update_detections(see(c2="person"))
    assert wall.snapshot()["fullscreen"] is not None

    wall.update_cameras("nvr", [make_camera(i, online=(i != 2)) for i in range(4)])
    snap = wall.snapshot()
    assert snap["fullscreen"] is None and snap["focus"] is None


def test_the_camera_is_shown_again_if_it_comes_back_within_the_hold():
    wall, _ = make_wall(hold_seconds=30)
    wall.update_detections(see(c2="person"))
    wall.update_cameras("nvr", [make_camera(i, online=(i != 2)) for i in range(4)])
    wall.update_cameras("nvr", [make_camera(i) for i in range(4)])
    assert wall.snapshot()["focus"]["camera"] == "nvr:2"


async def test_renderers_that_stopped_reporting_are_forgotten():
    """Regression: every page load added an entry that was never removed."""
    rt = make_runtime(DetectingAdapter([make_camera(0)]))
    for i in range(50):
        rt.record_renderer_stats(f"browser-{i}", {"tiles": []})
    assert len(rt.renderers) == 50

    for seen in rt.renderers.values():          # as if a minute passed
        seen["received"] -= 120
    rt.record_renderer_stats("browser-new", {"tiles": []})
    assert list(rt.renderers) == ["browser-new"]
    assert list(rt.health()["renderers"]) == ["browser-new"]


def test_detection_can_enlarge_a_camera_without_asking_for_hd():
    wall, _ = make_wall(action="fullscreen", use_main_stream=False)
    wall.update_detections(see(c2="person"))
    snap = wall.snapshot()
    assert snap["fullscreen"] == tile_of(wall, "nvr:2")["id"]     # still full screen
    assert tile_of(wall, "nvr:2")["quality"] == "sub"             # but no extra NVR load
    assert snap["budget"]["main_in_use"] == 0


def test_detection_asks_for_hd_by_default():
    wall, _ = make_wall(action="fullscreen")
    wall.update_detections(see(c2="person"))
    assert tile_of(wall, "nvr:2")["quality"] == "main"


def only_on_detection(wall):
    wall.set_device(DeviceConfig(main_stream_hold_seconds=0, main_stream_only_on_detection=True))


def test_full_resolution_can_be_saved_for_when_a_camera_sees_something():
    """A big tile holds a "Clear" session open all day for a view nobody is watching."""
    wall, _ = make_wall(action="promote", layout="1+3")
    only_on_detection(wall)
    assert wall.snapshot()["budget"]["main_in_use"] == 0           # the big tile waits
    wall.update_detections(see(c2="person"))
    assert tile_of(wall, "nvr:2")["quality"] == "main"
    assert wall.snapshot()["budget"]["main_in_use"] == 1


def test_that_setting_still_leaves_the_choice_of_hd_to_the_user():
    wall, _ = make_wall(action="promote", layout="1+3", use_main_stream=False)
    only_on_detection(wall)
    wall.update_detections(see(c2="person"))
    assert tile_of(wall, "nvr:2")["quality"] == "sub"
    assert wall.snapshot()["budget"]["main_in_use"] == 0


def test_a_highlighted_camera_gets_hd_even_though_its_tile_stays_small():
    """Otherwise reserving HD for detections would never hand it out in this mode."""
    wall, _ = make_wall(action="highlight", layout="2x2")
    only_on_detection(wall)
    wall.update_detections(see(c2="person"))
    assert tile_of(wall, "nvr:2")["quality"] == "main"


def test_a_person_going_fullscreen_gets_hd_even_when_hd_waits_for_detections():
    """The setting is about the wall left alone; someone who taps a camera wants to see it."""
    wall, _ = make_wall(action="promote", layout="2x2")
    only_on_detection(wall)
    wall.set_fullscreen("t1")
    assert tile_of(wall, "nvr:1")["quality"] == "main"


def test_a_person_going_fullscreen_on_a_highlighted_camera_still_gets_hd():
    """Nor does `use_main_stream: false` take it away because a detection is also on it."""
    wall, _ = make_wall(action="highlight", layout="2x2", use_main_stream=False)
    only_on_detection(wall)
    wall.update_detections(see(c1="person"))
    wall.set_fullscreen("t1")
    assert tile_of(wall, "nvr:1")["quality"] == "main"


def test_the_camera_goes_back_to_sd_when_the_detection_ends():
    wall, clock = make_wall(action="promote", layout="1+3", hold_seconds=0)
    only_on_detection(wall)
    wall.update_detections(see(c2="person"))
    assert tile_of(wall, "nvr:2")["quality"] == "main"
    wall.update_detections({})
    clock.advance(30)
    wall.tick()
    assert wall.snapshot()["budget"]["main_in_use"] == 0


def test_a_person_going_fullscreen_still_gets_hd_when_detection_does_not():
    wall, _ = make_wall(action="fullscreen", use_main_stream=False)
    wall.set_fullscreen("t0")
    assert tile_of(wall, "nvr:0")["quality"] == "main"


# ----- how long a highlight lingers -------------------------------------------

def test_the_badge_lingers_after_the_detection_stops():
    wall, clock = make_wall(action="highlight", highlight_seconds=10)
    wall.update_detections(see(c1="person"))
    assert tile_of(wall, "nvr:1")["detections"] == ["person"]

    wall.update_detections({})
    assert tile_of(wall, "nvr:1")["detections"] == ["person"]      # brief sighting, still shown
    clock.advance(9)
    wall.tick()
    assert tile_of(wall, "nvr:1")["detections"] == ["person"]
    clock.advance(2)
    wall.tick()
    assert tile_of(wall, "nvr:1")["detections"] == []


def test_zero_seconds_shows_the_badge_only_while_detecting():
    wall, _ = make_wall(action="highlight", highlight_seconds=0)
    wall.update_detections(see(c1="person"))
    assert tile_of(wall, "nvr:1")["detections"] == ["person"]
    wall.update_detections({})
    assert tile_of(wall, "nvr:1")["detections"] == []


def test_a_fresh_detection_restarts_the_linger():
    wall, clock = make_wall(action="highlight", highlight_seconds=10)
    wall.update_detections(see(c1="person"))
    clock.advance(8)
    wall.update_detections(see(c1="person"))
    clock.advance(8)
    wall.tick()
    assert tile_of(wall, "nvr:1")["detections"] == ["person"]


# ----- which layout a promotion uses ------------------------------------------

def test_promote_keeps_the_views_own_layout_by_default():
    """Regression: a 1+2 view jumped to 1+5, leaving three empty tiles."""
    wall, _ = make_wall(count=3, layout="1+2", action="promote", promote_layout="same")
    wall.update_detections(see(c2="person"))
    snap = wall.snapshot()
    assert snap["layout"]["id"] == "1+2"
    assert [(t["camera"] or {}).get("id") for t in snap["tiles"]] == ["nvr:2", "nvr:0", "nvr:1"]


def test_promote_can_still_override_the_layout():
    wall, _ = make_wall(count=3, layout="1+2", action="promote", promote_layout="1+7")
    wall.update_detections(see(c2="person"))
    assert wall.snapshot()["layout"]["id"] == "1+7"


def test_promote_can_follow_the_camera_count():
    wall, _ = make_wall(count=3, layout="2x2", action="promote", promote_layout="auto-feature")
    wall.update_detections(see(c2="person"))
    snap = wall.snapshot()
    assert snap["layout"]["id"] == "1+2"                      # three cameras
    assert (snap["tiles"][0]["camera"] or {})["id"] == "nvr:2"


def test_an_auto_view_promotes_within_the_layout_it_chose():
    wall, _ = make_wall(count=3, layout="auto-feature", action="promote", promote_layout="same")
    wall.update_detections(see(c1="person"))
    snap = wall.snapshot()
    assert snap["layout"]["id"] == "1+2"
    assert (snap["tiles"][0]["camera"] or {})["id"] == "nvr:1"


# ----- a state that never stops: the car parked on the drive ----------------------

def test_a_parked_car_stops_counting_once_it_is_scenery():
    """Reolink answers GetAiState with a level, so a parked car keeps "vehicle" asserted for
    as long as it sits there. The tile must not stay marked, nor the screen stay held."""
    wall, clock = make_wall(action="highlight", triggers=["vehicle"], highlight_seconds=0,
                            max_event_seconds=30)
    wall.update_detections(see(c0="vehicle"))
    assert tile_of(wall, "nvr:0")["detections"] == ["vehicle"]
    for _ in range(20):                                        # the car is not going anywhere
        clock.advance(2)
        wall.update_detections(see(c0="vehicle"))
        wall.tick()
    assert tile_of(wall, "nvr:0")["detections"] == []
    assert wall.snapshot()["focus"] is None


def test_the_next_car_arriving_counts_again():
    wall, clock = make_wall(action="highlight", triggers=["vehicle"], highlight_seconds=0,
                            max_event_seconds=10)
    wall.update_detections(see(c0="vehicle"))
    clock.advance(12)
    wall.update_detections(see(c0="vehicle"))                  # the same car, still there
    assert tile_of(wall, "nvr:0")["detections"] == []
    wall.update_detections({})                                 # it drives off
    clock.advance(2)
    wall.update_detections(see(c0="vehicle"))                  # and another one arrives
    assert tile_of(wall, "nvr:0")["detections"] == ["vehicle"]


def test_a_person_arriving_counts_although_the_car_is_scenery():
    """Each kind is judged on its own, or the parked car would mask the person beside it."""
    wall, clock = make_wall(action="highlight", triggers=["person", "vehicle"],
                            highlight_seconds=0, max_event_seconds=10)
    wall.update_detections(see(c0="vehicle"))
    clock.advance(12)
    wall.update_detections(see(c0="vehicle,person"))
    assert tile_of(wall, "nvr:0")["detections"] == ["person"]
    assert wall.snapshot()["focus"]["camera"] == "nvr:0"


def test_zero_reacts_for_as_long_as_the_nvr_keeps_reporting_it():
    """For a source whose detections really are events, and to get the old behaviour back."""
    wall, clock = make_wall(action="highlight", triggers=["vehicle"], highlight_seconds=0,
                            max_event_seconds=0)
    wall.update_detections(see(c0="vehicle"))
    for _ in range(60):
        clock.advance(10)
        wall.update_detections(see(c0="vehicle"))
        wall.tick()
    assert tile_of(wall, "nvr:0")["detections"] == ["vehicle"]

"""Per-camera settings: a name of the owner's own, keeping a camera off the wall, its stream
quality and its picture fit."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import make_camera
from test_api import FakeAdapter
from test_go2rtc import FakeGo2rtc
from test_wall import FakeClock
from viewport.api import create_app
from viewport.config import (AppConfig, CameraSettings, DetectionConfig, DeviceConfig, SourceConfig,
                             ViewConfig)
from viewport.go2rtc import Go2rtcClient
from viewport.runtime import Runtime
from viewport.store import ConfigStore
from viewport.wall import WallController


def make_wall(layout="2x2", views=None, max_main=1, **device):
    config = AppConfig(
        device=DeviceConfig(max_main_streams=max_main, main_stream_hold_seconds=0, **device),
        detection=DetectionConfig(enabled=True, min_focus_seconds=0, manual_override_seconds=30),
        views=views or [ViewConfig(name="All", layout=layout)],
    )
    wall = WallController(config, clock=FakeClock())
    wall.update_cameras("nvr", [make_camera(i) for i in range(4)])
    return wall


def tile(wall, camera_id):
    return next(t for t in wall.snapshot()["tiles"] if (t["camera"] or {}).get("id") == camera_id)


def on_wall(wall):
    return [t["camera"]["id"] for t in wall.snapshot()["tiles"] if t["camera"]]


# ----- names ------------------------------------------------------------------------

def test_a_camera_can_be_given_a_name_of_its_own():
    wall = make_wall()
    wall.set_camera_settings({"nvr:1": CameraSettings(name="Front gate")})
    assert tile(wall, "nvr:1")["camera"]["name"] == "Front gate"
    assert wall.reported_name("nvr:1") == "Cam 2"            # the NVR's own name is kept too


def test_the_name_survives_the_next_discovery():
    """Discovery reports the NVR's name every minute; that must not undo the owner's."""
    wall = make_wall()
    wall.set_camera_settings({"nvr:1": CameraSettings(name="Front gate")})
    wall.update_cameras("nvr", [make_camera(i) for i in range(4)])
    assert tile(wall, "nvr:1")["camera"]["name"] == "Front gate"


def test_a_blank_name_goes_back_to_the_reported_one():
    wall = make_wall()
    wall.set_camera_settings({"nvr:1": CameraSettings(name="Front gate")})
    wall.set_camera_settings({"nvr:1": CameraSettings(name="   ")})
    assert tile(wall, "nvr:1")["camera"]["name"] == "Cam 2"


def test_a_detection_announces_the_name_the_owner_chose():
    wall = make_wall()
    wall.set_camera_settings({"nvr:2": CameraSettings(name="Driveway")})
    wall.update_detections({"nvr:2": frozenset({"person"})})
    assert wall.snapshot()["focus"]["name"] == "Driveway"


# ----- keeping a camera off the wall -----------------------------------------------------

def test_a_hidden_camera_is_left_out_of_every_view():
    wall = make_wall(views=[ViewConfig(name="All", layout="2x2"),
                            ViewConfig(name="Pair", layout="1x2", cameras=["nvr:0", "nvr:1"])])
    wall.set_camera_settings({"nvr:1": CameraSettings(show=False)})
    assert "nvr:1" not in on_wall(wall)
    wall.set_view(1)
    assert on_wall(wall) == ["nvr:0"]                          # even where a view names it


def test_a_hidden_camera_is_not_watched_so_a_detection_cannot_bring_it_back():
    wall = make_wall()
    wall.set_camera_settings({"nvr:3": CameraSettings(show=False)})
    wall.update_detections({"nvr:3": frozenset({"person"})})
    assert wall.snapshot()["focus"] is None and "nvr:3" not in on_wall(wall)


def test_hiding_a_camera_lets_the_automatic_layout_shrink():
    wall = make_wall(layout="auto-feature")
    assert wall.snapshot()["layout"]["id"] == "1+3"
    wall.set_camera_settings({"nvr:3": CameraSettings(show=False)})
    assert wall.snapshot()["layout"]["id"] == "1+2"


# ----- quality ----------------------------------------------------------------------

def test_always_hd_plays_full_resolution_even_in_a_small_tile():
    wall = make_wall()
    assert tile(wall, "nvr:2")["quality"] == "sub"
    wall.set_camera_settings({"nvr:2": CameraSettings(quality="main")})
    assert tile(wall, "nvr:2")["quality"] == "main"


def test_always_hd_still_respects_how_many_full_resolution_streams_are_allowed():
    """The owner can ask for HD everywhere; the NVR still only gets max_main_streams of them."""
    wall = make_wall(max_main=1)
    wall.set_camera_settings({f"nvr:{i}": CameraSettings(quality="main") for i in range(4)})
    assert wall.snapshot()["budget"]["main_in_use"] == 1


def test_low_resolution_only_holds_even_when_someone_makes_it_full_screen():
    wall = make_wall()
    wall.set_camera_settings({"nvr:0": CameraSettings(quality="sub")})
    wall.set_fullscreen(tile(wall, "nvr:0")["id"])
    assert tile(wall, "nvr:0")["quality"] == "sub"
    assert wall.snapshot()["budget"]["main_in_use"] == 0


def test_always_hd_wins_over_hd_being_saved_for_detections():
    wall = make_wall(main_stream_only_on_detection=True)
    wall.set_camera_settings({"nvr:1": CameraSettings(quality="main")})
    assert tile(wall, "nvr:1")["quality"] == "main"


# ----- fit and validation ------------------------------------------------------------

def test_a_camera_can_fill_its_tile_while_the_rest_letterbox():
    wall = make_wall()
    wall.set_camera_settings({"nvr:1": CameraSettings(fit="cover")})
    assert tile(wall, "nvr:1")["fit"] == "cover"
    assert tile(wall, "nvr:0")["fit"] is None                  # follows display.fit


@pytest.mark.parametrize("bad", [{"name": "x" * 49}, {"quality": "4k"}, {"fit": "stretch"}, {"colour": "red"}])
def test_bad_settings_are_refused(bad):
    with pytest.raises(ValidationError):
        CameraSettings(**bad)


# ----- kept across restarts, and the API -------------------------------------------------

def build(tmp_path):
    config = AppConfig(sources=[SourceConfig(id="nvr", host="nvr", username="u", password="p")])
    store = ConfigStore(tmp_path / "state.json")
    rt = Runtime(config, adapters=[FakeAdapter([make_camera(0), make_camera(1)])], store=store,
                 go2rtc=Go2rtcClient("http://g", transport=httpx.MockTransport(FakeGo2rtc().handler)))
    rt.wall.update_cameras("nvr", [make_camera(0), make_camera(1)])
    return TestClient(create_app(config, rt, start_background=False)), rt


def test_settings_are_saved_shown_and_only_what_changed_is_stored(tmp_path):
    client, rt = build(tmp_path)
    resp = client.put("/api/config/cameras", json={
        "order": ["nvr:1", "nvr:0"],
        "settings": {"nvr:1": {"name": "Gate", "quality": "main"}, "nvr:0": {},
                     "gone:5": {"show": False}},                 # a camera not found right now
    })
    assert resp.status_code == 200
    cameras = {c["id"]: c for c in resp.json()["cameras"]}
    assert cameras["nvr:1"]["name"] == "Gate" and cameras["nvr:1"]["reported_name"] == "Cam 2"
    assert cameras["nvr:1"]["settings"]["quality"] == "main"
    assert cameras["nvr:1"]["main"]["width"] == 1920             # stream details for the page
    assert set(resp.json()["camera_settings"]) == {"nvr:1", "gone:5"}   # defaults are not stored

    restored = ConfigStore(tmp_path / "state.json").apply(AppConfig())
    assert restored.camera_settings["nvr:1"].name == "Gate"
    assert restored.camera_settings["gone:5"].show is False


def test_reordering_alone_leaves_the_settings_alone(tmp_path):
    client, _ = build(tmp_path)
    client.put("/api/config/cameras", json={"order": [], "settings": {"nvr:1": {"name": "Gate"}}})
    resp = client.put("/api/config/cameras", json={"order": ["nvr:1"]})
    assert resp.json()["camera_settings"]["nvr:1"]["name"] == "Gate"


def test_one_unreadable_camera_in_the_state_file_does_not_cost_the_others(tmp_path):
    (tmp_path / "state.json").write_text(
        '{"camera_settings": {"nvr:0": {"name": "Porch"}, "nvr:1": {"quality": "8k"}}}')
    restored = ConfigStore(tmp_path / "state.json").apply(AppConfig())
    assert list(restored.camera_settings) == ["nvr:0"]


@pytest.mark.parametrize("settings", [{"nvr:0": {"name": "x" * 60}}, {"nvr:0": {"extra": 1}},
                                      {"n" * 65: {"name": "ok"}}])
def test_the_api_refuses_bad_camera_settings(tmp_path, settings):
    client, _ = build(tmp_path)
    assert client.put("/api/config/cameras", json={"order": [], "settings": settings}).status_code == 422

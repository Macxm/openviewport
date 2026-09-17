"""The admin API: /api/config and the two edit routes."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_camera
from test_api import FakeAdapter
from test_go2rtc import FakeGo2rtc
from viewport.adapters.reolink import ReolinkAdapter
from viewport.api import create_app
from viewport.config import AppConfig, SourceConfig, ViewConfig
from viewport.go2rtc import Go2rtcClient
from viewport.runtime import Runtime
from viewport.store import ConfigStore


def build(tmp_path, *, reolink: bool = False, state: bool = True, views=None):
    source = SourceConfig(id="nvr", host="nvr.local", username="admin", password="hunter2")
    config = AppConfig(sources=[source],
                       views=views or [ViewConfig(name="All cameras"),
                                       ViewConfig(name="Featured", layout="1+5")])
    cameras = [make_camera(i) for i in range(4)]
    adapter = ReolinkAdapter(source) if reolink else FakeAdapter(cameras)
    adapter.id = "nvr"
    fake = FakeGo2rtc()
    store = ConfigStore(tmp_path / "state.json" if state else "")
    rt = Runtime(config, adapters=[adapter], store=store,
                 go2rtc=Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(fake.handler)))
    rt.wall.update_cameras("nvr", cameras)
    rt._update_desired_streams()
    rt.registry.reachable = True
    return TestClient(create_app(config, rt, start_background=False)), rt, store


# ----- GET /api/config ------------------------------------------------------

def test_config_lists_everything_the_page_needs(tmp_path):
    client, _, _ = build(tmp_path)
    body = client.get("/api/config").json()
    assert body["editable"] is True
    assert [v["name"] for v in body["views"]] == ["All cameras", "Featured"]
    assert [c["id"] for c in body["cameras"]] == [f"nvr:{i}" for i in range(4)]
    assert "auto" in body["layouts"] and "1+5" in body["layouts"]
    assert body["sources"][0]["id"] == "nvr"


def test_config_never_returns_a_password(tmp_path):
    """The username is shown so you can see which account is configured; the password is
    write-only — it can be set here and never read back."""
    client, _, _ = build(tmp_path)
    body = client.get("/api/config").json()
    assert "hunter2" not in client.get("/api/config").text
    source = body["sources"][0]
    assert source["password_set"] is True
    assert "password" not in source
    assert source["username"] == "admin"


def test_config_reports_read_only_without_a_state_file(tmp_path):
    client, _, _ = build(tmp_path, state=False)
    body = client.get("/api/config").json()
    assert body["editable"] is False and body["state_file"] is None


# ----- PUT /api/config/views ------------------------------------------------

def test_views_can_be_replaced_and_are_applied_live(tmp_path):
    client, rt, store = build(tmp_path)
    resp = client.put("/api/config/views", json={
        "views": [{"name": "Front door", "layout": "2x2", "cameras": ["nvr:0", "nvr:1"]}]})
    assert resp.status_code == 200

    assert [v["name"] for v in resp.json()["views"]] == ["Front door"]
    assert rt.wall.snapshot()["view"]["name"] == "Front door"
    assert rt.wall.snapshot()["layout"]["id"] == "2x2"
    assert [v.name for v in store.apply(AppConfig()).views] == ["Front door"]   # persisted


def test_the_current_view_is_kept_by_name_across_an_edit(tmp_path):
    client, rt, _ = build(tmp_path)
    rt.wall.set_view(1)
    assert rt.wall.snapshot()["view"]["name"] == "Featured"

    client.put("/api/config/views", json={
        "views": [{"name": "New one", "layout": "auto", "cameras": "all"},
                  {"name": "Featured", "layout": "3x3", "cameras": "all"}]})
    assert rt.wall.snapshot()["view"]["name"] == "Featured"
    assert rt.wall.snapshot()["layout"]["id"] == "3x3"


def test_an_unknown_layout_is_refused(tmp_path):
    client, rt, _ = build(tmp_path)
    resp = client.put("/api/config/views",
                      json={"views": [{"name": "x", "layout": "9x9", "cameras": "all"}]})
    assert resp.status_code == 422
    assert [v.name for v in rt.config.views] == ["All cameras", "Featured"]   # unchanged


def test_an_empty_view_list_is_refused(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.put("/api/config/views", json={"views": []}).status_code == 422


# ----- PUT /api/config/sources/{id} -----------------------------------------

def test_source_settings_can_be_changed(tmp_path):
    client, rt, store = build(tmp_path, reolink=True)
    resp = client.put("/api/config/sources/nvr",
                      json={"protocol": "rtsp", "refresh_seconds": 120})
    assert resp.status_code == 200
    assert rt.config.sources[0].protocol == "rtsp"
    assert rt.sources["nvr"].refresh_seconds == 120
    assert all(s.startswith("rtsp://") for s in rt.registry.desired.values())
    assert store.load()["sources"]["nvr"]["protocol"] == "rtsp"


def test_changing_the_protocol_clears_an_automatic_fallback(tmp_path):
    """Otherwise a transport pinned by the fallback would outrank the admin's choice."""
    client, rt, _ = build(tmp_path, reolink=True)
    camera = rt.wall.cameras["nvr:0"]
    rt.sources["nvr"].adapter.next_protocol(camera, "sub")       # pinned to rtsp
    rt.fallbacks["nvr_0_sub"] = "rtsp"

    client.put("/api/config/sources/nvr", json={"protocol": "flv"})
    assert rt.fallbacks == {}
    assert rt.registry.desired["nvr_0_sub"].startswith("http://")


def test_renderers_are_told_to_reconnect_after_a_protocol_change(tmp_path):
    client, rt, _ = build(tmp_path, reolink=True)
    client.put("/api/config/sources/nvr", json={"protocol": "rtsp"})
    epochs = {t["stream"]: t["epoch"] for t in rt.wall.snapshot()["tiles"] if t["stream"]}
    assert all(e >= 1 for e in epochs.values())


@pytest.mark.parametrize("payload", [{"host": "other"}, {"type": "onvif"}, {"port": 9000}])
def test_how_a_source_connects_cannot_be_changed_in_place(tmp_path, payload):
    """Changing these means a different device: remove it and add it again."""
    client, rt, _ = build(tmp_path, reolink=True)
    assert client.put("/api/config/sources/nvr", json=payload).status_code == 422
    assert rt.config.sources[0].host == "nvr.local"


def test_an_unknown_source_is_a_404(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.put("/api/config/sources/nope", json={"protocol": "rtsp"}).status_code == 404


def test_an_empty_edit_is_refused(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.put("/api/config/sources/nvr", json={}).status_code == 422


def test_an_invalid_refresh_interval_is_refused(tmp_path):
    client, rt, _ = build(tmp_path, reolink=True)
    assert client.put("/api/config/sources/nvr", json={"refresh_seconds": 1}).status_code == 422
    assert rt.config.sources[0].refresh_seconds == 60


# ----- the page itself ------------------------------------------------------

def test_admin_page_and_assets_are_served(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.get("/admin").status_code == 200
    assert client.get("/static/admin.js").status_code == 200
    assert client.get("/static/admin.css").status_code == 200
    assert client.get("/static/admin-logic.js").status_code == 200


def test_page_assets_are_revalidated_so_an_update_is_never_half_applied(tmp_path):
    """Without Cache-Control a browser can keep last release's stylesheet for days and pair
    it with the new page, which is what happened the first time the admin page changed."""
    client, _, _ = build(tmp_path)
    first = client.get("/static/admin.css")
    assert first.headers["cache-control"] == "no-cache"
    again = client.get("/static/admin.css", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert client.get("/static/wall.js").headers["cache-control"] == "no-cache"


# ----- layouts, display and device ------------------------------------------

MY_LAYOUT = {"id": "wide", "name": "Wide pair", "cols": 3, "rows": 2,
             "tiles": [{"x": 0, "y": 0, "w": 2, "h": 2}, {"x": 2, "y": 0}, {"x": 2, "y": 1}]}


def test_config_offers_layouts_with_their_geometry(tmp_path):
    client, _, _ = build(tmp_path)
    body = client.get("/api/config").json()
    assert body["layouts"][0] == "auto"
    assert {"2x2", "1+5", "5x5", "2+8"} <= set(body["layouts"])
    assert body["layout_geometry"]["1+5"]["tiles"][0] == {"id": "t0", "x": 0, "y": 0, "w": 2, "h": 2}
    assert body["custom_layouts"] == []


def test_a_custom_layout_can_be_added_used_and_persisted(tmp_path):
    client, rt, store = build(tmp_path)
    resp = client.put("/api/config/layouts", json={"layouts": [MY_LAYOUT]})
    assert resp.status_code == 200
    assert "wide" in resp.json()["layouts"]

    client.put("/api/config/views", json={"views": [{"name": "Wide", "layout": "wide", "cameras": "all"}]})
    snap = rt.wall.snapshot()
    assert snap["layout"]["id"] == "wide"
    assert snap["tiles"][0]["w"] == 2 and len(snap["tiles"]) == 3
    assert [l.id for l in store.apply(AppConfig()).layouts] == ["wide"]


@pytest.mark.parametrize("tiles,status", [
    ([{"x": 0, "y": 0, "w": 4}], 422),                                  # off the grid
    ([{"x": 0, "y": 0, "w": 2, "h": 2}, {"x": 1, "y": 1}], 422),        # overlapping
    ([], 422),                                                          # no tiles
])
def test_a_broken_custom_layout_is_refused(tmp_path, tiles, status):
    client, rt, _ = build(tmp_path)
    resp = client.put("/api/config/layouts", json={"layouts": [{**MY_LAYOUT, "tiles": tiles}]})
    assert resp.status_code == status
    assert rt.config.layouts == []


def test_a_custom_layout_cannot_take_a_built_in_name(tmp_path):
    client, _, _ = build(tmp_path)
    resp = client.put("/api/config/layouts", json={"layouts": [{**MY_LAYOUT, "id": "2x2"}]})
    assert resp.status_code == 422 and "built in" in resp.json()["detail"]


def test_a_layout_still_used_by_a_view_cannot_be_removed(tmp_path):
    client, rt, _ = build(tmp_path)
    client.put("/api/config/layouts", json={"layouts": [MY_LAYOUT]})
    client.put("/api/config/views", json={"views": [{"name": "Wide", "layout": "wide", "cameras": "all"}]})
    resp = client.put("/api/config/layouts", json={"layouts": []})
    assert resp.status_code == 409
    assert rt.wall.snapshot()["layout"]["id"] == "wide"


def test_display_settings_reach_the_renderer_and_are_persisted(tmp_path):
    client, rt, store = build(tmp_path)
    assert rt.wall.snapshot()["display"]["fit"] == "contain"
    current = client.get("/api/config").json()["display"]
    resp = client.put("/api/config/display", json={**current, "fit": "cover", "show_clock": True})
    assert resp.status_code == 200
    assert rt.wall.snapshot()["display"] == {**current, "fit": "cover", "show_clock": True}
    assert store.apply(AppConfig()).display.fit == "cover"


def test_device_settings_change_the_stream_budget_at_once(tmp_path):
    client, rt, store = build(tmp_path)
    rt.wall.set_fullscreen("t0")
    assert rt.wall.snapshot()["budget"]["main_in_use"] == 1

    current = client.get("/api/config").json()["device"]
    assert client.put("/api/config/device", json={**current, "max_main_streams": 0}).status_code == 200
    budget = rt.wall.snapshot()["budget"]
    assert (budget["main_in_use"], budget["max_main"]) == (0, 0)
    assert store.apply(AppConfig()).device.max_main_streams == 0


@pytest.mark.parametrize("bad", [{"max_main_streams": 99}, {"max_total_streams": 0},
                                 {"main_stream_min_fraction": 2}, {"main_stream_hold_seconds": -1}])
def test_impossible_device_settings_are_refused(tmp_path, bad):
    client, rt, _ = build(tmp_path)
    current = client.get("/api/config").json()["device"]
    assert client.put("/api/config/device", json={**current, **bad}).status_code == 422
    assert rt.wall.limits.max_main == 1


def test_a_custom_layout_is_offered_under_the_name_it_was_given(tmp_path):
    """Regression: the admin page showed the id, never the name its owner typed."""
    client, _, _ = build(tmp_path)
    client.put("/api/config/layouts", json={"layouts": [MY_LAYOUT]})
    body = client.get("/api/config").json()
    assert body["layout_names"]["wide"] == "Wide pair"
    assert body["layout_names"]["2x2"] == "2x2"          # built-ins keep their id


def test_a_custom_layout_without_a_name_falls_back_to_its_id(tmp_path):
    client, _, _ = build(tmp_path)
    client.put("/api/config/layouts", json={"layouts": [{**MY_LAYOUT, "name": ""}]})
    assert client.get("/api/config").json()["layout_names"]["wide"] == "wide"


# ----- camera importance ------------------------------------------------------

def test_cameras_are_listed_in_importance_order(tmp_path):
    client, rt, _ = build(tmp_path)
    body = client.get("/api/config").json()
    assert [c["id"] for c in body["cameras"]] == [f"nvr:{i}" for i in range(4)]
    assert body["camera_order"] == []          # nothing pinned yet: discovery order


def test_the_important_camera_fills_the_first_tile(tmp_path):
    client, rt, store = build(tmp_path)
    client.put("/api/config/views", json={"views": [{"name": "All", "layout": "1+3", "cameras": "all"}]})
    assert rt.wall.snapshot()["tiles"][0]["camera"]["id"] == "nvr:0"

    resp = client.put("/api/config/cameras", json={"order": ["nvr:2", "nvr:3"]})
    assert resp.status_code == 200
    tiles = [t["camera"]["id"] for t in rt.wall.snapshot()["tiles"] if t["camera"]]
    assert tiles == ["nvr:2", "nvr:3", "nvr:0", "nvr:1"]     # listed first, then the rest
    assert store.apply(AppConfig()).camera_order == ["nvr:2", "nvr:3"]


def test_the_order_survives_the_next_discovery(tmp_path):
    client, rt, _ = build(tmp_path)
    client.put("/api/config/cameras", json={"order": ["nvr:3"]})
    rt.wall.update_cameras("nvr", [make_camera(i) for i in range(4)] + [make_camera(4)])
    assert rt.wall.camera_order[0] == "nvr:3"


def test_an_unknown_camera_id_is_kept_because_it_may_be_offline_now(tmp_path):
    client, rt, _ = build(tmp_path)
    assert client.put("/api/config/cameras", json={"order": ["nvr:9", "nvr:2"]}).status_code == 200
    assert rt.wall.camera_order[0] == "nvr:2"        # nvr:9 is not here, so it is skipped
    assert rt.config.camera_order == ["nvr:9", "nvr:2"]


def test_a_view_that_names_its_cameras_still_wins(tmp_path):
    client, rt, _ = build(tmp_path)
    client.put("/api/config/cameras", json={"order": ["nvr:3"]})
    client.put("/api/config/views", json={
        "views": [{"name": "Two", "layout": "2x2", "cameras": ["nvr:1", "nvr:0"]}]})
    tiles = [t["camera"]["id"] for t in rt.wall.snapshot()["tiles"] if t["camera"]]
    assert tiles == ["nvr:1", "nvr:0"]


# ----- first-run setup --------------------------------------------------------

def test_a_fresh_install_is_marked_as_not_yet_configured(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.get("/api/config").json()["configured"] is False


def test_finishing_setup_is_remembered(tmp_path):
    client, _, store = build(tmp_path)
    assert client.post("/api/config/setup-done").json()["configured"] is True
    assert client.get("/api/config").json()["configured"] is True
    assert store.configured() is True


def test_a_read_only_install_is_never_asked_to_set_itself_up(tmp_path):
    """Nothing could be saved, so walking someone through setup would only frustrate."""
    client, _, _ = build(tmp_path, state=False)
    assert client.get("/api/config").json()["configured"] is True


def test_an_install_that_already_has_settings_is_not_asked_to_set_up(tmp_path):
    """Upgrading should not walk someone through a wizard they finished long ago."""
    client, _, store = build(tmp_path)
    assert client.get("/api/config").json()["configured"] is False
    client.put("/api/config/display", json={"fit": "cover"})
    assert client.get("/api/config").json()["configured"] is True

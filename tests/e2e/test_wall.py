"""The wall itself: it plays, it goes full screen in HD, it keeps its video behind the agent,
and it reacts to what the cameras detect."""

from __future__ import annotations

import httpx

from conftest import NVR, TOKEN, WALL, open_wall, player_stats, wait_playing, wait_until, wall_tiles


def streaming_tiles(api):
    return [t["id"] for t in wall_tiles(api) if t["stream"]]


def test_every_online_camera_plays_and_the_offline_one_says_so(desktop, api):
    page = desktop.page()
    open_wall(page)
    wait_playing(page, streaming_tiles(api))
    offline = [t for t in wall_tiles(api) if t["camera"] and not t["camera"]["online"]]
    assert len(offline) == 1 and offline[0]["stream"] is None
    assert "offline" in page.locator(f'[data-tile="{offline[0]["id"]}"]').inner_text().lower()


def test_video_reaches_the_screen_only_through_the_agent(desktop, api):
    """go2rtc is not published: every video connection goes to the agent's relay."""
    page = desktop.page()
    sockets: list[str] = []
    page.on("websocket", lambda ws: sockets.append(ws.url))
    open_wall(page)
    wait_playing(page, streaming_tiles(api))
    media = [url for url in sockets if "/api/wall/ws" not in url]
    assert media and all(url.startswith(WALL.replace("http", "ws") + "/api/media/ws") for url in media)


def test_a_tile_goes_full_screen_in_hd_and_comes_back(desktop, api):
    page = desktop.page()
    open_wall(page)
    wait_playing(page, streaming_tiles(api))
    tile = next(t for t in wall_tiles(api) if t["stream"] and t["quality"] == "sub")

    page.locator(f'[data-tile="{tile["id"]}"]').click()
    wait_until(lambda: next(s for s in player_stats(page) if s["tile"] == tile["id"])["stream"].endswith("_main")
               and api.get("/api/wall").json()["fullscreen"] == tile["id"], 30, "full screen in HD")
    wait_playing(page, [tile["id"]])

    page.keyboard.press("Escape")
    wait_until(lambda: api.get("/api/wall").json()["fullscreen"] is None, 10, "back to the grid")


def test_a_screen_without_the_token_gets_nothing(desktop):
    page = desktop.page()
    open_wall(page, token="")
    banner = page.locator("#banner")
    banner.wait_for(state="visible", timeout=15_000)
    assert "token" in banner.inner_text()
    assert not page.locator("#wall video").count()
    assert httpx.get(f"{WALL}/api/wall").status_code == 401


def test_a_link_cannot_inject_script_into_the_status_panel(desktop, api):
    """?mode= once went into the panel's HTML unescaped."""
    page = desktop.page()
    open_wall(page, query="hud=1&mode=%3Cimg%20src%3Dx%20onerror%3Dwindow.pwned%3D1%3E")
    wait_playing(page, streaming_tiles(api))
    page.locator("#hud table").wait_for()
    assert page.evaluate("window.pwned") is None
    assert not page.locator("#hud img").count()
    assert "Player mse" in page.locator("#hud").inner_text()


def test_a_detection_brings_its_camera_forward(desktop, admin, api, restore):
    detection = {**admin.get("/api/config").json()["detection"],
                 "enabled": True, "action": "fullscreen", "triggers": ["person"], "poll_seconds": 1,
                 "hold_seconds": 3, "min_focus_seconds": 0, "manual_override_seconds": 0, "cameras": "all"}
    admin.put("/api/config/detection", json=detection).raise_for_status()
    page = desktop.page()
    open_wall(page)
    wait_playing(page, streaming_tiles(api))

    httpx.post(f"{NVR}/mock/detect", params={"channel": 1, "kind": "people", "seconds": 8}).raise_for_status()
    focus = wait_until(lambda: api.get("/api/wall").json()["focus"], 20, "the detection to take the screen")
    assert focus["camera"] == "nvr:1" and focus["reason"] == "person"
    tile = next(t for t in wall_tiles(api) if t["camera"] and t["camera"]["id"] == "nvr:1")
    badge = page.locator(f'[data-tile="{tile["id"]}"] .detection')
    badge.wait_for(state="visible", timeout=10_000)
    assert "person" in badge.inner_text().lower()
    wait_until(lambda: api.get("/api/wall").json()["focus"] is None, 30, "the camera to step back afterwards")

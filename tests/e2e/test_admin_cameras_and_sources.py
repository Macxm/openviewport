"""Cameras set up one by one, and sources added and removed, as the wall sees the result."""

from __future__ import annotations

from conftest import NVR_PASSWORD, open_admin, unlock, wait_until, wall_tiles


def open_camera(page, camera_id: str):
    item = page.locator(f'li.camera-item[data-key="{camera_id}"]')
    item.locator(".camera-toggle").click()
    item.locator(".camera-body").wait_for()
    return item


def save(page):
    page.locator("#savebar").wait_for(state="visible")
    page.locator("#save").click()
    page.locator("#savebar").wait_for(state="hidden", timeout=15_000)


def test_a_camera_renamed_here_is_renamed_on_the_wall(desktop, api, restore):
    page = desktop.page()
    unlock(desktop)
    open_admin(page, "cameras")
    item = open_camera(page, "nvr:1")
    item.locator(".camera-body input[type=text]").fill("Back gate")
    assert item.locator(".camera-name").inner_text() == "Back gate"        # the heading follows as you type
    save(page)
    wait_until(lambda: any((t["camera"] or {}).get("name") == "Back gate" for t in wall_tiles(api)), 10,
               "the new name on the wall")


def test_a_camera_kept_off_the_wall_leaves_it(desktop, api, restore):
    page = desktop.page()
    unlock(desktop)
    open_admin(page, "cameras")
    item = open_camera(page, "nvr:2")
    item.locator(".camera-body input.switch").click()                      # Show on the wall
    assert "Not on the wall" in item.locator(".camera-badges").inner_text()
    save(page)
    wait_until(lambda: all((t["camera"] or {}).get("id") != "nvr:2" for t in wall_tiles(api)), 10,
               "the camera to leave the wall")


def test_always_hd_plays_a_small_tile_in_hd(desktop, api, restore):
    page = desktop.page()
    unlock(desktop)
    open_admin(page, "cameras")
    item = open_camera(page, "nvr:1")
    item.locator(".camera-body select").first.select_option("main")
    save(page)
    wait_until(lambda: any((t["camera"] or {}).get("id") == "nvr:1" and t["quality"] == "main"
                           for t in wall_tiles(api)), 10, "the small tile to play in HD")


def test_a_new_source_form_can_be_thrown_away(desktop):
    page = desktop.page()
    unlock(desktop)
    open_admin(page, "sources")
    page.locator("#add-source").click()
    page.locator("#new-source").wait_for()
    page.get_by_role("button", name="Discard this new source").click()
    page.locator("#new-source").wait_for(state="detached")
    assert page.locator("#add-source").is_visible()


def test_a_source_added_here_can_be_removed_again(desktop, api):
    page = desktop.page()
    page.on("dialog", lambda dialog: dialog.accept())
    unlock(desktop)
    open_admin(page, "sources")
    page.locator("#add-source").click()
    form = page.locator("#new-source")
    form.locator("input[type=text]").first.fill("extra")
    form.locator("select").first.select_option("rtsp")
    form.locator("textarea").fill("Extra camera, rtsp://e2e-nvr:554/Preview_01_sub")
    form.locator("input[type=text]").last.fill("admin")
    form.locator("input[type=password]").fill(NVR_PASSWORD)
    form.get_by_role("button", name="Add source").click()

    card = page.locator('article[data-source="extra"]')
    card.wait_for(timeout=15_000)
    wait_until(lambda: any(c["id"] == "extra:0" for c in api.get("/api/cameras").json()), 30,
               "the new source's camera to be found")

    card.get_by_role("button", name="Remove source").click()
    card.wait_for(state="detached", timeout=15_000)
    wait_until(lambda: all(c["id"] != "extra:0" for c in api.get("/api/cameras").json()), 15,
               "its camera to go with it")


def test_a_password_inside_a_stream_address_is_refused(desktop, api):
    page = desktop.page()
    unlock(desktop)
    open_admin(page, "sources")
    page.locator("#add-source").click()
    form = page.locator("#new-source")
    form.locator("input[type=text]").first.fill("leaky")
    form.locator("select").first.select_option("rtsp")
    form.locator("textarea").fill(f"Leaky, rtsp://admin:{NVR_PASSWORD}@e2e-nvr:554/Preview_01_sub")
    form.get_by_role("button", name="Add source").click()
    toast = page.locator("#toast")
    toast.wait_for(state="visible")
    assert "own fields" in toast.inner_text()
    assert all(s["id"] != "leaky" for s in api.get("/api/config").json()["sources"])

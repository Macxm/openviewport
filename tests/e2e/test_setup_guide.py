"""A first run, walked through by the setup guide from nothing to a working wall.

One test, because each step builds on the last, on an agent of its own (FRESH): it ends
with a password set, which no other test should have to live with.
"""

from __future__ import annotations

from conftest import FRESH, NVR_PASSWORD, open_admin, open_wall, player_stats, wait_until


def guide_button(page, name: str):
    return page.locator("#guide-foot").get_by_role("button", name=name)


def test_a_first_run_ends_with_a_protected_page_and_a_working_wall(desktop):
    page = desktop.page()
    open_admin(page, "sources", base=FRESH)

    # Welcome: the guide starts by itself, and the header says the page is not protected yet.
    page.locator("#panel-welcome").wait_for()
    assert "Not protected" in page.locator("#lock-button").inner_text()
    guide_button(page, "Start").click()

    # 1. Protect the settings.
    security = page.locator("#panel-security")
    security.wait_for()
    passwords = security.locator("form input[type=password]")
    passwords.nth(0).fill("short")
    passwords.nth(1).fill("short")
    security.get_by_role("button", name="Set password").click()
    assert "at least 8" in security.locator(".form-error").inner_text()
    passwords.nth(0).fill("a long first password")
    passwords.nth(1).fill("a long first password")
    security.get_by_role("button", name="Set password").click()
    wait_until(lambda: page.locator("#lock-button").inner_text().strip() == "Lock", 10,
               "the page to become protected")
    guide_button(page, "Next").click()

    # 2. Connect the cameras.
    page.locator("#panel-sources").wait_for()
    page.locator("#add-source").click()
    form = page.locator("#new-source")
    texts = form.locator("input[type=text]")
    texts.nth(0).fill("nvr")                  # name
    texts.nth(1).fill("e2e-nvr")              # address
    texts.nth(2).fill("admin")                # username
    form.locator("input[type=password]").fill(NVR_PASSWORD)
    form.get_by_role("button", name="Add source").click()
    page.locator('article[data-source="nvr"]').wait_for(timeout=15_000)
    wait_until(lambda: "No cameras" not in page.locator('article[data-source="nvr"] .card-tools').inner_text(),
               60, "the NVR's cameras to be found")
    guide_button(page, "Next").click()

    # 3. Name and order them.
    page.locator("#panel-cameras").wait_for()
    first = page.locator('li.camera-item[data-key="nvr:0"]')
    first.locator(".camera-toggle").click()
    first.locator(".camera-body input[type=text]").fill("Front door")
    guide_button(page, "Save and continue").click()

    # 4. What the wall shows: the default is fine.
    page.locator("#panel-views").wait_for(timeout=15_000)
    guide_button(page, "Next").click()

    # Done. A reload is the settings now, not the guide.
    page.locator("#panel-done").wait_for()
    page.reload()
    page.locator("#shell").wait_for(state="visible")
    assert page.locator("#panel-welcome").is_hidden() and page.locator("#panel-done").is_hidden()
    assert not page.evaluate("document.body.classList.contains('guide')")

    # And the wall plays, with the name chosen on the way.
    wall = desktop.page()
    open_wall(wall, base=FRESH, token="")
    wait_until(lambda: any(s.get("state") == "playing" and (s.get("frames") or 0) > 2 for s in player_stats(wall)),
               60, "the wall to play")
    assert "Front door" in wall.locator("#wall").inner_text()

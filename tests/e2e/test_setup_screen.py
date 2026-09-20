"""A device with no network puts its own on the screen, because there is nothing else to show.

The agents in this suite run against the stand-in device, so the access point is turned on
through the admin API and the wall is watched for what it does about it.
"""

from __future__ import annotations

import pytest

from conftest import TOKEN, open_wall, wait_until


@pytest.fixture
def access_point(admin):
    """Put the stand-in device on its own network, and put it back afterwards."""
    admin.post("/api/device/access-point", json={"on": True}).raise_for_status()
    yield
    admin.post("/api/device/access-point", json={"on": False}).raise_for_status()
    admin.post("/api/device/network/prefer", json={"link": "ethernet"}).raise_for_status()


def test_a_connected_wall_shows_no_setup_screen(desktop):
    page = desktop.page()
    open_wall(page, token=TOKEN)
    page.locator(".tile").first.wait_for()
    assert page.locator("#setup").is_hidden()


def test_a_device_on_its_own_network_says_how_to_join_it(desktop, access_point):
    page = desktop.page()
    open_wall(page, token=TOKEN)
    # The device is asked how it is connected on a slow loop: this is what the screen does
    # when the answer changes, not how quickly it notices.
    setup = page.locator("#setup")
    wait_until(lambda: setup.is_visible(), 40, "the setup screen to appear")

    words = setup.inner_text()
    assert "Set up this screen" in words
    assert "OpenViewport-" in words, "the network to join"
    assert page.locator("#setup canvas").count() >= 1, "and a code to scan instead of typing"


def test_the_code_on_screen_is_a_real_qr_code(desktop, access_point):
    """Drawn from the agent's matrix: three finder squares in the corners, on white."""
    page = desktop.page()
    open_wall(page, token=TOKEN)
    wait_until(lambda: page.locator("#setup").is_visible(), 40, "the setup screen")

    corners = page.locator("#setup canvas").first.evaluate(
        """canvas => {
             const ctx = canvas.getContext('2d');
             const step = canvas.width / (33 + 8);          // modules, including the margin
             const at = (mx, my) => {
               const d = ctx.getImageData(Math.floor((mx + 4.5) * step),
                                          Math.floor((my + 4.5) * step), 1, 1).data;
               return d[0] < 128 ? 1 : 0;                    // dark module?
             };
             return { topLeft: at(0, 0), topRight: at(32, 0), bottomLeft: at(0, 32),
                      quiet: at(-2, -2), middleOfFinder: at(3, 3) };
           }""")
    assert corners["topLeft"] == 1 and corners["topRight"] == 1 and corners["bottomLeft"] == 1
    assert corners["middleOfFinder"] == 1, "the centre of a finder is dark"
    assert corners["quiet"] == 0, "and the margin around it is clear"


def test_joining_a_network_takes_the_screen_back_to_the_cameras(desktop, admin, access_point):
    page = desktop.page()
    open_wall(page, token=TOKEN)
    wait_until(lambda: page.locator("#setup").is_visible(), 40, "the setup screen")

    admin.post("/api/device/network/join",
               json={"ssid": "Kitchen", "password": "a good password"}).raise_for_status()

    wait_until(lambda: page.locator("#setup").is_hidden(), 40, "the setup screen to go away")
    page.locator(".tile").first.wait_for()

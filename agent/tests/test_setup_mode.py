"""A device with no network puts up its own, and the wall says how to join it."""

from __future__ import annotations

import httpx
import pytest

from conftest import make_camera
from test_api import FakeAdapter
from test_go2rtc import FakeGo2rtc
from viewport import qr
from viewport.config import AppConfig, AuthConfig, ViewConfig
from viewport.go2rtc import Go2rtcClient
from viewport.hostctl import HostControl
from viewport.runtime import Runtime
from viewport.store import ConfigStore


def make_runtime(tmp_path, host, token=""):
    config = AppConfig(auth=AuthConfig(token=token), views=[ViewConfig(name="All", layout="auto")])
    rt = Runtime(config, adapters=[FakeAdapter([make_camera(0)])],
                 store=ConfigStore(tmp_path / "state.json"), host=host,
                 go2rtc=Go2rtcClient("http://g", transport=httpx.MockTransport(FakeGo2rtc().handler)))
    rt.wall.update_cameras("nvr", [make_camera(0)])
    return rt


async def test_a_device_with_no_network_puts_up_its_own(tmp_path):
    host = HostControl(fake=True)
    host._fake.link = "none"
    rt = make_runtime(tmp_path, host)

    await rt._offer_setup()

    setup = rt.wall.snapshot()["setup"]
    assert setup["ssid"] == host._fake.ap_ssid
    assert setup["password"] == host._fake.ap_password
    assert "access-point" in host._fake.actions


async def test_the_screen_gets_something_a_phone_can_scan(tmp_path):
    host = HostControl(fake=True)
    host._fake.link = "none"
    rt = make_runtime(tmp_path, host, token="wall-token")

    await rt._offer_setup()
    setup = rt.wall.snapshot()["setup"]

    assert len(setup["join_code"]) == len(setup["join_code"][0]), "a square"
    assert setup["url"].startswith("http://10.42.0.1:8080/admin")
    assert "token=wall-token" in setup["url"], "so the phone is not locked out on arrival"
    assert setup["url_code"], "and a code for that address too"


async def test_a_connected_device_is_left_alone(tmp_path):
    """Nothing here may interrupt a wall that is working."""
    host = HostControl(fake=True)                     # ethernet by default
    rt = make_runtime(tmp_path, host)

    await rt._offer_setup()

    assert rt.wall.snapshot()["setup"] is None
    assert "access-point" not in host._fake.actions


async def test_joining_a_network_takes_the_offer_away(tmp_path):
    host = HostControl(fake=True)
    host._fake.link = "none"
    rt = make_runtime(tmp_path, host)
    await rt._offer_setup()
    assert rt.wall.snapshot()["setup"] is not None

    await host.join("Kitchen", "a good password")
    await rt._offer_setup()

    assert rt.wall.snapshot()["setup"] is None


async def test_a_device_with_no_helper_says_nothing_at_all(tmp_path):
    rt = make_runtime(tmp_path, HostControl(socket_path=str(tmp_path / "none.sock"), fake=False))
    await rt._offer_setup()
    assert rt.wall.snapshot()["setup"] is None


async def test_the_wall_is_told_only_when_something_changes(tmp_path):
    """The snapshot goes to every screen, so it must not be republished on a timer."""
    host = HostControl(fake=True)
    host._fake.link = "none"
    rt = make_runtime(tmp_path, host)
    queue = rt.wall.subscribe()
    queue.get_nowait()                                # the snapshot every subscriber starts with

    await rt._offer_setup()
    assert queue.qsize() == 1, "the offer arrived"
    queue.get_nowait()

    await rt._offer_setup()
    assert queue.qsize() == 0, "and saying the same thing again told nobody"


def test_the_join_string_is_what_a_camera_expects():
    code = qr.wifi_join("OpenViewport-f00d", "join-me-here-42")
    assert code == "WIFI:T:WPA;S:OpenViewport-f00d;P:join-me-here-42;;"

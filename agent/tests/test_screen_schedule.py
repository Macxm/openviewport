"""Turning the television off overnight: when, and how often it is told."""

from __future__ import annotations

from datetime import time

import httpx
import pytest

from conftest import make_camera
from test_api import FakeAdapter
from test_go2rtc import FakeGo2rtc
from viewport.config import AppConfig, DisplayConfig, ScreenSchedule, ViewConfig
from viewport.go2rtc import Go2rtcClient
from viewport.hostctl import HostControl
from viewport.runtime import Runtime, screen_should_be_on
from viewport.store import ConfigStore


# ----- the decision -----------------------------------------------------------------

@pytest.mark.parametrize("now,expected", [
    ("22:59", True),      # the ordinary evening
    ("23:00", False),     # off on the minute
    ("03:00", False),     # the middle of the night, past midnight
    ("06:29", False),
    ("06:30", True),      # and on again
    ("12:00", True),
])
def test_a_night_that_crosses_midnight(now, expected):
    assert screen_should_be_on(time.fromisoformat(now), "23:00", "06:30") is expected


@pytest.mark.parametrize("now,expected", [("12:59", True), ("13:00", False),
                                          ("13:59", False), ("14:00", True)])
def test_an_afternoon_that_does_not(now, expected):
    """Off at 13:00, on at 14:00 is an hour in the middle of the day, not twenty-three."""
    assert screen_should_be_on(time.fromisoformat(now), "13:00", "14:00") is expected


def test_the_same_time_twice_means_no_schedule_at_all():
    assert screen_should_be_on(time.fromisoformat("03:00"), "22:00", "22:00") is True


def test_nonsense_leaves_the_screen_alone():
    assert screen_should_be_on(time.fromisoformat("03:00"), "bedtime", "06:30") is True


# ----- how often the device is told --------------------------------------------------

def make_runtime(tmp_path, host, **schedule):
    config = AppConfig(display=DisplayConfig(screen=ScreenSchedule(**schedule)),
                       views=[ViewConfig(name="All", layout="auto")])
    rt = Runtime(config, adapters=[FakeAdapter([make_camera(0)])], host=host,
                 store=ConfigStore(tmp_path / "state.json"),
                 go2rtc=Go2rtcClient("http://g", transport=httpx.MockTransport(FakeGo2rtc().handler)))
    return rt


async def test_the_screen_is_told_once_not_every_fifteen_seconds(tmp_path, monkeypatch):
    """A television told to turn on every pass is one nobody can turn off by hand."""
    host = HostControl(fake=True)
    rt = make_runtime(tmp_path, host, enabled=True, off_at="23:00", on_at="06:30")
    monkeypatch.setattr("viewport.runtime.screen_should_be_on", lambda *a: False)

    await rt._apply_screen_schedule()
    await rt._apply_screen_schedule()
    await rt._apply_screen_schedule()

    assert host._fake.actions.count("display") == 1
    assert host._fake.display_on is False


async def test_it_is_told_again_when_the_hour_comes_round(tmp_path, monkeypatch):
    host = HostControl(fake=True)
    rt = make_runtime(tmp_path, host, enabled=True)
    monkeypatch.setattr("viewport.runtime.screen_should_be_on", lambda *a: False)
    await rt._apply_screen_schedule()

    monkeypatch.setattr("viewport.runtime.screen_should_be_on", lambda *a: True)
    await rt._apply_screen_schedule()

    assert host._fake.display_on is True
    assert host._fake.actions.count("display") == 2


async def test_nothing_happens_at_all_when_the_schedule_is_off(tmp_path):
    host = HostControl(fake=True)
    rt = make_runtime(tmp_path, host, enabled=False)
    await rt._apply_screen_schedule()
    assert "display" not in host._fake.actions


async def test_a_device_that_cannot_turn_a_screen_off_is_not_asked_to(tmp_path):
    host = HostControl(socket_path=str(tmp_path / "none.sock"), fake=False)
    rt = make_runtime(tmp_path, host, enabled=True)
    await rt._apply_screen_schedule()          # no helper: nothing to ask, and no exception


async def test_a_refusal_is_retried_rather_than_remembered_as_done(tmp_path, monkeypatch):
    host = HostControl(fake=True)
    rt = make_runtime(tmp_path, host, enabled=True)
    monkeypatch.setattr("viewport.runtime.screen_should_be_on", lambda *a: False)

    async def refuse(on):
        raise __import__("viewport.hostctl", fromlist=["HostError"]).HostError("no cec here")

    monkeypatch.setattr(host, "display", refuse)
    await rt._apply_screen_schedule()
    assert rt._screen_on is None, "so the next pass tries again"

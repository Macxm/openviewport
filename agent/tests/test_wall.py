"""Wall state, focused on the time-dependent main-stream hold-down."""

from __future__ import annotations

from conftest import make_camera
from viewport.config import AppConfig, DeviceConfig, ViewConfig
from viewport.wall import WallController


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_wall(hold: float = 30.0, max_main: int = 1, cameras: int = 4):
    config = AppConfig(
        device=DeviceConfig(max_main_streams=max_main, main_stream_hold_seconds=hold),
        views=[ViewConfig(name="All cameras", layout="2x2")],
    )
    clock = FakeClock()
    wall = WallController(config, clock=clock)
    wall.update_cameras("nvr", [make_camera(i) for i in range(cameras)])
    return wall, clock


def quality_of(wall: WallController, tile_id: str):
    return next(t["quality"] for t in wall.snapshot()["tiles"] if t["id"] == tile_id)


def test_grid_starts_on_sub_streams():
    wall, _ = make_wall()
    assert quality_of(wall, "t0") == "sub"


def test_main_is_held_after_leaving_fullscreen_then_released():
    wall, clock = make_wall(hold=30)

    wall.set_fullscreen("t0")
    assert quality_of(wall, "t0") == "main"

    wall.set_fullscreen(None)
    assert quality_of(wall, "t0") == "main"          # held, not released immediately

    clock.advance(10)
    wall.tick()
    assert quality_of(wall, "t0") == "main"          # still inside the window

    clock.advance(25)                                 # 35 s since the tile lost demand
    wall.tick()
    assert quality_of(wall, "t0") == "sub"           # released


def test_tick_publishes_the_release_to_subscribers():
    wall, clock = make_wall(hold=30)
    wall.set_fullscreen("t0")
    wall.set_fullscreen(None)
    queue = wall.subscribe()
    while not queue.empty():
        queue.get_nowait()

    clock.advance(31)
    wall.tick()
    snap = queue.get_nowait()
    assert next(t["quality"] for t in snap["tiles"] if t["id"] == "t0") == "sub"


def test_tick_is_a_no_op_while_nothing_expires():
    wall, clock = make_wall(hold=30)
    wall.set_fullscreen("t0")
    wall.set_fullscreen(None)
    version = wall.version
    clock.advance(5)
    wall.tick()
    assert wall.version == version                    # no needless recompute or publish


def test_hold_yields_to_a_different_camera_going_fullscreen():
    wall, clock = make_wall(hold=30, max_main=1)
    wall.set_fullscreen("t0")
    wall.set_fullscreen(None)
    wall.set_fullscreen("t2")

    assert quality_of(wall, "t2") == "main"
    assert quality_of(wall, "t0") == "sub"
    assert wall.snapshot()["budget"]["main_in_use"] == 1


def test_zero_hold_releases_immediately():
    wall, _ = make_wall(hold=0)
    wall.set_fullscreen("t0")
    assert quality_of(wall, "t0") == "main"
    wall.set_fullscreen(None)
    assert quality_of(wall, "t0") == "sub"


def test_repeated_toggling_keeps_one_main_stream_throughout():
    """The point of the hold: the NVR sees one Clear session, not one per toggle."""
    wall, clock = make_wall(hold=30)
    seen = []
    for _ in range(5):
        wall.set_fullscreen("t0")
        seen.append(wall.active_streams())
        clock.advance(2)
        wall.set_fullscreen(None)
        seen.append(wall.active_streams())
        clock.advance(2)
    assert all("nvr_0_main" in s for s in seen)
    assert all(sum("_main" in n for n in s) == 1 for s in seen)


def test_restart_stream_bumps_only_that_stream():
    wall, _ = make_wall()
    wall.restart_stream("nvr_1_sub")
    epochs = {t["stream"]: t["epoch"] for t in wall.snapshot()["tiles"] if t["stream"]}
    assert epochs["nvr_1_sub"] == 1
    assert all(v == 0 for k, v in epochs.items() if k != "nvr_1_sub")

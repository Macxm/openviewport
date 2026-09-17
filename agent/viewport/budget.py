"""Stream budget: decides which stream (main, sub or none) each tile plays.

This is the one place that protects the NVR and the device's decoder:

* Tiles use the low-resolution sub stream unless they are big on screen.
* At most `max_main` main streams play at once (largest tiles win).
* At most `max_total` distinct streams play at once.
* The same stream shown in two tiles counts once (go2rtc shares one upstream connection).
* A main stream that is no longer wanted is held for `main_hold_seconds` if the budget
  is free, so flipping in and out of full screen does not make the NVR open and close a
  "Clear" session each time. Real demand always outranks a held stream.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .models import Camera, Quality


@dataclass(frozen=True)
class TileRequest:
    tile_id: str
    camera: Camera | None
    fraction: float       # share of the screen (0..1)
    visible: bool = True  # False while another tile is full screen
    allow_main: bool = True   # False keeps this tile on the sub stream however big it is
    force_main: bool = False  # True asks for the main stream however small it is


@dataclass(frozen=True)
class Assignment:
    tile_id: str
    camera_id: str | None
    quality: Quality | None
    stream: str | None
    reason: str


@dataclass(frozen=True)
class BudgetLimits:
    max_main: int = 1
    max_total: int = 16
    main_min_fraction: float = 0.4
    keep_hidden_warm: bool = True
    # Keep a main stream for this long after the tile stops deserving it (0 = release at once).
    main_hold_seconds: float = 30.0


def wants_main_stream(tile: TileRequest, limits: BudgetLimits) -> bool:
    """Whether a tile deserves the full-resolution stream: normally because it is big
    enough on screen, or because something was detected on it (`force_main`), which is
    what the wall uses when full resolution is reserved for detections.

    The single definition of "demand": `assign_streams` uses it to hand out the main
    budget, and `WallController` uses it to decide when a hold-down window starts.
    """
    return (tile.camera is not None and tile.camera.online and tile.allow_main
            and tile.visible and (tile.force_main or tile.fraction >= limits.main_min_fraction))


def assign_streams(tiles: list[TileRequest], limits: BudgetLimits,
                   held_main: Sequence[str] | frozenset[str] = ()) -> list[Assignment]:
    """`held_main` names main streams still inside their hold-down window, most recently
    wanted first: when the budget cannot keep them all, the one most likely to be open on
    the NVR right now is the one kept."""
    decided: dict[str, tuple[Quality | None, str]] = {}

    # 1. Tiles that can't play anything.
    for t in tiles:
        if t.camera is None:
            decided[t.tile_id] = (None, "empty")
        elif not t.camera.online:
            decided[t.tile_id] = (None, "offline")
        elif not t.visible and not limits.keep_hidden_warm:
            decided[t.tile_id] = (None, "hidden")

    # 2. Main streams go to the biggest visible tiles, up to the limit — but a tile with
    #    a detection on it comes first, however small, or reserving full resolution for
    #    detections would never give it to a camera that is only highlighted.
    #    Tile order breaks ties, so the result is stable.
    order = {t.tile_id: i for i, t in enumerate(tiles)}
    wants_main = sorted(
        (t for t in tiles if t.tile_id not in decided and wants_main_stream(t, limits)),
        key=lambda t: (not t.force_main, -t.fraction, order[t.tile_id]),
    )
    main_streams: set[str] = set()
    for t in wants_main:
        name = t.camera.stream_name("main")
        if name in main_streams or len(main_streams) < limits.max_main:
            main_streams.add(name)
            decided[t.tile_id] = ("main", "main: large tile")
        else:
            decided[t.tile_id] = ("sub", f"sub: main budget full ({limits.max_main})")

    # 2b. Spare main budget keeps recently-released main streams alive for a moment,
    #     so toggling full screen doesn't churn the NVR connection. Demand wins first,
    #     which is why this runs after the loop above.
    if held_main:
        rank = {name: i for i, name in enumerate(held_main)}
        candidates = sorted(
            (t for t in tiles if t.tile_id not in decided and t.visible and t.camera is not None
             and t.camera.stream_name("main") in rank),
            key=lambda t: (rank[t.camera.stream_name("main")], order[t.tile_id]),
        )
        for t in candidates:
            name = t.camera.stream_name("main")
            if name in main_streams or len(main_streams) < limits.max_main:
                main_streams.add(name)
                decided[t.tile_id] = ("main", "main: held after full screen")

    # 3. Everything else gets its sub stream.
    for t in tiles:
        if t.tile_id not in decided:
            decided[t.tile_id] = ("sub", "sub: small tile" if t.visible else "sub: kept warm")

    # 4. Enforce the total cap, visible tiles first, main streams before sub streams.
    in_use: set[str] = set()
    ranked = sorted(tiles, key=lambda t: (not t.visible, decided[t.tile_id][0] != "main", order[t.tile_id]))
    for t in ranked:
        quality, reason = decided[t.tile_id]
        if quality is None:
            continue
        name = t.camera.stream_name(quality)
        if name not in in_use and len(in_use) >= limits.max_total:
            decided[t.tile_id] = (None, f"none: stream budget full ({limits.max_total})")
            continue
        in_use.add(name)

    result = []
    for t in tiles:
        quality, reason = decided[t.tile_id]
        result.append(Assignment(
            tile_id=t.tile_id,
            camera_id=t.camera.id if t.camera else None,
            quality=quality,
            stream=t.camera.stream_name(quality) if (t.camera and quality) else None,
            reason=reason,
        ))
    return result


def summarize(assignments: list[Assignment], limits: BudgetLimits) -> dict:
    streams = {a.stream for a in assignments if a.stream}
    mains = {a.stream for a in assignments if a.quality == "main"}
    return {
        "main_in_use": len(mains),
        "max_main": limits.max_main,
        "streams_in_use": len(streams),
        "max_total": limits.max_total,
    }

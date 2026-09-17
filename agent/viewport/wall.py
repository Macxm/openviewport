"""Wall state: which view is showing, which tile is full screen, and what each tile plays.

Renderers (the browser wall now, a native Pi player later) subscribe to snapshots
and send commands back. They never pick streams themselves.

What is on screen is the view, adjusted by up to two things:
* a person's full-screen choice (`fullscreen`), and
* detection focus: a camera that sees something becomes primary (see focus.py for *which*
  camera; `_present()` for *how*). Anyone using the wall pauses detection-driven changes
  for `detection.manual_override_seconds`, so the screen is never snatched from under them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from .budget import (Assignment, BudgetLimits, TileRequest, assign_streams, summarize,
                     wants_main_stream)
from .config import (AppConfig, CameraSettings, DetectionConfig, DisplayConfig, LayoutConfig,
                     ViewConfig)
from .focus import DetectionGate, Focus, FocusPolicy, FocusTracker
from .layouts import SAME_LAYOUT, Layout, LayoutSet
from .models import Camera

log = logging.getLogger(__name__)


class WallController:
    def __init__(self, config: AppConfig, clock: Callable[[], float] = time.monotonic):
        self.config = config
        self.limits = BudgetLimits(
            max_main=config.device.max_main_streams,
            max_total=config.device.max_total_streams,
            main_min_fraction=config.device.main_stream_min_fraction,
            keep_hidden_warm=config.device.keep_grid_warm,
            main_hold_seconds=config.device.main_stream_hold_seconds,
        )
        self._clock = clock
        # Bumped when a stream's upstream changes under it (e.g. a transport fallback),
        # so renderers reconnect at once instead of waiting out their retry back-off.
        self.stream_epochs: dict[str, int] = {}
        # Main stream name -> when a tile last genuinely deserved it.
        self._main_demanded_at: dict[str, float] = {}
        self._held_main: frozenset[str] = frozenset()
        # Cameras as their sources report them, and as the wall shows them: the owner's own
        # name applied (camera_settings). Everything outside reads `cameras`.
        self._reported: dict[str, Camera] = {}
        self.cameras: dict[str, Camera] = {}
        self.camera_order: list[str] = []
        self.view_index = 0
        self.fullscreen: str | None = None          # a person's choice, in the view's tile ids
        self.version = 0
        self.layouts = _layout_set(config.layouts)
        self._layout: Layout = self.layouts.get("1x1", 0)
        self._tile_cameras: dict[str, str | None] = {}
        self._effective_fullscreen: str | None = None
        # Detection focus.
        self.detections: dict[str, frozenset[str]] = {}
        self._detections_at = float("-inf")
        # camera id -> (what it saw, when), so a badge can linger after the detection stops.
        self._highlight: dict[str, tuple[frozenset[str], float]] = {}
        self._visible: dict[str, frozenset[str]] = {}
        self._tracker = FocusTracker(_policy(config.detection))
        # Level -> event: what the NVR reports continuously is news only while it is new.
        self._gate = DetectionGate(config.detection.max_event_seconds)
        # When a person last used the wall. The override is measured from here with the
        # *current* setting, so shortening it in the admin page takes effect at once.
        self._user_acted_at = float("-inf")
        self._presented: tuple[Focus, str] | None = None   # (focus, "tile"|"layout"|"highlight")
        self._override_was_active = False
        self._assignments: list[Assignment] = []
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._recompute()

    # ----- inputs ---------------------------------------------------------

    def update_cameras(self, source_id: str, cameras: list[Camera]) -> None:
        kept = {k: v for k, v in self._reported.items() if v.source_id != source_id}
        kept.update({c.id: c for c in cameras})
        if kept != self._reported:
            self._reported = kept
            self._rebuild_cameras()

    def set_camera_settings(self, settings: dict[str, CameraSettings]) -> None:
        """Per-camera names, visibility, quality and fit (admin page)."""
        self.config.camera_settings = dict(settings)
        self._rebuild_cameras()

    def camera_setting(self, camera_id: str | None) -> CameraSettings:
        return self.config.camera_settings.get(camera_id or "") or _DEFAULT_CAMERA

    def reported_name(self, camera_id: str) -> str:
        """The name the NVR or camera gives, before the owner's own."""
        camera = self._reported.get(camera_id)
        return camera.name if camera else camera_id

    def shown(self, camera_id: str) -> bool:
        return self.camera_setting(camera_id).show

    def _rebuild_cameras(self) -> None:
        self.cameras = {
            camera_id: (camera.model_copy(update={"name": name}) if (name := self.camera_setting(camera_id).name)
                        else camera)
            for camera_id, camera in self._reported.items()
        }
        self.camera_order = self._ordered(self.cameras)
        self._recompute()

    def set_camera_order(self, camera_order: list[str]) -> None:
        """Set which cameras matter most (admin page)."""
        self.config.camera_order = list(camera_order)
        self.camera_order = self._ordered(self.cameras)
        self._recompute()

    def _ordered(self, cameras: dict[str, Camera]) -> list[str]:
        """Cameras by importance, then by where they were discovered."""
        source_order = [s.id for s in self.config.sources]
        discovered = sorted(cameras.values(), key=lambda c: (
            source_order.index(c.source_id) if c.source_id in source_order else 99, c.channel))
        priority = {camera_id: i for i, camera_id in enumerate(self.config.camera_order)}
        unlisted = len(priority)
        return [camera.id for _, camera in sorted(
            enumerate(discovered),
            key=lambda pair: (priority.get(pair[1].id, unlisted), pair[0]),
        )]

    def set_view(self, index: int, by_user: bool = True) -> None:
        views = self.config.views
        index %= len(views)
        interrupted = by_user and self._user_acted()
        if index != self.view_index or self.fullscreen is not None or interrupted:
            self.view_index = index
            self.fullscreen = None
            self._recompute()

    def step_view(self, delta: int, by_user: bool = True) -> None:
        self.set_view(self.view_index + delta, by_user=by_user)

    def set_views(self, views: list[ViewConfig]) -> None:
        """Replace the configured views (admin page). Keeps the current view if it survived."""
        if not views:
            raise ValueError("at least one view is required")
        current = self.view.name
        self.config.views = list(views)
        # Stay on the same view by name where possible, so editing an unrelated view
        # doesn't yank the wall somewhere else.
        names = [v.name for v in views]
        self.view_index = names.index(current) if current in names else 0
        self.fullscreen = None
        self._recompute()

    def restart_stream(self, name: str) -> None:
        """Tell renderers to reconnect one stream: its source changed underneath them."""
        self.stream_epochs[name] = self.stream_epochs.get(name, 0) + 1
        self._recompute()

    def set_fullscreen(self, tile_id: str | None, by_user: bool = True) -> None:
        _, base_tiles = self._base()
        if tile_id is not None and tile_id not in base_tiles:
            raise KeyError(tile_id)
        if tile_id is not None and base_tiles.get(tile_id) is None:
            return  # nothing to enlarge
        interrupted = by_user and self._user_acted()
        if tile_id != self.fullscreen or interrupted:
            self.fullscreen = tile_id
            self._recompute()

    def toggle_fullscreen(self, tile_id: str) -> None:
        # Judged by what is on screen: a tile full screen because of a detection closes too.
        self.set_fullscreen(None if self._effective_fullscreen == tile_id else tile_id)

    # ----- detection focus ------------------------------------------------

    def update_detections(self, active: dict[str, frozenset[str]]) -> None:
        """Latest detections from the sources, by camera id. Called on each detection poll."""
        now = self._clock()
        watched = self.watched_cameras()
        active = {cam: frozenset(types) for cam, types in active.items() if cam in watched and types}
        active = self._gate.update(active, now)
        self._detections_at = now
        before = (_key(self._tracker.focus), self.visible_detections())
        self.detections = active
        for camera_id, types in active.items():
            self._highlight[camera_id] = (types, now)
        self._tracker.camera_order = self.camera_order
        self._tracker.update(active, now)
        if (_key(self._tracker.focus), self.visible_detections()) != before:
            self._recompute()

    def visible_detections(self) -> dict[str, frozenset[str]]:
        """What each tile should show: live detections, plus ones still within the linger."""
        now = self._clock()
        seconds = self.config.detection.highlight_seconds
        visible = dict(self.detections)
        for camera_id, (types, at) in list(self._highlight.items()):
            if now - at >= max(seconds, 0.0):
                if camera_id not in self.detections:
                    del self._highlight[camera_id]       # expired: stop tracking it
                continue
            visible.setdefault(camera_id, types)
        return visible

    def dismiss_focus(self) -> None:
        """A person waved the detection away: back to the view, and leave it for a while."""
        self._tracker.clear()
        self._user_acted()
        self._recompute()

    def set_layouts(self, layouts: list[LayoutConfig]) -> None:
        """Replace the user-defined layouts (admin page)."""
        self.config.layouts = list(layouts)
        self.layouts = _layout_set(layouts)
        self._recompute()

    def set_display(self, display: DisplayConfig) -> None:
        self.config.display = display
        self._recompute()

    def set_device(self, device: Any) -> None:
        """New decode-budget and timing settings; the budget is rebuilt from them."""
        self.config.device = device
        self.limits = BudgetLimits(
            max_main=device.max_main_streams,
            max_total=device.max_total_streams,
            main_min_fraction=device.main_stream_min_fraction,
            keep_hidden_warm=device.keep_grid_warm,
            main_hold_seconds=device.main_stream_hold_seconds,
        )
        self._recompute()

    def set_detection_config(self, detection: DetectionConfig) -> None:
        self.config.detection = detection
        self._tracker = FocusTracker(_policy(detection), camera_order=self.camera_order)
        self._gate = DetectionGate(detection.max_event_seconds)
        self.detections = {}
        self._highlight = {}
        self._recompute()

    def focus_presented(self) -> bool:
        """Whether a detection is currently changing what is on screen."""
        return self._presented is not None and self._presented[1] != "highlight"

    def _user_acted(self) -> bool:
        """Start the manual override. True if that interrupted a detection on screen."""
        self._user_acted_at = self._clock()
        return self.focus_presented()

    def override_active(self) -> bool:
        """Someone used the wall recently; automatic changes (focus, cycling) wait."""
        return self._clock() < self._user_acted_at + self.config.detection.manual_override_seconds

    def watched_cameras(self) -> set[str]:
        """Cameras whose detections count. A camera kept off the wall is not one of them:
        otherwise a detection would bring onto the screen the camera its owner hid."""
        cams = self.config.detection.cameras
        watched = set(self.cameras) if cams == "all" else set(cams) & set(self.cameras)
        return {camera_id for camera_id in watched if self.shown(camera_id)}

    # ----- derived state --------------------------------------------------

    @property
    def view(self) -> ViewConfig:
        return self.config.views[self.view_index]

    def _view_cameras(self, view: ViewConfig) -> list[str]:
        cameras = self.camera_order if view.cameras == "all" else view.cameras
        return [camera_id for camera_id in cameras if self.shown(camera_id)]

    def _base(self) -> tuple[Layout, dict[str, str | None]]:
        """The current view's layout and which camera sits in each tile."""
        view = self.view
        camera_ids = self._view_cameras(view)
        try:
            layout = self.layouts.get(view.layout, len(camera_ids))
        except ValueError as exc:
            log.error("view %r: %s", view.name, exc)
            layout = self.layouts.get("auto", len(camera_ids))
        return layout, {tile.id: (camera_ids[i] if i < len(camera_ids) else None)
                        for i, tile in enumerate(layout.tiles)}

    def _present(self, layout: Layout, tiles: dict[str, str | None]
                 ) -> tuple[Layout, dict[str, str | None], str | None]:
        """Apply detection focus to the view. Returns layout, tile cameras, full-screen tile."""
        fullscreen = self.fullscreen
        self._presented = None
        now = self._clock()
        self._override_was_active = self.override_active()
        focus = self._tracker.focus
        camera = self.cameras.get(focus.camera_id) if focus else None
        # An offline camera has nothing to show: presenting it would leave the wall full
        # screen on a black tile until the hold expired.
        if focus is None or camera is None or not camera.online:
            return layout, tiles, fullscreen
        action = self.config.detection.action
        if action == "highlight":
            self._presented = (focus, "highlight")
            return layout, tiles, fullscreen
        if self._override_was_active:
            return layout, tiles, fullscreen            # someone is using the wall

        if action == "fullscreen":
            in_view = next((tid for tid, cam in tiles.items() if cam == focus.camera_id), None)
            if in_view is not None:
                # The usual full screen, so the rest of the grid stays warm for the return.
                self._presented = (focus, "tile")
                return layout, tiles, in_view
            self._presented = (focus, "layout")
            return self.layouts.get("1x1", 1), {"t0": focus.camera_id}, None

        # promote: the detected camera takes the first (largest) tile, the view's others
        # fill the rest. "same" keeps the view's own layout, so the wall does not change
        # shape — a 1+2 view stays 1+2 rather than jumping to a layout with empty tiles.
        view_cams = [cam for cam in tiles.values() if cam is not None]
        ordered = [focus.camera_id] + [cam for cam in view_cams if cam != focus.camera_id]
        wanted = self.config.detection.promote_layout
        if wanted == SAME_LAYOUT:
            promoted = layout
        else:
            try:
                promoted = self.layouts.get(wanted, len(ordered))
            except ValueError:
                promoted = layout
        self._presented = (focus, "layout")
        return promoted, {tile.id: (ordered[i] if i < len(ordered) else None)
                          for i, tile in enumerate(promoted.tiles)}, None

    def _recompute(self) -> None:
        self._visible = self.visible_detections()
        layout, tiles = self._base()
        if self.fullscreen is not None and tiles.get(self.fullscreen) is None:
            self.fullscreen = None
        layout, tiles, fullscreen = self._present(layout, tiles)
        self._layout, self._tile_cameras, self._effective_fullscreen = layout, tiles, fullscreen

        # Which tiles may ask the NVR for a full-resolution stream. A detection can make a
        # camera primary without also asking for its main stream (`use_main_stream`), and
        # `main_stream_only_on_detection` holds everything on sub streams until a camera
        # sees something — so both settings are the user's to make, and motion follows them.
        focused = self._presented[0].camera_id if self._presented else None
        only_on_detection = self.config.device.main_stream_only_on_detection
        # Neither setting overrules a person: a tile someone made full screen themselves
        # keeps its claim, because they asked to see it.
        chosen = fullscreen if (fullscreen is not None and fullscreen == self.fullscreen and (
            self._presented is None or self._presented[1] == "highlight")) else None

        requests = []
        for tile in layout.tiles:
            camera_id = self._tile_cameras[tile.id]
            camera = self.cameras.get(camera_id) if camera_id else None
            if fullscreen is None:
                fraction, visible = layout.fraction(tile), True
            elif tile.id == fullscreen:
                fraction, visible = 1.0, True
            else:
                fraction, visible = layout.fraction(tile), False
            # A camera's own quality setting is the most specific choice there is, so it comes
            # first: "sub" is never full resolution, not even full screen, and "main" asks for
            # it in any tile. The stream budget still has the last word on how many play.
            quality = self.camera_setting(camera_id).quality if camera_id else "auto"
            if quality == "sub":
                allow_main, force_main = False, False
            elif quality == "main":
                allow_main, force_main = True, True
            elif tile.id == chosen:
                allow_main, force_main = True, False
            elif camera_id is not None and camera_id == focused:
                allow_main = self.config.detection.use_main_stream
                force_main = allow_main and only_on_detection
            else:
                allow_main, force_main = not only_on_detection, False
            requests.append(TileRequest(tile.id, camera, fraction, visible, allow_main, force_main))

        self._held_main = self._note_demand_and_hold(requests)
        by_recency = sorted(self._held_main, key=lambda n: self._main_demanded_at[n], reverse=True)
        self._assignments = assign_streams(requests, self.limits, by_recency)
        self.version += 1
        self._publish()

    def _note_demand_and_hold(self, requests: list[TileRequest]) -> frozenset[str]:
        """Record which main streams are wanted now, and return those still held."""
        hold = self.limits.main_hold_seconds
        if hold <= 0:
            self._main_demanded_at.clear()
            return frozenset()
        now = self._clock()
        for t in requests:
            if wants_main_stream(t, self.limits):
                self._main_demanded_at[t.camera.stream_name("main")] = now
        # Drop expired entries so the map cannot grow without bound.
        self._main_demanded_at = {n: at for n, at in self._main_demanded_at.items()
                                  if now - at < hold}
        return frozenset(self._main_demanded_at)

    def tick(self) -> None:
        """Re-evaluate everything time-based. Cheap; the runtime calls it about once a second."""
        now = self._clock()
        changed = False
        if self.limits.main_hold_seconds > 0 and self._main_demanded_at:
            still_held = frozenset(n for n, at in self._main_demanded_at.items()
                                   if now - at < self.limits.main_hold_seconds)
            changed |= still_held != self._held_main
        if self._tracker.focus is not None or self.detections or self._highlight:
            # Detections nobody has refreshed for a while are stale (the NVR went away):
            # never let a last sighting hold the screen forever.
            stale_after = max(3 * self.config.detection.poll_seconds, 10.0)
            if self.detections and now - self._detections_at > stale_after:
                self.detections = {}
                changed = True
            before = _key(self._tracker.focus)
            self._tracker.update(self.detections, now)
            changed |= _key(self._tracker.focus) != before
            changed |= self.visible_detections() != self._visible   # a highlight ran out
        changed |= self.override_active() != self._override_was_active
        if changed:
            self._recompute()

    def active_streams(self) -> set[str]:
        return {a.stream for a in self._assignments if a.stream}

    def snapshot(self) -> dict[str, Any]:
        layout = self._layout
        by_tile = {a.tile_id: a for a in self._assignments}
        tiles = []
        for tile in layout.tiles:
            a = by_tile[tile.id]
            camera_id = self._tile_cameras.get(tile.id)
            camera = self.cameras.get(camera_id) if camera_id else None
            if camera is not None:
                camera_public = camera.public()
            elif camera_id is not None:
                camera_public = {"id": camera_id, "name": camera_id, "online": False}
            else:
                camera_public = None
            tiles.append({
                "id": tile.id, "x": tile.x, "y": tile.y, "w": tile.w, "h": tile.h,
                "camera": camera_public,
                "stream": a.stream, "quality": a.quality, "reason": a.reason,
                "epoch": self.stream_epochs.get(a.stream, 0) if a.stream else 0,
                "visible": self._effective_fullscreen is None or self._effective_fullscreen == tile.id,
                "detections": sorted(self._visible.get(camera_id, ())) if camera_id else [],
                "focus": bool(self._presented and self._presented[0].camera_id == camera_id),
                # The camera's own picture fit, or None to follow display.fit.
                "fit": _fit(self.camera_setting(camera_id)) if camera_id else None,
            })
        return {
            "type": "wall",
            "version": self.version,
            "device": {"name": self.config.device.name},
            "view": {"index": self.view_index, "name": self.view.name},
            "views": [{"index": i, "name": v.name} for i, v in enumerate(self.config.views)],
            "layout": layout.public(),
            "fullscreen": self._effective_fullscreen,
            "focus": self._focus_public(),
            "detection": {"enabled": self.config.detection.enabled,
                          "action": self.config.detection.action},
            "tiles": tiles,
            "budget": summarize(self._assignments, self.limits),
            "display": self.config.display.model_dump(),
            "player": {
                "go2rtc_url": self.config.go2rtc.public_url,
                "transport": self.config.go2rtc.player_transport,
                "mode": self.config.go2rtc.player_mode,
                "mjpeg_fallback": self.config.go2rtc.mjpeg_fallback,
            },
        }

    def _focus_public(self) -> dict[str, Any] | None:
        if self._presented is None:
            return None
        focus, presentation = self._presented
        camera = self.cameras.get(focus.camera_id)
        return {"camera": focus.camera_id, "name": camera.name if camera else focus.camera_id,
                "reason": focus.reason, "presentation": presentation}

    # ----- subscriptions --------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8)
        self._subscribers.add(queue)
        queue.put_nowait(self.snapshot())
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def _publish(self) -> None:
        if not self._subscribers:
            return
        snap = self.snapshot()
        for queue in self._subscribers:
            if queue.full():   # slow renderer: drop stale snapshots, keep the newest
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(snap)


_DEFAULT_CAMERA = CameraSettings()


def _fit(settings: CameraSettings) -> str | None:
    return None if settings.fit == "default" else settings.fit


def _policy(detection: DetectionConfig) -> FocusPolicy:
    return FocusPolicy(triggers=tuple(detection.triggers), hold_seconds=detection.hold_seconds,
                       min_focus_seconds=detection.min_focus_seconds)


def _key(focus: Focus | None) -> tuple[str, str] | None:
    """What matters for redrawing: which camera and why, not when it was last seen."""
    return (focus.camera_id, focus.reason) if focus else None


def _layout_set(layouts: list[LayoutConfig]) -> LayoutSet:
    built = []
    for spec in layouts:
        try:
            built.append(spec.build())
        except ValueError as exc:      # validated on the way in; a stale state file may not be
            log.warning("ignoring layout %r: %s", spec.id, exc)
    return LayoutSet(extra=tuple(built))

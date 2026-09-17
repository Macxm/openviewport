"""Background work: camera discovery, go2rtc sync and view cycling."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from .adapters import SourceAdapter, build_adapter
from .adapters.onvif import OnvifError
from .adapters.reolink import ReolinkError
from .config import AppConfig
from .go2rtc import Go2rtcClient, StreamRegistry
from .models import Camera, Quality
from .credentials import redact, register_secret, safe_error
from .secretstore import SecretStore
from .store import EDITABLE_SOURCE_FIELDS, ConfigStore
from .wall import WallController

log = logging.getLogger(__name__)

# Renderers report every 5 s, so this is roughly 15 s of a stream refusing to play.
FAILURES_BEFORE_FALLBACK = 3
# Don't flip the same stream again until the new transport has had a fair chance.
FALLBACK_COOLDOWN_S = 60.0
# How often the view rotation checks the clock. Also its resolution.
_CYCLE_TICK_SECONDS = 1.0
# A renderer that has not reported for this long is gone.
RENDERER_TIMEOUT_S = 60.0


class SourceState:
    def __init__(self, adapter: SourceAdapter, refresh_seconds: int):
        self.adapter = adapter
        self.refresh_seconds = refresh_seconds
        self.task: asyncio.Task[None] | None = None
        self.reachable: bool | None = None
        self.last_error: str | None = None
        self.last_success: float | None = None
        self.camera_count = 0
        self.failures = 0
        self.detection_failures = 0
        self.detection_retry_at = 0.0
        self.detection_error: str | None = None

    def next_delay(self) -> float:
        if self.failures == 0:
            return self.refresh_seconds
        # Exponential back-off with jitter so a struggling NVR isn't hammered.
        base = min(5 * 2 ** (self.failures - 1), 300)
        return base * random.uniform(0.8, 1.2)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.adapter.id,
            "reachable": self.reachable,
            "cameras": self.camera_count,
            "model": getattr(self.adapter, "model", None),
            "firmware": getattr(self.adapter, "firmware", None),
            "protocols": list(getattr(self.adapter, "available_protocols", ()) or ()) or None,
            "detection": {"supported": self.adapter.supports_detection,
                          "last_error": self.detection_error},
            "last_error": self.last_error,
            "last_success_age_s": None if self.last_success is None
            else round(time.monotonic() - self.last_success, 1),
        }


class Runtime:
    def __init__(self, config: AppConfig, adapters: list[SourceAdapter] | None = None,
                 go2rtc: Go2rtcClient | None = None, store: ConfigStore | None = None,
                 secrets_store: "SecretStore | None" = None):
        self.config = config
        self.store = store if store is not None else ConfigStore()
        adapters = adapters if adapters is not None else [build_adapter(s) for s in config.sources]
        refresh = {s.id: s.refresh_seconds for s in config.sources}
        self.sources = {a.id: SourceState(a, refresh.get(a.id, 60)) for a in adapters}
        self.go2rtc = go2rtc or Go2rtcClient(config.go2rtc.api_url)
        self.secrets = secrets_store if secrets_store is not None else SecretStore()
        # Sources that came from viewport.yaml: those stay the file's business. Not simply
        # everything in `config`: saved sources are overlaid onto it before we start, and
        # counting those as declared would make them impossible to remove.
        self.declared_sources = {s.id for s in config.sources} - self.store.added_source_ids
        self.registry = StreamRegistry(self.go2rtc, list(self.sources))
        self.wall = WallController(config)
        self.renderers: dict[str, dict[str, Any]] = {}
        # Automatic transport fallback, driven by what renderers report.
        self._stream_failures: dict[str, int] = {}
        self._fallback_at: dict[str, float] = {}
        self.fallbacks: dict[str, str] = {}         # stream -> transport it fell back to
        self._tasks: list[asyncio.Task[None]] = []
        self._sync_now = asyncio.Event()

    # ----- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        for state in self.sources.values():
            self._watch(state)
        self._tasks.append(asyncio.create_task(self._sync_loop(), name="go2rtc-sync"))
        # Always running: holds, detection focus and the manual override all expire with time.
        self._tasks.append(asyncio.create_task(self._tick_loop(), name="wall-tick"))
        # Also always running, idle while detection is off, so the admin page can switch it on.
        self._tasks.append(asyncio.create_task(self._detection_loop(), name="detection"))
        # Also always running: the rotation interval is editable at runtime.
        self._tasks.append(asyncio.create_task(self._cycle_loop(), name="cycle-views"))

    def _watch(self, state: SourceState) -> None:
        state.task = asyncio.create_task(self._discovery_loop(state),
                                         name=f"discover:{state.adapter.id}")
        self._tasks.append(state.task)

    async def add_source(self, source: Any, password: str | None = None,
                         username: str | None = None) -> None:
        """Add a source while running: build its adapter and start discovering."""
        if source.id in self.sources:
            raise ValueError(f"a source called {source.id!r} already exists")
        if username:
            source.username = username
        if password:
            source.password = password
        register_secret(source.password)
        adapter = build_adapter(source)              # raises for an unusable configuration
        self.config.sources.append(source)
        state = SourceState(adapter, source.refresh_seconds)
        self.sources[source.id] = state
        self.registry.manage(self.sources)
        self._persist_sources()
        if password is not None or username is not None:
            self._save_credentials(source.id, username, password)
        if self._tasks:                              # only once the runtime is running
            self._watch(state)
            await self.refresh_source(state)

    async def remove_source(self, source_id: str) -> None:
        """Remove a source, its cameras and its streams."""
        state = self.sources.pop(source_id, None)
        if state is None:
            raise KeyError(source_id)
        if state.task is not None:
            state.task.cancel()
            self._tasks = [t for t in self._tasks if t is not state.task]
        self.config.sources = [s for s in self.config.sources if s.id != source_id]
        self.wall.update_cameras(source_id, [])
        self._update_desired_streams()
        # Prune its streams while the registry still counts them as ours, then stop
        # managing the prefix.
        await self.registry.sync()
        self.registry.manage(self.sources)
        try:
            await state.adapter.close()
        except Exception:      # noqa: BLE001 - a source being removed must not hold this up
            log.exception("error closing %s", source_id)
        self._persist_sources()
        if self.secrets.writable:
            self.secrets.forget_source(source_id)

    def _persist_sources(self) -> None:
        """Remember sources added here. Those from viewport.yaml are not ours to store."""
        if not self.store.writable:
            return
        added = [s.model_dump(exclude={"password"}) for s in self.config.sources
                 if s.id not in self.declared_sources]
        self.store.save_section("added_sources", added)

    def _save_credentials(self, source_id: str, username: str | None, password: str | None) -> None:
        if not self.secrets.writable:
            log.warning("no secrets file configured: credentials for %s are not stored", source_id)
            return
        self.secrets.set_source(source_id, username, password)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for state in self.sources.values():
            try:
                await state.adapter.close()
            except Exception:  # noqa: BLE001 - best effort on shutdown
                log.exception("error closing %s", state.adapter.id)
        await self.go2rtc.close()

    # ----- discovery ------------------------------------------------------

    async def refresh_source(self, state: SourceState) -> None:
        adapter = state.adapter
        try:
            cameras = await adapter.discover()
        except (httpx.HTTPError, ReolinkError, OnvifError, ValueError, KeyError) as exc:
            state.failures += 1
            state.reachable = False
            state.last_error = safe_error(exc)   # httpx quotes URLs with the NVR session token
            log.warning("source %s: discovery failed (%s)", adapter.id, state.last_error)
            return
        state.failures = 0
        state.reachable = True
        state.last_error = None
        state.last_success = time.monotonic()
        state.camera_count = len(cameras)
        self.wall.update_cameras(adapter.id, cameras)
        self._update_desired_streams()

    async def _discovery_loop(self, state: SourceState) -> None:
        while True:
            await self.refresh_source(state)
            await asyncio.sleep(state.next_delay())

    # ----- go2rtc ---------------------------------------------------------

    def _update_desired_streams(self) -> None:
        desired: dict[str, str] = {}
        for camera in self.wall.cameras.values():
            adapter = self.sources[camera.source_id].adapter
            for quality in ("main", "sub"):
                name = camera.stream_name(quality)
                desired[name] = adapter.stream_source(camera, quality)
                if self.config.go2rtc.mjpeg_fallback:
                    desired[f"{name}_mjpeg"] = f"ffmpeg:{name}#video=mjpeg"
        changed = desired != self.registry.desired
        # Always publish: this also marks the registry ready to prune stale streams,
        # which it must not do before the first successful discovery.
        self.registry.set_desired(desired)
        if changed:
            self._sync_now.set()

    async def _sync_loop(self) -> None:
        while True:
            ok = await self.registry.sync()
            if not ok:
                log.warning("go2rtc unreachable: %s", self.registry.last_error)
            self._sync_now.clear()
            timeout = self.config.go2rtc.sync_seconds if ok else 3
            try:
                await asyncio.wait_for(self._sync_now.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    async def poll_detections(self) -> None:
        """One detection poll: a batched request per source that supports it."""
        watched = self.wall.watched_cameras()
        active: dict[str, frozenset[str]] = {}
        now = time.monotonic()
        for state in self.sources.values():
            adapter = state.adapter
            if not adapter.supports_detection or now < state.detection_retry_at:
                continue
            cameras = [c for c in self.wall.cameras.values()
                       if c.source_id == adapter.id and c.online and c.id in watched]
            if not cameras:
                continue
            try:
                active.update(await adapter.detections(cameras))
            except (httpx.HTTPError, ReolinkError, OnvifError, ValueError, KeyError) as exc:
                # Back off hard: a struggling NVR must not be polled every two seconds.
                state.detection_failures += 1
                delay = min(self.config.detection.poll_seconds * 2 ** state.detection_failures, 120.0)
                state.detection_retry_at = now + delay
                state.detection_error = safe_error(exc)
                log.warning("source %s: detection poll failed (%s); next try in %.0f s",
                            adapter.id, state.detection_error, delay)
                continue
            state.detection_failures = 0
            state.detection_error = None
        self.wall.update_detections(active)

    async def _detection_loop(self) -> None:
        while True:
            if self.config.detection.enabled:
                await self.poll_detections()
            await asyncio.sleep(self.config.detection.poll_seconds)

    def apply_layouts(self, layouts: list[Any]) -> None:
        self.wall.set_layouts(layouts)
        if self.store.writable:
            self.store.save_section("layouts", [layout.model_dump() for layout in layouts])

    def apply_camera_order(self, camera_order: list[str]) -> None:
        self.wall.set_camera_order(camera_order)
        if self.store.writable:
            self.store.save_section("camera_order", list(camera_order))

    def apply_camera_settings(self, settings: dict[str, Any]) -> None:
        """Per-camera names, visibility, quality and fit. A camera left at the defaults is
        not stored, so the file only says what someone actually changed."""
        kept = {camera_id: s for camera_id, s in settings.items() if not s.is_default()}
        self.wall.set_camera_settings(kept)
        if self.store.writable:
            self.store.save_section("camera_settings", {k: v.model_dump() for k, v in kept.items()})

    def apply_display(self, display: Any) -> None:
        self.wall.set_display(display)
        if self.store.writable:
            self.store.save_section("display", display.model_dump())

    def apply_device(self, device: Any) -> None:
        self.wall.set_device(device)
        if self.store.writable:
            self.store.save_section("device", device.model_dump())

    def apply_detection(self, detection: Any) -> None:
        """Swap in new detection settings (admin page) and remember them."""
        self.wall.set_detection_config(detection)
        for state in self.sources.values():
            state.detection_failures, state.detection_retry_at, state.detection_error = 0, 0.0, None
        if self.store.writable:
            self.store.save_detection(detection)

    async def _tick_loop(self) -> None:
        """Lets the wall release held streams and focus once their windows close."""
        while True:
            await asyncio.sleep(1)
            self.wall.tick()

    async def _cycle_loop(self) -> None:
        """Rotate through the views.

        A one-second tick rather than one long sleep: the interval is editable at runtime,
        and sleeping for it would both ignore a change until the old interval elapsed and,
        when set to zero, spin.
        """
        waited = 0.0
        while True:
            await asyncio.sleep(_CYCLE_TICK_SECONDS)
            seconds = self.config.device.cycle_views_seconds
            if seconds <= 0 or len(self.config.views) < 2:
                waited = 0.0
                continue
            # Not while someone is looking at one camera, a detection holds the screen,
            # or a person changed something a moment ago. The wait restarts afterwards,
            # so a view always gets its full time on screen.
            if (self.wall.fullscreen is not None or self.wall.focus_presented()
                    or self.wall.override_active()):
                waited = 0.0
                continue
            waited += _CYCLE_TICK_SECONDS
            if waited >= seconds:
                waited = 0.0
                self.wall.step_view(1, by_user=False)

    # ----- configuration edits (admin page) --------------------------------

    def apply_views(self, views: list[Any]) -> None:
        """Swap in a new set of views and remember them across restarts."""
        self.wall.set_views(views)
        if self.store.writable:
            self.store.save_views(self.config.views)

    async def update_source(self, source_id: str, edits: dict[str, Any],
                            username: str | None = None, password: str | None = None) -> None:
        """Change one source's settings, and optionally its credentials."""
        source = next((s for s in self.config.sources if s.id == source_id), None)
        if source is None:
            raise KeyError(source_id)
        unknown = set(edits) - set(EDITABLE_SOURCE_FIELDS)
        if unknown:
            raise ValueError(f"cannot change {', '.join(sorted(unknown))} here; "
                             "remove the source and add it again to change how it connects")
        self.apply_source_edits(source_id, edits)
        if username or password:
            if username:
                source.username = username
            if password:
                source.password = password
                register_secret(password)
            self._save_credentials(source_id, username, password)
            # The adapter captured the old credentials when it was built.
            await self._rebuild(self.sources[source_id], source)

    async def _rebuild(self, state: SourceState, source: Any) -> None:
        """Replace a source's adapter, e.g. after its credentials changed."""
        running = state.task is not None
        if state.task is not None:
            state.task.cancel()
            self._tasks = [t for t in self._tasks if t is not state.task]
            state.task = None
        try:
            await state.adapter.close()
        except Exception:      # noqa: BLE001 - the old session is going away regardless
            log.exception("error closing %s", source.id)
        state.adapter = build_adapter(source)
        state.refresh_seconds = source.refresh_seconds
        state.failures = 0
        if running:
            self._watch(state)
            await self.refresh_source(state)

    def apply_source_edits(self, source_id: str, edits: dict[str, Any]) -> None:
        """Settings that take effect without a new session."""
        source = next((s for s in self.config.sources if s.id == source_id), None)
        if source is None:
            raise KeyError(source_id)
        for field, value in edits.items():
            setattr(source, field, value)     # validated: SourceConfig validates assignment
        state = self.sources.get(source_id)
        if state is not None:
            state.refresh_seconds = source.refresh_seconds
            if {"protocol", "protocol_fallback"} & set(edits):
                # An earlier automatic fallback pinned a transport; that pin would
                # otherwise outrank the setting just chosen here.
                state.adapter.clear_protocol_overrides()
                for stream in [s for s in self.fallbacks if s.startswith(f"{source_id}_")]:
                    self.fallbacks.pop(stream, None)
                    self._stream_failures.pop(stream, None)
                    self._fallback_at.pop(stream, None)
        # Protocol changes alter every stream URL for this source.
        self._update_desired_streams()
        for stream in list(self.registry.desired):
            if stream.startswith(f"{source_id}_"):
                self.wall.restart_stream(stream)
        if self.store.writable:
            self.store.save_sources(self.config.sources)
        self._persist_sources()

    # ----- status ---------------------------------------------------------

    def record_renderer_stats(self, renderer_id: str, stats: dict[str, Any]) -> None:
        # Player errors are go2rtc's words, and go2rtc quotes source URLs with passwords.
        # Scrub them before they are stored, returned by /api/health or logged.
        tiles = [
            {**t, "error": redact(str(t["error"]))[:200]} if isinstance(t, dict) and t.get("error") else t
            for t in (stats.get("tiles") or [])
        ]
        now = time.time()
        # Every page load brings a new renderer id, so forget the ones that stopped
        # reporting. Otherwise this grows for as long as the device runs.
        self.renderers = {rid: seen for rid, seen in self.renderers.items()
                          if now - seen["received"] < RENDERER_TIMEOUT_S}
        self.renderers[renderer_id] = {"received": now, **stats, "tiles": tiles}
        self._note_stream_health(tiles)

    # ----- automatic transport fallback -----------------------------------

    def _camera_for_stream(self, stream: str) -> tuple[Camera, Quality] | tuple[None, None]:
        for camera in self.wall.cameras.values():
            for quality in ("main", "sub"):
                if camera.stream_name(quality) == stream:
                    return camera, quality
        return None, None

    def _note_stream_health(self, tiles: list[Any]) -> None:
        """Count how often each stream is reported broken, and switch transport if it sticks.

        Renderers report what they see; the decision stays here. If go2rtc itself is
        unreachable every tile fails at once, which says nothing about any one transport.
        """
        if not self.registry.reachable:
            return
        switched: list[str] = []
        for tile in tiles:
            if not isinstance(tile, dict):
                continue
            stream, state = tile.get("stream"), tile.get("state")
            if not isinstance(stream, str) or not stream:
                continue
            # An MJPEG variant is a transcode of the same upstream.
            stream = stream[: -len("_mjpeg")] if stream.endswith("_mjpeg") else stream
            if state == "playing":
                self._stream_failures.pop(stream, None)
            elif state == "error":
                self._stream_failures[stream] = self._stream_failures.get(stream, 0) + 1
                if (self._stream_failures[stream] >= FAILURES_BEFORE_FALLBACK
                        and self._fall_back(stream, str(tile.get("error", ""))[:120])):
                    switched.append(stream)
        if switched:
            # Register the new source first, then tell renderers to reconnect. The sync
            # is asynchronous, so a renderer may still race it once; its player is new
            # and retries a second later, by which time go2rtc has the new upstream.
            self._update_desired_streams()
            for stream in switched:
                self.wall.restart_stream(stream)

    def _fall_back(self, stream: str, error: str) -> bool:
        now = time.monotonic()
        if now - self._fallback_at.get(stream, float("-inf")) < FALLBACK_COOLDOWN_S:
            return False
        camera, quality = self._camera_for_stream(stream)
        if camera is None:
            return False
        adapter = self.sources[camera.source_id].adapter
        protocol = adapter.next_protocol(camera, quality)
        if protocol is None:
            return False
        self._fallback_at[stream] = now
        self._stream_failures.pop(stream, None)
        self.fallbacks[stream] = protocol
        log.warning("stream %s kept failing (%s); retrying over %s", stream, error, protocol)
        return True

    def health(self) -> dict[str, Any]:
        sources = [s.public() for s in self.sources.values()]
        now = time.time()
        renderers = {k: v for k, v in self.renderers.items()
                     if now - v["received"] < RENDERER_TIMEOUT_S}
        ok = self.registry.reachable and all(s["reachable"] for s in sources)
        return {
            "status": "ok" if ok else "degraded",
            "sources": sources,
            "go2rtc": {"reachable": self.registry.reachable, "last_error": self.registry.last_error,
                       "streams_registered": len(self.registry.desired)},
            "streams": {"fallbacks": dict(self.fallbacks),
                        "failing": {k: v for k, v in self._stream_failures.items() if v}},
            "wall": {"view": self.wall.view.name, "fullscreen": self.wall.fullscreen,
                     "budget": self.wall.snapshot()["budget"]},
            "renderers": renderers,
        }

"""Persistence for edits made in the admin page.

`config/viewport.yaml` stays the declared baseline and is never rewritten: it is
mounted read-only, it holds the NVR credentials, and an appliance should boot the
same way twice. Anything changed at runtime is written to a separate JSON file
instead, and overlaid on top of the YAML at startup.

A corrupt or unreadable state file is logged and ignored rather than fatal: a
viewport that will not boot is worse than one showing its configured views.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import (AppConfig, CameraSettings, DetectionConfig, DeviceConfig, DisplayConfig,
                     LayoutConfig, SourceConfig, ViewConfig)

log = logging.getLogger(__name__)

# Source fields the admin page may change. Host, username and password are absent on
# purpose: they live in .env / viewport.yaml, and writing them here would copy NVR
# credentials into a second file for no gain.
EDITABLE_SOURCE_FIELDS = ("protocol", "protocol_fallback", "refresh_seconds", "channels")


class ConfigStore:
    """Reads and writes the runtime overrides file."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        raw = str(path or os.environ.get("VIEWPORT_STATE_FILE", "")).strip()
        self.path: Path | None = Path(raw) if raw else None
        # Sources that `apply` put into the configuration from this file, as opposed to the
        # ones viewport.yaml declares: only these may be removed from the admin page.
        self.added_source_ids: set[str] = set()

    @property
    def writable(self) -> bool:
        """Whether edits can be kept. False means the admin page is read-only."""
        if self.path is None:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return os.access(self.path.parent, os.W_OK)
        except OSError:
            return False

    def load(self) -> dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable state file %s: %s", self.path, exc)
            return {}
        if not isinstance(data, dict):
            log.warning("ignoring state file %s: expected an object", self.path)
            return {}
        return data

    def save(self, data: dict[str, Any]) -> None:
        if self.path is None:
            raise RuntimeError("no state file configured (set VIEWPORT_STATE_FILE)")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        # Written 0600 and renamed into place, so a crash mid-write cannot leave a
        # half-parsed file behind.
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # ----- overlaying onto the declared config ----------------------------

    def apply(self, config: AppConfig) -> AppConfig:
        """Return `config` with any saved edits overlaid."""
        data = self.load()
        views = _validated_views(data.get("views"))
        if views:
            config.views = views
        # Sources added in the admin page. Their credentials live in the secrets file,
        # never here, so this file stays safe to copy around.
        for raw in data.get("added_sources") or []:
            try:
                source = SourceConfig.model_validate(raw)
            except ValidationError as exc:
                log.warning("ignoring saved source: %s", exc)
                continue
            if all(existing.id != source.id for existing in config.sources):
                config.sources.append(source)
                self.added_source_ids.add(source.id)
        _apply_source_edits(config.sources, data.get("sources"))
        for key, model in (("detection", DetectionConfig), ("display", DisplayConfig),
                          ("device", DeviceConfig)):
            if isinstance(data.get(key), dict):
                try:
                    setattr(config, key, model.model_validate(data[key]))
                except ValidationError as exc:
                    log.warning("ignoring saved %s settings: %s", key, exc)
        if isinstance(data.get("camera_order"), list):
            config.camera_order = [str(c) for c in data["camera_order"]]
        if isinstance(data.get("camera_settings"), dict):
            settings = {}
            for camera_id, raw in data["camera_settings"].items():
                try:
                    settings[str(camera_id)] = CameraSettings.model_validate(raw)
                except ValidationError as exc:     # one bad camera must not cost the others
                    log.warning("ignoring saved settings for camera %s: %s", camera_id, exc)
            config.camera_settings = settings
        if isinstance(data.get("layouts"), list):
            try:
                config.layouts = [LayoutConfig.model_validate(layout) for layout in data["layouts"]]
            except ValidationError as exc:
                log.warning("ignoring saved layouts: %s", exc)
        return config

    def configured(self) -> bool:
        """Whether someone has finished (or dismissed) first-run setup.

        An install that already has settings saved counts as configured, so upgrading does
        not walk an existing user through a wizard they finished months ago.
        """
        data = self.load()
        return bool(data.get("configured")) or any(key in data for key in self.SECTIONS
                                                   if key != "configured")

    def mark_configured(self) -> None:
        self.save_section("configured", True)

    def save_detection(self, detection: DetectionConfig) -> None:
        self.save_section("detection", detection.model_dump())

    #: The only sections that may be written. Credentials belong to none of them.
    SECTIONS = ("views", "sources", "detection", "display", "device", "layouts",
                "camera_order", "camera_settings", "added_sources", "configured")

    def save_section(self, key: str, value: Any) -> None:
        """Store one section of runtime settings (never credentials)."""
        if key not in self.SECTIONS:
            # Not an assert: this guards what reaches the disk, and -O removes asserts.
            raise ValueError(f"{key!r} is not a settings section; expected one of "
                             f"{', '.join(self.SECTIONS)}")
        data = self.load()
        data[key] = value
        self.save(data)

    def save_views(self, views: list[ViewConfig]) -> None:
        data = self.load()
        data["views"] = [v.model_dump() for v in views]
        self.save(data)

    def save_sources(self, sources: list[SourceConfig]) -> None:
        data = self.load()
        data["sources"] = {
            s.id: {f: getattr(s, f) for f in EDITABLE_SOURCE_FIELDS} for s in sources
        }
        self.save(data)


def _validated_views(raw: Any) -> list[ViewConfig]:
    if not isinstance(raw, list) or not raw:
        return []
    try:
        return [ViewConfig.model_validate(v) for v in raw]
    except ValidationError as exc:
        log.warning("ignoring saved views: %s", exc)
        return []


def _apply_source_edits(sources: list[SourceConfig], raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    by_id = {s.id: s for s in sources}
    for source_id, edits in raw.items():
        source = by_id.get(source_id)
        if source is None or not isinstance(edits, dict):
            continue  # a source that was removed from the YAML since the edit
        for field in EDITABLE_SOURCE_FIELDS:
            if field not in edits:
                continue
            try:
                setattr(source, field, edits[field])
            except ValidationError as exc:
                log.warning("ignoring saved %s.%s: %s", source_id, field, exc)

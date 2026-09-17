"""Configuration: a YAML file with ${ENV_VAR} / ${ENV_VAR:-default} substitution."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .credentials import env_value, register_secret
from .layouts import MAX_GRID, MAX_TILES, Layout, build_layout

log = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Recursively replace ${VAR} and ${VAR:-default} in strings.

    Each VAR may instead be supplied as a file through VAR_FILE (Docker and Kubernetes
    secrets), so `password: ${NVR_PASSWORD}` also works with NVR_PASSWORD_FILE.
    """
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: env_value(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


class DeviceConfig(BaseModel):
    name: str = "Viewport"
    # How many full-resolution (main) streams this device may play at once.
    # This is the decode budget: keep it at 1 on a Raspberry Pi 5.
    max_main_streams: int = Field(1, ge=0, le=16)
    # Hard cap on distinct streams played at once (main + sub).
    max_total_streams: int = Field(16, ge=1, le=64)
    # A tile gets the main stream only if it covers at least this share of the screen.
    main_stream_min_fraction: float = Field(0.4, gt=0, le=1)
    # Keep every tile on the low-resolution sub stream until a camera detects something,
    # then give that camera the full-resolution stream (if `detection.use_main_stream`).
    # The big tile is normally full resolution all day; this trades that for a quiet NVR
    # and a cooler device, and full resolution appears when there is something to see.
    main_stream_only_on_detection: bool = False
    # Keep the grid's sub streams running while one camera is full screen,
    # so returning to the grid is instant. Costs decode time on weak devices.
    keep_grid_warm: bool = True
    # Hold a main stream this long after its tile stops deserving it, so flipping in
    # and out of full screen doesn't make the NVR open a new "Clear" session each time.
    # 0 releases immediately. Only ever uses main budget that is otherwise idle.
    main_stream_hold_seconds: float = Field(30.0, ge=0, le=600)
    # Rotate through views every N seconds (0 = off). Paused while full screen.
    cycle_views_seconds: int = Field(0, ge=0)


class Go2rtcConfig(BaseModel):
    # Where the agent reaches the go2rtc API.
    api_url: str = "http://127.0.0.1:1984"
    # Where the renderer (browser) reaches go2rtc. Blank = same host as the wall, port 1984.
    public_url: str = ""
    # Player transport order for the browser wall: mse, mjpeg.
    player_mode: str = "mse"
    # How the player reaches go2rtc. `proxy` relays video through the agent, so go2rtc can
    # stay on loopback and video needs auth.token. `direct` connects the browser straight to
    # go2rtc (public_url): one hop less, but go2rtc must then be reachable from the screen.
    player_transport: Literal["proxy", "direct"] = "proxy"
    # Also register an MJPEG version of every stream (transcoded by go2rtc's ffmpeg).
    # Only for browsers without H.264 support and for automated tests. Costs CPU.
    mjpeg_fallback: bool = False
    sync_seconds: int = Field(30, ge=5)


class TileSpec(BaseModel):
    """One tile of a user-defined layout, in grid cells."""

    x: int = Field(ge=0, le=MAX_GRID - 1)
    y: int = Field(ge=0, le=MAX_GRID - 1)
    w: int = Field(1, ge=1, le=MAX_GRID)
    h: int = Field(1, ge=1, le=MAX_GRID)


class LayoutConfig(BaseModel):
    """A layout of the user's own, offered alongside the built-in ones."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9+_.-]{0,23}$")
    name: str = ""
    cols: int = Field(ge=1, le=MAX_GRID)
    rows: int = Field(ge=1, le=MAX_GRID)
    tiles: list[TileSpec] = Field(min_length=1, max_length=MAX_TILES)

    @model_validator(mode="after")
    def _geometry_fits(self) -> "LayoutConfig":
        self.build()          # raises ValueError for overlaps or tiles off the grid
        return self

    def build(self) -> Layout:
        return build_layout(self.id, self.cols, self.rows, [t.model_dump() for t in self.tiles])


class DisplayConfig(BaseModel):
    """How the renderer draws the wall. Renderers apply these; they never pick streams."""

    # contain shows the whole picture (letterboxed); cover fills the tile (crops).
    fit: Literal["contain", "cover"] = "contain"
    show_labels: bool = True
    show_quality_badges: bool = True
    show_clock: bool = False
    # What an offline camera's tile looks like: say so, or leave it dark.
    offline_style: Literal["message", "blank"] = "message"
    highlight_detections: bool = True
    hide_cursor_seconds: int = Field(3, ge=0, le=120)


DetectionType = Literal["person", "vehicle", "animal", "face", "motion"]


class DetectionConfig(BaseModel):
    """Make a camera primary when it sees something. Off by default: it polls the NVR."""

    enabled: bool = False
    # One batched request per interval for all watched cameras.
    poll_seconds: float = Field(2.0, ge=1.0, le=60.0)
    # What counts, highest priority first.
    triggers: list[DetectionType] = Field(default_factory=lambda: ["person", "vehicle"], min_length=1)
    # fullscreen: the camera fills the screen. promote: it takes the big tile of
    # `promote_layout`, the rest of the view around it. highlight: only mark its tile.
    action: Literal["fullscreen", "promote", "highlight"] = "fullscreen"
    # Which layout `promote` uses: "same" keeps the view's own layout and just moves the
    # camera into its first (largest) tile; "auto"/"auto-feature" pick one to fit the
    # camera count; or name a layout to override.
    promote_layout: str = "same"
    # Whether the camera a detection picks also gets the full-resolution stream. Off keeps
    # it on the sub stream: bigger on screen, but no extra load on the NVR.
    use_main_stream: bool = True
    # Stay on the camera this long after its last detection.
    hold_seconds: float = Field(15.0, ge=0, le=600)
    # How long one uninterrupted detection counts for. Reolink reports a *state*, so a car
    # parked on the drive keeps "vehicle" asserted for hours; past this it is scenery, and
    # counts again only once it has stopped and happened afresh. 0 = react for as long as
    # the NVR reports it.
    max_event_seconds: float = Field(30.0, ge=0, le=3600)
    # Keep the badge and outline on a tile this long after the detection stops, so a brief
    # sighting is still visible to someone who looks up a moment later. 0 = only while
    # the camera is actually detecting.
    highlight_seconds: float = Field(10.0, ge=0, le=600)
    # Don't hand the screen to another camera sooner than this (unless the first goes quiet).
    min_focus_seconds: float = Field(5.0, ge=0, le=300)
    # After someone uses the wall (full screen, view change, dismiss), leave it alone this long.
    manual_override_seconds: float = Field(60.0, ge=0, le=3600)
    # "all", or camera ids ("<source>:<channel>") to watch.
    cameras: Literal["all"] | list[str] = "all"

    @field_validator("triggers")
    @classmethod
    def _unique_triggers(cls, triggers: list[str]) -> list[str]:
        return list(dict.fromkeys(triggers))


class AuthConfig(BaseModel):
    # Shared token for /api/*. Blank switches authentication off, which is fine for
    # development on a trusted LAN but not for anything reachable from elsewhere.
    # Renderers pass it as `Authorization: Bearer <token>` or, for the WebSocket and
    # the wall page, a `token` query parameter. It lets a screen *watch* the wall.
    token: str = ""
    # Changing the configuration needs the admin password instead: the token travels in
    # the kiosk URL, so anyone who can read the TV's address must not be able to
    # reconfigure the wall. Blank means the token alone is enough (development).
    admin_username: str = "admin"
    # Supply one of these. A plaintext password is hashed at startup and not kept;
    # the hash avoids it being in the environment at all (python -m viewport.auth prints one).
    admin_password: str = ""
    admin_password_hash: str = ""
    session_hours: float = Field(12.0, gt=0, le=720)
    # Host names this device may be reached by, beyond the ones a LAN uses anyway (IP
    # addresses, localhost, single-label names, .local/.lan/.home.arpa/.internal). Needed
    # for a reverse proxy's public name; "*.example.com" matches its subdomains. It stops
    # DNS rebinding, where a web page points its own domain at this device. See security.py.
    allowed_hosts: list[str] = Field(default_factory=list)

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: Any) -> Any:
        if isinstance(value, str):            # VIEWPORT_ALLOWED_HOSTS=a.example.com,b.example.com
            return [part.strip() for part in value.split(",") if part.strip()]
        return value


class RtspCamera(BaseModel):
    """One camera of a `type: rtsp` source, given by URL."""

    name: str = Field(min_length=1)
    main: str = Field(min_length=1)       # full-resolution RTSP URL
    sub: str = ""                         # low-resolution URL; falls back to main


class SourceConfig(BaseModel):
    # The admin page edits a few of these in place, so assignments are validated too.
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    type: Literal["reolink", "onvif", "rtsp"] = "reolink"
    # reolink and onvif talk to a device; rtsp just plays the URLs below.
    host: str = ""
    port: int | None = None           # HTTP(S) API port; default 80/443 (onvif: 8000)
    https: bool = False
    verify_tls: bool = False          # NVRs usually ship self-signed certificates
    username: str = ""
    password: str = ""
    cameras_urls: list[RtspCamera] = Field(default_factory=list)   # `type: rtsp` only
    # auto: HTTP-FLV for H.264 up to 5 MP, RTSP otherwise (per Frigate's Reolink guidance).
    protocol: Literal["auto", "rtsp", "flv"] = "auto"
    # After a stream fails repeatedly, retry it over the other transport (RTSP <-> FLV).
    # Applies even when `protocol` is pinned: a transport that cannot connect is no
    # use pinned. Set false to always honour `protocol`.
    protocol_fallback: bool = True
    rtsp_port: int = 554
    refresh_seconds: int = Field(60, ge=10)
    channels: list[int] | None = None  # limit to these 0-based channels

    @field_validator("port", mode="before")
    @classmethod
    def _blank_port(cls, v: Any) -> Any:
        return None if v in ("", None) else v

    @field_validator("https", "verify_tls", mode="before")
    @classmethod
    def _blank_bool(cls, v: Any) -> Any:
        return False if v in ("", None) else v

    @field_validator("protocol_fallback", mode="before")
    @classmethod
    def _blank_true(cls, v: Any) -> Any:
        # Unlike the flags above, this one defaults on: blank means "leave it on".
        return True if v in ("", None) else v

    @model_validator(mode="after")
    def _type_needs_its_own_fields(self) -> "SourceConfig":
        if self.type == "rtsp":
            if not self.cameras_urls:
                raise ValueError("a source of type 'rtsp' needs cameras_urls")
        else:
            if not self.host:
                raise ValueError(f"a source of type '{self.type}' needs a host")
            if not self.username:
                raise ValueError(f"a source of type '{self.type}' needs a username")
        return self


class ViewConfig(BaseModel):
    name: str
    layout: str = "auto"
    # "all", or a list of camera ids in "<source>:<channel>" form (channel is 0-based).
    cameras: Literal["all"] | list[str] = "all"


class CameraSettings(BaseModel):
    """What the owner has decided about one camera. The defaults change nothing."""

    model_config = ConfigDict(extra="forbid")

    # Shown instead of the name the NVR or camera reports. Blank keeps that name. It is
    # never written back to the device.
    name: str = Field("", max_length=48)
    # False keeps the camera off the wall entirely: out of every view, and not watched
    # for detections.
    show: bool = True
    # auto: full resolution when the tile is big enough (the device settings decide).
    # main: full resolution whenever the stream budget allows, even in a small tile, which
    #   holds one of the NVR's few full-resolution sessions open and makes going full screen
    #   instant. sub: never, not even full screen.
    quality: Literal["auto", "main", "sub"] = "auto"
    # How the picture fills its tile; "default" follows display.fit.
    fit: Literal["default", "contain", "cover"] = "default"

    @field_validator("name")
    @classmethod
    def _tidy_name(cls, name: str) -> str:
        return " ".join(name.split())

    def is_default(self) -> bool:
        return self == CameraSettings()


class AppConfig(BaseModel):
    device: DeviceConfig = Field(default_factory=DeviceConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    layouts: list[LayoutConfig] = Field(default_factory=list)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    go2rtc: Go2rtcConfig = Field(default_factory=Go2rtcConfig)
    # Camera ids ("<source>:<channel>") in order of importance. Cameras not listed follow
    # in discovery order. This decides which camera lands in a layout's first (largest)
    # tile wherever a view says `cameras: all`; a view can still name its own list.
    camera_order: list[str] = Field(default_factory=list)
    # Per-camera choices by camera id: a name of the owner's own, whether it is shown, and
    # its stream quality. Kept for cameras that are not discovered right now, in case they
    # come back.
    camera_settings: dict[str, CameraSettings] = Field(default_factory=dict)
    sources: list[SourceConfig] = Field(default_factory=list)
    views: list[ViewConfig] = Field(default_factory=lambda: [ViewConfig(name="All cameras")])

    @field_validator("sources")
    @classmethod
    def _unique_ids(cls, sources: list[SourceConfig]) -> list[SourceConfig]:
        ids = [s.id for s in sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source ids must be unique")
        return sources

    @field_validator("views")
    @classmethod
    def _at_least_one_view(cls, views: list[ViewConfig]) -> list[ViewConfig]:
        return views or [ViewConfig(name="All cameras")]

    @field_validator("layouts")
    @classmethod
    def _unique_layout_ids(cls, layouts: list[LayoutConfig]) -> list[LayoutConfig]:
        ids = [layout.id for layout in layouts]
        if len(ids) != len(set(ids)):
            raise ValueError("layout ids must be unique")
        return layouts


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    path = Path(path or os.environ.get("VIEWPORT_CONFIG", "config/viewport.yaml"))
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    config = AppConfig.model_validate(_without_unconfigured_sources(expand_env(raw)))
    register_config_secrets(config)
    return config


def _without_unconfigured_sources(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop declared sources that have nothing to talk to yet.

    An appliance is installed before anyone knows the NVR's address: the baseline YAML
    declares a source whose host comes from the environment, and a blank one would only
    crash-loop the agent. Left out here, the wall comes up empty and the admin page's setup
    guide adds the real one.
    """
    sources = raw.get("sources")
    if not isinstance(sources, list):
        return raw
    kept = []
    for source in sources:
        if not isinstance(source, dict):
            kept.append(source)
            continue
        configured = source.get("cameras_urls") if source.get("type") == "rtsp" else source.get("host")
        if configured:
            kept.append(source)
        else:
            log.warning("source %r has nothing to connect to yet: add it in the admin page",
                        source.get("id", "?"))
    return {**raw, "sources": kept}


def register_config_secrets(config: AppConfig) -> None:
    """Tell the redactor about every secret in the config, so no log or response shows it."""
    register_secret(config.auth.token)
    register_secret(config.auth.admin_password)
    for source in config.sources:
        register_secret(source.password)

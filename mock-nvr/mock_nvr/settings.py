"""Mock NVR settings, read from MOCK_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_NAMES = [
    "Front Door", "Driveway", "Garden", "Garage",
    "Back Door", "Side Gate", "Porch", "Patio",
    "Hallway", "Kitchen", "Workshop", "Shed",
    "Street", "Parking", "Pool", "Roof",
]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _channel_set(value: str) -> frozenset[int]:
    return frozenset(int(c) for c in value.replace(" ", "").split(",") if c != "")


def _size(value: str) -> tuple[int, int]:
    w, h = value.lower().split("x")
    return int(w), int(h)


@dataclass(frozen=True)
class StreamProfile:
    quality: str          # "main" | "sub"
    codec: str            # "h264" | "h265"
    width: int
    height: int
    fps: int
    bitrate_kbps: int


@dataclass(frozen=True)
class Settings:
    username: str = "admin"
    password: str = "mockpass"
    channels: int = 4
    names: list[str] = field(default_factory=lambda: list(DEFAULT_NAMES))
    offline: frozenset[int] = frozenset()          # 0-based: a named camera that is down
    # 0-based: unused NVR slots. A real NVR reports every slot it has, so these come back
    # from GetChannelstatus with an empty name and online 0 (seen on an NVS8, fw 3.4.0.318).
    empty: frozenset[int] = frozenset()
    model: str = "RLN8-410"
    main: StreamProfile = StreamProfile("main", "h264", 1920, 1080, 15, 4096)
    sub: StreamProfile = StreamProfile("sub", "h264", 640, 360, 15, 512)
    # What GetNetPort reports. The mock always serves HTTP itself; this only changes the
    # answer, so the agent can be tested against an NVR with HTTP switched off.
    http_enabled: bool = True
    max_main_per_channel: int = 2                  # Reolink: 2 "Clear" viewers
    max_sub_per_channel: int = 10                  # Reolink: 10 "Fluent" viewers
    http_port: int = 80
    rtsp_port: int = 554
    go2rtc_api_port: int = 1985
    go2rtc_bin: str = "go2rtc"
    workdir: str = "/tmp/mock-nvr"

    @classmethod
    def from_env(cls) -> "Settings":
        names = [n.strip() for n in _env("MOCK_CAMERA_NAMES", "").split(",") if n.strip()]
        offline = _channel_set(_env("MOCK_OFFLINE_CHANNELS", ""))
        empty = _channel_set(_env("MOCK_EMPTY_CHANNELS", ""))
        mw, mh = _size(_env("MOCK_MAIN_SIZE", "1920x1080"))
        sw, sh = _size(_env("MOCK_SUB_SIZE", "640x360"))
        main_codec = _env("MOCK_MAIN_CODEC", "h264").lower()
        if main_codec not in ("h264", "h265"):
            raise ValueError("MOCK_MAIN_CODEC must be h264 or h265")
        channels = int(_env("MOCK_CHANNELS", "4"))
        if not 1 <= channels <= 16:
            raise ValueError("MOCK_CHANNELS must be between 1 and 16")
        return cls(
            username=_env("MOCK_USERNAME", "admin"),
            password=_env("MOCK_PASSWORD", "mockpass"),
            channels=channels,
            names=names + DEFAULT_NAMES[len(names):],
            offline=offline,
            empty=empty,
            http_enabled=_env("MOCK_HTTP_ENABLED", "true").lower() not in ("0", "false", "no"),
            model=_env("MOCK_MODEL", "RLN8-410"),
            main=StreamProfile("main", main_codec, mw, mh, int(_env("MOCK_MAIN_FPS", "15")),
                               int(_env("MOCK_MAIN_BITRATE", "4096"))),
            sub=StreamProfile("sub", "h264", sw, sh, int(_env("MOCK_SUB_FPS", "15")),
                              int(_env("MOCK_SUB_BITRATE", "512"))),
            max_main_per_channel=int(_env("MOCK_MAX_MAIN_PER_CHANNEL", "2")),
            max_sub_per_channel=int(_env("MOCK_MAX_SUB_PER_CHANNEL", "10")),
            http_port=int(_env("MOCK_HTTP_PORT", "80")),
            rtsp_port=int(_env("MOCK_RTSP_PORT", "554")),
            go2rtc_api_port=int(_env("MOCK_GO2RTC_API_PORT", "1985")),
            go2rtc_bin=_env("MOCK_GO2RTC_BIN", "go2rtc"),
            workdir=_env("MOCK_WORKDIR", "/tmp/mock-nvr"),
        )

    def channel_name(self, channel: int) -> str:
        if channel in self.empty:
            return ""
        return self.names[channel] if channel < len(self.names) else f"Camera {channel + 1}"

    def is_down(self, channel: int) -> bool:
        """No video: either an offline camera or an unused slot."""
        return channel in self.offline or channel in self.empty

    def profile(self, quality: str) -> StreamProfile:
        return self.main if quality == "main" else self.sub

    @staticmethod
    def rtsp_path(channel: int, quality: str) -> str:
        """Reolink NVR RTSP path: channels are 1-based and zero-padded."""
        return f"Preview_{channel + 1:02d}_{quality}"

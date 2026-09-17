"""Writes the ffmpeg test-pattern scripts and the go2rtc config that serves them over RTSP.

go2rtc starts an ffmpeg process only while someone is watching a stream, so idle
channels cost no CPU.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

from .settings import Settings, StreamProfile

FONT_CANDIDATES = [
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",            # Alpine (font-dejavu)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Debian/Ubuntu
    "/usr/share/fonts/droid/DroidSans-Bold.ttf",               # Alpine (font-droid)
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",       # macOS (running natively)
]


def find_font() -> str | None:
    return next((f for f in FONT_CANDIDATES if Path(f).exists()), None)


def ffmpeg_command(settings: Settings, channel: int, profile: StreamProfile, label_file: Path,
                   font: str | None) -> list[str]:
    hue = (channel * 47) % 360
    fontsize = max(14, profile.height // 22)
    filters = [f"hue=h={hue}"]
    if font:
        filters.append(
            f"drawtext=fontfile={font}:textfile={label_file}:fontcolor=white:fontsize={fontsize}"
            f":box=1:boxcolor=black@0.55:boxborderw={fontsize // 3}:x=w-tw-{fontsize}:y={fontsize}"
            f":line_spacing={fontsize // 3}"
        )
    if profile.codec == "h265":
        codec = ["-c:v", "libx265", "-preset", "ultrafast", "-tune", "zerolatency",
                 "-x265-params", f"keyint={profile.fps}:min-keyint={profile.fps}:bframes=0:log-level=error"]
    else:
        codec = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                 "-g", str(profile.fps), "-bf", "0", "-profile:v", "high"]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
        "-f", "lavfi", "-i", f"testsrc2=size={profile.width}x{profile.height}:rate={profile.fps}",
        "-vf", ",".join(filters),
        *codec,
        "-pix_fmt", "yuv420p",
        "-b:v", f"{profile.bitrate_kbps}k", "-maxrate", f"{profile.bitrate_kbps}k",
        "-bufsize", f"{profile.bitrate_kbps * 2}k",
        "-f", "rtsp", "-rtsp_transport", "tcp",
    ]


def write_runtime_files(settings: Settings) -> Path:
    """Create per-stream scripts and labels plus go2rtc.yaml. Returns the config path."""
    workdir = Path(settings.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    font = find_font()
    streams: dict[str, list[str]] = {}
    for channel in range(settings.channels):
        if settings.is_down(channel):
            continue
        for quality in ("main", "sub"):
            profile = settings.profile(quality)
            name = settings.rtsp_path(channel, quality)
            label = workdir / f"{name}.txt"
            codec = "H.265" if profile.codec == "h265" else "H.264"
            # %{localtime} is expanded by ffmpeg's drawtext on every frame (a live clock).
            label.write_text(
                f"CH{channel + 1}  {settings.channel_name(channel)}\n"
                f"{quality.upper()}  {profile.width}x{profile.height}  {codec}  {profile.fps}fps\n"
                "%{localtime}\n"
            )
            script = workdir / f"{name}.sh"
            cmd = ffmpeg_command(settings, channel, profile, label, font)
            script.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(c) for c in cmd) + ' "$1"\n')
            script.chmod(0o755)
            streams[name] = [f"exec:{script} {{output}}"]

    config = {
        "log": {"level": "warn"},
        "api": {"listen": f"127.0.0.1:{settings.go2rtc_api_port}"},
        "rtsp": {
            "listen": f":{settings.rtsp_port}",
            "username": settings.username,
            "password": settings.password,
        },
        "webrtc": {"listen": ""},
        "srtp": {"listen": ""},
        "streams": streams,
    }
    path = workdir / "go2rtc.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path

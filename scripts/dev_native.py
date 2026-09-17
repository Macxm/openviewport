#!/usr/bin/env python3
"""Run the whole dev stack without Docker (mock NVR + go2rtc + agent).

Needs `go2rtc` and `ffmpeg` on PATH (or GO2RTC_BIN) and the Python deps:
    pip install -e "agent[dev]" -r mock-nvr/requirements.txt

Ports: wall 8080, go2rtc 1984/8554, mock NVR HTTP 18081 / RTSP 18554.
Extra environment variables are passed through (e.g. NVR_PROTOCOL=rtsp, PLAYER_MODE=mjpeg).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    go2rtc = os.environ.get("GO2RTC_BIN") or shutil.which("go2rtc")
    if not go2rtc or not shutil.which("ffmpeg"):
        print("go2rtc and ffmpeg must be installed (or set GO2RTC_BIN)", file=sys.stderr)
        return 1
    workdir = Path(os.environ.get("DEV_WORKDIR", "/tmp/viewport-dev"))
    workdir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    mock_env = {
        **env,
        "PYTHONPATH": str(ROOT / "mock-nvr"),
        "MOCK_HTTP_PORT": "18081", "MOCK_RTSP_PORT": "18554", "MOCK_GO2RTC_API_PORT": "11985",
        "MOCK_GO2RTC_BIN": go2rtc, "MOCK_WORKDIR": str(workdir / "mock-nvr"),
        "MOCK_USERNAME": env.get("NVR_USERNAME", "admin"),
        "MOCK_PASSWORD": env.get("NVR_PASSWORD", "mockpass"),
    }
    agent_env = {
        "NVR_HOST": "127.0.0.1", "NVR_HTTP_PORT": "18081", "NVR_RTSP_PORT": "18554",
        "GO2RTC_API_URL": "http://127.0.0.1:1984",
        **env,
        "PYTHONPATH": str(ROOT / "agent"),
        "VIEWPORT_CONFIG": str(ROOT / "config" / "viewport.yaml"),
    }
    # go2rtc rewrites its own config when a stream is deleted, and a rewritten config holds
    # every source URL — camera passwords included. In compose that file is mounted read-only
    # for exactly this reason; here it is a copy in the work directory, so the tracked
    # config/go2rtc.yaml can never end up with credentials in it.
    go2rtc_config = workdir / "go2rtc.yaml"
    shutil.copyfile(ROOT / "config" / "go2rtc.yaml", go2rtc_config)
    go2rtc_config.chmod(0o600)
    procs = [
        subprocess.Popen([sys.executable, "-m", "mock_nvr"], env=mock_env),
        subprocess.Popen([go2rtc, "-config", str(go2rtc_config)], cwd=workdir),
    ]
    time.sleep(1.5)
    procs.append(subprocess.Popen([sys.executable, "-m", "viewport"], env=agent_env))
    print("Wall: http://localhost:8080   Mock NVR: http://localhost:18081   go2rtc: http://localhost:1984")

    def shutdown(*_):
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    while all(p.poll() is None for p in procs):
        time.sleep(1)
    print("a process exited; stopping", file=sys.stderr)
    shutdown()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

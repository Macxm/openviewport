"""Entry point: write runtime files, start go2rtc (RTSP side), then serve the HTTP API."""

from __future__ import annotations

import logging
import signal
import subprocess
import sys

import uvicorn

from .app import create_app
from .settings import Settings
from .streams import find_font, write_runtime_files

log = logging.getLogger("mock_nvr")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    config_path = write_runtime_files(settings)
    if not find_font():
        log.warning("No font found; test patterns will have no labels")
    log.info("Mock NVR: %d channels, RTSP :%d, HTTP :%d (user %s)",
             settings.channels, settings.rtsp_port, settings.http_port, settings.username)
    go2rtc = subprocess.Popen([settings.go2rtc_bin, "-config", str(config_path)])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.http_port, log_level="warning")
    finally:
        go2rtc.terminate()
        try:
            go2rtc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            go2rtc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

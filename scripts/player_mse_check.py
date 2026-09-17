#!/usr/bin/env python3
"""Checks the browser player's MSE path (buffering, live-edge tracking, restart, stop)
against a fake go2rtc WebSocket that streams fragmented MP4.

Uses AV1 so it runs in browsers without H.264 (e.g. Playwright's Chromium on Linux).
Needs ffmpeg with libsvtav1, websockets and playwright.

    python scripts/player_mse_check.py
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path

import websockets
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "agent" / "viewport" / "web"
WS_PORT, HTTP_PORT = 19841, 19842
MIME = 'video/mp4; codecs="av01.0.05M.08"'
connections = {"count": 0}

TEST_PAGE = """<!doctype html><meta charset=utf-8><body style="margin:0;background:#000">
<div id=a style="position:relative;width:640px;height:360px"></div>
<script type=module>
import { StreamPlayer } from '/player.js';
const p = new StreamPlayer({ baseUrl: 'http://127.0.0.1:%d', stream: 'cam', mode: 'mse',
                             codecs: ['av01.0.05M.08'] });
document.getElementById('a').append(p.element);
p.start();
window.player = p;
</script>""" % WS_PORT


async def ws_handler(ws):
    connections["count"] += 1
    try:
        msg = json.loads(await ws.recv())
    except websockets.ConnectionClosed:
        return
    if msg.get("type") != "mse" or "av01" not in msg.get("value", ""):
        await ws.send(json.dumps({"type": "error", "value": "mse: codecs not matched"}))
        return
    await ws.send(json.dumps({"type": "mse", "value": MIME}))
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15",
        "-c:v", "libsvtav1", "-preset", "12", "-g", "15", "-pix_fmt", "yuv420p",
        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1",
        stdout=asyncio.subprocess.PIPE, env={**os.environ, "SVT_LOG": "1"})
    try:
        while True:
            chunk = await proc.stdout.read(16384)
            if not chunk:
                break
            await ws.send(chunk)
    except websockets.ConnectionClosed:
        pass
    finally:
        proc.kill()
        await proc.wait()


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = TEST_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


def serve_http():
    handler = functools.partial(Handler, directory=str(WEB))
    http.server.ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), handler).serve_forever()


def serve_ws(ready: threading.Event):
    async def run():
        async with websockets.serve(ws_handler, "127.0.0.1", WS_PORT):
            ready.set()
            await asyncio.Future()
    asyncio.run(run())


def main() -> int:
    ready = threading.Event()
    threading.Thread(target=serve_http, daemon=True).start()
    threading.Thread(target=serve_ws, args=(ready,), daemon=True).start()
    ready.wait(5)
    failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{HTTP_PORT}/")

        def stats():
            return page.evaluate("window.player && window.player.stats()")

        deadline = time.time() + 20
        while time.time() < deadline and (stats() or {}).get("state") != "playing":
            time.sleep(0.3)
        s = stats()
        print("after start:", s)
        if s.get("state") != "playing":
            failures.append("player never reached 'playing'")

        time.sleep(25)
        s = stats()
        buffered = page.evaluate("(() => { const b = window.player.sb.buffered;"
                                 " return b.length ? b.end(b.length-1) - b.start(0) : 0; })()")
        print("after 25 s:", s, "buffered span:", round(buffered, 1))
        if s["frames"] < 300:
            failures.append(f"too few frames decoded: {s['frames']}")
        if s.get("latency_s", 99) > 2.0:
            failures.append(f"latency too high: {s.get('latency_s')}")
        if buffered > 20:
            failures.append(f"buffer not trimmed: {buffered:.1f}s")

        before = connections["count"]
        page.evaluate("window.player.restart('test')")
        deadline = time.time() + 15
        while time.time() < deadline and not (connections["count"] > before and stats()["state"] == "playing"):
            time.sleep(0.3)
        print("after restart:", stats(), "connections:", connections["count"])
        if stats()["state"] != "playing":
            failures.append("did not recover after restart()")

        page.evaluate("window.player.stop()")
        time.sleep(1)
        after_stop = (stats()["state"], page.evaluate("document.querySelectorAll('video').length"))
        print("after stop:", after_stop)
        if after_stop != ("stopped", 0):
            failures.append(f"stop() did not clean up: {after_stop}")

        # A browser offering no matching codec should get a clear error and retry.
        page.evaluate("""(async () => {
          const { StreamPlayer } = await import('/player.js');
          window.p2 = new StreamPlayer({ baseUrl: 'http://127.0.0.1:%d', stream: 'cam', mode: 'mse',
                                         codecs: ['avc1.640029', 'av01.0.05M.08'] });
          window.p2.codecList = ['nonexistent.codec'];
          window.p2.start();
        })()""" % WS_PORT)
        time.sleep(2)
        s2 = page.evaluate("window.p2.stats()")
        print("no-codec player:", s2)
        if s2["state"] != "error" or "cannot play" not in s2.get("error", ""):
            failures.append("missing-codec error not reported")
        browser.close()
    failures += [f"page error: {e}" for e in errors]
    if failures:
        print("FAILED:\n  " + "\n  ".join(failures))
        return 1
    print("MSE player check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Smoke check of a *running* stack, whatever it is pointed at (a real NVR included): the wall
plays, full screen swaps to main, the stream budget holds, the admin page loads and fits a
phone. It writes nothing, but it briefly changes what the shared wall shows. Saves screenshots.

    docker compose --profile test run --rm smoke        # containerised (the normal way)
    python scripts/e2e_check.py --channel chrome        # or against a host browser

The regression suite is tests/e2e, run by scripts/e2e.sh on a stack of its own.

Codec notes, because MSE playback needs a browser that can decode H.264/H.265:
  * Playwright's bundled Chromium has neither, on any platform. Use --mode mjpeg with it
    (the stack must then run with MJPEG_FALLBACK=true).
  * Playwright's Firefox and WebKit do decode both, including on arm64 Linux, so the
    container runs --browser firefox with real MSE playback.
  * On a Mac, --channel chrome uses the installed Google Chrome.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright


API_TOKEN = ""


def post_json(url: str, payload: dict):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    if API_TOKEN:
        req.add_header("Authorization", f"Bearer {API_TOKEN}")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def get_json(url: str):
    req = urllib.request.Request(url)
    if API_TOKEN:
        req.add_header("Authorization", f"Bearer {API_TOKEN}")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def wait_for_stack(url: str, timeout_s: float):
    """Wait for the agent to serve a wall with playable tiles.

    `docker compose run` starts the services it depends on, so the stack may be only
    seconds old and discovery may not have finished.
    """
    deadline = time.time() + timeout_s
    last = "no response"
    while time.time() < deadline:
        try:
            wall = get_json(f"{url}/api/wall")
            playable = [t["id"] for t in wall["tiles"] if t["stream"]]
            if playable:
                return wall, playable
            last = "connected, but no tile has a stream yet"
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(1)
    raise AssertionError(f"stack not ready after {timeout_s:.0f}s ({last}); check {url}/api/health")


def wait_for(page, predicate_js: str, timeout_s: float, what: str):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if page.evaluate(predicate_js):
            return
        time.sleep(0.5)
    stats = page.evaluate("window.viewport && window.viewport.stats()")
    raise AssertionError(f"timed out waiting for {what}; tile stats: {json.dumps(stats, indent=1)}")


ADMIN_SECTIONS = ["sources", "cameras", "layouts", "views", "display", "detection", "device"]

# A finger dragged across a fresh 3 × 2 layout, corner to corner. The events go to the cell
# the touch started on, as a browser's implicit touch capture sends them: an editor that
# listens per cell never hears the rest of the drag.
TOUCH_DRAG_JS = """async () => {
  const card = document.querySelector('#layouts > article:last-of-type');
  const cells = card.querySelector('.layout-cells');
  const cell = (x, y) => cells.querySelector(`[data-x="${x}"][data-y="${y}"]`);
  cell(0, 0).scrollIntoView({ block: 'center' });
  const centre = (node) => {
    const r = node.getBoundingClientRect();
    return { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
  };
  const [from, to] = [centre(cell(0, 0)), centre(cell(2, 1))];
  const send = (type, at) => cell(0, 0).dispatchEvent(new PointerEvent(type, {
    bubbles: true, cancelable: true, pointerId: 9, pointerType: 'touch', isPrimary: true,
    button: 0, buttons: 1, ...at }));
  const before = card.querySelectorAll('.layout-tile').length;
  send('pointerdown', from);
  send('pointermove', to);
  send('pointerup', to);
  await new Promise((resolve) => setTimeout(resolve, 100));
  const after = document.querySelector('#layouts > article:last-of-type');
  return { before, after: after.querySelectorAll('.layout-tile').length,
           touchAction: getComputedStyle(after.querySelector('.layout-cells')).touchAction };
}"""


def check_admin_on_a_phone(browser, url: str, token: str, out: Path, errors: list[str]) -> None:
    """The admin page at phone width: nothing wider than the screen, the section tabs in one
    row, an unsaved change bringing up the save bar until it is discarded, and a layout
    joined by touch. Nothing is saved: writes are refused, because this may be pointed at
    a real installation."""
    phone = browser.new_page(viewport={"width": 360, "height": 740})
    phone.on("pageerror", lambda e: errors.append(f"admin page: {e}"))
    refused: list[str] = []

    def refuse_writes(route):
        if route.request.method in ("GET", "HEAD"):
            route.continue_()
        else:
            refused.append(f"{route.request.method} {route.request.url}")
            route.abort()

    phone.route("**/api/**", refuse_writes)
    phone.goto(url + "/admin" + (f"?token={quote(token, safe='')}" if token else "") + "#sources")
    phone.wait_for_function("() => !document.getElementById('shell').hidden"
                            " || document.getElementById('unlock-dialog').open", timeout=10_000)
    if phone.evaluate("document.getElementById('lockable').disabled || document.getElementById('unlock-dialog').open"):
        print("admin on a phone skipped: the admin page is locked")
        phone.close()
        return

    for section in ADMIN_SECTIONS:
        phone.evaluate(f"location.hash = '#{section}'")
        phone.wait_for_function(f"() => !document.getElementById('panel-{section}').hidden")
        phone.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
        too_wide = phone.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert too_wide <= 0, f"admin section {section} is {too_wide}px wider than a phone screen"
    rows = phone.evaluate("new Set([...document.querySelectorAll('#tabs .tab')].map((t) => t.offsetTop)).size")
    assert rows == 1, f"the admin page's section tabs wrap onto {rows} rows on a phone"

    phone.evaluate("location.hash = '#display'")
    phone.wait_for_function("() => !document.getElementById('panel-display').hidden")
    switch = phone.locator("#display input.switch").first
    was = switch.is_checked()
    switch.click()
    phone.wait_for_function("() => !document.getElementById('savebar').hidden")
    assert phone.evaluate("document.querySelector('.tab[data-section=display]').classList.contains('dirty')"), \
        "an unsaved change is not marked on its section's tab"
    phone.screenshot(path=str(out / "6-admin-phone.png"))
    phone.click("#discard")
    phone.wait_for_function("() => document.getElementById('savebar').hidden")
    assert switch.is_checked() == was, "discard did not put the setting back"

    phone.evaluate("location.hash = '#layouts'")
    phone.wait_for_function("() => !document.getElementById('panel-layouts').hidden")
    phone.click("#add-layout")
    phone.wait_for_timeout(500)            # the new layout scrolls into view smoothly
    drag = phone.evaluate(TOUCH_DRAG_JS)
    assert drag["touchAction"] == "none", "a finger on the layout editor would scroll the page"
    assert (drag["before"], drag["after"]) == (6, 1), f"a touch drag did not join the cells: {drag}"
    phone.click("#discard")
    phone.wait_for_function("() => document.getElementById('savebar').hidden")

    assert not refused, f"the admin check tried to change the configuration: {refused}"
    print("admin on a phone OK: fits the screen, one row of tabs, save bar, touch layout editing")
    phone.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--mode", default=None, help="player mode override: mse or mjpeg")
    ap.add_argument("--channel", default=None, help="browser channel, e.g. chrome (chromium only)")
    ap.add_argument("--browser", default=os.environ.get("E2E_BROWSER", "chromium"),
                    choices=["chromium", "firefox", "webkit"],
                    help="Playwright browser; firefox and webkit can decode H.264/H.265 "
                         "(default: $E2E_BROWSER, else chromium)")
    ap.add_argument("--transport", default=os.environ.get("E2E_TRANSPORT", "proxy"),
                    choices=["proxy", "direct"],
                    help="proxy: video through the agent (default); direct: straight from go2rtc")
    ap.add_argument("--go2rtc", default=None,
                    help="go2rtc URL the page uses with --transport direct "
                         "(default: the wall's host on port 1984)")
    ap.add_argument("--out", default="e2e-output")
    ap.add_argument("--token", default=os.environ.get("VIEWPORT_API_TOKEN", ""),
                    help="agent API token, if auth.token is set")
    ap.add_argument("--timeout", type=float, default=30)
    args = ap.parse_args()
    global API_TOKEN
    API_TOKEN = args.token
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    wall, playable = wait_for_stack(args.url, args.timeout)
    # Start from a known state: another renderer may have left a camera full screen or
    # moved to another view, and the wall is shared.
    post_json(f"{args.url}/api/wall/fullscreen", {"tile": None})
    post_json(f"{args.url}/api/wall/view", {"index": 0})
    query = ("?hud=1"
             + (f"&mode={args.mode}" if args.mode else "")
             + f"&transport={args.transport}"
             + (f"&go2rtc={quote(args.go2rtc, safe='')}" if args.go2rtc and args.transport == "direct" else "")
             + (f"&token={quote(args.token, safe='')}" if args.token else ""))

    with sync_playwright() as p:
        launcher = getattr(p, args.browser)
        browser = launcher.launch(channel=args.channel) if args.channel else launcher.launch()
        print(f"browser: {args.browser}" + (f" ({args.channel})" if args.channel else "")
              + f", transport: {args.transport}")
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        errors: list[str] = []
        sockets: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("websocket", lambda ws: sockets.append(ws.url))
        page.goto(args.url + query)

        all_playing = (
            "(() => { const s = window.viewport && window.viewport.stats(); if (!s) return false;"
            f" const want = {json.dumps(playable)};"
            " return want.every(id => s.some(t => t.tile === id && t.state === 'playing' && (t.frames||0) > 2)); })()"
        )
        wait_for(page, all_playing, args.timeout, "all grid tiles to play")
        page.screenshot(path=str(out / "1-grid.png"))
        print(f"grid OK: {len(playable)} tiles playing")

        media = [u for u in sockets if "/api/wall/ws" not in u]
        if args.transport == "proxy":
            # The point of the relay: the screen never needs to reach go2rtc at all.
            stray = [u for u in media if "/api/media/ws" not in u]
            assert not stray, f"player bypassed the agent relay: {stray}"
            assert media, "no media connections were made"
            print(f"relay OK: {len(media)} video connections, all through the agent")

        # The budget must hold in the grid. Not "no main streams at all": the wall is
        # shared, so someone may be watching one full screen, and a stream released moments
        # ago is deliberately held for a while. What must never break is the cap.
        budget = get_json(f"{args.url}/api/wall")["budget"]
        assert budget["main_in_use"] <= budget["max_main"], budget
        if budget["main_in_use"]:
            print(f"note: {budget['main_in_use']} full-resolution stream(s) already in use "
                  "(someone watching, or a hold-down still running)")

        first = playable[0]
        page.click(f'.tile[data-tile="{first}"]')
        main_playing = (
            "(() => { const s = window.viewport.stats();"
            f" const t = s.find(x => x.tile === {json.dumps(first)});"
            " return window.viewport.snapshot.fullscreen === " + json.dumps(first) +
            " && t && t.state === 'playing' && /_main/.test(t.stream) && (t.frames||0) > 2; })()"
        )
        wait_for(page, main_playing, args.timeout, "full screen tile to switch to the main stream")
        time.sleep(1)
        page.screenshot(path=str(out / "2-fullscreen.png"))
        snap = get_json(f"{args.url}/api/wall")
        assert 1 <= snap["budget"]["main_in_use"] <= snap["budget"]["max_main"], snap["budget"]
        print("full screen OK: main stream playing")

        streams = get_json(f"{args.url}/api/streams")["go2rtc"]
        connected = {k: v for k, v in streams.items() if v["upstream_connected"]}
        mains = [k for k in connected if "_main" in k and not k.endswith("_mjpeg")]
        print("upstream connections:", json.dumps(connected, indent=1))
        assert len(mains) <= snap["budget"]["max_main"], f"too many main upstreams: {mains}"

        page.keyboard.press("Escape")
        wait_for(page, all_playing + " && window.viewport.snapshot.fullscreen === null",
                 args.timeout, "grid to come back")
        page.screenshot(path=str(out / "3-back-to-grid.png"))
        print("back to grid OK")

        if len(wall["views"]) > 1:
            page.keyboard.press("]")
            wait_for(page, "window.viewport.snapshot.view.index === 1", 10,
                     "next view (is someone else changing the view?)")
            time.sleep(3)
            page.screenshot(path=str(out / "4-next-view.png"))
            print("view switch OK:", get_json(f"{args.url}/api/wall")["layout"]["id"])
            page.keyboard.press("[")
        else:
            print("view switch skipped: only one view is configured")

        # Admin page: only a smoke test. It renders entirely from /api/config, so a
        # broken fetch, a rename in the payload or a JS error all show up as no cards.
        page.goto(args.url + "/admin" + (f"?token={quote(args.token, safe='')}" if args.token else ""))
        wait_for(page, "document.querySelectorAll('#views .card').length > 0", 10,
                 "the admin page to list views")
        cards = page.evaluate("document.querySelectorAll('#views .card').length")
        sources = page.evaluate("document.querySelectorAll('#sources .card').length")
        assert sources >= 1, "admin page listed no sources"
        page.screenshot(path=str(out / "5-admin.png"), full_page=True)
        print(f"admin OK: {cards} views, {sources} source(s)")

        check_admin_on_a_phone(browser, args.url, args.token, out, errors)

        browser.close()
    if errors:
        print("page errors:", errors, file=sys.stderr)
        return 1
    print("E2E passed. Screenshots in", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

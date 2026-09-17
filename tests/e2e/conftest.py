"""The browser regression suite.

It runs in the `e2e` container against a stack of its own (scripts/e2e.sh): a mock NVR with
four cameras, one of them offline, and two agents.

* `WALL` is set up like a real installation: the product's viewport.yaml, an API token and
  an admin password.
* `FRESH` is a first run: no sources, no token, no password, nothing saved.

Every address and credential comes from the environment the compose file sets. A test that
changes a setting on `WALL` puts it back (`restore`), so the tests do not depend on their order.
A page that throws a script error fails its test, and a failing test leaves a screenshot in
e2e-output/.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from playwright.sync_api import sync_playwright

WALL = os.environ.get("E2E_WALL", "http://e2e-agent:8080")
FRESH = os.environ.get("E2E_FRESH", "http://e2e-agent-fresh:8080")
NVR = os.environ.get("E2E_NVR", "http://e2e-nvr")
TOKEN = os.environ.get("E2E_TOKEN", "")
ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "")
NVR_PASSWORD = os.environ.get("E2E_NVR_PASSWORD", "")
OUT = Path(os.environ.get("E2E_OUT", "e2e-output"))
# Playwright's Firefox and WebKit decode H.264 and H.265; its Chromium decodes neither.
BROWSER = os.environ.get("E2E_BROWSER", "firefox")


def wait_until(check, timeout: float = 30.0, what: str = "a condition", interval: float = 0.25):
    """Poll `check` until it returns something truthy, and return that."""
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = check()
        except Exception as exc:  # noqa: BLE001 - keep polling, and say what the last failure was
            last = exc
        else:
            if last:
                return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}; last: {last!r}")


# ----- the API ----------------------------------------------------------------------------

@pytest.fixture(scope="session")
def api():
    """WALL's API as a screen sees it: with the token."""
    with httpx.Client(base_url=WALL, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10) as client:
        yield client


@pytest.fixture(scope="session")
def admin():
    """WALL's API signed in as admin."""
    with httpx.Client(base_url=WALL, timeout=10) as client:
        client.post("/api/admin/login", json={"username": "admin", "password": ADMIN_PASSWORD}).raise_for_status()
        yield client


@pytest.fixture(scope="session", autouse=True)
def stack_is_ready(api, admin):
    """The agents answer as soon as they start, but finding the cameras takes a moment. WALL
    stands for an installation that is already set up, so its setup guide is marked done;
    FRESH keeps its first run for the guide's own test."""
    wait_until(lambda: any(t["stream"] for t in api.get("/api/wall").json()["tiles"]), 120,
               "the wall to have cameras with streams")
    admin.post("/api/config/setup-done").raise_for_status()
    api.post("/api/wall/fullscreen", json={"tile": None}).raise_for_status()


@pytest.fixture
def restore(admin):
    """Put every setting on WALL back as it was when the test started."""
    before = admin.get("/api/config").json()
    yield
    for path, body in (("/api/config/display", before["display"]),
                       ("/api/config/device", before["device"]),
                       ("/api/config/detection", before["detection"]),
                       ("/api/config/views", {"views": before["views"]}),
                       ("/api/config/cameras", {"order": before["camera_order"],
                                                "settings": before["camera_settings"]}),
                       ("/api/config/layouts", {"layouts": before["custom_layouts"]})):
        admin.put(path, json=body).raise_for_status()
    admin.post("/api/wall/fullscreen", json={"tile": None})


def wall_tiles(api):
    return api.get("/api/wall").json()["tiles"]


# ----- the browser ----------------------------------------------------------------------------

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"report_{report.when}", report)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as playwright:
        launched = getattr(playwright, BROWSER).launch()
        yield launched
        launched.close()


class Session:
    """A browser context with its pages, collecting script errors from all of them."""

    def __init__(self, browser, **options):
        self.context = browser.new_context(**options)
        self.errors: list[str] = []

    def page(self):
        page = self.context.new_page()
        page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        return page


def _open(browser, request, **options):
    session = Session(browser, **options)
    yield session
    report = getattr(request.node, "report_call", None)
    if report is not None and report.failed:
        OUT.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)
        for i, page in enumerate(session.context.pages):
            try:
                page.screenshot(path=str(OUT / f"failed-{name}-{i}.png"), full_page=True)
            except Exception:  # noqa: BLE001 - a screenshot must never hide the real failure
                pass
    session.context.close()
    assert not session.errors, f"script errors on the page: {session.errors}"


@pytest.fixture
def desktop(browser, request):
    yield from _open(browser, request, viewport={"width": 1280, "height": 800})


@pytest.fixture
def phone(browser, request):
    yield from _open(browser, request, viewport={"width": 360, "height": 740})


def unlock(session, base: str = WALL) -> None:
    """Sign the browser in as admin without going through the dialog."""
    resp = session.context.request.post(f"{base}/api/admin/login",
                                        data={"username": "admin", "password": ADMIN_PASSWORD})
    assert resp.ok, resp.text()


def open_wall(page, base: str = WALL, token: str = TOKEN, query: str = "") -> None:
    parts = [f"token={quote(token, safe='')}"] if token else []
    if query:
        parts.append(query)
    page.goto(f"{base}/" + (f"?{'&'.join(parts)}" if parts else ""))


def player_stats(page) -> list[dict]:
    return page.evaluate("() => (window.viewport && window.viewport.stats()) || []")


def wait_playing(page, tile_ids, timeout: float = 60.0) -> None:
    def all_playing():
        stats = {s["tile"]: s for s in player_stats(page)}
        return all(stats.get(t, {}).get("state") == "playing" and (stats[t].get("frames") or 0) > 2
                   for t in tile_ids)
    wait_until(all_playing, timeout, f"tiles {tile_ids} to play video")


def open_admin(page, section: str, base: str = WALL, token: str = "") -> None:
    page.goto(f"{base}/admin" + (f"?token={quote(token, safe='')}" if token else "") + f"#{section}")
    page.wait_for_function("() => !document.getElementById('shell').hidden"
                           " || document.getElementById('unlock-dialog').open")

"""HTTP defences that hold whether or not the API token and admin password are set.

* **Headers on every response.** A content security policy that allows this origin's own
  scripts and nothing else (so an injected `<img onerror>` runs nothing), no framing (the
  admin page cannot be clickjacked), no MIME sniffing, and no referrer: the wall's address
  can carry the API token. API responses are never cached; they carry camera names and,
  unlocked, usernames.

* **No cross-site use from a browser.** Any web page that someone on the LAN opens can send
  requests to this device. CORS stops it reading most answers, but not a form post, and not
  a WebSocket at all: it could watch the video relay whenever no token is set. Browsers say
  where a request comes from (`Sec-Fetch-Site`, `Origin`), so changing requests and every
  WebSocket from another site are refused. Clients that are not browsers — a native
  renderer, curl — send neither header and are unaffected.

* **DNS rebinding.** A web page can point its own domain at this device's LAN address,
  which makes its requests same-origin. Only the names a device on a LAN is reached by are
  accepted: IP addresses, `localhost`, single-label names (`viewport`), and names ending in
  .local, .lan, .home.arpa, .internal or .localdomain. Anything else — a reverse proxy's
  public name, say — has to be listed in `auth.allowed_hosts` (VIEWPORT_ALLOWED_HOSTS).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

LAN_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# What may go into the policy as a host. Anything else is left out rather than escaped.
_SAFE_HOST = re.compile(r"[A-Za-z0-9.\-]+(:\d{1,5})?|\[[0-9A-Fa-f:.]+\](:\d{1,5})?")


def host_name(host_header: str) -> str:
    """The name in a Host header, without its port, lower-case."""
    host = host_header.strip()
    if host.startswith("["):                       # [::1]:8080
        return host[1:host.find("]")].lower() if "]" in host else ""
    if host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host.rstrip(".").lower()


def host_allowed(host_header: str, allowed: Sequence[str] = ()) -> bool:
    name = host_name(host_header)
    if not name:
        return False
    if "*" in allowed:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if name == "localhost" or "." not in name or name.endswith(LAN_SUFFIXES):
        return True
    for pattern in allowed:
        pattern = pattern.strip().lower().rstrip(".")
        if pattern.startswith("*.") and (name.endswith(pattern[1:]) or name == pattern[2:]):
            return True
        if name == pattern:
            return True
    return False


def _origin_host(origin: str) -> str | None:
    """"http://viewport:8080" -> "viewport:8080". None for "null" or anything unparsable."""
    if "://" not in origin:
        return None
    return origin.split("://", 1)[1].split("/", 1)[0].lower() or None


def from_another_site(scope: Scope, headers: Headers) -> bool:
    """Whether a browser sent this from a page on another site."""
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None:
        # same-origin, same-site (another port on this host) and none (typed, bookmarked)
        # are all fine; only another site is not.
        return fetch_site == "cross-site"
    origin = headers.get("origin")
    if origin is None:
        return False                                # not a browser, or a plain navigation
    # Older browsers: compare the page's origin with where the request went. A proxy may
    # rewrite Host, so the forwarded one counts too; a page cannot set either header itself.
    sent_from = _origin_host(origin)
    targets = {headers.get("host", "").lower(), headers.get("x-forwarded-host", "").lower()} - {""}
    return sent_from is None or sent_from not in targets


def content_security_policy(host_header: str, extra_connect: Sequence[str] = ()) -> str:
    connect = ["'self'"]
    safe = bool(_SAFE_HOST.fullmatch(host_header.strip()))
    if safe:
        # 'self' covers ws:// to this origin in current browsers; older Safari needs it said.
        connect += [f"ws://{host_header.strip()}", f"wss://{host_header.strip()}"]
    for source in extra_connect:
        # "{host}" is the name this request came in on, e.g. go2rtc on the same device.
        if "{host}" in source:
            name = host_name(host_header)
            if not safe or not name:
                continue
            source = source.replace("{host}", f"[{name}]" if ":" in name else name)
        connect.append(source)
    return "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' blob: data:",
        "media-src 'self' blob:",
        f"connect-src {' '.join(connect)}",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, allowed_hosts: Sequence[str] = (),
                 extra_connect: Sequence[str] = ()):
        self.app = app
        self.allowed_hosts = tuple(allowed_hosts)
        self.extra_connect = tuple(extra_connect)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        websocket = scope["type"] == "websocket"
        if not host_allowed(host, self.allowed_hosts):
            await self._refuse(scope, receive, send, websocket, 400,
                               "unexpected Host: add this name to VIEWPORT_ALLOWED_HOSTS")
            return
        checked = websocket or scope.get("method", "GET") in UNSAFE_METHODS
        if checked and from_another_site(scope, headers):
            await self._refuse(scope, receive, send, websocket, 403, "requests from another site are refused")
            return
        if websocket:
            await self.app(scope, receive, send)
            return

        policy = content_security_policy(host, self.extra_connect)
        api = scope.get("path", "").startswith("/api/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                present = {name.lower() for name, _ in raw}

                def add(name: str, value: str) -> None:
                    if name.encode() not in present:
                        raw.append((name.encode(), value.encode()))

                add("content-security-policy", policy)
                add("x-content-type-options", "nosniff")
                add("x-frame-options", "DENY")
                add("referrer-policy", "no-referrer")
                add("permissions-policy", "camera=(), microphone=(), geolocation=()")
                if api:
                    add("cache-control", "no-store")
                message["headers"] = raw
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send, websocket: bool,
                      status: int, text: str) -> None:
        if websocket:
            await WebSocketClose(code=1008)(scope, receive, send)
        else:
            await PlainTextResponse(text, status_code=status)(scope, receive, send)

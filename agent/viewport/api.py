"""HTTP/WebSocket API and the browser wall."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import signal
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import (APIRouter, Depends, FastAPI, HTTPException, Request, Response,
                     WebSocket, WebSocketDisconnect, status)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, media
from .config import (AppConfig, CameraSettings, DetectionConfig, DeviceConfig, DisplayConfig,
                     LayoutConfig, RtspCamera, SourceConfig, ViewConfig, register_config_secrets)
from .auth import COOKIE, MIN_PASSWORD_LENGTH, admin_from_config, hash_password
from .credentials import safe_error
from .hostctl import HostControl, HostError
from .layouts import AUTO_NAMES, SAME_LAYOUT
from .runtime import Runtime
from .security import SecurityMiddleware
from .store import EDITABLE_SOURCE_FIELDS

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"
RENDERER_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RevalidatedStaticFiles(StaticFiles):
    """The wall's and admin page's files keep their names from one release to the next (there
    is no build step to fingerprint them). Sent without a Cache-Control header, a browser
    guesses how long to trust its copy, a tenth of the file's age, which can be days, and
    pairs a new page with an old stylesheet or script after an update. `no-cache` makes it
    ask each time; a file that has not changed costs a 304."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def bearer_token(headers, params) -> str | None:
    """Read the API token from an Authorization header, else a `token` query parameter.

    Browsers cannot set headers on a WebSocket handshake, so the query parameter is not
    just a convenience. It does mean the token can appear in logs and browser history:
    treat it as a device credential for a LAN appliance, not a user password.
    """
    header = headers.get("authorization") or ""
    if header[:7].lower() == "bearer ":
        return header[7:].strip() or None
    return params.get("token") or None


def token_matches(configured: str, supplied: str | None) -> bool:
    if not configured:
        return True                      # authentication disabled
    return bool(supplied) and secrets.compare_digest(supplied, configured)


class ViewRequest(BaseModel):
    index: int


class FullscreenRequest(BaseModel):
    tile: str | None = None


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class ViewsRequest(BaseModel):
    views: list[ViewConfig] = Field(min_length=1)


class CameraOrderRequest(BaseModel):
    order: list[str] = Field(default_factory=list, max_length=256)
    # Every camera's settings, replacing the saved ones. Absent leaves them alone, so a
    # client that only reorders need not know about them.
    settings: dict[str, CameraSettings] | None = Field(None, max_length=256)


class LayoutsRequest(BaseModel):
    layouts: list[LayoutConfig] = Field(default_factory=list, max_length=32)


class SourceEditRequest(BaseModel):
    """What can change on an existing source.

    Credentials can be set but never read back; they are stored in the secrets file (see
    docs/credentials.md). How a source *connects* — its type, host and port — is fixed:
    remove it and add it again to change that.
    """
    model_config = ConfigDict(extra="forbid")

    protocol: Literal["auto", "rtsp", "flv"] | None = None
    protocol_fallback: bool | None = None
    refresh_seconds: int | None = Field(None, ge=10)
    channels: list[int] | None = None
    username: str | None = None
    password: str | None = None


class NewSourceRequest(BaseModel):
    """A source added from the admin page."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    type: Literal["reolink", "onvif", "rtsp"] = "reolink"
    host: str = ""
    port: int | None = None
    https: bool = False
    verify_tls: bool = False
    username: str = ""
    password: str = ""
    protocol: Literal["auto", "rtsp", "flv"] = "auto"
    protocol_fallback: bool = True
    rtsp_port: int = 554
    refresh_seconds: int = Field(60, ge=10)
    channels: list[int] | None = None
    cameras_urls: list[RtspCamera] = Field(default_factory=list)


class JoinRequest(BaseModel):
    """Joining a wifi network. The password is never read back, like every other secret."""
    model_config = ConfigDict(extra="forbid")

    ssid: str
    password: str = ""


class NetworkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ssid: str


class PreferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    link: Literal["ethernet", "wifi"]


class AccessPointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: bool


class ResetRequest(BaseModel):
    """How far back to go.

    configuration: the settings made in this page, so the setup guide starts again. The admin
    password and the NVR's credentials stay, so whoever owns the device keeps it.
    device: those as well — everything this device knows, for handing it to someone else.
    """
    model_config = ConfigDict(extra="forbid")

    scope: Literal["configuration", "device"] = "configuration"
    forget_network: bool = False


class PasswordRequest(BaseModel):
    """Choosing the admin password in the page, or changing it."""
    model_config = ConfigDict(extra="forbid")

    username: str = Field("admin", max_length=64)
    current_password: str = Field("", max_length=1024)
    new_password: str = Field(max_length=1024)


def _direct_player_sources(config: AppConfig) -> list[str]:
    """What the page's content security policy must allow it to connect to beyond itself:
    with `player_transport: direct` the player talks to go2rtc on its own address."""
    if config.go2rtc.player_transport != "direct":
        return []
    public = config.go2rtc.public_url.strip().rstrip("/")
    if public:
        return [re.sub(r"^http", "ws", public)]
    return ["ws://{host}:1984", "wss://{host}:1984"]


def create_app(config: AppConfig, runtime: Runtime | None = None, *, start_background: bool = True,
               host: HostControl | None = None) -> FastAPI:
    rt = runtime or Runtime(config)
    device = host if host is not None else HostControl()
    admin = admin_from_config(config.auth, rt.secrets)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_background:
            await rt.start()
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title="Viewport agent", version=__version__, lifespan=lifespan)
    app.state.runtime = rt
    app.state.admin = admin
    app.mount("/static", RevalidatedStaticFiles(directory=WEB_DIR), name="static")
    app.add_middleware(SecurityMiddleware, allowed_hosts=config.auth.allowed_hosts,
                       extra_connect=_direct_player_sources(config))

    register_config_secrets(config)   # also covers configs built in code, not loaded from YAML
    token = config.auth.token
    if not token:
        log.warning("no auth.token set: the API is open to anyone who can reach this port")
    if not admin.required:
        log.warning("no admin password set: anyone who can reach the API can reconfigure "
                    "the wall (set VIEWPORT_ADMIN_PASSWORD)")

    def signed_in(request: Request) -> bool:
        return admin.session_valid(request.cookies.get(COOKIE))

    async def require_token(request: Request) -> None:
        """Watching the wall: the API token, or an admin session, which outranks it."""
        if signed_in(request):
            return
        if not token_matches(token, bearer_token(request.headers, request.query_params)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API token",
                                headers={"WWW-Authenticate": "Bearer"})

    async def require_admin(request: Request) -> None:
        """Changing the configuration.

        With no admin password set this falls back to the API token — never to nothing,
        or setting a token but no password would leave the configuration wide open.
        """
        if signed_in(request):
            return
        if not admin.required:
            await require_token(request)
            return
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "sign in (unlock the admin page) to change the configuration")

    # The wall page and its static files carry no camera data and no credentials, so they
    # stay open: the page reads the token from its own URL and uses it for the API.
    @app.get("/", include_in_schema=False)
    async def wall_page() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/healthz")
    async def liveness() -> dict[str, str]:
        """Unauthenticated liveness probe for the container healthcheck.

        Deliberately only a word: /api/health carries camera names, models and the
        go2rtc error text, and needs the token.
        """
        return {"status": rt.health()["status"]}

    api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])
    # Everything that changes the configuration.
    admin_api = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])

    def start_session(request: Request, response: Response) -> None:
        # Secure whenever the page was reached over HTTPS, directly or through a proxy that
        # says so. Over plain HTTP it cannot be (see docs/security.md): the browser would not
        # send it back.
        https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(COOKIE, admin.issue_session(), httponly=True, samesite="strict",
                            max_age=int(admin.session_hours * 3600), secure=https)

    def client_of(request: Request) -> str:
        """Who is trying to log in, for pausing that client alone after repeated failures."""
        return request.client.host if request.client else ""

    def session_payload(request: Request, authenticated: bool) -> dict[str, Any]:
        return {"authenticated": authenticated, "required": admin.required,
                "username": admin.username, "locked_for": round(admin.locked_for(client_of(request))),
                # "config": set in VIEWPORT_ADMIN_PASSWORD or viewport.yaml, so not changeable
                # here; "page": chosen in the admin page; None: there is no password yet.
                "password_source": admin.source,
                "can_set_password": admin.source != "config" and rt.secrets.writable,
                # Whether a screen needs the API token to watch. Not the token, only whether
                # there is one, which anyone can find out by trying without it.
                "token_required": bool(token)}

    @app.post("/api/admin/login")
    async def admin_login(req: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
        if not admin.required:
            return session_payload(request, authenticated=True)
        client = client_of(request)
        if admin.locked_for(client):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                f"too many attempts; try again in {admin.locked_for(client):.0f} s")
        if not admin.check_login(req.username, req.password, client):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")
        start_session(request, response)
        return session_payload(request, authenticated=True)

    @app.post("/api/admin/logout")
    async def admin_logout(request: Request, response: Response) -> dict[str, Any]:
        response.delete_cookie(COOKIE, httponly=True, samesite="strict")
        return session_payload(request, authenticated=not admin.required)

    @app.get("/api/admin/session")
    async def admin_session(request: Request) -> dict[str, Any]:
        return session_payload(request, authenticated=not admin.required or signed_in(request))

    @app.post("/api/admin/password")
    async def set_admin_password(req: PasswordRequest, request: Request,
                                 response: Response) -> dict[str, Any]:
        """Choose the admin password in the page, or change it.

        The first one needs what changing the configuration needed until then: the API
        token, if one is set. That is trust on first use, and no worse than before — until
        a password exists anyone who can reach the API can change everything. Changing it
        needs the current one, rate-limited like a login. A password set in the
        configuration wins and cannot be changed here, which is also how a forgotten one is
        recovered.
        """
        if admin.source == "config":
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "the admin password is set in the configuration "
                                "(VIEWPORT_ADMIN_PASSWORD); change it there")
        if not rt.secrets.writable:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "there is nowhere to keep a password: set VIEWPORT_SECRETS_FILE")
        first = not admin.required
        if first:
            await require_token(request)
        else:
            client = client_of(request)
            if admin.locked_for(client):
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                    f"too many attempts; try again in {admin.locked_for(client):.0f} s")
            if not admin.check_login(admin.username, req.current_password, client):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "the current password is wrong")
        if len(req.new_password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(422, f"use at least {MIN_PASSWORD_LENGTH} characters")
        username = req.username.strip() or "admin"
        password_hash = hash_password(req.new_password)
        try:
            rt.secrets.set_admin_login(username, password_hash)
        except OSError as exc:
            raise HTTPException(400, safe_error(exc)) from None
        admin.set_password(username, password_hash)
        log.warning("admin password %s in the admin page", "set" if first else "changed")
        start_session(request, response)          # whoever chose it stays signed in; everyone else is not
        return session_payload(request, authenticated=True)

    @api.get("/health")
    async def health() -> dict[str, Any]:
        return {"version": __version__, **rt.health()}

    @api.get("/cameras")
    async def cameras() -> list[dict[str, Any]]:
        return [rt.wall.cameras[c].model_dump() for c in rt.wall.camera_order]

    @api.get("/wall")
    async def wall() -> dict[str, Any]:
        return rt.wall.snapshot()

    @api.post("/wall/view")
    async def set_view(req: ViewRequest) -> dict[str, Any]:
        rt.wall.set_view(req.index)
        return rt.wall.snapshot()

    @api.post("/wall/view/next")
    async def next_view() -> dict[str, Any]:
        rt.wall.step_view(1)
        return rt.wall.snapshot()

    @api.post("/wall/view/prev")
    async def prev_view() -> dict[str, Any]:
        rt.wall.step_view(-1)
        return rt.wall.snapshot()

    @api.post("/wall/focus/dismiss")
    async def dismiss_focus() -> dict[str, Any]:
        rt.wall.dismiss_focus()
        return rt.wall.snapshot()

    @api.post("/wall/fullscreen")
    async def fullscreen(req: FullscreenRequest) -> dict[str, Any]:
        try:
            rt.wall.set_fullscreen(req.tile)
        except KeyError:
            raise HTTPException(404, f"unknown tile {req.tile}") from None
        return rt.wall.snapshot()

    @api.get("/streams")
    async def streams() -> dict[str, Any]:
        """Live go2rtc view of our streams: is an upstream connection open, how many viewers."""
        try:
            return {"active": sorted(rt.wall.active_streams()), "go2rtc": await rt.registry.stats()}
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, f"go2rtc unreachable: {safe_error(exc)}") from None

    @api.post("/sources/refresh")
    async def refresh_sources() -> dict[str, Any]:
        await asyncio.gather(*(rt.refresh_source(s) for s in rt.sources.values()))
        return rt.health()

    @api.get("/config")
    async def get_config(request: Request) -> dict[str, Any]:
        """The admin page reads the configuration without signing in, and shows it locked
        until someone unlocks it. Changing anything needs the admin session (admin_api)."""
        return config_payload(unlocked=signed_in(request) or not admin.required)

    def camera_payload(camera) -> dict[str, Any]:
        return {"id": camera.id, "name": camera.name, "reported_name": rt.wall.reported_name(camera.id),
                "online": camera.online, "source": camera.source_id, "channel": camera.channel,
                "model": camera.model,
                "main": camera.main.model_dump() if camera.main else None,
                "sub": camera.sub.model_dump() if camera.sub else None,
                "settings": rt.wall.camera_setting(camera.id).model_dump()}

    def config_payload(unlocked: bool = True) -> dict[str, Any]:
        """Everything the admin page needs in one call. Credentials are never included, and
        while the page is locked neither are usernames nor where settings are kept."""
        return {
            "editable": rt.store.writable,
            "locked": not unlocked,
            # False puts the admin page into first-run setup, which walks through the
            # sections in the order they are best done.
            "configured": rt.store.configured() if rt.store.writable else True,
            "state_file": str(rt.store.path) if unlocked and rt.store.path else None,
            "layouts": rt.wall.layouts.names(),
            # Geometry so the admin page can draw each layout and its tiles.
            "layout_geometry": {name: layout.public()
                                for name, layout in rt.wall.layouts.all.items()},
            # What to call each layout: a custom one shows the name its owner gave it.
            "layout_names": {name: _layout_name(config, name) for name in rt.wall.layouts.all},
            "custom_layouts": [layout.model_dump() for layout in config.layouts],
            "display": config.display.model_dump(),
            "device": config.device.model_dump(),
            "views": [v.model_dump() for v in config.views],
            "detection": config.detection.model_dump(),
            "detection_types": ["person", "vehicle", "animal", "face", "motion"],
            # In importance order, which is the order views fill their tiles.
            "cameras": [camera_payload(rt.wall.cameras[i]) for i in rt.wall.camera_order],
            "camera_order": list(config.camera_order),
            # Including cameras not discovered right now, so saving does not lose theirs.
            "camera_settings": {k: v.model_dump() for k, v in config.camera_settings.items()},
            "sources": [
                # Never the password: it can be set here but not read back.
                {"id": s.id, "type": s.type, "host": s.host,
                 "username": s.username if unlocked else None, "username_set": bool(s.username),
                 "password_set": bool(s.password), "port": s.port, "https": s.https,
                 "declared": s.id in rt.declared_sources,
                 "editable_fields": list(EDITABLE_SOURCE_FIELDS),
                 **{f: getattr(s, f) for f in EDITABLE_SOURCE_FIELDS}}
                for s in config.sources
            ],
        }

    @admin_api.put("/config/views")
    async def put_views(req: ViewsRequest) -> dict[str, Any]:
        for view in req.views:
            if view.layout not in AUTO_NAMES and view.layout not in rt.wall.layouts.all:
                raise HTTPException(422, f"unknown layout {view.layout!r}")
        try:
            rt.apply_views(req.views)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, safe_error(exc)) from None
        return config_payload()

    @admin_api.put("/config/detection")
    async def put_detection(req: DetectionConfig) -> dict[str, Any]:
        if (req.promote_layout != SAME_LAYOUT and req.promote_layout not in AUTO_NAMES
                and req.promote_layout not in rt.wall.layouts.all):
            raise HTTPException(422, f"unknown layout {req.promote_layout!r}")
        try:
            rt.apply_detection(req)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, safe_error(exc)) from None
        return config_payload()

    # ----- the device itself -------------------------------------------------------
    # Reading is allowed wherever the settings may be read, so the panel is useful while the
    # page is locked. Everything that *changes* the device needs the admin password, and goes
    # no further than hostctl's fixed vocabulary.

    @api.get("/device")
    async def device_status() -> dict[str, Any]:
        health = rt.health()
        return {
            "name": config.device.name,
            "version": __version__,
            "configured": rt.store.configured(),
            "host": await device.status(),
            "sources": [{"id": s["id"], "reachable": s["reachable"], "cameras": s["cameras"]}
                        for s in health.get("sources", [])],
            "cameras": len(rt.wall.cameras),
        }

    @admin_api.get("/device/networks")
    async def device_networks() -> dict[str, Any]:
        """Scanning is an action, not a reading: it takes the radio and shows the neighbours."""
        try:
            return {"networks": await device.networks()}
        except HostError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None

    @admin_api.post("/device/network/join")
    async def device_join(req: JoinRequest) -> dict[str, Any]:
        try:
            return {"host": {**await device.join(req.ssid, req.password)}}
        except HostError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    @admin_api.post("/device/network/forget")
    async def device_forget(req: NetworkRequest) -> dict[str, Any]:
        try:
            await device.forget(req.ssid)
        except HostError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
        return {"host": await device.status()}

    @admin_api.post("/device/network/prefer")
    async def device_prefer(req: PreferRequest) -> dict[str, Any]:
        try:
            await device.prefer(req.link)
        except HostError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
        return {"host": await device.status()}

    @admin_api.post("/device/access-point")
    async def device_access_point(req: AccessPointRequest) -> dict[str, Any]:
        try:
            await device.access_point(req.on)
        except HostError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
        return {"host": await device.status()}

    @admin_api.post("/device/reboot")
    async def device_reboot() -> dict[str, Any]:
        try:
            result = await device.reboot()
        except HostError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
        log.warning("reboot asked for from the admin page")
        return {"accepted": True, **result}

    @admin_api.post("/device/shutdown")
    async def device_shutdown() -> dict[str, Any]:
        try:
            result = await device.shutdown()
        except HostError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
        log.warning("shutdown asked for from the admin page")
        return {"accepted": True, **result}

    @admin_api.post("/device/reset")
    async def device_reset(req: ResetRequest) -> dict[str, Any]:
        """Forget what was set up here, and come back as if newly installed.

        The agent restarts itself rather than trying to unpick a running wall: the container
        is restarted by Docker (restart: unless-stopped), and comes up reading whatever is
        left. Without that supervision the agent simply stops, which is also honest.
        """
        if not rt.store.writable:
            raise HTTPException(status.HTTP_409_CONFLICT, "there is nowhere to keep settings, "
                                                          "so there is nothing to reset")
        forgotten = []
        rt.store.save({})
        forgotten.append("settings")
        if req.scope == "device":
            if rt.secrets.writable:
                rt.secrets.clear()
                forgotten.append("the admin password and every saved credential")
            if req.forget_network and device.available:
                try:
                    status_now = await device.status()
                    if status_now.get("ssid"):
                        await device.forget(status_now["ssid"])
                        forgotten.append("the wifi network")
                except HostError as exc:
                    log.warning("could not forget the network: %s", exc)
        log.warning("reset asked for from the admin page: %s", ", ".join(forgotten))
        asyncio.get_running_loop().call_later(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))
        return {"forgotten": forgotten, "restarting": True}

    @admin_api.post("/config/setup-done")
    async def setup_done() -> dict[str, Any]:
        if rt.store.writable:
            rt.store.mark_configured()
        return config_payload()

    @admin_api.put("/config/cameras")
    async def put_camera_order(req: CameraOrderRequest) -> dict[str, Any]:
        """Which cameras matter most, and how each is shown. Unknown ids are kept: a camera
        may be offline now."""
        if req.settings is not None and any(len(camera_id) > 64 for camera_id in req.settings):
            raise HTTPException(422, "camera ids are at most 64 characters")
        try:
            rt.apply_camera_order(req.order)
            if req.settings is not None:
                rt.apply_camera_settings(req.settings)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, safe_error(exc)) from None
        return config_payload()

    @admin_api.put("/config/layouts")
    async def put_layouts(req: LayoutsRequest) -> dict[str, Any]:
        """Replace the user-defined layouts. Built-in names cannot be taken over."""
        clashes = sorted({layout.id for layout in req.layouts} & set(rt.wall.layouts.all)
                         - {layout.id for layout in config.layouts})
        if clashes:
            raise HTTPException(422, f"these names are built in: {', '.join(clashes)}")
        in_use = ({v.layout for v in config.views} | {config.detection.promote_layout}) - {SAME_LAYOUT}
        removed = sorted((({layout.id for layout in config.layouts}) - {l.id for l in req.layouts}) & in_use)
        if removed:
            raise HTTPException(409, f"still used by a view or by detection: {', '.join(removed)}")
        try:
            rt.apply_layouts(req.layouts)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, safe_error(exc)) from None
        return config_payload()

    @admin_api.put("/config/display")
    async def put_display(req: DisplayConfig) -> dict[str, Any]:
        try:
            rt.apply_display(req)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, safe_error(exc)) from None
        return config_payload()

    @admin_api.put("/config/device")
    async def put_device(req: DeviceConfig) -> dict[str, Any]:
        try:
            rt.apply_device(req)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, safe_error(exc)) from None
        return config_payload()

    @admin_api.post("/config/sources")
    async def add_source(req: NewSourceRequest) -> dict[str, Any]:
        # Addresses are saved with the settings, which must never hold a password: the
        # username and password have fields of their own, kept in the secrets file.
        addresses = [url for camera in req.cameras_urls for url in (camera.main, camera.sub) if url]
        if any(urlsplit(url).username or urlsplit(url).password for url in addresses if "@" in url):
            raise HTTPException(422, "put the username and password in their own fields, not in "
                                     "the stream address: they are kept apart from the settings")
        try:
            source = SourceConfig(**req.model_dump(exclude={"password", "username"}),
                                  username=req.username, password=req.password)
            await rt.add_source(source, password=req.password or None,
                                username=req.username or None)
        except ValueError as exc:
            raise HTTPException(422, safe_error(exc)) from None
        except OSError as exc:
            raise HTTPException(400, safe_error(exc)) from None
        return config_payload()

    @admin_api.delete("/config/sources/{source_id}")
    async def remove_source(source_id: str) -> dict[str, Any]:
        if source_id in rt.declared_sources:
            raise HTTPException(409, f"{source_id} comes from viewport.yaml; remove it there")
        try:
            await rt.remove_source(source_id)
        except KeyError:
            raise HTTPException(404, f"unknown source {source_id}") from None
        return config_payload()

    @admin_api.put("/config/sources/{source_id}")
    async def put_source(source_id: str, req: SourceEditRequest) -> dict[str, Any]:
        edits = req.model_dump(exclude_none=True)
        credentials = {k: edits.pop(k) for k in ("username", "password") if k in edits}
        if not edits and not credentials:
            raise HTTPException(422, "no changes given")
        try:
            await rt.update_source(source_id, edits, username=credentials.get("username"),
                                   password=credentials.get("password"))
        except KeyError:
            raise HTTPException(404, f"unknown source {source_id}") from None
        except (ValueError, OSError) as exc:
            raise HTTPException(400, safe_error(exc)) from None
        return config_payload()

    @app.get("/admin", include_in_schema=False)
    async def admin_page() -> FileResponse:
        return FileResponse(WEB_DIR / "admin.html", headers={"Cache-Control": "no-store"})

    @app.websocket("/api/media/ws")
    async def media_socket(ws: WebSocket) -> None:
        """The player's video, relayed from go2rtc. See media.py for why."""
        stream = ws.query_params.get("src", "")
        await ws.accept()
        if not token_matches(token, bearer_token(ws.headers, ws.query_params)):
            await ws.close(code=1008, reason="invalid or missing API token")
            return
        if not media.allowed_stream(stream, rt.wall.active_streams(), config.go2rtc.mjpeg_fallback):
            # A renderer may only play what the wall assigned: the budget holds server-side.
            await ws.close(code=1008, reason="stream is not on the wall")
            return
        await media.relay(ws, media.upstream_url(config.go2rtc.api_url, stream))

    @app.websocket("/api/wall/ws")
    async def wall_socket(ws: WebSocket) -> None:
        if not token_matches(token, bearer_token(ws.headers, ws.query_params)):
            # 1008 = policy violation. Closing before accept() would be a bare HTTP 403,
            # which the browser reports only as "connection failed".
            await ws.accept()
            await ws.close(code=1008, reason="invalid or missing API token")
            return
        await ws.accept()
        # The id is the client's own choice and ends up in /api/health: keep it short and plain.
        requested = ws.query_params.get("renderer") or ""
        renderer_id = (requested if RENDERER_ID.fullmatch(requested)
                       else f"renderer-{uuid.uuid4().hex[:6]}")
        queue = rt.wall.subscribe()

        async def pump() -> None:
            while True:
                await ws.send_json(await queue.get())

        sender = asyncio.create_task(pump())
        try:
            while True:
                text = await ws.receive_text()
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                handle_renderer_message(rt, renderer_id, msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            rt.wall.unsubscribe(queue)

    app.include_router(api)
    app.include_router(admin_api)
    return app


def _layout_name(config: AppConfig, layout_id: str) -> str:
    return next((layout.name for layout in config.layouts
                 if layout.id == layout_id and layout.name), layout_id)


def handle_renderer_message(rt: Runtime, renderer_id: str, msg: Any) -> None:
    if not isinstance(msg, dict):
        return
    kind = msg.get("type")
    try:
        if kind == "fullscreen":
            tile = msg.get("tile")
            if tile is None:
                rt.wall.set_fullscreen(None)
            else:
                rt.wall.toggle_fullscreen(str(tile))
        elif kind == "focus" and msg.get("action") == "dismiss":
            rt.wall.dismiss_focus()
        elif kind == "view":
            if "index" in msg:
                rt.wall.set_view(int(msg["index"]))
            else:
                rt.wall.step_view(int(msg.get("step", 1)))
        elif kind == "stats" and isinstance(msg.get("tiles"), list):
            rt.record_renderer_stats(renderer_id, {"tiles": msg["tiles"][:64],
                                                   "user_agent": str(msg.get("user_agent", ""))[:200]})
    except (KeyError, ValueError, TypeError) as exc:
        log.debug("ignored renderer message %r: %s", msg, exc)

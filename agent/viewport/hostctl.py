"""Talking to the device the agent runs on.

The agent is a container with a read-only filesystem, no Linux capabilities and no root.
Joining a wifi network, rebooting or powering the device down all need root on the *host*, so
none of it can happen in here. Instead a small service on the host (`deploy/pi/hostd.py`)
listens on a Unix socket that is mounted into this container and accepts a fixed vocabulary of
requests, each with validated arguments. This module is the client for that socket.

That division is the point: a stolen admin password reaches this vocabulary and nothing else.
There is no shell on the other side, no free-form command, and no path by which "join this
network" becomes "run this".

Three backends, so the same code runs everywhere:

* `helper` — the socket is there, and this is a real appliance.
* `fake`   — VIEWPORT_HOST_FAKE=1: an in-memory device for development and the tests. It
             reports plainly that it is simulated, and the admin page says so, because a
             "Reboot" button that silently does nothing would be worse than no button.
* `none`   — neither: the device section shows what it can and refuses every action.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

SOCKET_ENV = "VIEWPORT_HOST_SOCKET"
DEFAULT_SOCKET = "/run/openviewport/host.sock"
TIMEOUT_SECONDS = 15.0
# A wifi passphrase is 8–63 characters (WPA2) or a 64-character hex PSK; an SSID is 1–32 bytes.
SSID_MAX_BYTES = 32
PSK_RANGE = (8, 63)


class HostError(RuntimeError):
    """The host could not do it, with a message meant for the admin page."""


@dataclass(frozen=True)
class Network:
    """One wifi network the device can see."""

    ssid: str
    signal: int = 0               # 0-100
    security: str = ""            # "", "WPA2", "WPA3", ...
    saved: bool = False
    active: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"ssid": self.ssid, "signal": self.signal, "security": self.security,
                "saved": self.saved, "active": self.active}


def check_ssid(ssid: str) -> str:
    ssid = ssid.strip()
    if not ssid or len(ssid.encode()) > SSID_MAX_BYTES:
        raise HostError("that is not a usable network name")
    if any(ord(c) < 32 for c in ssid):
        raise HostError("that is not a usable network name")
    return ssid


def check_psk(psk: str) -> str:
    """An open network takes no passphrase, so blank is allowed; anything else must be valid."""
    if psk == "":
        return psk
    if re.fullmatch(r"[0-9a-fA-F]{64}", psk):
        return psk
    if not PSK_RANGE[0] <= len(psk) <= PSK_RANGE[1]:
        raise HostError(f"a wifi password is {PSK_RANGE[0]} to {PSK_RANGE[1]} characters")
    if any(ord(c) < 32 for c in psk):
        raise HostError("that password has characters wifi cannot carry")
    return psk


class HostControl:
    """Whatever the device can do about itself, or an honest account of why it cannot."""

    def __init__(self, socket_path: str | None = None, fake: bool | None = None):
        self._socket = str(socket_path if socket_path is not None
                           else os.environ.get(SOCKET_ENV, DEFAULT_SOCKET))
        if fake is None:
            fake = os.environ.get("VIEWPORT_HOST_FAKE", "").lower() in ("1", "true", "yes")
        self._fake = FakeHost() if fake else None

    @property
    def kind(self) -> str:
        if self._fake is not None:
            return "fake"
        return "helper" if os.path.exists(self._socket) else "none"

    @property
    def available(self) -> bool:
        return self.kind != "none"

    async def status(self) -> dict[str, Any]:
        """What the device knows about itself. Never raises: this drives a read-only panel."""
        if not self.available:
            return {"kind": "none"}
        try:
            return {"kind": self.kind, **await self._call("status")}
        except HostError as exc:
            return {"kind": self.kind, "error": str(exc)}

    async def networks(self) -> list[dict[str, Any]]:
        result = await self._call("networks")
        seen = result.get("networks", [])
        return [Network(**{k: n[k] for k in ("ssid", "signal", "security", "saved", "active")
                           if k in n}).as_dict() for n in seen]

    async def join(self, ssid: str, psk: str) -> dict[str, Any]:
        return await self._call("join", ssid=check_ssid(ssid), psk=check_psk(psk))

    async def forget(self, ssid: str) -> dict[str, Any]:
        return await self._call("forget", ssid=check_ssid(ssid))

    async def access_point(self, on: bool) -> dict[str, Any]:
        return await self._call("access-point", on=bool(on))

    async def prefer(self, link: str) -> dict[str, Any]:
        if link not in ("ethernet", "wifi"):
            raise HostError("a connection is either ethernet or wifi")
        return await self._call("prefer", link=link)

    async def reboot(self) -> dict[str, Any]:
        return await self._call("reboot")

    async def shutdown(self) -> dict[str, Any]:
        return await self._call("shutdown")

    # ----- the socket ---------------------------------------------------------

    async def _call(self, op: str, **args: Any) -> dict[str, Any]:
        if self._fake is not None:
            return self._fake.call(op, **args)
        if not self.available:
            raise HostError("this installation has no host helper, so it cannot do that "
                            "(see docs/raspberry-pi.md)")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self._socket), TIMEOUT_SECONDS)
        except (OSError, asyncio.TimeoutError):
            raise HostError("the host helper is not answering") from None
        try:
            writer.write(json.dumps({"op": op, **args}).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), TIMEOUT_SECONDS)
        except (OSError, asyncio.TimeoutError):
            raise HostError("the host helper stopped answering") from None
        finally:
            writer.close()
        try:
            answer = json.loads(line or b"{}")
        except ValueError:
            raise HostError("the host helper answered with nonsense") from None
        if not answer.get("ok"):
            raise HostError(str(answer.get("error") or "the host refused"))
        result = answer.get("result") or {}
        return result if isinstance(result, dict) else {"result": result}


@dataclass
class FakeHost:
    """A device that exists only in memory, for development and the tests.

    It behaves like the real one — joining a network takes it off the access point, a wrong
    password fails — so the admin page and the browser tests exercise the same paths.
    """

    link: str = "ethernet"
    ssid: str = ""
    address: str = "192.0.2.10"
    access_point: bool = False
    saved: set[str] = field(default_factory=set)
    actions: list[str] = field(default_factory=list)
    seen: list[Network] = field(default_factory=lambda: [
        Network("Kitchen", 82, "WPA2"), Network("Kitchen 5G", 74, "WPA3"),
        Network("Neighbour", 31, "WPA2"), Network("Guests", 55, ""),
    ])
    passwords: dict[str, str] = field(default_factory=lambda: {"Kitchen": "a good password"})

    def call(self, op: str, **args: Any) -> dict[str, Any]:
        self.actions.append(op)
        if op == "status":
            return {"link": "access-point" if self.access_point else self.link,
                    "ssid": self.ssid, "addresses": [self.address], "hostname": "openviewport",
                    "access_point": self.access_point, "simulated": True,
                    "uptime_seconds": 4242, "temperature_c": 47.5, "throttled": False,
                    "disk_free_bytes": 12 * 1024**3, "disk_total_bytes": 32 * 1024**3}
        if op == "networks":
            return {"networks": [{**n.as_dict(),
                                  "saved": n.ssid in self.saved,
                                  "active": n.ssid == self.ssid} for n in self.seen]}
        if op == "join":
            ssid, psk = args["ssid"], args["psk"]
            known = next((n for n in self.seen if n.ssid == ssid), None)
            if known is not None and known.security and psk != self.passwords.get(ssid, psk):
                raise HostError("that password was not accepted by the network")
            self.link, self.ssid, self.access_point = "wifi", ssid, False
            self.saved.add(ssid)
            self.address = "192.0.2.20"
            return {"link": "wifi", "ssid": ssid, "addresses": [self.address]}
        if op == "forget":
            self.saved.discard(args["ssid"])
            if self.ssid == args["ssid"]:
                self.link, self.ssid = "none", ""
            return {}
        if op == "access-point":
            self.access_point = bool(args["on"])
            if self.access_point:
                self.link, self.ssid = "access-point", ""
            return {"access_point": self.access_point}
        if op == "prefer":
            self.link = args["link"]
            return {"link": self.link}
        if op in ("reboot", "shutdown"):
            return {"accepted": True, "simulated": True}
        raise HostError(f"the host helper does not know how to {op}")

#!/usr/bin/env python3
"""The part that needs root, kept as small as it can be.

The agent runs in a container with a read-only filesystem, no Linux capabilities and no root.
Joining a wifi network, rebooting and powering down need all three, so they happen here
instead: one service, started by systemd, listening on a Unix socket that is mounted into the
container. `agent/viewport/hostctl.py` is the client.

What makes this safe to run as root is what it will not do:

* it answers only the operations below, and nothing else;
* every argument is checked here again, whatever the caller claimed to have checked;
* nothing is ever passed to a shell — every command is an argument list;
* there is no operation that runs a command, a script or a path of the caller's choosing.

So the worst an attacker who reaches the socket can do is what someone standing at the device
could do anyway: move it to another network, restart it, or switch it off.

Standard library only: Raspberry Pi OS has python3, and this must not need pip.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

SOCKET_PATH = os.environ.get("VIEWPORT_HOST_SOCKET", "/run/openviewport/host.sock")
# The agent container's user, which must be able to open the socket. Nothing else needs to.
SOCKET_GID = int(os.environ.get("VIEWPORT_HOST_GID", "10001"))
STATE_DIR = Path(os.environ.get("VIEWPORT_HOST_STATE", "/var/lib/openviewport"))
AP_FILE = STATE_DIR / "access-point.json"
AP_SSID_PREFIX = "OpenViewport"
COMMAND_TIMEOUT = 60.0
SSID_MAX_BYTES = 32
PSK_RANGE = (8, 63)

log = logging.getLogger("openviewport.hostd")


class Refused(Exception):
    """Not going to do that, with a reason fit to show someone."""


def check_ssid(ssid: Any) -> str:
    if not isinstance(ssid, str):
        raise Refused("that is not a network name")
    ssid = ssid.strip()
    if not ssid or len(ssid.encode()) > SSID_MAX_BYTES or any(ord(c) < 32 for c in ssid):
        raise Refused("that is not a usable network name")
    return ssid


def check_psk(psk: Any) -> str:
    if not isinstance(psk, str):
        raise Refused("that is not a password")
    if psk == "" or re.fullmatch(r"[0-9a-fA-F]{64}", psk):
        return psk
    if not PSK_RANGE[0] <= len(psk) <= PSK_RANGE[1] or any(ord(c) < 32 for c in psk):
        raise Refused(f"a wifi password is {PSK_RANGE[0]} to {PSK_RANGE[1]} characters")
    return psk


def run(argv: list[str], timeout: float = COMMAND_TIMEOUT,
        feed: str | None = None) -> tuple[int, str, str]:
    """One command, as a list, never through a shell. `feed` goes to its standard input."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                              input=feed)
    except FileNotFoundError:
        raise Refused(f"{argv[0]} is not installed on this device") from None
    except subprocess.TimeoutExpired:
        raise Refused(f"{argv[0]} took too long") from None
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def terse(line: str) -> list[str]:
    r"""Split one line of `nmcli -t` output, where a colon in a value is written \:."""
    fields, current, escaped = [], "", False
    for char in line:
        if escaped:
            current += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append(current)
            current = ""
        else:
            current += char
    fields.append(current)
    return fields


class Device:
    """The device, as far as nmcli and /proc are concerned."""

    def __init__(self, runner: Callable[..., tuple[int, str, str]] = run,
                 state_dir: Path = STATE_DIR):
        self.run = runner
        self.state_dir = state_dir

    # ----- reading ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        link, ssid, access_point = "none", "", False
        code, out, _ = self.run(["nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device", "status"])
        if code == 0:
            for line in out.splitlines():
                kind, state, connection = (terse(line) + ["", "", ""])[:3]
                if state != "connected":
                    continue
                if kind == "ethernet" and link != "wifi":
                    link, ssid = "ethernet", ""
                elif kind == "wifi":
                    link, ssid = "wifi", connection
        details: dict[str, str] = {}
        if self.access_point_is_up():
            link, ssid, access_point = "access-point", "", True
            # Its name and password again, so a device that was already showing its own
            # network when this service started can still put them on the screen.
            found = self.access_point_details(create=False)
            if found:
                details = {"access_point_ssid": found["ssid"],
                           "access_point_password": found["password"]}
        return {"link": link, "ssid": ssid, "access_point": access_point,
                "addresses": self.addresses(), "hostname": socket.gethostname(),
                # Whether a screen can be turned off at all, so the settings page can say.
                "can_control_display": bool(shutil.which("cec-client") or shutil.which("vcgencmd")),
                **details, **self.vitals()}

    def addresses(self) -> list[str]:
        code, out, _ = self.run(["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show"])
        if code != 0:
            return []
        found = []
        for line in out.splitlines():
            value = terse(line)[-1].strip()
            if "/" in value:
                address = value.split("/")[0]
                if address and not address.startswith("127."):
                    found.append(address)
        return found

    def vitals(self) -> dict[str, Any]:
        """Uptime, temperature, whether the power supply is coping, and disk space."""
        vitals: dict[str, Any] = {}
        try:
            vitals["uptime_seconds"] = float(Path("/proc/uptime").read_text().split()[0])
        except (OSError, ValueError):
            pass
        try:
            millidegrees = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
            vitals["temperature_c"] = round(int(millidegrees) / 1000, 1)
        except (OSError, ValueError):
            pass
        if shutil.which("vcgencmd"):
            code, out, _ = self.run(["vcgencmd", "get_throttled"], timeout=5)
            if code == 0 and "=" in out:
                try:
                    # Any bit set means the board has been undervolted or throttled at some
                    # point, which is the usual reason a Pi "randomly" misbehaves.
                    vitals["throttled"] = int(out.split("=", 1)[1], 0) != 0
                except ValueError:
                    pass
        try:
            usage = shutil.disk_usage("/")
            vitals["disk_free_bytes"], vitals["disk_total_bytes"] = usage.free, usage.total
        except OSError:
            pass
        return vitals

    def networks(self) -> list[dict[str, Any]]:
        saved = self.saved_connections()
        code, out, err = self.run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
                                   "device", "wifi", "list", "--rescan", "auto"])
        if code != 0:
            raise Refused(err or "this device cannot see wifi networks")
        seen: dict[str, dict[str, Any]] = {}
        for line in out.splitlines():
            in_use, ssid, signal, security = (terse(line) + ["", "", "", ""])[:4]
            # A hidden network comes back with a blank or whitespace name: there is nothing
            # for anyone to pick, so it is left out rather than listed as an empty row.
            if not ssid.strip() or ssid in seen:
                continue
            seen[ssid] = {"ssid": ssid, "signal": int(signal) if signal.isdigit() else 0,
                          "security": "" if security in ("", "--") else security,
                          "saved": ssid in saved, "active": in_use.strip() == "*"}
        return sorted(seen.values(), key=lambda n: n["signal"], reverse=True)

    def saved_connections(self) -> set[str]:
        code, out, _ = self.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
        if code != 0:
            return set()
        return {terse(line)[0] for line in out.splitlines()
                if len(terse(line)) > 1 and terse(line)[1].endswith("wireless")}

    # ----- changing -----------------------------------------------------------

    def join(self, ssid: str, psk: str) -> dict[str, Any]:
        argv = ["nmcli", "device", "wifi", "connect", ssid]
        if psk:
            argv += ["password", psk]
        code, _, err = self.run(argv)
        if code != 0:
            lowered = err.lower()
            if "secrets" in lowered or "password" in lowered or "not provided" in lowered:
                raise Refused("that password was not accepted by the network")
            raise Refused(err.splitlines()[-1] if err else f"could not join {ssid}")
        self.access_point_down()
        return self.status()

    def forget(self, ssid: str) -> dict[str, Any]:
        code, _, err = self.run(["nmcli", "connection", "delete", "id", ssid])
        if code != 0:
            raise Refused(err.splitlines()[-1] if err else f"{ssid} was not saved here")
        return self.status()

    def prefer(self, link: str) -> dict[str, Any]:
        if link not in ("ethernet", "wifi"):
            raise Refused("a connection is either ethernet or wifi")
        wanted = "802-3-ethernet" if link == "ethernet" else "802-11-wireless"
        code, out, _ = self.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
        if code != 0:
            raise Refused("this device has no connections to choose between")
        changed = False
        for line in out.splitlines():
            fields = terse(line)
            if len(fields) < 2:
                continue
            name, kind = fields[0], fields[1]
            priority = "100" if kind == wanted else "0"
            self.run(["nmcli", "connection", "modify", name,
                      "connection.autoconnect-priority", priority])
            if kind == wanted:
                self.run(["nmcli", "connection", "up", name])
                changed = True
        if not changed:
            raise Refused(f"this device has no {link} connection set up")
        return self.status()

    # ----- its own network ----------------------------------------------------

    def access_point_details(self, create: bool = True) -> dict[str, str]:
        """The name and password of this device's own network, made once and kept.

        Reading the device's status must not invent one: only putting the access point up
        does that, which is why `create` exists.
        """
        try:
            saved = json.loads(AP_FILE.read_text()) if AP_FILE.exists() else {}
        except (OSError, ValueError):
            saved = {}
        if not saved.get("ssid") or not saved.get("password"):
            if not create:
                return {}
            suffix = socket.gethostname().split(".")[0][-4:] or secrets.token_hex(2)
            saved = {"ssid": f"{AP_SSID_PREFIX}-{suffix}",
                     "password": secrets.token_urlsafe(12)}
            self.state_dir.mkdir(parents=True, exist_ok=True)
            AP_FILE.write_text(json.dumps(saved, indent=2) + "\n")
            os.chmod(AP_FILE, 0o600)
        return saved

    def access_point_is_up(self) -> bool:
        code, out, _ = self.run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show",
                                 "--active"])
        if code != 0:
            return False
        return any(terse(line)[0] == "Hotspot" for line in out.splitlines() if line)

    def access_point_down(self) -> None:
        if self.access_point_is_up():
            self.run(["nmcli", "connection", "down", "Hotspot"])

    def access_point(self, on: bool) -> dict[str, Any]:
        if not on:
            self.access_point_down()
            return self.status()
        details = self.access_point_details()
        code, _, err = self.run(["nmcli", "device", "wifi", "hotspot", "con-name", "Hotspot",
                                 "ssid", details["ssid"], "password", details["password"]])
        if code != 0:
            raise Refused(err.splitlines()[-1] if err else "this device cannot make its own network")
        return {**self.status(), "access_point_ssid": details["ssid"],
                "access_point_password": details["password"]}

    # ----- the screen ---------------------------------------------------------

    def display(self, on: bool) -> dict[str, Any]:
        """Turn the television on or off.

        HDMI-CEC asks the television itself, which is what people mean by "off": the panel is
        dark and it draws almost nothing. Where there is no CEC, the Pi can stop driving the
        output instead, which blanks the picture but leaves the set awake.
        """
        if shutil.which("cec-client"):
            command = "on 0" if on else "standby 0"
            code, _, err = self.run(["cec-client", "-s", "-d", "1"], timeout=20, feed=command)
            if code == 0:
                return {"display": "on" if on else "off", "how": "cec"}
            log.warning("cec-client refused (%s); falling back to the output itself", err[:120])
        if shutil.which("vcgencmd"):
            code, _, err = self.run(["vcgencmd", "display_power", "1" if on else "0"], timeout=10)
            if code == 0:
                return {"display": "on" if on else "off", "how": "output"}
            raise Refused(err or "this device could not change its output")
        raise Refused("this device has no way to turn a screen off "
                      "(install cec-utils for HDMI-CEC)")

    # ----- power --------------------------------------------------------------

    def reboot(self) -> dict[str, Any]:
        self.run(["systemctl", "reboot"], timeout=10)
        return {"accepted": True}

    def shutdown(self) -> dict[str, Any]:
        self.run(["systemctl", "poweroff"], timeout=10)
        return {"accepted": True}


def handle(device: Device, request: dict[str, Any]) -> dict[str, Any]:
    """One request to one answer. Anything not named here is refused."""
    op = request.get("op")
    if op == "status":
        return device.status()
    if op == "networks":
        return {"networks": device.networks()}
    if op == "join":
        return device.join(check_ssid(request.get("ssid")), check_psk(request.get("psk", "")))
    if op == "forget":
        return device.forget(check_ssid(request.get("ssid")))
    if op == "prefer":
        link = request.get("link")
        return device.prefer(link if isinstance(link, str) else "")
    if op == "access-point":
        return device.access_point(bool(request.get("on")))
    if op == "display":
        return device.display(bool(request.get("on")))
    if op == "reboot":
        return device.reboot()
    if op == "shutdown":
        return device.shutdown()
    raise Refused(f"this device does not know how to {op!r}")


class Handler(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self) -> None:
        raw = self.rfile.readline(64 * 1024)
        try:
            request = json.loads(raw or b"{}")
            if not isinstance(request, dict):
                raise ValueError("not an object")
        except ValueError:
            self.answer({"ok": False, "error": "that was not a request"})
            return
        op = request.get("op")
        try:
            result = handle(self.server.device, request)          # type: ignore[attr-defined]
        except Refused as exc:
            log.warning("refused %s: %s", op, exc)
            self.answer({"ok": False, "error": str(exc)})
            return
        except Exception:                                          # pragma: no cover - last resort
            log.exception("failed %s", op)
            self.answer({"ok": False, "error": "the device could not do that"})
            return
        if op not in ("status", "networks"):
            log.warning("did %s", op)                              # the ones worth a record
        self.answer({"ok": True, "result": result})

    def answer(self, payload: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(payload).encode() + b"\n")


class Server(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, path: str, device: Device):
        if os.path.exists(path):
            os.unlink(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(path, Handler)
        self.device = device
        # The agent's user may talk to it; nobody else on the device may.
        try:
            os.chown(path, 0, SOCKET_GID)
        except (OSError, PermissionError):
            log.warning("could not give the socket to group %s", SOCKET_GID)
        os.chmod(path, 0o660)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not shutil.which("nmcli"):
        log.error("NetworkManager (nmcli) is needed and is not installed")
        return 1
    server = Server(SOCKET_PATH, Device())
    log.info("listening on %s", SOCKET_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())

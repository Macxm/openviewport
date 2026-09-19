"""The host helper: what it accepts, what it refuses, and what it makes of nmcli's output.

nmcli itself is stood in for, so these run anywhere. What the real one does on a Raspberry Pi
is not something a test on a Mac can tell us; what this service does with its answers is.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("hostd", ROOT / "deploy" / "pi" / "hostd.py")
hostd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hostd)


class FakeNmcli:
    """Answers argument lists with canned output, and remembers what it was asked."""

    def __init__(self, answers: dict[tuple[str, ...], tuple[int, str, str]] | None = None):
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        for prefix, answer in self.answers.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return answer
        return (0, "", "")


DEVICE_STATUS = (0, "wifi:connected:Kitchen\nethernet:unavailable:\nloopback:connected:lo", "")
WIFI_LIST = (0, "*:Kitchen:82:WPA2\n :Neighbour:31:WPA2\n :Guests:55:--\n : :0:", "")
CONNECTIONS = (0, "Kitchen:802-11-wireless\nWired connection 1:802-3-ethernet", "")


def device(answers=None, tmp_path=None):
    fake = FakeNmcli(answers)
    return hostd.Device(runner=fake, state_dir=tmp_path or Path("/tmp")), fake


# ----- what it will not do ---------------------------------------------------------

def test_an_operation_it_does_not_have_is_refused():
    dev, fake = device()
    with pytest.raises(hostd.Refused, match="does not know how to"):
        hostd.handle(dev, {"op": "run", "command": "rm -rf /"})
    assert fake.calls == [], "and nothing was run"


@pytest.mark.parametrize("ssid", ["", "   ", "x" * 33, "a\x00b", 42, None])
def test_a_network_name_it_could_not_be_is_refused_here_too(ssid):
    """The client checks as well; this is the side that matters, since it runs as root."""
    dev, fake = device()
    with pytest.raises(hostd.Refused):
        hostd.handle(dev, {"op": "join", "ssid": ssid, "psk": "12345678"})
    assert fake.calls == []


@pytest.mark.parametrize("psk", ["short", "x" * 64, 12345678, "with\nnewline"])
def test_a_password_it_could_not_be_is_refused(psk):
    dev, fake = device()
    with pytest.raises(hostd.Refused):
        hostd.handle(dev, {"op": "join", "ssid": "Kitchen", "psk": psk})
    assert fake.calls == []


def test_nothing_is_ever_handed_to_a_shell():
    """Every call is an argument list, so a name full of shell characters stays a name."""
    dev, fake = device()
    awkward = "Kitchen; rm -rf /"
    hostd.handle(dev, {"op": "join", "ssid": awkward, "psk": "a good password"})
    joined = next(c for c in fake.calls if c[:4] == ["nmcli", "device", "wifi", "connect"])
    assert joined[4] == awkward, "passed whole, as one argument"
    assert all(isinstance(part, str) for part in joined)


# ----- reading nmcli ---------------------------------------------------------------

def test_what_it_makes_of_the_device_list():
    dev, _ = device({("nmcli", "-t", "-f", "TYPE,STATE,CONNECTION"): DEVICE_STATUS,
                     ("nmcli", "-t", "-f", "IP4.ADDRESS"): (0, "IP4.ADDRESS[1]:192.168.1.50/24", ""),
                     ("nmcli", "-t", "-f", "NAME,TYPE,DEVICE"): (0, "", "")})
    status = dev.status()
    assert (status["link"], status["ssid"]) == ("wifi", "Kitchen")
    assert status["addresses"] == ["192.168.1.50"]
    assert status["access_point"] is False


def test_the_loopback_address_is_not_offered_as_the_way_in():
    dev, _ = device({("nmcli", "-t", "-f", "IP4.ADDRESS"):
                     (0, "IP4.ADDRESS[1]:127.0.0.1/8\nIP4.ADDRESS[1]:192.168.1.50/24", "")})
    assert dev.addresses() == ["192.168.1.50"]


def test_networks_come_back_strongest_first_and_without_the_blank_one():
    dev, _ = device({("nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY"): WIFI_LIST,
                     ("nmcli", "-t", "-f", "NAME,TYPE"): CONNECTIONS})
    found = dev.networks()
    assert [n["ssid"] for n in found] == ["Kitchen", "Guests", "Neighbour"]
    assert found[0]["active"] and found[0]["saved"] and found[0]["security"] == "WPA2"
    assert found[1]["security"] == "", "-- means no security, not a name"


def test_a_colon_in_a_network_name_survives_nmcli_s_escaping():
    dev, _ = device({("nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY"):
                     (0, r" :Bill\:s wifi:70:WPA2", ""),
                     ("nmcli", "-t", "-f", "NAME,TYPE"): (0, "", "")})
    assert dev.networks()[0]["ssid"] == "Bill:s wifi"


def test_a_wifi_scan_that_fails_says_so_rather_than_returning_nothing():
    dev, _ = device({("nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY"):
                     (1, "", "Error: Device 'wlan0' not found.")})
    with pytest.raises(hostd.Refused, match="wlan0"):
        dev.networks()


# ----- joining ---------------------------------------------------------------------

def test_a_refused_password_is_reported_as_one():
    dev, _ = device({("nmcli", "device", "wifi", "connect"):
                     (4, "", "Error: Secrets were required, but not provided.")})
    with pytest.raises(hostd.Refused, match="not accepted by the network"):
        dev.join("Kitchen", "the wrong one")


def test_an_open_network_is_joined_without_a_password():
    dev, fake = device()
    dev.join("Guests", "")
    joined = next(c for c in fake.calls if c[:4] == ["nmcli", "device", "wifi", "connect"])
    assert "password" not in joined


def test_joining_takes_the_device_off_its_own_network():
    dev, fake = device({("nmcli", "-t", "-f", "NAME,TYPE,DEVICE"): (0, "Hotspot:wifi:wlan0", "")})
    dev.join("Kitchen", "a good password")
    assert ["nmcli", "connection", "down", "Hotspot"] in fake.calls


# ----- its own network --------------------------------------------------------------

def test_the_access_point_password_is_made_once_and_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(hostd, "AP_FILE", tmp_path / "access-point.json")
    dev, _ = device(tmp_path=tmp_path)
    first = dev.access_point_details()
    assert first["ssid"].startswith("OpenViewport-") and len(first["password"]) >= 12
    assert dev.access_point_details() == first, "the same one next time, or nobody could join"
    assert oct((tmp_path / "access-point.json").stat().st_mode)[-3:] == "600"


def test_turning_the_access_point_on_returns_what_to_put_on_the_screen(tmp_path, monkeypatch):
    monkeypatch.setattr(hostd, "AP_FILE", tmp_path / "access-point.json")
    dev, fake = device(tmp_path=tmp_path)
    answer = dev.access_point(True)
    assert answer["access_point_ssid"] and answer["access_point_password"]
    hotspot = next(c for c in fake.calls if c[:4] == ["nmcli", "device", "wifi", "hotspot"])
    assert answer["access_point_password"] in hotspot


# ----- the socket conversation ------------------------------------------------------

def test_an_answer_is_one_line_of_json():
    dev, _ = device({("nmcli", "-t", "-f", "TYPE,STATE,CONNECTION"): DEVICE_STATUS})
    payload = json.dumps({"ok": True, "result": hostd.handle(dev, {"op": "status"})})
    assert "\n" not in payload
    assert json.loads(payload)["result"]["ssid"] == "Kitchen"


def test_power_is_asked_of_systemd_not_of_a_shell():
    dev, fake = device()
    assert hostd.handle(dev, {"op": "reboot"})["accepted"] is True
    assert hostd.handle(dev, {"op": "shutdown"})["accepted"] is True
    assert fake.calls == [["systemctl", "reboot"], ["systemctl", "poweroff"]]

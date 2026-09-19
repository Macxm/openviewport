"""The device section: what anyone allowed to look may see, and what needs the password."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_camera
from test_api import FakeAdapter
from test_go2rtc import FakeGo2rtc
from viewport.api import create_app
from viewport.config import AppConfig, AuthConfig, SourceConfig
from viewport.go2rtc import Go2rtcClient
from viewport.hostctl import HostControl
from viewport.runtime import Runtime
from viewport.secretstore import SecretStore
from viewport.store import ConfigStore

PASSWORD = "correct horse battery staple"
TOKEN = "wall-token-1234"
# Everything that changes the device. None of it may work on the token alone.
ACTIONS = [("get", "/api/device/networks", None),
           ("post", "/api/device/network/join", {"ssid": "Kitchen", "password": "a good password"}),
           ("post", "/api/device/network/forget", {"ssid": "Kitchen"}),
           ("post", "/api/device/network/prefer", {"link": "wifi"}),
           ("post", "/api/device/access-point", {"on": True}),
           ("post", "/api/device/reboot", {}),
           ("post", "/api/device/shutdown", {}),
           ("post", "/api/device/reset", {"scope": "configuration"})]


@pytest.fixture
def stack(tmp_path):
    config = AppConfig(auth=AuthConfig(token=TOKEN, admin_password=PASSWORD),
                       sources=[SourceConfig(id="nvr", host="nvr", username="viewer", password="p")])
    rt = Runtime(config, adapters=[FakeAdapter([make_camera(0)])],
                 store=ConfigStore(tmp_path / "state.json"),
                 secrets_store=SecretStore(tmp_path / "secrets.json"),
                 go2rtc=Go2rtcClient("http://g", transport=httpx.MockTransport(FakeGo2rtc().handler)))
    rt.wall.update_cameras("nvr", [make_camera(0)])
    host = HostControl(fake=True)
    client = TestClient(create_app(config, rt, start_background=False, host=host))
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client, rt, host


def unlock(client):
    assert client.post("/api/admin/login", json={"username": "admin", "password": PASSWORD}).status_code == 200


# ----- looking ---------------------------------------------------------------------

def test_the_panel_reads_without_the_admin_password(stack):
    """It is shown while the page is locked, like the rest of the settings."""
    client, _, _ = stack
    body = client.get("/api/device").json()
    assert body["name"] and body["version"]
    assert body["host"]["link"] == "ethernet"
    assert body["sources"][0]["id"] == "nvr"
    assert body["cameras"] == 1


def test_looking_still_needs_the_token(stack):
    client, _, _ = stack
    del client.headers["Authorization"]
    assert client.get("/api/device").status_code == 401


def test_a_device_with_no_helper_says_so_instead_of_failing(tmp_path):
    config = AppConfig(auth=AuthConfig(token=TOKEN, admin_password=PASSWORD))
    rt = Runtime(config, adapters=[], store=ConfigStore(tmp_path / "s.json"),
                 secrets_store=SecretStore(tmp_path / "secrets.json"),
                 go2rtc=Go2rtcClient("http://g", transport=httpx.MockTransport(FakeGo2rtc().handler)))
    client = TestClient(create_app(config, rt, start_background=False,
                                   host=HostControl(socket_path=str(tmp_path / "none.sock"), fake=False)))
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    assert client.get("/api/device").json()["host"] == {"kind": "none"}
    unlock(client)
    refused = client.post("/api/device/reboot")
    assert refused.status_code == 503 and "no host helper" in refused.json()["detail"]


# ----- doing -----------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", ACTIONS)
def test_every_action_needs_the_admin_password(stack, method, path, body):
    client, _, host = stack
    answer = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
    assert answer.status_code == 401, f"{path} acted on the token alone"
    assert host._fake.actions == [], "and nothing reached the device"


def test_joining_a_network_moves_the_device_onto_it(stack):
    client, _, _ = stack
    unlock(client)
    answer = client.post("/api/device/network/join",
                         json={"ssid": "Kitchen", "password": "a good password"})
    assert answer.status_code == 200
    assert client.get("/api/device").json()["host"]["ssid"] == "Kitchen"


def test_a_refused_password_comes_back_as_a_message_not_a_crash(stack):
    client, _, _ = stack
    unlock(client)
    answer = client.post("/api/device/network/join", json={"ssid": "Kitchen", "password": "wrong one"})
    assert answer.status_code == 400 and "not accepted" in answer.json()["detail"]


def test_a_network_name_that_could_not_exist_is_refused_before_the_device_sees_it(stack):
    client, _, host = stack
    unlock(client)
    answer = client.post("/api/device/network/join", json={"ssid": "x" * 40, "password": "12345678"})
    assert answer.status_code == 400
    assert "join" not in host._fake.actions


def test_the_wifi_password_is_never_read_back(stack):
    """It goes to the host and nowhere else: not into the settings file, not into a response."""
    client, rt, _ = stack
    unlock(client)
    client.post("/api/device/network/join", json={"ssid": "Kitchen", "password": "a good password"})
    saved = rt.store.path.read_text() if rt.store.path.exists() else ""
    assert "a good password" not in saved
    assert "a good password" not in client.get("/api/device").text


def test_resetting_the_configuration_keeps_the_password_and_the_credentials(stack):
    client, rt, _ = stack
    unlock(client)
    rt.store.mark_configured()
    rt.secrets.set_source("nvr", "viewer", "nvr-password")

    answer = client.post("/api/device/reset", json={"scope": "configuration"})
    assert answer.status_code == 200 and answer.json()["restarting"] is True
    assert rt.store.load() == {}, "the settings are gone"
    assert rt.secrets.load()["nvr"]["password"] == "nvr-password", "the NVR login is not"


def test_resetting_the_device_forgets_everything_it_was_told(stack):
    client, rt, _ = stack
    unlock(client)
    rt.secrets.set_source("nvr", "viewer", "nvr-password")

    answer = client.post("/api/device/reset", json={"scope": "device"})
    assert answer.status_code == 200
    assert rt.store.load() == {} and rt.secrets.load() == {}
    assert "nvr-password" not in rt.secrets.path.read_text()


def test_resetting_the_device_can_also_forget_the_network(stack):
    client, _, host = stack
    unlock(client)
    client.post("/api/device/network/join", json={"ssid": "Kitchen", "password": "a good password"})

    client.post("/api/device/reset", json={"scope": "device", "forget_network": True})
    assert "forget" in host._fake.actions
    assert host._fake.ssid == ""

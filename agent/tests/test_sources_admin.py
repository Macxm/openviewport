"""Adding, changing and removing sources from the admin page."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_camera
from test_api import FakeAdapter
from test_go2rtc import FakeGo2rtc
from viewport.api import create_app
from viewport.config import AppConfig, SourceConfig
from viewport.go2rtc import Go2rtcClient
from viewport.runtime import Runtime
from viewport.secretstore import SecretStore
from viewport.store import ConfigStore

PASSWORD = "Str0ng&pass!"
NEW_SOURCE = {"id": "shed", "type": "rtsp", "username": "viewer", "password": PASSWORD,
              "cameras_urls": [{"name": "Shed", "main": "rtsp://10.0.0.9/main"}]}


def build(tmp_path, declared=True):
    sources = [SourceConfig(id="nvr", host="nvr.local", username="admin", password="hunter2")] if declared else []
    config = AppConfig(sources=sources)
    fake = FakeGo2rtc()
    rt = Runtime(config, adapters=[FakeAdapter([make_camera(0)])] if declared else [],
                 store=ConfigStore(tmp_path / "state.json"),
                 secrets_store=SecretStore(tmp_path / "secrets.json"),
                 go2rtc=Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(fake.handler)))
    if declared:
        rt.wall.update_cameras("nvr", [make_camera(0)])
    return TestClient(create_app(config, rt, start_background=False)), rt, tmp_path


# ----- adding -----------------------------------------------------------------

def test_a_source_can_be_added_and_starts_working(tmp_path):
    client, rt, _ = build(tmp_path)
    resp = client.post("/api/config/sources", json=NEW_SOURCE)
    assert resp.status_code == 200
    assert [s["id"] for s in resp.json()["sources"]] == ["nvr", "shed"]
    assert "shed" in rt.sources
    assert rt.registry.prefixes == ("nvr_", "shed_")      # its streams are ours to manage


def test_an_added_source_survives_a_restart_without_its_password_in_the_settings(tmp_path):
    client, rt, path = build(tmp_path)
    client.post("/api/config/sources", json=NEW_SOURCE)

    settings = json.loads((path / "state.json").read_text())
    assert [s["id"] for s in settings["added_sources"]] == ["shed"]
    assert PASSWORD not in (path / "state.json").read_text()

    restored = ConfigStore(path / "state.json").apply(AppConfig())
    SecretStore(path / "secrets.json").apply(restored.sources)
    assert restored.sources[0].id == "shed"
    assert restored.sources[0].password == PASSWORD        # came from the secrets file


def test_a_source_added_here_can_still_be_removed_after_a_restart(tmp_path):
    """Saved sources are overlaid onto the configuration before the runtime starts, so the
    runtime must not take everything in the configuration as declared in viewport.yaml:
    that hid the Remove button and made the API refuse, for a source only the admin page
    knew about."""
    client, _, path = build(tmp_path)
    client.post("/api/config/sources", json=NEW_SOURCE)

    store = ConfigStore(path / "state.json")
    declared = [SourceConfig(id="nvr", host="nvr.local", username="admin", password="hunter2")]
    restored = store.apply(AppConfig(sources=declared))
    fake = FakeGo2rtc()
    rt = Runtime(restored, store=store, secrets_store=SecretStore(path / "secrets.json"),
                 go2rtc=Go2rtcClient("http://go2rtc:1984", transport=httpx.MockTransport(fake.handler)))
    assert rt.declared_sources == {"nvr"}

    after = TestClient(create_app(restored, rt, start_background=False))
    listed = {s["id"]: s["declared"] for s in after.get("/api/config").json()["sources"]}
    assert listed == {"nvr": True, "shed": False}
    assert after.delete("/api/config/sources/shed").status_code == 200
    assert after.delete("/api/config/sources/nvr").status_code == 409


@pytest.mark.parametrize("url", ["rtsp://viewer:Str0ng@10.0.0.9/main", "rtsp://viewer@10.0.0.9/main"])
def test_credentials_inside_a_stream_address_are_refused(tmp_path, url):
    """The addresses are saved with the settings, which must never hold a password."""
    client, _, path = build(tmp_path)
    source = {**NEW_SOURCE, "cameras_urls": [{"name": "Shed", "main": url}]}
    resp = client.post("/api/config/sources", json=source)
    assert resp.status_code == 422 and "their own fields" in resp.json()["detail"]
    assert not (path / "state.json").exists() or "Str0ng" not in (path / "state.json").read_text()


def test_the_secrets_file_is_private_and_holds_only_credentials(tmp_path):
    client, _, path = build(tmp_path)
    client.post("/api/config/sources", json=NEW_SOURCE)
    secrets = path / "secrets.json"
    assert secrets.stat().st_mode & 0o077 == 0
    assert json.loads(secrets.read_text()) == {"shed": {"username": "viewer", "password": PASSWORD}}


def test_a_duplicate_id_is_refused(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.post("/api/config/sources", json={**NEW_SOURCE, "id": "nvr"}).status_code == 422


@pytest.mark.parametrize("bad", [{"type": "rtsp", "cameras_urls": []},
                                 {"type": "onvif", "host": ""},
                                 {"type": "reolink", "host": "h", "username": ""}])
def test_a_source_missing_what_its_type_needs_is_refused(tmp_path, bad):
    client, rt, _ = build(tmp_path)
    resp = client.post("/api/config/sources", json={**NEW_SOURCE, **bad})
    assert resp.status_code == 422
    assert len(rt.config.sources) == 1


# ----- removing ---------------------------------------------------------------

async def test_removing_a_source_takes_its_cameras_and_streams_with_it(tmp_path):
    client, rt, _ = build(tmp_path)
    client.post("/api/config/sources", json=NEW_SOURCE)
    await rt.refresh_source(rt.sources["shed"])
    assert any(c.startswith("shed:") for c in rt.wall.cameras)

    resp = client.delete("/api/config/sources/shed")
    assert resp.status_code == 200
    assert "shed" not in rt.sources
    assert not any(c.startswith("shed:") for c in rt.wall.cameras)
    assert not any(s.startswith("shed_") for s in rt.registry.desired)
    assert rt.registry.prefixes == ("nvr_",)


def test_removing_a_source_forgets_its_credentials(tmp_path):
    client, _, path = build(tmp_path)
    client.post("/api/config/sources", json=NEW_SOURCE)
    client.delete("/api/config/sources/shed")
    assert json.loads((path / "secrets.json").read_text()) == {}


def test_a_source_from_the_config_file_cannot_be_removed_here(tmp_path):
    client, rt, _ = build(tmp_path)
    resp = client.delete("/api/config/sources/nvr")
    assert resp.status_code == 409 and "viewport.yaml" in resp.json()["detail"]
    assert "nvr" in rt.sources


def test_removing_something_that_is_not_there_is_a_404(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.delete("/api/config/sources/ghost").status_code == 404


# ----- credentials ------------------------------------------------------------

async def test_a_password_can_be_set_but_never_read_back(tmp_path):
    client, rt, path = build(tmp_path)
    resp = client.put("/api/config/sources/nvr", json={"password": "a-new-password"})
    assert resp.status_code == 200
    assert rt.config.sources[0].password == "a-new-password"
    assert "a-new-password" not in resp.text
    assert json.loads((path / "secrets.json").read_text())["nvr"]["password"] == "a-new-password"


async def test_changing_a_password_rebuilds_the_adapter(tmp_path):
    """The old adapter still holds the old credentials, so it has to be replaced."""
    client, rt, _ = build(tmp_path)
    before = rt.sources["nvr"].adapter
    client.put("/api/config/sources/nvr", json={"password": "a-new-password"})
    assert rt.sources["nvr"].adapter is not before
    assert before.closed


async def test_changing_only_a_timing_setting_leaves_the_session_alone(tmp_path):
    client, rt, _ = build(tmp_path)
    before = rt.sources["nvr"].adapter
    client.put("/api/config/sources/nvr", json={"refresh_seconds": 120})
    assert rt.sources["nvr"].adapter is before           # no needless re-login
    assert rt.sources["nvr"].refresh_seconds == 120


def test_a_stored_password_wins_over_the_environment(tmp_path):
    """So that setting one in the admin page visibly takes effect."""
    store = SecretStore(tmp_path / "secrets.json")
    store.set_source("nvr", "viewer", "from-the-page")
    sources = [SourceConfig(id="nvr", host="h", username="admin", password="from-the-env")]
    store.apply(sources)
    assert (sources[0].username, sources[0].password) == ("viewer", "from-the-page")


def test_an_unreadable_secrets_file_does_not_stop_the_agent(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text("{ broken")
    sources = [SourceConfig(id="nvr", host="h", username="admin", password="from-the-env")]
    SecretStore(path).apply(sources)
    assert sources[0].password == "from-the-env"

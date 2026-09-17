"""The deployment's own security properties, checked in the files that set them, so that an
edit to docker-compose.yml cannot quietly undo one."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]
PRODUCT = ["agent", "go2rtc", "mock-nvr"]
RUNNING = PRODUCT + ["e2e-agent", "e2e-agent-fresh", "e2e-go2rtc", "e2e-go2rtc-fresh", "e2e-nvr"]


def test_go2rtc_is_never_published():
    """Its API answers with every stream's source URL, camera passwords included."""
    for name in ("go2rtc", "e2e-go2rtc", "e2e-go2rtc-fresh"):
        assert "ports" not in SERVICES[name], name
    dev = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text())
    assert "go2rtc" not in (dev.get("services") or {})


def test_go2rtc_lets_no_other_origin_in():
    config = yaml.safe_load((ROOT / "config" / "go2rtc.yaml").read_text())
    assert "origin" not in config["api"]


def test_the_wall_is_published_on_loopback_unless_someone_says_otherwise():
    assert SERVICES["agent"]["ports"] == ["${VIEWPORT_BIND:-127.0.0.1}:${VIEWPORT_PORT:-8080}:8080"]


@pytest.mark.parametrize("name", RUNNING)
def test_every_service_is_hardened(name):
    service = SERVICES[name]
    assert service.get("read_only") is True, f"{name}: read-only filesystem"
    assert service.get("cap_drop") == ["ALL"], f"{name}: capabilities dropped"
    assert "no-new-privileges:true" in service.get("security_opt", []), f"{name}: no new privileges"
    assert service.get("cap_add", []) in ([], ["NET_BIND_SERVICE"]), f"{name}: only the capability to listen"
    assert service.get("logging", {}).get("options", {}).get("max-size"), f"{name}: logs capped"


@pytest.mark.parametrize("name", ["go2rtc", "e2e-go2rtc", "e2e-go2rtc-fresh"])
def test_go2rtc_runs_as_an_unprivileged_user(name):
    assert SERVICES[name]["user"] == "10001:10001"


def test_the_agent_image_does_not_run_as_root():
    dockerfile = (ROOT / "agent" / "Dockerfile").read_text()
    assert "\nUSER viewport" in dockerfile


def test_the_agent_listens_where_the_mapping_and_the_healthcheck_look():
    """VIEWPORT_PORT in .env publishes the wall on another host port, and the agent reads that
    same name for its own bind port (viewport/__main__.py), with all of .env reaching it
    through env_file. Unpinned, changing the published port moves the listener off 8080 and the
    wall answers nothing: it builds, starts, logs no error and serves no page."""
    environment = SERVICES["agent"]["environment"]
    assert str(environment["VIEWPORT_PORT"]) == "8080"
    assert environment["VIEWPORT_HOST"] == "0.0.0.0"
    assert SERVICES["agent"]["ports"][0].endswith(":8080")


def test_a_second_copy_of_the_project_can_have_a_stack_of_its_own():
    """`name:` deliberately ignores the directory, so without this a second checkout's `up`
    takes over the first one's containers and shares its settings volume and secrets."""
    assert COMPOSE["name"] == "${COMPOSE_PROJECT_NAME:-viewport}"
    assert "\nCOMPOSE_PROJECT_NAME=viewport\n" in (ROOT / ".env.example").read_text()


def test_the_mock_nvr_only_runs_when_asked_for():
    assert SERVICES["mock-nvr"]["profiles"] == ["mock"]


def test_an_installation_runs_two_containers_and_no_more():
    """Everything else — the mock NVR, the tests, the regression suite's own stack — has to
    ask to be started, so a Raspberry Pi runs the wall and nothing it does not need."""
    always_on = [name for name, service in SERVICES.items() if not service.get("profiles")]
    assert sorted(always_on) == ["agent", "go2rtc"]


def test_the_example_env_files_are_the_dev_and_production_switch():
    """`.env.example` is a real installation; `.env.dev.example` is the mock NVR and the
    bind-mounted pages. Which one you copy is the whole difference."""
    production = (ROOT / ".env.example").read_text()
    development = (ROOT / ".env.dev.example").read_text()
    for setting in ("COMPOSE_PROFILES=mock", "docker-compose.dev.yml"):
        assert setting not in production.replace("#", ""), f"production must not enable {setting}"
        assert setting in development, f"development is missing {setting}"
    assert "NVR_HOST=mock-nvr" in development
    assert "\nNVR_HOST=\n" in production, "a real install is told its NVR, or asks in the page"


def test_nothing_in_the_regression_stack_is_published():
    for name, service in SERVICES.items():
        if name.startswith("e2e"):
            assert "ports" not in service, name


def test_no_real_credentials_are_baked_into_images():
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert ".env" in ignored
    for name in ("agent", "mock-nvr"):
        assert ".env" in (ROOT / name / ".dockerignore").read_text().split(), name

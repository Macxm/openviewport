"""The Raspberry Pi installer, run for real against stand-ins for the system it changes.

It edits the .env someone's NVR password lives in and writes systemd units, so the parts that
can be checked without a Pi are checked here: that it fills in every placeholder, publishes
the wall where it was told to, generates a token, and leaves an existing .env's own settings
alone. Docker, systemd and apt are stubs that only record that they were called.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "pi"
UNITS = ("viewport.service", "viewport-kiosk.service")
STUBS = {
    "docker": "#!/bin/sh\ncase $1 in --version) echo 'Docker version 27.0.0';; compose) shift;; esac\nexit 0\n",
    "systemctl": "#!/bin/sh\n[ \"$1\" = get-default ] && echo multi-user.target\nexit 0\n",
    "apt-get": "#!/bin/sh\nexit 0\n",
    "apt-cache": "#!/bin/sh\nexit 0\n",
    "usermod": "#!/bin/sh\nexit 0\n",
}

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="the installer insists on root")


def install(tmp_path: Path, *args: str, env_file: str | None = None) -> tuple[Path, Path, str]:
    """Run the installer against a copy of the repo, and return (repo, systemd dir, output)."""
    repo = tmp_path / "viewport"
    (repo / "deploy" / "pi").mkdir(parents=True)
    for name in ("install.sh", "kiosk.sh", *UNITS):
        shutil.copy(DEPLOY / name, repo / "deploy" / "pi" / name)
    (repo / "deploy" / "pi" / "install.sh").chmod(0o755)
    shutil.copy(ROOT / ".env.example", repo / ".env.example")
    if env_file is not None:
        (repo / ".env").write_text(env_file)
        (repo / ".env").chmod(0o600)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in STUBS.items():
        (bin_dir / name).write_text(script)
        (bin_dir / name).chmod(0o755)
    # Units must not really land in /etc during a test, and shell has no DESTDIR, so the one
    # path they are written to is redirected in the copy that runs.
    etc = tmp_path / "etc-systemd"
    etc.mkdir()
    script = (repo / "deploy" / "pi" / "install.sh").read_text()
    script = script.replace("/etc/systemd/system", str(etc))
    (repo / "deploy" / "pi" / "install.sh").write_text(script)

    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SUDO_USER": "pi"}
    done = subprocess.run(["sh", str(repo / "deploy" / "pi" / "install.sh"), *args],
                          capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    return repo, etc, done.stdout


def values(env_text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in env_text.splitlines()
                if "=" in line and not line.startswith("#"))


def test_it_publishes_the_wall_where_it_was_told_and_generates_a_token(tmp_path):
    repo, _, output = install(tmp_path, "--user", "pi", "--bind", "0.0.0.0", "--port", "8090")
    env = values((repo / ".env").read_text())
    assert env["VIEWPORT_BIND"] == "0.0.0.0"
    assert env["VIEWPORT_PORT"] == "8090"
    assert len(env["VIEWPORT_API_TOKEN"]) >= 32, "a token so not everyone can watch"
    assert env["VIEWPORT_API_TOKEN"] in output, "the owner has to be told the token once"
    assert stat.S_IMODE((repo / ".env").stat().st_mode) == 0o600


def test_every_placeholder_in_the_units_is_filled_in(tmp_path):
    repo, etc, _ = install(tmp_path, "--user", "pi")
    for name in UNITS:
        unit = (etc / name).read_text()
        assert "@@" not in unit, f"{name}: an unsubstituted placeholder"
        assert str(repo) in unit
    assert "User=pi" in (etc / "viewport-kiosk.service").read_text()


def test_the_kiosk_can_be_left_out(tmp_path):
    _, etc, _ = install(tmp_path, "--user", "pi", "--no-kiosk")
    assert (etc / "viewport.service").exists()
    assert not (etc / "viewport-kiosk.service").exists()


def test_an_existing_env_keeps_its_settings_and_its_token(tmp_path):
    """Running it again, or after editing .env by hand, must not undo either."""
    before = ("COMPOSE_PROJECT_NAME=viewport\n"
              "VIEWPORT_BIND=127.0.0.1\nVIEWPORT_PORT=8080\n"
              "VIEWPORT_API_TOKEN=a-token-chosen-earlier\n"
              "NVR_HOST=192.0.2.10\nNVR_PASSWORD=hunter2\n")
    repo, _, output = install(tmp_path, "--user", "pi", "--port", "9000", env_file=before)
    env = values((repo / ".env").read_text())
    assert env["VIEWPORT_API_TOKEN"] == "a-token-chosen-earlier"
    assert env["NVR_HOST"] == "192.0.2.10" and env["NVR_PASSWORD"] == "hunter2"
    assert env["VIEWPORT_PORT"] == "9000", "the port it was asked for still applies"
    assert "hunter2" not in output, "the installer must not print what it read"

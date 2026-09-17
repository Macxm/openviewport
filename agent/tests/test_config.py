from __future__ import annotations

import pytest
from pydantic import ValidationError

from viewport.config import AppConfig, expand_env, load_config


def test_expand_env_uses_values_and_defaults(monkeypatch):
    monkeypatch.setenv("NVR_HOST", "10.0.0.5")
    monkeypatch.setenv("EMPTY", "")
    monkeypatch.delenv("MISSING", raising=False)
    data = {"a": "${NVR_HOST}", "b": ["${MISSING:-x}", "${EMPTY:-fallback}"], "c": 3}
    assert expand_env(data) == {"a": "10.0.0.5", "b": ["x", "fallback"], "c": 3}


def test_load_config_from_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("NVR_PASSWORD", "secret")
    path = tmp_path / "viewport.yaml"
    path.write_text(
        "sources:\n"
        "  - id: nvr\n"
        "    host: 192.168.1.20\n"
        "    port: ''\n"
        "    https: ''\n"
        "    username: viewer\n"
        "    password: ${NVR_PASSWORD}\n"
    )
    cfg = load_config(path)
    src = cfg.sources[0]
    assert (src.port, src.https, src.password) == (None, False, "secret")
    assert src.protocol == "auto"
    assert cfg.views[0].cameras == "all"
    assert cfg.device.max_main_streams == 1


def test_repo_config_is_valid(monkeypatch):
    from pathlib import Path
    cfg = load_config(Path(__file__).resolve().parents[2] / "config" / "viewport.yaml")
    assert cfg.sources[0].host == "mock-nvr"
    assert [v.layout for v in cfg.views] == ["auto", "1+5"]


def test_duplicate_source_ids_rejected():
    src = {"id": "nvr", "host": "h", "username": "u"}
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"sources": [src, src]})


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/viewport.yaml")


def test_a_source_with_no_address_yet_is_left_out(tmp_path, monkeypatch):
    """An appliance is installed before anyone knows the NVR's address. The baseline YAML
    takes the host from the environment, and a blank one used to crash-loop the agent;
    the wall should come up empty instead, for the setup guide to fill in."""
    monkeypatch.delenv("NVR_HOST", raising=False)
    path = tmp_path / "viewport.yaml"
    path.write_text("sources:\n"
                    "  - id: nvr\n"
                    "    host: ${NVR_HOST:-}\n"
                    "    username: viewer\n"
                    "  - id: shed\n"
                    "    host: 192.0.2.10\n"
                    "    username: viewer\n")
    assert [s.id for s in load_config(path).sources] == ["shed"]


def test_an_rtsp_source_is_judged_by_its_urls_not_its_host(tmp_path):
    path = tmp_path / "viewport.yaml"
    path.write_text("sources:\n"
                    "  - id: cams\n"
                    "    type: rtsp\n"
                    "    cameras_urls:\n"
                    "      - name: Shed\n"
                    "        main: rtsp://192.0.2.10/main\n")
    assert [s.id for s in load_config(path).sources] == ["cams"]

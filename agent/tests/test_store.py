"""Runtime config overrides: what the admin page saves, and how it is overlaid."""

from __future__ import annotations

import json

import pytest

from viewport.config import AppConfig, SourceConfig, ViewConfig
from viewport.store import ConfigStore


def make_config() -> AppConfig:
    return AppConfig(
        sources=[SourceConfig(id="nvr", host="nvr", username="u", password="secret")],
        views=[ViewConfig(name="All cameras")],
    )


def store_at(tmp_path, name="state.json") -> ConfigStore:
    return ConfigStore(tmp_path / name)


def test_no_state_file_means_read_only():
    store = ConfigStore("")
    assert store.path is None
    assert store.writable is False
    assert store.load() == {}


def test_writable_when_the_directory_exists(tmp_path):
    assert store_at(tmp_path).writable is True


def test_saved_views_replace_the_declared_ones(tmp_path):
    store = store_at(tmp_path)
    store.save_views([ViewConfig(name="Front", layout="2x2", cameras=["nvr:0"])])

    config = store.apply(make_config())
    assert [v.name for v in config.views] == ["Front"]
    assert config.views[0].layout == "2x2"
    assert config.views[0].cameras == ["nvr:0"]


def test_state_file_is_written_atomically_and_privately(tmp_path):
    store = store_at(tmp_path)
    store.save_views([ViewConfig(name="Front")])
    assert store.path.exists()
    assert not list(tmp_path.glob("*.tmp"))          # nothing left behind
    assert store.path.stat().st_mode & 0o077 == 0    # owner-only


def test_saved_source_edits_are_overlaid(tmp_path):
    store = store_at(tmp_path)
    source = SourceConfig(id="nvr", host="nvr", username="u", password="p",
                          protocol="rtsp", refresh_seconds=120)
    store.save_sources([source])

    config = store.apply(make_config())
    assert config.sources[0].protocol == "rtsp"
    assert config.sources[0].refresh_seconds == 120


def test_credentials_are_never_written_to_the_state_file(tmp_path):
    store = store_at(tmp_path)
    store.save_sources([SourceConfig(id="nvr", host="nvr.local", username="admin",
                                     password="hunter2")])
    text = store.path.read_text()
    for secret in ("hunter2", "admin", "nvr.local"):
        assert secret not in text
    assert set(json.loads(text)["sources"]["nvr"]) == {
        "protocol", "protocol_fallback", "refresh_seconds", "channels"}


def test_edits_for_a_source_that_no_longer_exists_are_skipped(tmp_path):
    store = store_at(tmp_path)
    store.save({"sources": {"gone": {"protocol": "rtsp"}}})
    config = store.apply(make_config())
    assert config.sources[0].protocol == "auto"


# ----- a broken state file must never stop the viewport booting --------------

def test_unparseable_state_file_is_ignored(tmp_path):
    store = store_at(tmp_path)
    store.path.write_text("{not json")
    config = store.apply(make_config())
    assert [v.name for v in config.views] == ["All cameras"]


def test_state_file_that_is_not_an_object_is_ignored(tmp_path):
    store = store_at(tmp_path)
    store.path.write_text("[1, 2, 3]")
    assert store.load() == {}


def test_invalid_saved_views_are_ignored(tmp_path):
    store = store_at(tmp_path)
    store.save({"views": [{"layout": "2x2"}]})       # no name
    config = store.apply(make_config())
    assert [v.name for v in config.views] == ["All cameras"]


def test_invalid_saved_source_value_is_ignored(tmp_path):
    store = store_at(tmp_path)
    store.save({"sources": {"nvr": {"refresh_seconds": 2, "protocol": "rtsp"}}})
    config = store.apply(make_config())
    assert config.sources[0].refresh_seconds == 60   # below the minimum: rejected
    assert config.sources[0].protocol == "rtsp"      # the valid edit still lands


def test_empty_saved_views_do_not_wipe_the_configured_ones(tmp_path):
    store = store_at(tmp_path)
    store.save({"views": []})
    config = store.apply(make_config())
    assert [v.name for v in config.views] == ["All cameras"]


def test_only_settings_sections_can_be_written(tmp_path):
    """A guard on what reaches the disk must not vanish under python -O."""
    store = store_at(tmp_path)
    with pytest.raises(ValueError, match="not a settings section"):
        store.save_section("sources_password", {"secret": "no"})
    assert not store.path.exists()


def test_each_settings_section_round_trips(tmp_path):
    store = store_at(tmp_path)
    for key in ConfigStore.SECTIONS:
        store.save_section(key, [] if key in ("views", "layouts") else {})
    assert set(json.loads(store.path.read_text())) == set(ConfigStore.SECTIONS)

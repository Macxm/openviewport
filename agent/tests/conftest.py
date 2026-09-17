from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "mock-nvr"))

from mock_nvr.app import create_app as create_mock_app  # noqa: E402
from mock_nvr.settings import Settings as MockSettings  # noqa: E402
from viewport.models import Camera, StreamInfo  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_secret_registry():
    """Secrets registered by one test must not mask text in another."""
    from viewport.credentials import forget_secrets
    forget_secrets()
    yield
    forget_secrets()


@pytest.fixture
def mock_settings() -> MockSettings:
    return MockSettings(channels=4, offline=frozenset({3}), password="p@ss:w/rd&1")


@pytest.fixture
def mock_transport(mock_settings) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=create_mock_app(mock_settings))


def make_camera(channel: int, source: str = "nvr", online: bool = True, name: str | None = None) -> Camera:
    return Camera(
        id=f"{source}:{channel}", source_id=source, channel=channel,
        name=name or f"Cam {channel + 1}", online=online,
        main=StreamInfo(codec="h264", width=1920, height=1080, fps=15),
        sub=StreamInfo(codec="h264", width=640, height=360, fps=15),
    )

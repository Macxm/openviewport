"""Plain RTSP source: cameras given by URL.

The fallback for anything the other adapters do not cover — a camera from a brand with no
adapter, a stream from another system, a test pattern. Nothing is discovered, because there
is nothing to ask: the URLs are the configuration.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

from ..config import SourceConfig
from ..models import Camera, Quality
from .base import SourceAdapter


def with_credentials(url: str, username: str, password: str) -> str:
    """Add user:password to a URL that has none. URLs that carry their own are left alone."""
    parts = urlsplit(url)
    if parts.username is not None or not username:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    userinfo = quote(username, safe="") + (f":{quote(password, safe='')}" if password else "")
    return urlunsplit((parts.scheme, f"{userinfo}@{host}", parts.path, parts.query, parts.fragment))


class RtspAdapter(SourceAdapter):
    def __init__(self, config: SourceConfig):
        self.id = config.id
        self.config = config
        self.model = "RTSP"
        self.firmware: str | None = None
        self.available_protocols: tuple[str, ...] = ("rtsp",)

    async def discover(self) -> list[Camera]:
        # Online is assumed: there is no device to ask. A stream that will not play is
        # reported by the renderer, like any other.
        return [
            Camera(id=f"{self.id}:{channel}", source_id=self.id, channel=channel,
                   name=camera.name, online=True)
            for channel, camera in enumerate(self.config.cameras_urls)
        ]

    def stream_source(self, camera: Camera, quality: Quality) -> str:
        entry = self.config.cameras_urls[camera.channel]
        url = (entry.sub or entry.main) if quality == "sub" else entry.main
        return with_credentials(url, self.config.username, self.config.password)

    async def close(self) -> None:
        return None

"""Shared data types."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Quality = Literal["main", "sub"]


class StreamInfo(BaseModel):
    codec: str | None = None        # "h264" | "h265"
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    bitrate_kbps: int | None = None
    # Seconds between keyframes. A player joining the stream can show nothing until the
    # next one, so this is most of the wait when a tile switches to this stream.
    keyframe_seconds: float | None = None

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)


class Camera(BaseModel):
    id: str                          # "<source>:<channel>"
    source_id: str
    channel: int                     # 0-based
    name: str
    online: bool = True
    model: str | None = None
    main: StreamInfo | None = None
    sub: StreamInfo | None = None

    def stream_name(self, quality: Quality) -> str:
        """Name of this camera's stream in go2rtc."""
        return f"{self.source_id}_{self.channel}_{quality}"

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "online": self.online}

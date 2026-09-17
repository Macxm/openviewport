"""Interface every camera/NVR source implements."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Camera, Quality


class SourceAdapter(ABC):
    id: str

    @abstractmethod
    async def discover(self) -> list[Camera]:
        """Return the source's cameras. Should be cheap: it runs every refresh interval."""

    @abstractmethod
    def stream_source(self, camera: Camera, quality: Quality) -> str:
        """The go2rtc source URL for one of the camera's streams."""

    #: Whether `detections()` does anything. The runtime only polls sources that say so.
    supports_detection: bool = False

    async def detections(self, cameras: list[Camera]) -> dict[str, frozenset[str]]:
        """What each camera detects right now, by camera id.

        Types come from `focus.DETECTION_TYPES`: person, vehicle, animal, face, motion.
        Called every `detection.poll_seconds` with the online cameras being watched, so an
        implementation must batch its requests. Cameras detecting nothing may be omitted.
        """
        return {}

    def next_protocol(self, camera: Camera, quality: Quality) -> str | None:
        """Move one stream to this source's next transport and return its name.

        Called by the runtime when a stream keeps failing, so a camera whose RTSP
        is unhappy can be retried over HTTP-FLV and vice versa. Return None when the
        source has only one transport, or when fallback is switched off.
        """
        return None

    def clear_protocol_overrides(self) -> None:
        """Forget transports pinned by next_protocol().

        Called when the source's own protocol setting changes, so an admin edit is not
        silently overruled by an earlier automatic fallback.
        """

    async def close(self) -> None:
        """Release sessions (e.g. log out of the NVR)."""

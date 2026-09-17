"""go2rtc client and stream registry.

go2rtc is the on-device relay: one upstream connection per stream, shared by every
consumer, opened only while something is watching. Streams are registered at runtime
with PATCH, which (unlike PUT) does not write camera credentials into go2rtc.yaml.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .credentials import redact, safe_error

__all__ = ["Go2rtcClient", "StreamRegistry", "redact"]

log = logging.getLogger(__name__)



class Go2rtcClient:
    def __init__(self, api_url: str, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 5.0):
        self._client = httpx.AsyncClient(base_url=api_url.rstrip("/"), timeout=timeout, transport=transport)

    async def list_streams(self) -> dict[str, Any]:
        resp = await self._client.get("/api/streams")
        resp.raise_for_status()
        return resp.json() or {}

    async def set_stream(self, name: str, src: str) -> None:
        resp = await self._client.patch("/api/streams", params={"name": name, "src": src})
        resp.raise_for_status()

    async def delete_stream(self, name: str) -> None:
        # go2rtc takes the stream *name* in `src` here (`?name=` is accepted, returns 200
        # and deletes nothing). After removing the stream it rewrites go2rtc.yaml, which
        # is a 400 on our read-only config mount even though the delete worked. So on a
        # 400, check the outcome rather than trusting the status code.
        resp = await self._client.delete("/api/streams", params={"src": name})
        if resp.status_code == 400 and name not in await self.list_streams():
            return
        resp.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()


class StreamRegistry:
    """Keeps go2rtc's stream list in line with the cameras we know about."""

    def __init__(self, client: Go2rtcClient, managed_prefixes: list[str]):
        self.client = client
        self.prefixes = tuple(f"{p}_" for p in managed_prefixes)
        self.desired: dict[str, str] = {}
        self._applied: dict[str, str] = {}
        # Until a source has reported its cameras, an empty `desired` means
        # "nothing discovered yet", not "remove everything".
        self.primed = False
        self.reachable = False
        self.last_error: str | None = None

    def manage(self, source_ids) -> None:
        """Which stream-name prefixes belong to us, so pruning only touches our streams."""
        self.prefixes = tuple(f"{source_id}_" for source_id in source_ids)

    def set_desired(self, streams: dict[str, str]) -> None:
        self.desired = dict(streams)
        self.primed = True

    async def sync(self) -> bool:
        """Push changes to go2rtc. Returns True when go2rtc matches the desired state."""
        try:
            current = await self.client.list_streams()
            # go2rtc restarted (or lost streams): re-apply everything.
            if any(name not in current for name in self._applied):
                self._applied = {}
            for name, src in self.desired.items():
                if self._applied.get(name) != src or name not in current:
                    await self.client.set_stream(name, src)
                    self._applied[name] = src
                    log.info("go2rtc stream set: %s -> %s", name, redact(src))
            for name in list(current):
                if self.primed and name.startswith(self.prefixes) and name not in self.desired:
                    await self.client.delete_stream(name)
                    self._applied.pop(name, None)
                    log.info("go2rtc stream removed: %s", name)
        except (httpx.HTTPError, ValueError) as exc:
            self.reachable = False
            # Never str(exc): httpx quotes the request URL, and a PATCH URL carries the
            # camera password (percent-encoded) in `src=`.
            self.last_error = safe_error(exc)
            return False
        self.reachable = True
        self.last_error = None
        return True

    async def stats(self) -> dict[str, Any]:
        """Per-stream upstream/consumer counts for managed streams (credentials removed)."""
        current = await self.client.list_streams()
        out = {}
        for name, info in sorted(current.items()):
            if not name.startswith(self.prefixes):
                continue
            info = info or {}
            producers = info.get("producers") or []
            consumers = info.get("consumers") or []
            out[name] = {
                "source": redact(producers[0].get("url", "")) if producers else None,
                # A producer entry with more than a URL means go2rtc holds an open upstream connection.
                "upstream_connected": any(len(p) > 1 for p in producers),
                "consumers": len(consumers),
            }
        return out

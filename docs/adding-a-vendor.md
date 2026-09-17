# Adding a vendor

A source adapter is the only thing that knows how a particular device works. Everything
else — the stream budget, the wall, the relay — is vendor-neutral.

Before writing one, check whether [ONVIF](hardware/README.md) already covers your device: it
discovers channels, resolutions and stream URLs on most brands. A native adapter is worth it
when the vendor offers something ONVIF does not — real camera names, the configured codec,
detection events, or a transport that behaves better.

## The interface

`agent/viewport/adapters/base.py`:

```python
class SourceAdapter(ABC):
    id: str
    supports_detection: bool = False

    async def discover(self) -> list[Camera]: ...
    def stream_source(self, camera: Camera, quality: Quality) -> str: ...

    async def detections(self, cameras) -> dict[str, frozenset[str]]: ...   # optional
    def next_protocol(self, camera, quality) -> str | None: ...             # optional
    def clear_protocol_overrides(self) -> None: ...                         # optional
    async def close(self) -> None: ...
```

**`discover()`** returns the cameras, and runs every `refresh_seconds`, so it must be cheap:
batch requests, reuse one session, and never poll in a loop of your own. Fill in `main` and
`sub` `StreamInfo` where the device will tell you — the budget uses codec and resolution to
choose a transport, and the admin page shows them.

**`stream_source()`** returns a URL go2rtc can open. It is called often and must not do I/O:
work everything out during discovery and remember it.

**`detections()`** returns the types each camera currently sees, from
`focus.DETECTION_TYPES` (`person`, `vehicle`, `animal`, `face`, `motion`). One batched
request for all cameras; set `supports_detection = True` to be polled at all.

**`next_protocol()`** moves one stream to another transport after repeated failures. Return
`None` if the device offers only one.

## Writing it

1. `agent/viewport/adapters/yourvendor.py`.
2. Register it in `ADAPTERS` in `adapters/__init__.py`.
3. Add the type to `SourceConfig.type` and, if it needs different fields, to the per-type
   validation in `config.py`.
4. Tests, using `httpx.MockTransport` with canned responses from the real device. Take the
   responses from real hardware — namespaces and field names are exactly where mocks drift.
5. A page in `docs/hardware/` with the model, firmware and anything surprising.

`adapters/onvif.py` is a good model for a device that needs a real protocol;
`adapters/rtsp.py` for one that needs almost nothing.

## Rules that are not negotiable

- **Never log or return credentials.** Use `credentials.redact()` and `safe_error()`, never
  `str(exc)` — HTTP libraries put request URLs in their messages, and yours carry passwords
  or tokens.
- **Be gentle with the device.** One session, reused. Batch commands. Back off on errors. A
  wall that breaks someone's recorder is worse than no wall.
- **Decide nothing about streams.** Report what the device offers; `budget.py` chooses.
- **Every behaviour change comes with a test**, and the mock is the test double.

## Testing against real hardware

```bash
NVR_TYPE=yourvendor NVR_HOST=… docker compose up -d --force-recreate agent
curl -s localhost:8080/api/health
curl -s localhost:8080/api/cameras
docker compose --profile test run --rm smoke    # the wall must actually play
```

The smoke check is the one that matters: discovery can look perfect while the URLs do not play.
Before a pull request, also run `sh scripts/e2e.sh`, which checks everything else against the
mock NVR on a stack of its own.

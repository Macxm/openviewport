# Reolink NVS8

Tested 15 Sep 2026 against a real unit, using a **view-only** NVR account.

| | |
|---|---|
| Model | NVS8 (`exactType: NVR`, hardware `N7MB01`) |
| Firmware | `v3.4.0.318_24053118` (build 31 May 2024) |
| Channels reported | 12 (3 with cameras attached) |
| Disks | 2 |
| Status | **Works.** Grid, full screen, stream budget and admin all verified in a browser |

## Cameras on the tested unit

| Channel | Main stream | Sub stream |
|---|---|---|
| 0 | H.265 3840×2160, 25 fps, 6144 kbps | H.264 640×360, 10 fps, 256 kbps, AAC 16 kHz |
| 3 | H.265 3840×2160, 25 fps, 6144 kbps | H.264 640×360, 10 fps, 256 kbps, AAC 16 kHz |
| 4 | H.264 2560×1920, 30 fps, 6144 kbps | H.264 640×480, 10 fps, 256 kbps, AAC 16 kHz |

Wall performance, containerised Firefox over MSE: 0 dropped frames and 0.32–0.35 s latency
on all three sub streams; 1.7–2.4 s from connect to first video byte.

`GetEnc` reports `gop` as the keyframe interval in seconds (the web UI's "I-frame interval"):
2 on every main stream and 4 on every sub stream, as shipped.

## Switching a tile to HD

Measured on the Garage camera, from the wall assigning HD to the HD picture on screen:

| | |
|---|---|
| Cold, HD stream not open | 0.70 s and 1.88 s |
| Again within `main_stream_hold_seconds` | 0.04 s (the stream was still held) |

A cold switch is the RTSP session opening (about half a second) plus waiting for the next
keyframe (up to `gop` seconds); the SD picture stays up meanwhile. An I-frame interval of 1×
roughly halves the worst case; a camera set to Always HD switches instantly.

## "Driveway keeps disconnecting"

Investigated on 16 Sep 2026. The NVR was not dropping the 4K H.265 stream: a consumer held open
on it ran four minutes at 6.1 Mbit/s without a gap. What cycled was the browser: its page
stalled, the wall's watchdog restarted the tile, go2rtc lost its last consumer and closed the
RTSP session, and redialling 4K took tens of seconds. go2rtc's producer ids changing looked
like the NVR dropping the session, but they were the effect, not the cause. The watchdog now
excuses tiles when the page itself froze, and `main_stream_only_on_detection` keeps the wall on
SD streams until something happens (zero reconnects in three minutes afterwards).

## Detections are states, not events

Seen on 17 Sep 2026: a car parked on the drive keeps `GetAiState.vehicle.alarm_state` at 1 for
as long as it stays there, hours included, so the wall marked that tile continuously. The same
holds for `GetMdState`. Nothing in the answers says when it began, so an event has to be derived
from the change: `detection.max_event_seconds` (30 s) is how long one uninterrupted detection
counts for, after which it is scenery until it stops and happens again.

## Recommended settings

```bash
NVR_HTTPS=true        # this unit had HTTP switched off
NVR_PROTOCOL=auto     # resolves to RTSP here; see below
```

## Quirks found, and how the agent handles them

**HTTP is off by default and redirects to the HTTPS root.** `GetNetPort` reports
`httpEnable: 0`. Every HTTP request gets `302 → https://<nvr>` with the path dropped, so
following the redirect cannot reach the API. The agent now says so:
*"the NVR redirects HTTP to HTTPS (its HTTP port is off); set https: true (NVR_HTTPS=true)"*.

**HTTP-FLV is unavailable, even though RTMP is on.** Reolink serves `/flv` on the HTTP port
only. On the HTTPS port, `/flv` completes the TLS handshake and then closes the connection
without any response (curl exit 52) — no 401, not even for unauthenticated requests.
`rtmpEnable: 1` is reported and port 1935 answers an RTMP handshake, but that does not help
HTTP-FLV. The agent reads `GetNetPort` during discovery and only offers FLV when
`httpEnable` is on *and* the configured scheme is plain HTTP, so `auto` goes straight to
RTSP instead of failing FLV for ~15 s per stream before the fallback kicks in.

**Every slot is reported, not every camera.** `GetChannelstatus` lists all 12 channels.
Unused slots come back as `{"name": "", "online": 0}`. Listing them made `layout: auto`
draw a 4×4 wall with 9 dead tiles. Discovery now skips offline channels with no name;
a camera that is merely down keeps its name, so it still shows as offline. Listing a slot
explicitly in `sources[].channels` keeps it.

**Offline channels refuse `GetEnc`** with `rspCode -99, "device offline"`. Discovery no
longer asks for them.

**The sub stream's codec is not reported.** `GetEnc.subStream` has no `vType` (the main
stream does). go2rtc confirms the sub streams are H.264, so the existing "Reolink sub
streams are H.264" assumption holds.

**`GetChannelstatus` entries carry only `channel`, `name` and `online`** — no `typeInfo`,
`sleep` or `uid`. The per-camera model is therefore unknown.

## What a view-only account may call

Confirmed allowed: `Login`, `Logout`, `GetDevInfo`, `GetChannelstatus`, `GetEnc`,
`GetNetPort`, `GetAbility`, `GetMdState`, `GetAiState`.

`GetAiState` reports `people`, `vehicle` and `dog_cat` (`support: 1`) for all three cameras;
`face` is unsupported. Six of these commands batched into one request took 240 ms.

## Stream budget on real hardware

During the browser check with one camera full screen, go2rtc held exactly one main
upstream (`Preview_01_main`) and two sub upstreams. Discovery uses one API session and
two requests per refresh (device + channels + ports, then encoder settings).

## Checked with a person at the NVR

- ~~The Reolink phone app still gets **Clear** while the wall shows a camera full screen.~~
  **Confirmed by the owner, 16 Sep 2026: the phone app is fine.** The stream budget holds in
  practice, which was the whole point of it.
- Recordings have no gaps after a long run (hours) of the wall — still open.

## Security note

go2rtc 1.9.14 writes the full source URL into its **error** log, password included, e.g.
`error="streams: Get \"https://nvr/flv?...&password=<plain text>\": EOF"`, and its API returns
source URLs with passwords to anyone who asks. `docker-compose.yml` pipes its output through the
agent's redactor and never publishes it ([security](../security.md)).

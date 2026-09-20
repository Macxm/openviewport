# API

Everything under `/api/` needs the token when `auth.token` is set, as
`Authorization: Bearer <token>` or a `?token=` parameter; an admin session also counts. The
wall page, the admin page, `/static/*` and `/healthz` are open.

```bash
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/api/health
```

Whatever the credentials, the agent refuses a request whose `Host` is not a name a LAN device
is reached by (`400`; add others to `VIEWPORT_ALLOWED_HOSTS`), and a changing request or any
WebSocket that a browser says comes from another site (`403`, or close code `1008`). Clients
that are not browsers are unaffected. API answers are sent `Cache-Control: no-store`. See
[security](security.md).

## Status

| | | |
|---|---|---|
| `GET` | `/healthz` | `{"status": "ok"}`. Open, for container healthchecks; deliberately says nothing else |
| `GET` | `/api/health` | Sources, go2rtc, the budget, connected renderers, per-source detection state |
| `GET` | `/api/cameras` | Every camera, with its name as shown, and codec, resolution, frame rate, bitrate and keyframe interval per stream |
| `GET` | `/api/streams` | What go2rtc holds open, and which streams the wall has assigned. Credentials redacted |

## The wall

| | | |
|---|---|---|
| `GET` | `/api/wall` | The current snapshot — same shape the WebSocket sends |
| `POST` | `/api/wall/view` | `{"index": 1}` |
| `POST` | `/api/wall/view/next`, `/prev` | |
| `POST` | `/api/wall/fullscreen` | `{"tile": "t0"}` or `{"tile": null}` |
| `POST` | `/api/wall/focus/dismiss` | Back from a detection, and pause detection |
| `POST` | `/api/sources/refresh` | Re-read cameras now, instead of waiting for the next refresh |

## Signing in (unlocking)

| | | |
|---|---|---|
| `POST` | `/api/admin/login` | `{"username": "...", "password": "..."}`; sets the session cookie |
| `POST` | `/api/admin/logout` | Clears it |
| `GET` | `/api/admin/session` | See below. Open, so the page knows whether to offer Unlock |
| `POST` | `/api/admin/password` | `{"username", "current_password", "new_password"}`: choose the password, or change it |

```jsonc
{"authenticated": false, "required": true, "username": "admin",
 "locked_for": 0,                  // seconds before this client may try again
 "password_source": "page",        // "config" (the environment wins), "page", or null: none yet
 "can_set_password": true,         // false when it comes from the config, or there is no secrets file
 "token_required": true}           // whether screens need the API token
```

Five failed logins from one client answer `429` for a minute for that client; 30 across all
clients in 15 minutes, for five minutes for everyone.

`/api/admin/password` stores only a hash, in `VIEWPORT_SECRETS_FILE`, answers with the session
payload and sets a new session cookie for whoever called it; every other session ends. The first
password needs the API token if one is set. A change needs `current_password`, rate-limited like
a login. It is `409` when the password is set in the configuration, `422` under 8 characters.

Everything under **Configuration** that changes something needs the session; with no admin
password set it falls back to the API token.

## Configuration

| | | |
|---|---|---|
| `GET` | `/api/config` | Everything the admin page needs. **Never contains a password.** Readable with the token: `"locked": true` unless signed in, and then without usernames or the state file's path |
| `PUT` | `/api/config/views` | `{"views": [...]}` |
| `PUT` | `/api/config/cameras` | `{"order": ["nvr:4", ...], "settings": {"nvr:0": {"name", "show", "quality", "fit"}}}`. `settings` replaces all of them; leave it out to reorder only |
| `PUT` | `/api/config/layouts` | `{"layouts": [...]}` |
| `PUT` | `/api/config/display` | A display settings object |
| `PUT` | `/api/config/device` | A device settings object |
| `PUT` | `/api/config/detection` | A detection settings object |
| `POST` | `/api/config/sources` | A new source: `id`, `type`, `host`, `port`, `https`, `username`, `password`, or `cameras_urls` for `rtsp`. A stream address with credentials in it is `422` |
| `PUT` | `/api/config/sources/{id}` | `protocol`, `protocol_fallback`, `refresh_seconds`, `channels`, `username`, `password` |
| `DELETE` | `/api/config/sources/{id}` | A source added here; one declared in `viewport.yaml` is `409` |
| `POST` | `/api/config/setup-done` | The setup guide is finished; a fresh install stops offering it |

Each returns the updated `/api/config`. Invalid values are `422`; removing a layout still in
use is `409`. In the `cameras` list, `name` is the name as shown and `reported_name` the NVR's;
`camera_settings` also holds settings for cameras not discovered right now.

## The device it runs on

`GET /api/device` needs the token (or an admin session) and reads what the device knows about
itself: its name, version, camera count, and — where a host helper is installed — how it is
connected, at what address, its uptime, temperature and free disk.

```json
{
  "name": "Hallway", "version": "0.1.0", "cameras": 3,
  "host": {"kind": "helper", "link": "wifi", "ssid": "Kitchen",
           "addresses": ["192.168.1.50"], "access_point": false,
           "uptime_seconds": 486120, "temperature_c": 52.1, "throttled": false}
}
```

`host.kind` is `helper` on a device with one, `fake` on a development machine pretending to be
one, and `none` where there is neither — in which case the rest of the block is absent and the
operations below refuse.

Everything that *changes* the device needs the admin password, not just the token:

| | |
|---|---|
| `GET /api/device/networks` | wifi networks in range, strongest first |
| `POST /api/device/network/join` | `{"ssid": "…", "password": "…"}` |
| `POST /api/device/network/forget` | `{"ssid": "…"}` |
| `POST /api/device/network/prefer` | `{"link": "ethernet"}` or `"wifi"` |
| `POST /api/device/access-point` | `{"on": true}` — show the device's own network |
| `POST /api/device/reboot`, `/shutdown` | what they say |
| `POST /api/device/reset` | `{"scope": "configuration"}` or `{"scope": "device", "forget_network": true}` |

None of this happens in the agent's container. It is passed to a small service on the host
over a Unix socket, which accepts those operations and nothing else
([Raspberry Pi](raspberry-pi.md)).

## WebSockets

### `/api/wall/ws` — the renderer's channel

The snapshot carries a `setup` block while the device has no network but its own: the network
name, its password, the address to open, and a QR matrix for each (rows of 0 and 1, which the
renderer draws). It is `null` the rest of the time, which is almost always.

Receives a snapshot whenever anything changes:

```jsonc
{
  "type": "wall", "version": 42,
  "device": {"name": "Hall viewport"},
  "view": {"index": 0, "name": "All cameras"},
  "views": [{"index": 0, "name": "All cameras"}],
  "layout": {"id": "2x2", "cols": 2, "rows": 2, "tiles": [{"id": "t0", "x": 0, "y": 0, "w": 1, "h": 1}]},
  "fullscreen": null,
  "focus": {"camera": "nvr:0", "name": "Driveway", "reason": "person", "presentation": "tile"},
  "detection": {"enabled": true, "action": "fullscreen"},
  "display": {"fit": "contain", "show_labels": true},
  "budget": {"main_in_use": 1, "max_main": 1, "streams_in_use": 4, "max_total": 16},
  "player": {"transport": "proxy", "mode": "mse", "go2rtc_url": "", "mjpeg_fallback": false},
  "tiles": [{
    "id": "t0", "x": 0, "y": 0, "w": 1, "h": 1,
    "camera": {"id": "nvr:0", "name": "Driveway", "online": true},
    "stream": "nvr_0_sub", "quality": "sub", "reason": "sub: small tile",
    "epoch": 0, "visible": true, "detections": ["person"], "focus": true,
    "fit": null
  }]
}
```

`stream` is the only stream that tile may play. `epoch` changes when the same stream's
upstream changed underneath it (a transport fallback): reconnect rather than waiting out a
back-off. `fit` is the camera's own picture fit (`contain` or `cover`), or `null` to follow
`display.fit`. A camera kept off the wall is in no tile.

Messages a renderer may send:

```jsonc
{"type": "fullscreen", "tile": "t0"}          // null to leave full screen
{"type": "view", "step": 1}                   // or {"index": 2}
{"type": "focus", "action": "dismiss"}
{"type": "stats", "user_agent": "…", "tiles": [
  {"tile": "t0", "stream": "nvr_0_sub", "state": "playing", "width": 640, "height": 360,
   "frames": 1927, "dropped": 4, "latency_s": 0.64, "error": ""}
]}
```

Stats are how the agent learns a stream is broken: three `error` reports move that stream to
the other transport. Send them every few seconds.

### `/api/media/ws?src=<stream>` — video

Speaks go2rtc's player protocol, relayed. Send `{"type": "mse", "value": "<codec list>"}` or
`{"type": "mjpeg"}`; receive fMP4 segments or JPEG frames as binary messages. The agent
refuses any stream the wall has not assigned, closing with code `1008`.

`1008` also means a bad token — do not retry with the same one.

## Writing your own renderer

Subscribe to `/api/wall/ws`, draw `tiles` using `layout`, play each tile's `stream` from
`/api/media/ws`, and report stats. Do not choose streams: the agent's budget is what keeps
the NVR healthy, and the relay will refuse anything else anyway. `agent/viewport/web/wall.js`
is a complete example in about 400 lines.

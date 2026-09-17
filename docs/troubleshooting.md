# Troubleshooting

Start here:

```bash
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/api/health   # sources, go2rtc, the budget
docker compose logs agent --tail 50                                  # already scrubbed of credentials
```

(Leave the header out if no token is set.) `docker compose logs go2rtc` is scrubbed too.

go2rtc's own web UI is not published, on purpose ([security](security.md)). To see what it
holds, ask it from inside its container:

```bash
docker compose exec go2rtc python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:1984/api/streams').read().decode())"
```

That output contains the camera passwords: do not paste it anywhere.

## The agent cannot reach the NVR

**`the NVR redirects HTTP to HTTPS`** — its HTTP port is off. Set `NVR_HTTPS=true`.

**`answered plain HTTP, not TLS: set https: false`** (ONVIF) — `https` describes the vendor's
own API; the ONVIF service on port 8000 is normally plain HTTP. Set `NVR_HTTPS=false`.

**`login failed`** — wrong credentials, or the account is not allowed API access. Use a
view-only account and check it can log into the NVR's own web UI.

**Nothing at all** — check the route from *inside* a container; the host may reach the NVR
when the container cannot:

```bash
docker compose exec go2rtc nc -z -w3 192.0.2.10 554 && echo reachable
```

## Cameras appear but no picture

Check what the agent asked go2rtc for, and whether the upstream opened:

```bash
curl -s localhost:8080/api/streams
```

- `upstream_connected: false` everywhere with tiles showing "Reconnecting…": the NVR is
  refusing the streams. Check RTSP is enabled and the port is right.
- Try the other transport: `NVR_PROTOCOL=rtsp` (or `flv`). The agent falls back on its own
  after three failed reports, but pinning it makes the test decisive.
- **HTTP-FLV needs the NVR's HTTP port.** With HTTP disabled, only RTSP can work; `auto`
  already knows this on Reolink.

## Black tiles in the browser, but the streams are fine

The browser may not decode the codec. Playwright's bundled Chromium has neither H.264 nor
H.265; Firefox and WebKit have both. In a normal browser, check the status panel (`i` or
`?hud=1`): `playing` with a size means decoding is working.

Last resort for a browser without H.264: `MJPEG_FALLBACK=true` and `PLAYER_MODE=mjpeg`. It
transcodes, so it costs real CPU.

## A tile says "Reconnecting…"

The detail is in the status panel, not on the tile — the tile stays plain because the
underlying message can contain the camera password. Also check
`/api/health` → `streams.fallbacks` to see whether that stream has already been moved to the
other transport.

## The phone app cannot get a Clear stream

The viewport is meant to take one of the NVR's two. If it is taking more:

```bash
curl -s localhost:8080/api/streams | grep -c '_main'
```

Check `device.max_main_streams` is 1 (admin page → This device). Note the viewport keeps a
full-resolution stream for `main_stream_hold_seconds` (30 s by default) after leaving full
screen — that is deliberate, to avoid churning the NVR, but it does mean the slot is not
free the instant you press Escape.

## The wall keeps jumping between cameras

Detection focus. Either raise `min_focus_seconds`, narrow `triggers` (motion fires far more
than person), switch `action` to `highlight`, or turn detection off. Any interaction pauses
it for `manual_override_seconds`.

## A tile stays marked, and it is a parked car (or the bins)

Reolink's detection answers say what a camera **can see**, not what just changed: a car on the
drive is reported as a vehicle for as long as it sits there, and the wall used to mark that tile
for as long as it lasted. It now reacts to one thing for at most *React to one thing for at most*
seconds (*Detection → Timing*, 30 s by default), and to the same thing again only once it has
stopped and happened afresh. Lower it if a tile still draws the eye for too long; 0 goes back to
reacting for as long as the NVR reports it.

Something genuinely permanent in view, like a road, is better excluded: switch that camera off
under *Cameras to watch*, or narrow `triggers`.

## Switching a tile to HD takes a couple of seconds

That is mostly the camera: a player that joins a stream can show nothing until the next
keyframe. *Cameras* in the admin page shows each stream's keyframe interval. With the usual
2 s, a switch takes 0.7 to 2.5 s; the SD picture stays on screen meanwhile. To make it faster:

- set the camera's **I-frame interval to 1×** in the NVR's encoding settings (a slightly higher
  bitrate), or
- set that camera to **Always HD** under *Cameras*, which keeps its HD stream open, so going
  full screen is instant. It uses one of the NVR's full-resolution slots all the time.

Going back to a camera within *Keep HD after leaving full screen* (30 s) is already instant.

## The admin page is locked, or the password is forgotten

**Unlock** in the header asks for the admin password. If nobody knows it, set a new one in
`.env`, which takes the place of one chosen in the page, and restart:

```bash
VIEWPORT_ADMIN_PASSWORD='a new password'
```

```bash
docker compose up -d
```

After five wrong passwords a client waits a minute (`429 too many attempts`).

## "unexpected Host" or "requests from another site are refused"

**`400 unexpected Host`**: the device was opened by a name the agent does not accept, which is
how it stops DNS rebinding. IP addresses, `localhost`, single-label names and `.local`, `.lan`,
`.home.arpa`, `.internal` names always work. Add any other name, such as a reverse proxy's:

```bash
VIEWPORT_ALLOWED_HOSTS=cams.example.com
```

**`403 requests from another site are refused`**: a web page on another site tried to change
something or open a WebSocket. If that page is yours (a dashboard embedding the wall, say), it
has to be served from the agent's own address instead.

## The admin page says it is read-only

`VIEWPORT_STATE_FILE` is unset, or its directory is not writable. Under Compose it is
`/data/viewport-state.json` on the `viewport-state` volume.

## Everything was fine, now nothing is

```bash
docker compose ps                      # are go2rtc and the agent healthy?
docker compose restart agent
```

After an update from before September 2026, check `.env` against `.env.example`: the mock NVR
now only starts with `COMPOSE_PROFILES=mock`, and the wall is published on `127.0.0.1` unless
`VIEWPORT_BIND` says otherwise. A TV elsewhere on the network needs `VIEWPORT_BIND=0.0.0.0`.

The agent rediscovers cameras and re-registers streams on start, and will not tear down
streams a running go2rtc is still serving.

## Reporting a problem

Include the output of `/api/health`, the agent's log, your NVR model and firmware, and which
adapter you used. Please re-check any log you attach for credentials if it came from an older
build.

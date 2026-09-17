# Configuration

There are three places settings come from, in this order:

1. **`config/viewport.yaml`** — the declared baseline, mounted read-only and never rewritten.
   Values like `${NVR_HOST:-mock-nvr}` are filled from the environment.
2. **`.env`** — what those placeholders resolve to. Mode `0600`, git-ignored. Any `NAME` can
   instead come from a file via `NAME_FILE` (Docker and Kubernetes secrets).
3. **The admin page** (`/admin`) — sources, cameras, layouts, views, display, device and
   detection settings, saved to `VIEWPORT_STATE_FILE` and laid over the YAML at startup.
   Credentials go to `VIEWPORT_SECRETS_FILE` instead, never there.

The admin page is split into sections, one per tab, each with its own link
(`/admin#cameras`): Sources, Cameras, Views, Layouts, Display, Detection, This device and
Security.

- **A first run** is walked through by the **setup guide**: protect the settings with a
  password, connect the NVR or cameras, name and order them, choose what the wall shows. It
  can be run again from *This device → Setup guide*.
- **It is locked** until someone unlocks it with the admin password, and says *Not protected*
  in its header while there is none ([security](security.md)).
- **It is built for a phone** as much as a desktop. Every setting is one row with a line saying
  what it does, some with an (i) for more; rarely needed ones sit under **Advanced**.
- **Nothing is applied until you save**: a bar at the bottom offers **Save** or **Discard**, a
  dot marks any section with unsaved changes, and saving one section never loses what you
  changed in another. A source, and the password, save on their own.

## .env

| Variable | Default | Meaning |
|---|---|---|
| `NVR_TYPE` | `reolink` | `reolink`, `onvif` or `rtsp` |
| `NVR_HOST` | `mock-nvr` | Address of the NVR |
| `NVR_USERNAME`, `NVR_PASSWORD` | `admin` / `mockpass` | A **view-only** account |
| `NVR_HTTP_PORT` | *(blank)* | API port; blank means 80/443, ONVIF usually 8000 |
| `NVR_HTTPS` | `false` | `true` if the NVR's HTTP port is disabled. Not for ONVIF |
| `NVR_RTSP_PORT` | `554` | |
| `NVR_PROTOCOL` | `auto` | `auto`, `rtsp` or `flv` (Reolink only) |
| `NVR_PROTOCOL_FALLBACK` | `true` | Retry a failing stream over the other transport |
| `COMPOSE_PROJECT_NAME` | `viewport` | Names the stack and its settings volume. A second copy of the project on one machine needs its own |
| `COMPOSE_PROFILES` | unset; `mock` in `.env.dev.example` | `mock` also starts the mock NVR |
| `COMPOSE_FILE` | unset; dev files in `.env.dev.example` | `docker-compose.yml:docker-compose.dev.yml` adds live-edited web pages |
| `VIEWPORT_BIND` | `127.0.0.1` | Where the wall is published. `0.0.0.0` for a screen elsewhere on the LAN, and then set the token and a password |
| `VIEWPORT_PORT` | `8080` | |
| `VIEWPORT_API_TOKEN` | *(blank)* | Lets a screen watch the wall. Blank leaves it open |
| `VIEWPORT_ADMIN_USERNAME` | `admin` | |
| `VIEWPORT_ADMIN_PASSWORD` | *(blank)* | Needed to change anything. Blank: choose one in the admin page. Set here, it wins over that, which is how a forgotten one is reset |
| `VIEWPORT_ADMIN_PASSWORD_HASH` | *(blank)* | Use instead, to keep the password out of the environment |
| `VIEWPORT_ALLOWED_HOSTS` | *(blank)* | Names the device is reached by beyond IPs, `localhost`, single-label and `.local`/`.lan`/`.home.arpa`/`.internal` names, e.g. a reverse proxy's. Comma-separated; `*.example.com` allowed |
| `VIEWPORT_STATE_FILE` | `/data/viewport-state.json` | Where admin edits are kept; blank makes the admin page read-only |
| `VIEWPORT_SECRETS_FILE` | `/data/secrets.json` | Credentials for sources added in the admin page, and a password chosen there (mode `0600`) |
| `VIEWPORT_NAME` | `Dev Viewport` | Shown in the status panel |
| `VIEWPORT_MAX_MAIN_STREAMS` | `1` | **The setting that protects the NVR** |
| `VIEWPORT_MAIN_HOLD_SECONDS` | `30` | Keep a full-resolution stream after leaving full screen |
| `VIEWPORT_DETECTION` | `false` | Make a camera primary when it detects something |
| `PLAYER_TRANSPORT` | `proxy` | `proxy` (video via the agent) or `direct` |
| `PLAYER_MODE` | `mse` | `mse`, or `mjpeg` for browsers without H.264 |
| `MJPEG_FALLBACK` | `false` | Also register transcoded MJPEG streams (CPU heavy) |
| `LOG_LEVEL` | `INFO` | |
| `MOCK_*` | | The mock NVR: channels, codec, offline and empty slots, detections |

A password with `&`, `!` or `$` should be single-quoted: `NVR_PASSWORD='p@ss&word!'`.

## device — the decode budget

These protect both the NVR and the player. All editable at `/admin`.

| Key | Default | Meaning |
|---|---|---|
| `max_main_streams` | 1 | Full-resolution streams at once. Keep at 1 on a Pi: it leaves the vendor app its own "Clear" slot |
| `max_total_streams` | 16 | All streams at once; the same camera twice counts once |
| `main_stream_min_fraction` | 0.4 | Share of the screen a tile needs before it earns the full-resolution stream |
| `main_stream_only_on_detection` | false | Stay on sub streams until a camera detects something, then give *it* the full-resolution stream. Trades a permanently full-resolution big tile for a quieter NVR and a cooler device |
| `keep_grid_warm` | true | Keep the grid's sub streams connected during full screen, so going back is instant |
| `main_stream_hold_seconds` | 30 | Keep a full-resolution stream this long after its tile shrinks, so toggling does not churn the NVR |
| `cycle_views_seconds` | 0 | Rotate views automatically; 0 is off |

## display — how the wall is drawn

| Key | Default | Meaning |
|---|---|---|
| `fit` | `contain` | `contain` letterboxes; `cover` crops to fill the tile |
| `show_labels` | true | Camera names |
| `show_quality_badges` | true | The HD / SD badge |
| `show_clock` | false | A clock at the top |
| `offline_style` | `message` | `message` or `blank` for an offline camera |
| `highlight_detections` | true | Outline and label a tile that is detecting |
| `hide_cursor_seconds` | 3 | 0 keeps the cursor |

## detection — a camera goes primary

| Key | Default | Meaning |
|---|---|---|
| `enabled` | false | Polls the NVR while on |
| `poll_seconds` | 2 | One batched request per interval |
| `triggers` | `[person, vehicle]` | What counts, most important first |
| `action` | `fullscreen` | `fullscreen`, `promote` or `highlight` |
| `promote_layout` | `same` | `same` keeps the view's own layout and just moves the camera into its first tile; or `auto`, `auto-feature`, or a layout name to override |
| `use_main_stream` | true | Whether that camera also switches to full resolution |
| `max_event_seconds` | 30 | How long one uninterrupted detection counts for; 0 = as long as the NVR reports it |
| `highlight_seconds` | 10 | How long the badge stays after the detection stops |
| `hold_seconds` | 15 | Stay after the last detection |
| `min_focus_seconds` | 5 | Before another camera may take over |
| `manual_override_seconds` | 60 | Leave the wall alone after someone uses it |
| `cameras` | `all` | Which cameras to watch |

Reolink reports what a camera **can see**, not what just changed: a car parked on the drive is
reported as a vehicle for as long as it sits there. `max_event_seconds` is what keeps that from
marking the tile all day — after it, the car is part of the scene, and counts again only once it
has gone and something new has arrived. Set it to 0 for a source whose detections really are
events.

## cameras

```yaml
camera_order: ["nvr:4", "nvr:0"]      # most important first

camera_settings:
  "nvr:0":
    name: Front door        # shown on the wall instead of the NVR's name; never written to the NVR
    quality: main           # auto | main (always HD) | sub (always SD)
  "nvr:3":
    show: false             # off the wall: out of every view, and its detections ignored
  "nvr:4":
    fit: cover              # default (follow display.fit) | contain | cover
```

`camera_order` decides which camera fills a layout's first (largest) tile wherever a view
shows every camera. Cameras not listed follow in discovery order, and an id that is not
present is kept in case that camera comes back. A view that names its own cameras wins over
it, so a view can always be arranged by hand.

`camera_settings` holds what the owner has decided about each camera, and only that: a camera
at the defaults is not stored. `quality: main` asks for HD in any tile, which holds one of the
NVR's few full-resolution sessions open and makes going full screen instant; the device's
`max_main_streams` still decides how many play, and the admin page warns when more cameras
ask. `quality: sub` is never HD, not even full screen.

Both are edited under **Cameras** in the admin page, where each camera also shows its streams:
resolution, codec, frame rate, bitrate and keyframe interval. Switching a tile to HD waits for
the next keyframe, so an I-frame interval of 2 s means up to about 2.5 s; 1× in the NVR's
encoding settings makes it faster, for a slightly higher bitrate. Measured on a Reolink NVS8:
0.7 to 1.9 s from SD to HD, and 0.04 s when the HD stream was still held open.

## views

```yaml
views:
  - name: All cameras
    layout: auto          # or any layout name
    cameras: all          # or ["nvr:0", "nvr:3"] — <source id>:<channel>
  - name: Front
    layout: "1+5"
    cameras: ["nvr:0", "nvr:4"]
```

`[` and `]` move between views. Editing them in the admin page applies immediately.

## layouts

Built in: `1x1`, `1x2`, `2x2`, `2x3`, `3x3`, `4x4`, `5x5`, `1+2`, `1+3`, `1+5`, `1+7`,
`1+12`, `2+8`, and two that follow the camera count:

| | |
|---|---|
| `auto` | The smallest square grid that fits — 3 cameras give `2x2` |
| `auto-feature` | The smallest **one-big-tile** layout that fits — 3 cameras give `1+2`, not `1+5` with three empty tiles |

Both follow the count as cameras are added or removed. Add your own in the admin page, or in
the config:

```yaml
layouts:
  - id: gate
    name: Gate and yard
    cols: 3
    rows: 2
    tiles:
      - {x: 0, y: 0, w: 2, h: 2}
      - {x: 2, y: 0}
      - {x: 2, y: 1}
```

Tiles must fit the grid and must not overlap; gaps are allowed. Custom names cannot shadow a
built-in layout, and a layout cannot be deleted while a view or detection uses it.

## sources

See [supported hardware](hardware/README.md) for each type's fields and examples. Several
sources can run at once; camera ids are `<source id>:<channel>`.

Sources can also be **added in the admin page**, which is easier than editing files: choose a
type, give it an address and credentials, and it starts looking for cameras straight away.
Those sources are remembered in the settings file, and their passwords in a separate secrets
file (`VIEWPORT_SECRETS_FILE`, mode `0600`) — never together. A password can be set there but
never read back, and a password set in the admin page overrides the environment, so that
setting one visibly takes effect.

A source added in the admin page has a **Remove source** button on its card. One declared in
`viewport.yaml` or `.env` cannot be removed there, since it would come back on the next
restart. How a source connects (type, address, port) is fixed once it exists: remove it and
add it again to change that. A stream address that contains a username or password is refused;
use the Username and Password fields, which are kept in the secrets file. See
[handling credentials](credentials.md) for the reasoning and the limits.

## auth

| Key | Default | Meaning |
|---|---|---|
| `token` | *(blank)* | `VIEWPORT_API_TOKEN` |
| `admin_username`, `admin_password`, `admin_password_hash` | `admin`, *(blank)* | See `.env` above |
| `session_hours` | 12 | How long an unlocked admin page stays unlocked |
| `allowed_hosts` | *(blank)* | `VIEWPORT_ALLOWED_HOSTS` |

## go2rtc

`config/go2rtc.yaml` is the config for the compose stack, where go2rtc is reachable only from
the other containers and allows no other origin. `config/go2rtc.prod.yaml` is for running
without Docker on one device: API and RTSP on loopback only. Camera streams are added at
runtime and are never written to either file. `player_transport: direct` (the browser talking
to go2rtc itself) needs go2rtc's `api.origin` set to the wall's own origin, never `*`; the
default, `proxy`, needs neither.

# Architecture

## The problem

An NVR will happily serve a few streams and then start dropping them. Reolink allows **two
"Clear" (full-resolution) viewers and ten "Fluent" (low-resolution) viewers** per camera, and
the owner's phone app needs one of them. A camera wall that opens sixteen full-resolution
streams is not a slow wall: it is a wall that breaks recording and locks the owner out of
their own cameras.

So the design question is not "how do we show sixteen cameras" but **"how few streams can we
open, and who decides"**. Everything below follows from that.

## The pieces

```
  NVR / cameras
        │  one connection per stream, opened only while watched
     go2rtc            relay: RTSP / HTTP-FLV in, anything out. Never published.
        │  ws://…/api/ws?src=<stream>
      agent            FastAPI. Discovers cameras, decides streams, relays video,
        │              serves the wall and the admin page, and guards all of it.
        │  /api/wall/ws   state out, commands and health in
        │  /api/media/ws  video, behind the token, only for assigned streams
     renderer          the browser wall today; a native Pi player later
```

**go2rtc** holds one upstream connection per stream, shared by every consumer, and opens it
only while something is watching. Streams are registered at runtime over its API, so camera
credentials never reach its config file.

**The agent** (`agent/viewport/`) is the only thing that decides anything:

| Module | Responsibility |
|---|---|
| `adapters/` | Talking to devices. `reolink`, `onvif`, `rtsp`; see [adding a vendor](adding-a-vendor.md) |
| `budget.py` | Which stream each tile gets: main, sub or none |
| `focus.py` | Which camera a detection should make primary |
| `wall.py` | The wall's state, and how a focus is presented |
| `layouts.py` | Grids, feature layouts, and user-defined ones |
| `media.py` | The video relay |
| `auth.py` | The admin password (scrypt), sessions, and pausing clients that guess |
| `security.py` | Headers, the content security policy, refusing cross-site requests and unknown hosts |
| `credentials.py` | Keeping secrets out of logs, responses and screens |
| `store.py` | Runtime edits, kept apart from the declared config |
| `secretstore.py` | Passwords at rest: sources' credentials and the admin password's hash |
| `api.py` | HTTP and WebSocket |

**The renderer** subscribes to `/api/wall/ws`, plays exactly the streams it is told to, and
reports what it sees. It chooses nothing.

## The stream budget

`budget.py` is a pure function: tiles in, assignments out. Its rules, in order:

1. A tile with no camera, or an offline one, plays nothing.
2. Tiles covering at least `main_stream_min_fraction` of the screen want the full-resolution
   stream, and so does a tile marked `force_main` however small; forced tiles first, then the
   largest, win up to `max_main_streams`. The rest get the sub stream.
3. Spare full-resolution budget goes to streams still inside `main_stream_hold_seconds`,
   newest first — so flipping in and out of full screen does not make the NVR open and close
   a session each time, and when the budget cannot keep them all it keeps the one the NVR
   most likely still has open.
4. Everything else gets its sub stream, including tiles hidden behind a full-screen one when
   `keep_grid_warm` is on, so coming back is instant.
5. A hard cap of `max_total_streams` distinct streams. The same camera in two tiles counts
   once, because go2rtc shares the upstream.

Being pure makes it exhaustively testable, which matters: this is the code that decides
whether someone's recorder keeps working.

`wall.py` decides what each tile may ask for before the budget sees it, most specific first:

1. **The camera's own quality setting.** `sub` may never have full resolution, not even full
   screen; `main` asks for it in any tile (`force_main`).
2. **A person's full screen** always may.
3. **A camera a detection brought forward** may if `detection.use_main_stream`, and is forced
   when `device.main_stream_only_on_detection` holds everything else back.
4. **Everything else** may, unless `main_stream_only_on_detection` is on.

A camera its owner has kept off the wall (`camera_settings.show: false`) is in no view and is
not watched for detections, so no rule ever reaches it.

Because renderers could once ask go2rtc for any stream directly, the budget was advice. Now
video is relayed by the agent, which **refuses any stream the wall has not assigned**, so the
budget is enforced rather than trusted.

## Detection focus

`focus.py` is a state machine with an injected clock. Triggers are a priority list; a busier
camera cannot steal the screen from an equally important one; a more important detection may
take over after `min_focus_seconds`; a camera holds the screen for `hold_seconds` after its
last detection; detections nobody refreshes go stale, so an unreachable NVR cannot pin the
screen on a last sighting.

`wall.py` turns that into a presentation — full screen, promotion into a big tile, or a
highlight — and **always yields to a person**: any interaction pauses detection-driven
changes for `manual_override_seconds`.

## Protecting the NVR

| | |
|---|---|
| One session | One login per source, reused until it expires; commands batched |
| Slow polling | Camera list every `refresh_seconds` (60 s); exponential back-off on failure |
| One exception | The opt-in detection poll, one batched request every `poll_seconds`, backing off per source |
| One upstream | Everything goes through go2rtc, which shares connections |
| Budgeted | `max_main_streams` (default 1) leaves the vendor app its own Clear slot |
| No churn | Hold-downs on full-resolution streams; no delete-and-recreate on restart |
| Fallback | A stream failing repeatedly moves to the other transport, with a cooldown |

## Who may change what

Two credentials, for two different things ([security](security.md)): the API token lets a
screen watch, and the admin password lets someone change settings. The admin page reads the
settings with the token and shows them locked; `admin_api` routes need a session. Neither can
be bypassed from another web site: `security.py` refuses changing requests and WebSockets that
a browser marks as cross-site, names that are not a LAN device's (DNS rebinding), and framing,
and gives every page a content security policy that runs only the agent's own scripts.

## Keeping secrets

Credentials leak through error messages far more often than through code that handles them
deliberately: httpx puts request URLs in exceptions, go2rtc puts source URLs in its logs *and*
in the errors it sends the browser. `credentials.py` is the single implementation — patterns
for every encoding those URLs appear in, plus the exact secret values registered at startup —
applied to log records at creation, to stored errors, to go2rtc's own output, and in the
browser. See [security](security.md).

## Why not drive the vendor's web UI?

The reference projects for UniFi Protect do exactly that. For Reolink it would mean a
settings-first web client with no control over which stream each tile plays — precisely the
control the whole design depends on. Reading the device's own API and driving go2rtc keeps
that control where the budget can enforce it.

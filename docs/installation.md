# Installation

## What you need

- A computer to run it on: a Raspberry Pi 5, an Intel N100-class mini PC, or any machine with
  Docker. Development happens on a Mac.
- **Docker** with the Compose plugin. Nothing else: no Python, Node or browser on the host.
- An NVR or cameras on the same network: see [supported hardware](hardware/README.md).
- A **view-only** account on that NVR. The viewport only reads; do not give it an admin login.

## 1. Try it with the built-in mock NVR

Before touching your real cameras, run it against the built-in mock: `.env.dev.example`
starts a mock Reolink NVR that serves test patterns, so you can see everything working.
(`.env.example` is the real installation: the agent and go2rtc, and nothing else.)

```bash
git clone <this repository> viewport && cd viewport
cp .env.dev.example .env
chmod 600 .env
docker compose up -d --build
```

| | |
|---|---|
| http://localhost:8080 | the wall |
| http://localhost:8080/?hud=1 | the wall with the status panel (or press `i`) |
| http://localhost:8080/admin | the settings |
| http://localhost:8081 | the mock NVR's own page |

Four test-pattern cameras appear within a few seconds, one of them offline on purpose. Click a
tile for full screen, `Esc` to come back, `[` and `]` to change view.

The first time you open `/admin`, the **setup guide** walks you through protecting the
settings, connecting cameras, naming them and choosing what the wall shows. With the mock you
can skip the camera step: it is already connected.

## 2. Point it at your NVR

Create a view-only account on the NVR, and make sure its **HTTP (or HTTPS) and RTSP ports are
enabled**: on Reolink, *Settings → Network → Advanced → Port Settings*.

Then either add it in the admin page (*Sources → Add a source*, or step 2 of the setup guide),
or declare it in `.env`. Going from the mock to real cameras means starting from the other
example file, which runs no mock and no development extras:

```bash
cp .env.example .env && chmod 600 .env
```

```bash
NVR_TYPE=reolink              # or onvif, or rtsp
NVR_HOST=192.0.2.10
NVR_USERNAME=viewer
NVR_PASSWORD='the password'   # single quotes keep & ! $ literal
NVR_HTTPS=false               # true if the NVR's HTTP port is switched off
```

```bash
docker compose up -d --remove-orphans
```

Within a minute *Sources* in the admin page shows the NVR and how many cameras it found;
*Cameras* shows each one's resolution, codec and keyframe interval. If not, see
[troubleshooting](troubleshooting.md): the usual causes are an HTTPS-only NVR and RTSP
switched off.

While a camera is full screen, **check that the vendor's phone app can still open a
full-resolution (Clear) stream**. Reolink allows two at once, and the viewport takes at most one.

## 3. Secure it

Read [security](security.md) once. The short version:

**An admin password.** The setup guide asks for one first; so does *Security* in the admin
page, and its header says *Not protected* until there is one. Only a hash is kept. To set it
from `.env` instead (which also resets a forgotten one):

```bash
VIEWPORT_ADMIN_PASSWORD='a few words together work well'
```

**Where it is published.** The wall is on `127.0.0.1:8080` by default, which suits a kiosk
browser on the device itself. For a TV or another screen on the network:

```bash
VIEWPORT_BIND=0.0.0.0
```

**A token**, whenever anything other than the device itself shows the wall:

```bash
docker compose run --rm --no-deps agent python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```bash
VIEWPORT_API_TOKEN=<the token>
```

Screens then open `http://<device>:8080/?token=<the token>`.

**HTTPS**, if the network is not yours alone: put a reverse proxy in front of port 8080 (a
Caddy example is in [security](security.md)), and add its name to `VIEWPORT_ALLOWED_HOSTS`.

```bash
docker compose up -d
```

## 4. Put it on a screen

Only the agent's port needs to be reachable from the screen: video is relayed through the
agent, and go2rtc is never published.

### Raspberry Pi

`deploy/pi/install.sh` does the whole job: both services at boot, a full-screen browser on the
TV, and the settings reachable from your phone. See **[Raspberry Pi](raspberry-pi.md)**.

Pi performance has not been measured yet. Keep *HD streams at once*
(`device.max_main_streams`) at 1 there.

### Any other screen

Anything with a modern browser works: a smart TV's browser, a tablet on a wall mount, a spare
laptop. Open the URL with its token. The wall hides the pointer after a few seconds and needs no
input.

## Updating

```bash
git pull
docker compose up -d --build
```

Settings live in `.env` and in a Docker volume (`viewport-state`, which also holds the secrets
file), so an update touches neither. Browsers fetch the new pages straight away: the agent tells
them to check for newer copies every time.

Keeping a **second copy** of the project, to try an update in before committing to it? Give it a
name of its own in its `.env`:

```bash
COMPOSE_PROJECT_NAME=viewport-test
```

The stack's name deliberately ignores which directory it sits in, so without this `docker compose
up` in the copy adopts the running stack's containers and shares its settings and secrets.

## Running without Docker

Possible but unsupported: Python 3.11+, `go2rtc` and `ffmpeg` on `PATH`, then
`python scripts/dev_native.py`, with `config/go2rtc.prod.yaml` for go2rtc. Docker is the tested
and hardened path.

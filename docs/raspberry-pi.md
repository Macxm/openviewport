# Raspberry Pi

A Pi wired to a TV, showing the cameras from the moment it powers on, with its settings
reachable from your phone. Two containers run on it and nothing else.

| | |
|---|---|
| `viewport.service` | the stack: the agent and go2rtc. Starts at boot |
| `viewport-kiosk.service` | one full-screen Chromium on tty1, showing the wall |
| `openviewport-hostd.service` | the only part that runs as root: wifi, restart, power off |
| the wall | `http://localhost:8080/?token=…` — what the TV shows |
| the settings | `http://<the pi>.local:8080/admin` — from your phone or laptop |

## What you need

- A **Raspberry Pi 5** (a 4 will also run it) with a power supply, and an SD card or,
  better, an SSD.
- **Raspberry Pi OS Lite, 64-bit** (Bookworm or newer). Not the Desktop edition: the kiosk
  wants tty1 to itself. If you already run Desktop, install with `--no-kiosk` and open the
  wall in its own browser.
- Your NVR on the same network, and a **view-only account** on it.

## 1. The card

Raspberry Pi Imager → *Raspberry Pi OS (other)* → *Raspberry Pi OS Lite (64-bit)*. In the
settings cog set the hostname (this guide assumes `viewport`), a username and password, your
wifi if it is not on ethernet, and switch SSH on. Boot the Pi and log in:

```bash
ssh <you>@viewport.local
```

## 2. Docker

```bash
sudo apt update && sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

## 3. Install

```bash
git clone <this repository> ~/viewport && cd ~/viewport
sudo deploy/pi/install.sh
```

It checks Docker, writes `.env` (generating a token for the wall), installs `cage` and
Chromium, builds the images — a few minutes on a Pi — and enables both services. It prints
the settings link with the token in it when it is done. Running it again changes nothing it
has already done.

```
sudo deploy/pi/install.sh --help          # --user, --bind, --port, --no-kiosk
```

The TV shows the wall from here on, including after a power cut. There is nothing to log in
to and no desktop behind it.

## 4. First run

Open the link the installer printed, from your phone or laptop:

```
http://viewport.local:8080/admin?token=…
```

**Choose a password on the first screen.** Until you do, anyone on your network who has that
link can change the settings. The guide then walks you through adding the NVR, naming the
cameras and choosing what the wall shows — [installation](installation.md#2-point-it-at-your-nvr) covers
the same ground.

Afterwards the settings are at `http://viewport.local:8080/admin` and ask for that password;
the token is only needed for *watching* the wall, which the kiosk does for you.

## The network, and what is exposed

The installer publishes the wall on **all interfaces** (`VIEWPORT_BIND=0.0.0.0`), because
managing the device from your phone is the point. That means:

- **Watching** needs the token, which is in `.env` and in the kiosk's URL.
- **Changing** anything needs the admin password.
- It is **plain HTTP**. On your own home network that is a considered trade-off; if the
  network is shared, put HTTPS in front of it ([security](security.md)).

For a screen-only box, with nothing to manage from elsewhere:

```bash
sudo deploy/pi/install.sh --bind 127.0.0.1
```

Then nothing but the Pi itself can reach the wall, and you manage it over SSH. go2rtc is never
published either way: its API would hand out the camera password.

## Changing the device from the settings page

The **General** section shows what this device is — its name, version, how long it has been
running, its temperature, disk space, which network it is on and at what address — and can
change it: join or forget a wifi network, prefer ethernet or wifi, show the device's own setup
network, restart, shut down, or start again from scratch.

Anyone who may see the settings may read that. Everything that *changes* the device needs the
admin password.

None of it happens inside the container. The agent runs read-only, without Linux capabilities
and as nobody in particular, so it asks `openviewport-hostd`, a small service on the device
itself, over a Unix socket. That service answers eight operations and nothing else — status,
networks, join, forget, access-point, prefer, reboot, shutdown — checks every argument again on
arrival, and never passes anything to a shell. There is no operation that runs a command of the
caller's choosing, so the worst anyone who reaches the socket can do is what someone standing
at the device could do anyway. systemd takes away everything the service does not need
(`deploy/pi/openviewport-hostd.service`).

It needs NetworkManager, which Raspberry Pi OS uses by default. Without it the installer skips
the helper, and the General section shows what it can and says the rest is unavailable.

**Starting again** comes in two sizes. *Reset the settings* returns the cameras, views and
layouts to a fresh install and starts the setup guide, keeping the admin password and the NVR's
login. *Reset the device* forgets those too, and the wifi network with them: for handing the
device to someone else. The agent restarts itself either way, and comes back on what is left.

## Updating

```bash
sudo deploy/pi/update.sh
```

Pulls, rebuilds, restarts both services. `.env` and the settings volume are untouched, so the
cameras, views and password survive.

## Performance

Getting the most out of the hardware:

- Keep **HD streams at once** (`device.max_main_streams`) at 1. The NVR only serves two
  full-resolution viewers in total, the phone app included.
- A Pi 5 decodes **H.265 in hardware** but has **no H.264 hardware decoder** at all, so a
  4K H.264 main stream is the expensive case. Reolink 4K cameras are usually H.265.
- **HD only when something happens** (`device.main_stream_only_on_detection`, in *This
  device*) keeps every tile on its low-resolution stream until a camera detects something.
  On the development NVR this alone stopped a 4K tile from cycling.
- The wall's status panel (press `i`, or `?hud=1`) shows dropped frames and delay per tile,
  which is what to watch while tuning, alongside `vcgencmd measure_temp`.
- `deploy/pi/kiosk.sh` passes no hardware-decoding flags, on purpose. Flags like
  `--enable-features=VaapiVideoDecodeLinuxGL` may help or may break playback; measure before
  keeping one.

## When something is wrong

| What you see | Where to look |
|---|---|
| Black TV, no browser | `journalctl -u viewport-kiosk -f`. `cage` needs tty1: is the system booting to `multi-user.target` (`systemctl get-default`)? |
| Browser up, no cameras | `systemctl status viewport`, then `docker compose logs agent`. The usual cause is the NVR's HTTP or RTSP port being off |
| "The viewport agent rejected this token" | `.env`'s token changed after the kiosk started: `sudo systemctl restart viewport-kiosk` |
| The screen blanks after a while | `consoleblank=0` in `/boot/firmware/cmdline.txt`, then reboot |
| Nothing after a reboot | `systemctl is-enabled viewport viewport-kiosk` — the installer enables both |

More in [troubleshooting](troubleshooting.md).

## Working on it instead

`.env.example` is a real installation: the agent and go2rtc. For development,
`cp .env.dev.example .env` adds a mock Reolink NVR serving test patterns and bind-mounts the
web pages, so there is no NVR to point at and no rebuild to edit a page
([building](building.md)).

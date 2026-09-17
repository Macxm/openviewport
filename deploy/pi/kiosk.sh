#!/bin/sh
# The wall, full screen on the TV: cage (a Wayland kiosk compositor) running one Chromium.
# Started by viewport-kiosk.service; safe to run by hand from tty1 to try it.
#
# The wall is a Docker stack, so this waits for it to answer before opening the browser: a
# kiosk with no one at the keyboard must not end up sitting on an error page.
set -eu

dir=${VIEWPORT_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)}
env_file="$dir/.env"

# Read one value out of .env without sourcing it: that file holds the NVR's password.
value() {
    [ -f "$env_file" ] || return 0
    sed -n "s/^$1=//p" "$env_file" | tail -n 1 | sed -e "s/^['\"]//" -e "s/['\"]\$//"
}

port=$(value VIEWPORT_PORT)
port=${port:-8080}
token=$(value VIEWPORT_API_TOKEN)

url="http://127.0.0.1:$port/"
[ -n "$token" ] && url="$url?token=$token"

waited=0
while [ "$waited" -lt 300 ]; do
    if curl -fsS --max-time 2 "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then
        break
    fi
    sleep 2
    waited=$((waited + 2))
done
[ "$waited" -lt 300 ] || echo "the wall did not answer in $waited s; opening it anyway" >&2

: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR

browser=$(command -v chromium-browser || command -v chromium || true)
[ -n "$browser" ] || { echo "no chromium: sudo apt install chromium-browser" >&2; exit 1; }

# --autoplay-policy: video must start without anyone clicking. --user-data-dir keeps the
# profile out of the way. Nothing here enables hardware decoding: which flags help depends on
# the Pi and has not been measured yet (docs/raspberry-pi.md, "Performance").
exec cage -- "$browser" \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-features=Translate \
    --no-first-run \
    --check-for-update-interval=31536000 \
    --autoplay-policy=no-user-gesture-required \
    --password-store=basic \
    --user-data-dir="${XDG_CACHE_HOME:-$HOME/.cache}/viewport-kiosk" \
    "$url"

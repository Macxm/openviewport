#!/bin/sh
# Update the viewport in place: new code, new images, back on the screen.
#
#   sudo deploy/pi/update.sh
#
# Settings are not touched: they live in .env and in a Docker volume.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo" >&2; exit 1; }
dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$dir"

echo "== Fetching"
git pull --ff-only

echo "== Building"
docker compose build

echo "== Restarting"
systemctl restart viewport.service
systemctl is-active --quiet viewport-kiosk.service && systemctl restart viewport-kiosk.service

echo "== Done. docker compose ps:"
docker compose ps

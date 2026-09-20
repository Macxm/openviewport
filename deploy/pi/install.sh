#!/bin/sh
# Install the viewport on a Raspberry Pi running Raspberry Pi OS (Bookworm or newer).
#
#   sudo deploy/pi/install.sh [--user NAME] [--bind ADDR] [--port N] [--no-kiosk]
#
#   --user NAME   the account the kiosk browser runs as (default: whoever ran sudo)
#   --bind ADDR   where the wall is published (default 0.0.0.0, so the settings can be
#                 reached from your phone; 127.0.0.1 keeps it to this device alone)
#   --port N      the port it listens on (default 8080)
#   --no-kiosk    install the stack only, no browser on the TV
#
# It does four things, and says so as it goes:
#   1. checks Docker is present (it does not install Docker for you)
#   2. writes .env from .env.example if there is none, with a token generated for the wall
#   3. installs cage and chromium for the kiosk
#   4. installs and enables two services: viewport (the stack) and viewport-kiosk (the TV)
#
# Running it a second time changes nothing it has already done.
set -eu

user=${SUDO_USER:-}
bind=0.0.0.0
port=8080
kiosk=yes

while [ $# -gt 0 ]; do
    case $1 in
        --user) user=${2:?--user needs a name}; shift 2 ;;
        --bind) bind=${2:?--bind needs an address}; shift 2 ;;
        --port) port=${2:?--port needs a number}; shift 2 ;;
        --no-kiosk) kiosk=no; shift ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo" >&2; exit 1; }
dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)

say() { printf '\n== %s\n' "$*"; }

# ---- 1. Docker ----------------------------------------------------------------------------

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    cat >&2 <<'EOF'
Docker with the Compose plugin is needed, and is not here yet. On Raspberry Pi OS:

  sudo apt install -y ca-certificates curl
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

Then run this script again.
EOF
    exit 1
fi
docker=$(command -v docker)
say "Docker: $($docker --version)"

# ---- 2. .env ------------------------------------------------------------------------------

get_env() { sed -n "s/^$1=//p" "$dir/.env" | tail -n 1; }

set_env() {   # set_env KEY VALUE — replace it, or add it if it is not there
    _tmp=$(mktemp)
    awk -v k="$1" -v v="$2" 'BEGIN { seen = 0 }
        index($0, k "=") == 1 { print k "=" v; seen = 1; next }
        { print }
        END { if (!seen) print k "=" v }' "$dir/.env" > "$_tmp"
    cat "$_tmp" > "$dir/.env"      # keeps the file's own owner and mode
    rm -f "$_tmp"
}

if [ -f "$dir/.env" ]; then
    say "Settings: keeping the .env that is already here"
else
    say "Settings: writing .env from .env.example"
    cp "$dir/.env.example" "$dir/.env"
    chown "${user:-root}" "$dir/.env" 2>/dev/null || true
    chmod 600 "$dir/.env"
fi

set_env VIEWPORT_BIND "$bind"
set_env VIEWPORT_PORT "$port"

# The wall's clock and the overnight screen schedule are this device's local time, and a
# container is UTC unless it is told which zone it is in.
if [ -z "$(get_env TZ)" ] && command -v timedatectl >/dev/null 2>&1; then
    zone=$(timedatectl show -p Timezone --value 2>/dev/null || true)
    if [ -n "$zone" ]; then
        set_env TZ "$zone"
        say "Time zone: $zone, taken from this device"
    fi
fi

if [ -z "$(get_env VIEWPORT_API_TOKEN)" ]; then
    token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))' 2>/dev/null \
            || dd if=/dev/urandom bs=32 count=1 2>/dev/null | base64 | tr -d '=+/' )
    set_env VIEWPORT_API_TOKEN "$token"
    say "A token was generated, so only screens that have it can watch the cameras"
else
    token=$(get_env VIEWPORT_API_TOKEN)
    say "Token: keeping the one already in .env"
fi

# ---- 3. the kiosk's packages --------------------------------------------------------------

if [ "$kiosk" = yes ]; then
    browser_pkg=chromium-browser
    apt-cache show chromium-browser >/dev/null 2>&1 || browser_pkg=chromium
    say "Installing cage and $browser_pkg for the screen"
    # cec-utils is what lets the screen schedule turn the television off rather than just
    # blanking it. It is small, and useless to install later without knowing to.
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        cage "$browser_pkg" curl cec-utils
    [ -n "$user" ] && usermod -aG video,input,render,tty "$user"
fi

# Lets that account run `docker compose` without sudo. On a single-purpose box this is normal;
# it is also root by another name, so do not hand the account out.
[ -n "$user" ] && usermod -aG docker "$user"

# ---- 4. the services ----------------------------------------------------------------------

install_unit() {
    sed -e "s|@@DIR@@|$dir|g" -e "s|@@USER@@|${user:-root}|g" -e "s|@@DOCKER@@|$docker|g" \
        "$dir/deploy/pi/$1" > "/etc/systemd/system/$1"
    chmod 644 "/etc/systemd/system/$1"
}

say "Building the images (a few minutes the first time)"
(cd "$dir" && $docker compose build)

say "Installing the services"
install_unit viewport.service

# The helper is what lets the settings page join a wifi network, restart the device or reset
# it. Without NetworkManager there is nothing for it to drive, so it is skipped and the
# General section shows what it can without it.
if command -v nmcli >/dev/null 2>&1; then
    install_unit openviewport-hostd.service
    helper=yes
else
    echo "note: NetworkManager (nmcli) is not installed, so the network and power controls" >&2
    echo "      in the admin page will be unavailable. sudo apt install network-manager" >&2
    helper=no
fi

systemctl daemon-reload
systemctl enable --now viewport.service
if [ "$helper" = yes ]; then
    systemctl enable --now openviewport-hostd.service
fi

if [ "$kiosk" = yes ]; then
    if [ "$(systemctl get-default)" = graphical.target ]; then
        echo "note: this system boots to a desktop. The kiosk wants tty1 to itself, so either" >&2
        echo "      'sudo systemctl set-default multi-user.target', or re-run with --no-kiosk" >&2
        echo "      and open the wall in the desktop's own browser." >&2
    fi
    [ -n "$user" ] || { echo "a user is needed for the kiosk: --user NAME" >&2; exit 1; }
    install_unit viewport-kiosk.service
    systemctl daemon-reload
    systemctl enable --now viewport-kiosk.service
fi

# ---- what to do now -----------------------------------------------------------------------

host=$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo raspberrypi)
cat <<EOF

The viewport is installed and will come back on its own after a reboot.

  The TV        the kiosk is opening the wall now (systemctl status viewport-kiosk)
  The settings  http://$host.local:$port/admin?token=$token

Open that settings link from your phone or laptop and choose a password: until you do, anyone
on your network who has the link can change the settings. The guide there then walks you
through adding your NVR.

  systemctl status viewport         is the stack running
  journalctl -u viewport-kiosk -f   what the browser is doing
  sudo deploy/pi/update.sh          pull a new version and restart

EOF

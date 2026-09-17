# Security

What the viewport protects, how, and how to deploy it safely. Security comes first in this
project: where it and convenience disagree, the default is the safe one.

## What is worth protecting

| Asset | Why it matters |
|---|---|
| NVR credentials | Even a view-only account shows every camera, live, from anywhere it can log in |
| Live video | The whole point of the device, and the most sensitive thing on the network |
| The settings | Whoever can change them can add a source, or point a camera somewhere else |
| The NVR itself | Too many connections can drop the owner's phone app or break recording |

## A safe installation, in short

1. **Set an admin password.** The admin page asks for one first thing (the setup guide), and
   says *Not protected* in its header until there is one.
2. **Set `VIEWPORT_API_TOKEN`** if any screen other than the device itself shows the wall.
3. **Publish only what is needed.** The wall is on `127.0.0.1` unless `VIEWPORT_BIND` says
   otherwise; a TV elsewhere on the LAN needs `VIEWPORT_BIND=0.0.0.0`, and then 1 and 2.
4. **Use HTTPS if the network is not yours alone**: a reverse proxy in front of port 8080
   (below). Never expose port 8080 to the internet.
5. **Use a view-only account** on the NVR.

## Two credentials, for two different things

| | Grants | Travels |
|---|---|---|
| **API token** (`VIEWPORT_API_TOKEN`) | Watching the wall: its state, its video, changing view | In the screen's URL |
| **Admin password** | Changing the settings | Only in the unlock dialog |

They are separate because the token is in the screen's address, so it ends up in browser
history and in any proxy log. Anyone who can read the TV's address must not thereby be able to
reconfigure the wall. An admin session grants what the token grants, so the admin page needs
no token of its own.

### The admin page's lock

Anyone who may watch the wall can *read* the settings: the admin page shows them locked, with
**Unlock** in its header. Changing anything needs the admin password. **Lock** ends the
session again. While locked, the API leaves out usernames and where the settings are kept; it
never returns a password at all. A session that runs out while someone is editing asks for the
password again and then saves what they were saving.

### Where the admin password comes from

- **Chosen in the admin page** (Security, or the setup guide). Only an scrypt hash is kept, in
  `VIEWPORT_SECRETS_FILE`. Changing it needs the current one and ends every other session.
- **Or set in the configuration**: `VIEWPORT_ADMIN_PASSWORD`, or better only its hash,
  `VIEWPORT_ADMIN_PASSWORD_HASH` (`docker compose run --rm --no-deps agent python -m viewport.auth`
  prints one). One set there wins, cannot be changed from the page, and is **how a forgotten
  password is reset**: set it in `.env` and restart.

Until a password exists, choosing the first one needs what changing settings needed until then:
the API token, if one is set. That is trust on first use: someone on the network who reaches
a new installation before its owner could set the password first. It is no weaker than the open
page it replaces, and the owner recovers by setting `VIEWPORT_ADMIN_PASSWORD`.

How the password is protected:

- scrypt (N=16384, r=8, p=1): about 30 ms to check, slow for anyone guessing, unnoticeable to a
  person. Never stored or logged in the clear, never returned.
- A wrong username and a wrong password cost the same and answer the same.
- **Five failures pause that client for a minute**, the right password included. The pause is
  per client, so nobody can lock the owner out by failing on purpose; 30 failures across all
  clients within 15 minutes pause everyone for five minutes, which stops guessing spread over
  many addresses. (Behind a reverse proxy every request comes from the proxy, so the pause is
  effectively everyone's.)
- A session is a signed, HttpOnly, SameSite=strict cookie. Its key is derived from the stored
  hash, so changing the password ends every session, and sessions survive a restart. It is
  marked Secure whenever the page came over HTTPS, directly or through a proxy that says so
  with `X-Forwarded-Proto`.

**The residual risk is the network.** Over plain HTTP the password and the session cookie cross
the LAN readable. Use HTTPS, or treat the network as trusted.

### The API token

Blank by default, which leaves the wall's state and video open to anyone who can reach port
8080. Generate one:

```bash
docker compose run --rm --no-deps agent python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Clients send it as `Authorization: Bearer <token>` or `?token=`. Browsers cannot set headers on
a WebSocket, so the kiosk URL carries it: treat it as a device credential, rotate it if a screen
is replaced, and don't reuse it. Tokens are compared in constant time; uvicorn's access log is off.

Open without a token, by design: the wall page, the admin page and their static files (they
carry no camera data), and `/healthz`, which returns one word for the container healthcheck.

## In the browser

`agent/viewport/security.py` adds defences that hold whether or not a token or password is set.

| | |
|---|---|
| **Content security policy** | Only this origin's own scripts, styles and connections, no plugins, no `<base>`. An injected `<img onerror=…>` runs nothing. |
| **No framing** | `frame-ancestors 'none'` and `X-Frame-Options: DENY`: the admin page cannot be clickjacked. |
| **No referrer** | The wall's URL can carry the token; it is never sent to another site. |
| **No caching of the API** | Its answers carry camera names and, unlocked, usernames. |
| **Nothing from another site** | A web page anyone on the LAN opens can send requests to the device. CORS stops it reading answers, but not a form post, and not a WebSocket at all: it could have watched the video relay whenever no token was set. Changing requests and every WebSocket are refused when the browser says they come from another site (`Sec-Fetch-Site`, else `Origin`). Clients that are not browsers send neither and are unaffected. |
| **DNS rebinding** | A web page can point its own domain at the device's LAN address, which makes it same-origin. Only the names a LAN device is reached by are accepted: IP addresses, `localhost`, single-label names, and `.local`, `.lan`, `.home.arpa`, `.internal`, `.localdomain`. Any other name, such as a reverse proxy's, goes in `VIEWPORT_ALLOWED_HOSTS`. |

Everything the pages draw from data (camera names, errors, URL parameters) is set as text, not
HTML. The one place that builds HTML, the wall's status panel, escapes every value; URL
parameters are checked against the values they may take.

## Containers

`docker-compose.yml` runs every service:

- with a **read-only filesystem** (the agent writes only to its `/data` volume; `/tmp` is a
  `noexec` tmpfs),
- with **no Linux capabilities** and `no-new-privileges`. The mock NVR keeps one,
  `NET_BIND_SERVICE`, to listen on ports 80 and 554, and an executable `/tmp`; it is a test
  double, never part of an installation, and only starts with `COMPOSE_PROFILES=mock`,
- as a **non-root user** where the image allows: the agent as `viewport` (10001), go2rtc as 10001,
- with **capped logs** (3 × 10 MB), which an SD card needs.

**go2rtc is never published**, not even on loopback. Its API has no authentication, answers
with every stream's source URL *including the camera passwords*, serves any camera's video,
and will connect to any URL passed as `src=`; a loopback port is still reachable from a web
page through DNS rebinding. Its config (`config/go2rtc.yaml`) sets no CORS `origin`, so no web
page could read it even if it were reachable. The wall's video goes through the agent instead
(`go2rtc.player_transport: proxy`):

- the browser only connects to the agent, which checks the token;
- the agent relays only streams the wall has assigned, so a client cannot open HD streams on
  its own: the stream budget is enforced on the server;
- go2rtc's messages are scrubbed before they are forwarded.

The relay costs about 4% of one Apple Silicon core for a 4K H.265 stream plus two SD streams.
`player_transport: direct` skips it; use it **only** for a browser on the same device as go2rtc,
with go2rtc on loopback (`config/go2rtc.prod.yaml`).

To look at go2rtc while debugging, ask it from inside its container rather than publishing it:

```bash
docker compose exec go2rtc python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:1984/api/streams').read().decode())"
```

`agent/tests/test_deployment.py` checks all of this in the files that set it, so an edit to the
compose file cannot quietly undo it.

## HTTPS with a reverse proxy

Caddy obtains and renews a certificate by itself. For a device reached as `cams.example.com`:

```caddyfile
cams.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

```bash
VIEWPORT_ALLOWED_HOSTS=cams.example.com
```

Caddy forwards the `Host` and `X-Forwarded-Proto` headers, so the session cookie becomes Secure.
On a LAN without a public name, Caddy's `tls internal` gives a locally trusted certificate.

## Where secrets live

- **`.env`** (keep it `0600`; git-ignored, and excluded from every image build), read by Compose;
- or **files**, through the `NAME_FILE` convention of Docker and Kubernetes secrets: every
  `${NAME}` in `config/viewport.yaml` accepts `NAME_FILE` instead;
- **`VIEWPORT_SECRETS_FILE`** (`/data/secrets.json`, mode `0600`): credentials for sources added
  in the admin page, and the hash of an admin password chosen there.

```yaml
# docker-compose.secrets.yml, added to COMPOSE_FILE in .env
services:
  agent:
    environment:
      NVR_PASSWORD_FILE: /run/secrets/nvr_password
      VIEWPORT_API_TOKEN_FILE: /run/secrets/viewport_token
    secrets: [nvr_password, viewport_token]
secrets:
  nvr_password:
    file: ./secrets/nvr_password.txt     # chmod 600, and git-ignore the folder
  viewport_token:
    file: ./secrets/viewport_token.txt
```

A trailing newline in such a file is ignored; one that cannot be read stops the agent at
startup rather than silently using a blank password.

The agent never writes a secret anywhere else:

- `config/viewport.yaml` is mounted read-only and never rewritten.
- Admin-page edits go to `VIEWPORT_STATE_FILE`, which holds settings only. A stream address
  typed into an RTSP source is refused if it contains a username or password, since addresses
  are saved there; the credentials have fields of their own.
- Streams are registered in go2rtc with `PATCH`, which keeps them in memory; `PUT` would write
  them, credentials included, into `go2rtc.yaml`.

[Handling credentials](credentials.md) explains the design and what it does not protect against.

## Keeping secrets out of output

Credentials leak through error messages far more often than through code that handles them on
purpose. Four such leaks were found and closed, each with a regression test: httpx error
messages quoting request URLs; go2rtc sending a source URL, password included, to the browser,
which drew it on the TV; httpx logging every request URL; and go2rtc logging source URLs.

The defences, in `agent/viewport/credentials.py`:

| | |
|---|---|
| `redact(text)` | Removes credentials from URLs, query parameters, percent-encoded URLs inside URLs, and JSON-escaped text |
| Registered secrets | Every password and token in the config is scrubbed in each encoding it could appear in |
| `safe_error(exc)` | Used instead of `str(exc)`: an HTTP error becomes `HTTP 500 from PATCH /api/streams` |
| Log records | Scrubbed when created, so libraries' loggers are covered too; `RedactingFormatter` covers tracebacks |
| go2rtc's output | Piped through the same file in `docker-compose.yml` |
| The browser | `web/redact.js` scrubs player errors on arrival; a tile only ever says "Reconnecting…" |

`agent/tests/redaction_cases.json` holds cases the Python and JavaScript redactors must both pass.

## Checked, and still open

A review in September 2026 found and fixed: go2rtc's API published with CORS open to every
origin (any web page on the developer's machine could read the NVR password); a script injection
through the wall's `?mode=` parameter; no defence against cross-site requests, WebSocket
hijacking or DNS rebinding; a login lockout that let anyone lock the owner out; credentials
saved inside RTSP stream addresses; containers with writable filesystems and full capabilities.

Still open, and known:

- **Plain HTTP** unless you add a proxy (above).
- **Trust on first use** for the first admin password (above).
- **Secrets at rest are not encrypted**: an appliance that starts unattended must be able to read
  them, so anyone with the disk can too ([credentials](credentials.md)).
- **go2rtc's RTSP re-stream** inside the compose network needs no authentication; only the
  project's own containers are on that network.
- **Base images** are not scanned for vulnerabilities in CI; `audit` covers the Python dependencies.

## Reporting a problem

Until the project has a public home, report security issues privately to the maintainer rather
than in a public issue. Include the version (`/api/health`) and redact credentials from any logs
you attach; `docker compose logs` is already scrubbed.

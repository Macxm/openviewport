# Building and contributing

## The repository

```
agent/            the Python agent and the browser pages
  viewport/         adapters/, budget.py, focus.py, wall.py, media.py, api.py,
                    auth.py, security.py, credentials.py, store.py, secretstore.py
  viewport/web/     the wall and the admin page: plain ES modules, no build step
  tests/            pytest, the shared redaction cases, and JS tests run by `lint`
mock-nvr/         a fake Reolink NVR: the test double for real hardware
config/           viewport.yaml, go2rtc.yaml (compose), go2rtc.prod.yaml (no Docker)
scripts/          e2e.sh (the regression suite), e2e_check.py (smoke check), dev_native.py
tests/            the test image, and tests/e2e: the browser regression suite
docs/             this documentation
docker-compose.yml       the stack, its test services, and the regression suite's own stack
docker-compose.dev.yml   conveniences for working on the code (COMPOSE_FILE in .env)
```

## Everything runs in containers

No Python, Node or browser is needed on your machine, and CI runs the same commands:

```bash
docker compose --profile test run --rm unit     # unit and API tests (about 12 s)
docker compose --profile test run --rm lint     # JS syntax and tests, YAML, redaction parity
docker compose --profile test run --rm audit    # pinned dependencies vs advisories (needs network)
docker compose --profile test run --rm zizmor   # the GitHub Actions workflows, audited
sh scripts/e2e.sh                               # the browser regression suite (about 2 min)
```

Extra arguments **replace** a service's `command`, so `unit` needs its path repeating:

```bash
docker compose --profile test run --rm unit agent/tests -k budget
docker compose --profile test run --rm unit agent/tests --durations=5
sh scripts/e2e.sh -k setup_guide -x            # pytest arguments pass straight through
```

Without `agent/tests`, pytest collects from `/repo`, misses `agent/pyproject.toml`, and every
async test fails with "async functions are not natively supported".

### The browser regression suite

`sh scripts/e2e.sh` runs `tests/e2e` against a stack of its own, as a separate compose project
(`viewport-e2e`) that it removes again afterwards, pass or fail. It cannot touch a running wall,
a real NVR or your settings, and nothing in it is published. The stack is:

| | |
|---|---|
| `e2e-nvr` | the mock NVR: four cameras, channel 3 offline |
| `e2e-agent` | set up like a real installation: `config/viewport.yaml`, a token, an admin password |
| `e2e-agent-fresh` | a first run: no sources, no token, no password, for the setup guide |

The tests drive Firefox with real MSE video and cover the wall, HD full screen, the relay, the
token, detection, the admin lock, camera settings and sources as the wall sees them, the setup
guide from nothing to a working wall, the admin page on a phone, and the HTTP defences. A test
that changes a setting on `e2e-agent` puts it back (the `restore` fixture), so the order does not
matter. A page that throws a script error fails its test, and a failing test leaves a screenshot
in `e2e-output/`.

**Why Firefox.** MSE needs a browser that decodes H.264/H.265. Playwright's bundled Chromium has
neither, on any platform; its Firefox and WebKit have both, including on arm64 Linux.

### Checking a running installation

`docker compose --profile test run --rm smoke` checks the wall that is running now, whatever it
points at (your real NVR included): it plays, full screen switches to HD, the relay carries all
video, the admin page loads and fits a phone. It briefly changes what the shared wall shows, and
writes nothing. `python scripts/e2e_check.py --channel chrome` runs the same check with a Chrome
installed on the host.

### Continuous integration

Every pull request runs the checks above, plus GitHub's dependency review, which refuses a
change that adds a dependency with a known vulnerability. The workflows are kept hardened, and
`zizmor` fails the build if an edit undoes any of it:

- every action is pinned to a full commit SHA, with its version in a comment; Dependabot moves
  both together. A tag can be re-pointed at different code, a commit cannot;
- the token can only read the repository, and checkout does not leave it in `.git/config`;
- jobs have time limits, and a newer push to a pull request cancels the older run.

## Developing

With `.env` from `.env.dev.example` (the mock NVR and the bind-mounted pages; `.env.example`
is a real installation), `docker compose up -d --build` runs the whole stack against the
mock NVR, and `docker-compose.dev.yml` bind-mounts `agent/viewport/web`, so editing the wall or the
admin page only needs a browser reload. Python changes need `docker compose up -d --build agent`.

The admin page is three modules: `admin-logic.js` holds the rules that need no browser (joining
and splitting a layout's cells, unsaved-change tracking, describing streams), tested in node by
`lint`; `admin-ui.js` builds rows, switches and icons; `admin.js` is the page.

The pages run under a strict content security policy: no inline scripts or styles, no `eval`,
and connections only to their own origin. Set text with `textContent`, never `innerHTML` with
data in it.

Development machines are not appliances, so the General section's network and power controls
would have nothing to drive. `.env.dev.example` sets `VIEWPORT_HOST_FAKE=1`, which stands in a
device that exists only in memory: joining a network, a refused password, restarting and
resetting all behave, and the page says plainly that they are simulated. Adding
`VIEWPORT_HOST_FAKE_LINK=none` starts it with no network at all, which is what brings up the
setup screen on the wall.

The mock NVR can be told to behave like awkward hardware:

```bash
MOCK_MAIN_CODEC=h265        # H.265 main streams
MOCK_CHANNELS=16
MOCK_OFFLINE_CHANNELS=3     # a named camera that is down
MOCK_EMPTY_CHANNELS=5,6     # unused NVR slots, as a real NVR reports them
MOCK_HTTP_ENABLED=false     # an HTTPS-only NVR, so no HTTP-FLV
```

and to fake a detection:

```bash
curl -X POST 'localhost:8081/mock/detect?channel=1&kind=people&seconds=10'
```

## Dependencies

Runtime dependencies are hash-pinned in `agent/requirements.lock.txt` and installed in their
own image layer. Add the range to `agent/pyproject.toml`, then regenerate the lock with the
command in `agent/Dockerfile`. Never import a package that only arrives through another
package's extra. Dependabot proposes updates weekly.

## House rules

They exist because breaking them breaks someone's recorder or leaks their password:

- Stream selection lives in `budget.py` and `wall.py`. Renderers report; they never choose.
- Everything that opens a camera connection goes through go2rtc, which is never published.
- Never log or return credentials: `credentials.redact()` and `safe_error()`.
- New `/api/*` routes go on the authenticated routers; anything that changes settings on the
  admin one.
- `config/viewport.yaml` is never rewritten; runtime edits go to the state file, which must
  never hold credentials.
- The pages stay dependency-free: plain ES modules, no build step, nothing inline.
- Every service in `docker-compose.yml` stays read-only, capability-free and unpublished unless
  it must not be; `test_deployment.py` holds you to it.
- Every behaviour change comes with a test.

## Before opening a pull request

```bash
docker compose --profile test run --rm unit
docker compose --profile test run --rm lint
sh scripts/e2e.sh
```

If you touched anything that talks to a device, say which hardware and firmware you tested
against, and add a page to `docs/hardware/` if it is a new one.

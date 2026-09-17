# Tests

Everything runs in containers; nothing needs Python, Node or a browser on the host.

```bash
docker compose --profile test run --rm unit     # unit and API tests
docker compose --profile test run --rm lint     # JS syntax and tests, YAML, redaction parity
docker compose --profile test run --rm audit    # pinned dependencies vs advisories (needs network)
sh scripts/e2e.sh                               # the browser regression suite, on its own stack
docker compose --profile test run --rm smoke    # a quick check of the wall that is running now
```

- `unit` is self-contained (`agent/tests`), and so is `lint`.
- `scripts/e2e.sh` runs `tests/e2e` against a throwaway stack of its own: a mock NVR and two
  agents in a separate compose project, nothing published, removed afterwards. It never touches
  a running wall or a real NVR. See [building](../docs/building.md#the-browser-regression-suite).
- `smoke` runs `scripts/e2e_check.py` against the stack started by `docker compose up`,
  whatever it is pointed at. It briefly changes what the wall shows and writes nothing.

Extra arguments replace a service's `command`: `run --rm unit agent/tests -k budget`, and
`sh scripts/e2e.sh -k setup_guide -x` passes them on to pytest.

## Why Firefox

MSE playback needs a browser that can decode H.264/H.265. Playwright's bundled **Chromium has
neither** on any platform; its **Firefox and WebKit do**, including on arm64 Linux. So the
suite exercises real MSE, the same path the wall uses on a TV (`E2E_BROWSER=webkit` switches).

The smoke check can also run against a Chrome installed on a Mac, outside Docker, for anyone who
wants to confirm Chrome specifically: `python scripts/e2e_check.py --channel chrome` with
Playwright installed. It is not part of the normal workflow.

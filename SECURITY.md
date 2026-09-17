# Security policy

This software sits on a network with cameras and a recorder, and holds the credentials to
both. Security reports are welcome and taken seriously.

## Reporting a vulnerability

**Please do not open a public issue.** Use GitHub's private vulnerability reporting: the
*Security* tab → *Report a vulnerability*. That reaches the maintainer and nobody else.

Useful in a report:

- what version (`/api/health` gives it) and how it is deployed (Docker, the Pi installer, bare)
- what an attacker needs to start with: on the same network, a link someone clicks, an account
- what they end up with
- logs if they help — `docker compose logs` is already scrubbed of credentials, but check
  anything you paste

You will get an acknowledgement within a few days. Since this is one person's project rather
than a company, please allow reasonable time for a fix before publishing.

## What the project already tries to defend against

[docs/security.md](docs/security.md) is the threat model: what is defended, how, and — in
*Checked, and still open* — what is knowingly not. In short: go2rtc is never published because
its API hands out camera passwords; credentials never reach logs, API responses, the settings
file or the screen; the pages run under a strict content security policy and refuse cross-site
requests, WebSocket hijacking and DNS rebinding; containers are read-only and without
capabilities; changing settings needs an admin password, and watching can need a token.

Known and deliberate, so not vulnerabilities in themselves: plain HTTP unless you put a proxy
in front of it, the first admin password being whoever sets it first, and secrets stored
unencrypted so the device can boot unattended. A way to *exploit* one of those beyond what is
documented is very much worth reporting.

## Out of scope

Weaknesses of the cameras or the NVR themselves — report those to their vendor. Anything that
needs an account on the box already, or physical access to the SD card.

# Handling credentials

How camera and NVR passwords get into the viewport, what protects them, and what does not.
This is the design behind adding a source from the admin page.

## What we have to hold

A camera password, per source. The agent needs it **at every startup, unattended**: an
appliance on a shelf must come back after a power cut without anyone typing anything. That
one requirement rules out most of the obvious ideas, so it is worth stating first.

## The options, and why

| Option | Verdict |
|---|---|
| **`.env` / `NAME_FILE` only** (today) | Safe and simple, but every new camera means editing a file and restarting. Kept, but not enough on its own |
| **Secrets file the agent owns** | Chosen. Mode `0600`, separate from the settings file, never returned by the API |
| Encrypted with a key beside it | Theatre. The agent must decrypt unattended, so the key has to sit next to the data; it stops nobody who can read the file |
| Encrypted with the admin password | Real protection, but the agent could not start without someone typing the password. Wrong trade for an appliance |
| OS keyring | No usable keyring in a headless container; needs a session to unlock |
| A secrets manager (Vault, cloud KMS) | Right answer at fleet scale, far too much for one box on a shelf. The `NAME_FILE` support already allows it for anyone who has one |
| Per-source tokens instead of passwords | Would be better, but NVRs do not offer them. Nothing to use |

**Honest summary: on a single appliance, a password the agent can use unattended is a
password an attacker with the disk can read.** What is actually achievable is to keep it out
of everywhere it does not need to be, and to make the blast radius small.

## What the design does

- **A dedicated file**, `VIEWPORT_SECRETS_FILE` (`/data/secrets.json` under Compose), written
  `0600`, atomically, separate from the settings file so that settings can be copied,
  backed up or shared without carrying passwords.
- **Write-only through the API.** A password can be set from the admin page; it is never
  returned. `/api/config` reports `"password_set": true` and nothing more.
- **Never logged.** Every password loaded is registered with the redactor, so it cannot
  appear in a log line, an error, an API response or on the TV.
- **Never in git.** The file lives on a Docker volume; `.env` and `secrets.json` are ignored.
- **A password saved in the admin page wins over the environment**, so that setting one
  there visibly takes effect. Clearing it falls back to `NVR_PASSWORD` or `NVR_PASSWORD_FILE`,
  so anyone with a real secrets manager can still use it.
- **Not in stream addresses.** An RTSP source's addresses are saved with the settings, so an
  address with `user:password@` in it is refused; the Username and Password fields go here.
- **Admin credentials are needed to set one**, and adding a source is exactly as privileged
  as changing the stream budget.
- **The admin password, when chosen in the admin page, lives here too**: only its scrypt hash,
  under a key (`_admin`) that can never be a source's name. One set in the environment wins,
  which is how a forgotten password is reset.

## What this does not protect against, honestly

| Threat | Reality |
|---|---|
| Someone with the device's disk, or a backup of it | They can read the passwords. Use full-disk encryption if the device can be stolen |
| Someone with the admin password | Can point a source anywhere, but **cannot read existing passwords back** — they are write-only |
| Someone on the network | The admin page is plain HTTP by default, so a password, a camera's or the admin's, crosses the LAN readable while being set. Put it behind HTTPS ([security](security.md)) |
| A compromised agent process | Has the passwords by definition; it needs them to connect |
| A backup of the Docker volume | Contains `secrets.json`. Treat volume backups as secret |

## Rules for the code

- Passwords live in `SourceConfig.password` in memory, in the secrets file at rest, and
  nowhere else. Never in `viewport.yaml`, never in the settings state file.
- `credentials.register_secret()` on every password read, at startup and whenever one is set.
- The API never returns a password; `password_set` is the only thing that leaves the agent.
- A source's password is removed with the source.

## If this becomes a product

Worth doing later, roughly in order of value:

1. **HTTPS by default**: a certificate generated on first run, so the admin password and
   camera passwords are not sent in the clear on the LAN. Until then, a reverse proxy
   ([security](security.md)).
2. **Per-source accounts** on the NVR, so one leaked password exposes one camera.
3. **A read-only audit trail** of configuration changes: who added what source, when.
4. A `secrets manager` backend behind the same interface, for anyone running a fleet.

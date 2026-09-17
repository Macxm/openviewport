---
name: Hardware report
about: An NVR or camera that works, half-works, or does something unexpected
title: "[hardware] "
labels: hardware
---

**The device**

- Model:
- Firmware:
- Cameras attached, and their resolution/codec if you know:
- Which adapter you used: `reolink` / `onvif` / `rtsp`

**What happened**

What you expected, and what you got instead.

**How it was set up**

- Version (`curl localhost:8080/healthz` and the *About* line, or the commit):
- Docker, the Pi installer, or something else:
- Anything non-default in `.env` or the admin page:

**Logs**

`docker compose logs agent --tail 50` — already scrubbed of credentials, but do check what you
paste. `/api/health` and `/api/cameras` are usually the most telling.

**Anything else**

Whether the vendor's own app still worked at the same time is useful to know: the whole design
exists to keep it working.

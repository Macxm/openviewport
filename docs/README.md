# Documentation

An open-source live-view box for IP cameras and NVRs: a small Linux computer drives a TV
with a wall of camera tiles, without overloading the recorder.

| Page | What it covers |
|---|---|
| [Installation](installation.md) | Running it with Docker, pointing it at your NVR, and setting up a screen |
| [Raspberry Pi](raspberry-pi.md) | The appliance: services at boot, a kiosk on the TV, settings from your phone |
| [Configuration](configuration.md) | Every setting: `.env`, `viewport.yaml`, and the admin page |
| [Supported hardware](hardware/README.md) | Which cameras and NVRs work, which adapter to choose, tested devices |
| [Architecture](architecture.md) | How it works inside, and why it is built this way |
| [API](api.md) | HTTP and WebSocket reference for building your own renderer or automation |
| [Adding a vendor](adding-a-vendor.md) | Writing a source adapter for a device nobody has covered |
| [Building and contributing](building.md) | Working on the code: the repo, unit tests, the browser regression suite |
| [Security](security.md) | A safe installation in five steps, the admin lock, browser and container defences, what is still open |
| [Handling credentials](credentials.md) | Why passwords are stored the way they are, and what that does not protect against |
| [Troubleshooting](troubleshooting.md) | When a tile is black, the NVR complains, or nothing plays |

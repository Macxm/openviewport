# Contributing

Thanks for looking. The most useful contribution is a **hardware report**: what NVR or camera
you have, its firmware, and what it did — especially when it did something the code did not
expect. There is an issue template for it.

## Before you start

- [Building](docs/building.md) covers the repository, the containerised tests and the house
  rules. The rules are short, and each one exists because breaking it breaks someone's
  recorder or leaks their password.
- [Adding a vendor](docs/adding-a-vendor.md) is the guide for making a new brand work.
- [Architecture](docs/architecture.md) explains why the agent, not the renderer, decides which
  stream each tile plays. That one is load-bearing.

## The checks

Everything runs in containers; nothing needs Python, Node or a browser on your machine:

```bash
docker compose --profile test run --rm unit
docker compose --profile test run --rm lint
sh scripts/e2e.sh
```

Please add or adjust a test with every behaviour change. The mock NVR (`mock-nvr/`) is the test
double for real hardware, and it can be told to misbehave like the real thing.

If you touched anything that talks to a device, say which hardware and firmware you tested
against, and add a page to `docs/hardware/` if it is a new one.

## Security

Do not open a public issue for a vulnerability — [SECURITY.md](SECURITY.md) says how to report
one privately.

## Licence

Contributions are accepted under the [AGPL-3.0](LICENSE), the same licence as the project.

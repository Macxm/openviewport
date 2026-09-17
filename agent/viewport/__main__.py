"""Run the agent: python -m viewport"""

from __future__ import annotations

import logging
import os

import uvicorn

from .api import create_app
from .config import load_config, register_config_secrets
from .credentials import RedactingFormatter, install_log_redaction
from .runtime import Runtime
from .secretstore import SecretStore
from .store import ConfigStore


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    install_log_redaction()
    # httpx logs every request URL at INFO. Redaction covers it, but it is noise anyway.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = ConfigStore()
    secrets_store = SecretStore()
    # Overlay anything saved from the admin page on top of the declared YAML, then the
    # credentials, which live apart from the settings (see docs/credentials.md).
    config = store.apply(load_config())
    secrets_store.apply(config.sources)
    register_config_secrets(config)
    app = create_app(config, Runtime(config, store=store, secrets_store=secrets_store))
    uvicorn.run(app, host=os.environ.get("VIEWPORT_HOST", "0.0.0.0"),
                port=int(os.environ.get("VIEWPORT_PORT", "8080")), log_level="warning")


if __name__ == "__main__":
    main()

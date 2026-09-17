"""Camera passwords at rest.

Kept in their own file, separate from the settings, so settings can be copied, backed up or
shared without carrying credentials. Written 0600 and never returned by the API: a password
can be set from the admin page but not read back.

The reasoning, and what this does and does not protect against, is in docs/credentials.md.
The short version: an appliance must start unattended, so it must be able to read these
without anyone typing anything — which means anyone with the disk can read them too. What is
achievable is keeping them out of everywhere they do not need to be.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .credentials import register_secret

log = logging.getLogger(__name__)

# The admin page's login, when its password was chosen there. A source id can never start
# with an underscore, so this key cannot collide with a source.
ADMIN_KEY = "_admin"


class SecretStore:
    """Per-source credentials, by source id, and the admin login set from the page."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        raw = str(path if path is not None else os.environ.get("VIEWPORT_SECRETS_FILE", "")).strip()
        self.path: Path | None = Path(raw) if raw else None

    @property
    def writable(self) -> bool:
        if self.path is None:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return os.access(self.path.parent, os.W_OK)
        except OSError:
            return False

    def _read(self) -> dict[str, Any]:
        """The whole file, as it is. Writers start from this, so nothing they do not touch is lost."""
        if self.path is None or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            # Never fatal: a viewport that will not start is worse than one asking for a
            # password again.
            log.warning("ignoring unreadable secrets file %s: %s", self.path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def load(self) -> dict[str, dict[str, str]]:
        """Credentials by source id."""
        out = {}
        for source_id, entry in self._read().items():
            if isinstance(entry, dict) and not str(source_id).startswith("_"):
                out[str(source_id)] = {"username": str(entry.get("username", "")),
                                       "password": str(entry.get("password", ""))}
                register_secret(out[source_id]["password"])
        return out

    def admin_login(self) -> tuple[str, str] | None:
        """(username, password hash) chosen in the admin page, if any. Only a hash is kept."""
        entry = self._read().get(ADMIN_KEY)
        if not isinstance(entry, dict):
            return None
        username, password_hash = str(entry.get("username") or "admin"), str(entry.get("password_hash") or "")
        if not password_hash.startswith("scrypt$") or password_hash.count("$") != 5:
            if password_hash:
                log.warning("ignoring the admin password in %s: not a hash this agent made", self.path)
            return None
        return username, password_hash

    def set_admin_login(self, username: str, password_hash: str) -> None:
        data = self._read()
        data[ADMIN_KEY] = {"username": username, "password_hash": password_hash}
        self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        if self.path is None:
            raise RuntimeError("no secrets file configured (set VIEWPORT_SECRETS_FILE)")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def set_source(self, source_id: str, username: str | None, password: str | None) -> None:
        """Store credentials for one source. None leaves that field as it was."""
        data = self._read()
        stored = data.get(source_id)
        entry = ({"username": str(stored.get("username", "")), "password": str(stored.get("password", ""))}
                 if isinstance(stored, dict) else {"username": "", "password": ""})
        if username is not None:
            entry["username"] = username
        if password is not None:
            entry["password"] = password
            register_secret(password)
        data[source_id] = entry
        self._write(data)

    def forget_source(self, source_id: str) -> None:
        data = self._read()
        if data.pop(source_id, None) is not None:
            self._write(data)

    def apply(self, sources: list) -> None:
        """Overlay stored credentials onto the configured sources.

        A password set in the admin page wins over the environment, so that setting one
        there visibly takes effect. Clearing it falls back to the environment.
        """
        stored = self.load()
        for source in sources:
            entry = stored.get(source.id)
            if not entry:
                continue
            if entry.get("username"):
                source.username = entry["username"]
            if entry.get("password"):
                source.password = entry["password"]

"""Admin authentication.

Two credentials, because they protect different things:

* `auth.token` lets a screen watch the wall — read state, play video, change view. It
  travels in the kiosk URL, so it ends up in browser history and any proxy log.
* the **admin password** lets someone change the configuration. It is never in a URL.

An admin session implies the token's rights, so the admin page needs no token.

The password is stored as an scrypt hash, never in the clear. A session is a signed,
HttpOnly cookie whose key is derived from that hash, so changing the password invalidates
every session, and sessions survive a restart — a kiosk should not need re-authenticating
because the agent was updated.

Standard library only, and no new dependency for hashing.

**The residual risk is the network.** Over plain HTTP the password and the session cookie
cross the LAN readable. Put the agent behind HTTPS, or trust the network. See
docs/security.md.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

log = logging.getLogger(__name__)

COOKIE = "viewport_admin"
# scrypt at these parameters costs ~100 ms and 16 MB, which is slow for an attacker with
# the hash and unnoticeable on a login.
_N, _R, _P, _DKLEN, _SALT = 2**14, 8, 1, 32, 16
# After this many failures from one client, that client pauses, so a password cannot be
# ground down. The pause is per client: otherwise anyone on the network could keep the owner
# locked out just by failing five times a minute.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 60.0
ATTEMPT_WINDOW_SECONDS = 900.0
# And a ceiling across all clients, so guessing spread over many addresses stops too.
GLOBAL_MAX_ATTEMPTS = 30
GLOBAL_LOCKOUT_SECONDS = 300.0
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r),
                                  p=int(p), dklen=len(_unb64(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, _unb64(digest))


class AdminAuth:
    """Who may change the configuration."""

    def __init__(self, username: str, password_hash: str, session_hours: float = 12.0,
                 source: str | None = "config"):
        self.username = username or "admin"
        self.password_hash = password_hash
        self.session_hours = session_hours
        # Where the password came from: "config" (VIEWPORT_ADMIN_PASSWORD or viewport.yaml,
        # which the admin page cannot change) or "page" (chosen there). None: no password.
        self.source = source if password_hash else None
        self._failures: dict[str, list[float]] = {}     # client -> when it failed
        self._locked_until: dict[str, float] = {}       # client -> paused until
        self._all_failures: list[float] = []
        self._all_locked_until = 0.0

    @property
    def required(self) -> bool:
        """False means no admin password is set and the API token alone is enough."""
        return bool(self.password_hash)

    def set_password(self, username: str, password_hash: str) -> None:
        """A password chosen in the admin page. Every existing session ends with the old one."""
        self.username = username or "admin"
        self.password_hash = password_hash
        self.source = "page"
        self._failures.clear()
        self._locked_until.clear()
        self._all_failures.clear()
        self._all_locked_until = 0.0

    # ----- logging in ------------------------------------------------------

    def locked_for(self, client: str = "") -> float:
        """Seconds before `client` may try again: its own pause, or everyone's."""
        now = time.monotonic()
        return max(0.0, self._locked_until.get(client, 0.0) - now, self._all_locked_until - now)

    def check_login(self, username: str, password: str, client: str = "") -> bool:
        """Verify a login, counting failures per client. Always hashes, so a wrong username
        and a wrong password take the same time and neither can be told from the other."""
        if self.locked_for(client):
            return False
        reference = self.password_hash or hash_password(secrets.token_urlsafe(16))
        ok = verify_password(password, reference)
        ok = ok and hmac.compare_digest(username.strip(), self.username)
        self._record(ok, client)
        return ok

    def _record(self, ok: bool, client: str) -> None:
        now = time.monotonic()
        if ok:
            self._failures.pop(client, None)
            return
        # Forget what is too old to matter, so these maps cannot grow for as long as it runs.
        self._failures = {c: kept for c, times in self._failures.items()
                          if (kept := [t for t in times if now - t < ATTEMPT_WINDOW_SECONDS])}
        self._locked_until = {c: until for c, until in self._locked_until.items() if until > now}
        self._all_failures = [t for t in self._all_failures if now - t < ATTEMPT_WINDOW_SECONDS]

        failures = self._failures.setdefault(client, [])
        failures.append(now)
        self._all_failures.append(now)
        if len(failures) >= MAX_ATTEMPTS:
            self._locked_until[client] = now + LOCKOUT_SECONDS
            del self._failures[client]
            log.warning("admin login paused for %.0f s for %s after %d failed attempts",
                        LOCKOUT_SECONDS, client or "a client", MAX_ATTEMPTS)
        if len(self._all_failures) >= GLOBAL_MAX_ATTEMPTS:
            self._all_locked_until = now + GLOBAL_LOCKOUT_SECONDS
            self._all_failures.clear()
            log.warning("admin login paused for everyone for %.0f s after %d failed attempts "
                        "from several clients", GLOBAL_LOCKOUT_SECONDS, GLOBAL_MAX_ATTEMPTS)

    # ----- sessions --------------------------------------------------------

    def _key(self) -> bytes:
        # Derived from the stored hash: changing the password ends every session.
        return hashlib.sha256(b"viewport-session:" + self.password_hash.encode()).digest()

    def issue_session(self) -> str:
        payload = json.dumps({"u": self.username,
                              "exp": time.time() + self.session_hours * 3600}).encode()
        signature = hmac.new(self._key(), payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(signature)}"

    def session_valid(self, cookie: str | None) -> bool:
        if not cookie or not self.required:
            return False
        try:
            raw, signature = cookie.split(".", 1)
            payload = _unb64(raw)
            expected = hmac.new(self._key(), payload, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _unb64(signature)):
                return False
            return float(json.loads(payload)["exp"]) > time.time()
        except (ValueError, TypeError, KeyError):
            return False


def admin_from_config(auth, secrets=None) -> AdminAuth:
    """Build the admin check, hashing a plaintext password if one was supplied.

    A password given as VIEWPORT_ADMIN_PASSWORD is hashed here and the plaintext is not
    kept; VIEWPORT_ADMIN_PASSWORD_HASH avoids it being in the environment at all. Either
    wins over one chosen in the admin page, which is how a forgotten password is recovered.
    """
    password_hash = auth.admin_password_hash
    if not password_hash and auth.admin_password:
        password_hash = hash_password(auth.admin_password)
    if password_hash:
        return AdminAuth(auth.admin_username, password_hash, auth.session_hours, source="config")
    stored = secrets.admin_login() if secrets is not None else None
    if stored:
        username, stored_hash = stored
        return AdminAuth(username, stored_hash, auth.session_hours, source="page")
    return AdminAuth(auth.admin_username, "", auth.session_hours, source=None)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


if __name__ == "__main__":     # a hash to paste into VIEWPORT_ADMIN_PASSWORD_HASH
    import getpass
    print(hash_password(getpass.getpass("Admin password: ") or os.environ.get("PASSWORD", "")))

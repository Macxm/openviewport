"""Credentials: read them from files, and keep them out of everything we print.

Standard library only, on purpose: the same file runs inside the go2rtc container as
a log filter (go2rtc prints source URLs, passwords included, when a stream fails):

    go2rtc -config go2rtc.yaml 2>&1 | python3 -u credentials.py

`env_value()` supports the NAME_FILE convention of Docker and Kubernetes secrets, so a
password never has to live in an environment variable or in .env.

`redact()` removes credentials from arbitrary text. They turn up in more shapes than a
plain URL: as query parameters, percent-encoded when one URL travels inside another
(go2rtc's `src=`), with the password itself encoded a second time, and JSON-escaped.
Every secret the agent actually holds is also registered, so it is scrubbed in any of
those encodings even where no pattern would have caught it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus

MASK = "***"

# Parameter names whose values are secrets. Over-matching (e.g. "bypass=") only hides a
# harmless value; under-matching leaks a password, so there is no word boundary.
_KEYS = r"(?:password|passwd|pwd|pass|token|secret|api_key|apikey)"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # scheme://user:password@host  — greedy to the last "@" before the path, so a
    # password with a stray unencoded "@" is not half-revealed.
    (re.compile(r"(?P<pre>[a-z][a-z0-9+.\-]*://)[^/\s\"'<>]*@", re.IGNORECASE), r"\g<pre>" + MASK + "@"),
    # ?password=... &token=...  (value ends at &, whitespace, a quote or a backslash)
    (re.compile(r"(?P<key>" + _KEYS + r"=)[^&\s\"'\\#<>]*", re.IGNORECASE), r"\g<key>" + MASK),
    # The same two, percent-encoded inside another URL: rtsp%3A%2F%2Fuser%3Apass%40host
    (re.compile(r"(?P<pre>%3A%2F%2F)(?:(?!%2F)[^\s&\"'/])*%40", re.IGNORECASE), r"\g<pre>" + MASK + "%40"),
    (re.compile(r"(?P<key>" + _KEYS + r"%3D)(?:(?!%26)[^\s&\"'\\])*", re.IGNORECASE), r"\g<key>" + MASK),
]

# Exact secret values the agent holds, in every encoding they might be printed in.
_known: set[str] = set()


def register_secret(value: str | None) -> None:
    """Remember a secret so `redact()` scrubs it wherever it appears.

    Values shorter than 4 characters are ignored: replacing them everywhere would mangle
    ordinary text, and such a password offers no protection anyway.
    """
    if not value or len(value) < 4:
        return
    once = quote(value, safe="")
    for variant in (value, once, quote(once, safe=""), quote_plus(value), json.dumps(value)[1:-1]):
        if len(variant) >= 4:
            _known.add(variant)


def forget_secrets() -> None:
    """For tests."""
    _known.clear()


def redact(text: str | None) -> str:
    """Hide credentials in any text before it is logged, stored or returned."""
    if not text:
        return text or ""
    # Longest first, so a secret's encoded form is not half-replaced by a shorter one.
    for secret in sorted(_known, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, MASK)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def safe_error(exc: BaseException) -> str:
    """A one-line description of an exception that is safe to store, log and return.

    httpx puts the full request URL in its messages, and ours carry credentials (the
    go2rtc `src=` parameter) or session tokens (the Reolink `token=`). For an HTTP status
    error, report the status and the path only; for anything else, the first line.
    """
    response = getattr(exc, "_response", None) or _safe_attr(exc, "response")
    request = getattr(exc, "_request", None) or _safe_attr(exc, "request")
    if response is not None and request is not None:
        return redact(f"HTTP {response.status_code} from {request.method} {request.url.path}")
    first_line = str(exc).splitlines()[0] if str(exc) else ""
    return redact(first_line or exc.__class__.__name__)


def _safe_attr(obj: object, name: str) -> object:
    # httpx raises RuntimeError, not AttributeError, for an unset .request/.response.
    try:
        return getattr(obj, name, None)
    except RuntimeError:
        return None


def env_value(name: str) -> str | None:
    """`NAME` from the environment, else the contents of the file named by `NAME_FILE`.

    A trailing newline is dropped (secret files usually have one); other whitespace is
    kept, since it can be part of a password. A NAME_FILE that cannot be read is an
    error rather than a silent blank, so a mis-mounted secret fails loudly at startup.
    """
    value = os.environ.get(name)
    if value:
        return value
    path = os.environ.get(f"{name}_FILE")
    if not path:
        return None
    try:
        return Path(path).read_text().rstrip("\r\n")
    except OSError as exc:
        raise RuntimeError(f"{name}_FILE={path} cannot be read: {exc.strerror}") from None


def install_log_redaction() -> None:
    """Scrub credentials from every log record, from every library, before any handler.

    A log record factory rather than a handler filter: it also covers loggers we don't
    own (httpx logs each request URL at INFO, go2rtc's `src=` password included) and
    handlers added later. Tracebacks are rendered after the record exists, so handlers
    should also use `RedactingFormatter`.
    """
    if getattr(logging.getLogRecordFactory(), "_redacts", False):
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:        # a malformed record: let logging report it as usual
            return record
        record.msg, record.args = redact(message), ()
        return record

    factory._redacts = True      # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


class RedactingFormatter(logging.Formatter):
    """A formatter whose output, tracebacks included, has credentials removed."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def _filter_stream() -> int:
    """Copy stdin to stdout with credentials removed, line by line."""
    try:
        for line in sys.stdin:
            sys.stdout.write(redact(line))
            sys.stdout.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(_filter_stream())

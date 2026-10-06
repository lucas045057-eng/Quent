"""Structured, secret-redacting logging setup."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit


_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret[_-]?key|passphrase|token|password)(\s*[:=]\s*)([^\s,;]+)"
)
_URL_PATTERN = re.compile(r"(?i)\b(?:https?|wss?)://[^\s<>\"']+")
_AUTH_HEADER_PATTERN = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+"
)


def sanitize_endpoint(endpoint: str | None) -> str | None:
    """Return only a URL host/port; discard scheme, path, query and userinfo."""

    if not endpoint:
        return None
    try:
        parsed = urlsplit(endpoint.strip())
        if parsed.scheme.lower() not in {"http", "https", "ws", "wss"} or not parsed.hostname:
            return "REDACTED_ENDPOINT"
        try:
            port = parsed.port
        except ValueError:
            return "REDACTED_ENDPOINT"
        return f"{parsed.hostname}:{port}" if port is not None else parsed.hostname
    except ValueError:
        return "REDACTED_ENDPOINT"


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0).rstrip(".,;)")
    suffix = match.group(0)[len(raw):]
    return f"{sanitize_endpoint(raw)}{suffix}"


def redact_secrets(message: str) -> str:
    sanitized = _URL_PATTERN.sub(_redact_url, message)
    sanitized = _AUTH_HEADER_PATTERN.sub(r"\1REDACTED", sanitized)
    return _SECRET_PATTERN.sub(r"\1\2REDACTED", sanitized)


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Resolve %-style arguments before redaction; otherwise operational
        # errors are logged as a literal "%s" and cannot be diagnosed.
        message = redact_secrets(record.getMessage())
        context = []
        for field in ("symbol", "source", "status", "event_id"):
            if hasattr(record, field):
                context.append(f"{field}={getattr(record, field)}")
        record.msg = " ".join([message, *context]) if context else message
        record.args = ()
        return True


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("quant_phase1")
    logger.setLevel(level)
    logger.propagate = True
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)sZ %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    for handler in logger.handlers:
        if not any(isinstance(item, SecretRedactingFilter) for item in handler.filters):
            handler.addFilter(SecretRedactingFilter())
    return logger

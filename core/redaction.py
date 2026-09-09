"""Sanitize diagnostic copies without changing credentials used for transport."""
from __future__ import annotations

import re
import os
from collections.abc import Mapping
from urllib.parse import urlsplit

_KEY = re.compile(r"(?i)^(?:authorization|api[_-]?key|(?:api[_-]?|client[_-]?)?secret|password|signature|token|access[_-]?token|x-mbx-apikey)$")
_URL = re.compile(r"(?i)\b(?:https?|socks5h?|socks4a?)://[^\s<>\"']+")
_AUTH = re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|basic)\s+)?[^\s,;}\"']+")
_PAIR = re.compile(r"(?i)((?:api[_-]?key|(?:api[_-]?|client[_-]?)?secret|password|signature|token|access[_-]?token|x-mbx-apikey)[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}\"']+)")


def safe_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        if not parts.scheme or not parts.hostname:
            return "[REDACTED_URL]"
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        port = f":{parts.port}" if parts.port is not None else ""
        return f"{parts.scheme}://{host}{port}"
    except (ValueError, TypeError):
        return "[REDACTED_URL]"


def sanitize_text(value: object) -> str:
    text = str(value)
    for name in ("EXCHANGE_API_KEY", "EXCHANGE_SECRET", "EXCHANGE_PASSWORD"):
        secret = os.getenv(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    # Remove path/query/fragment too: exceptions often include signed requests.
    text = _URL.sub(lambda match: safe_url(match.group()), text)
    text = _AUTH.sub(r"\1[REDACTED]", text)
    return _PAIR.sub(r"\1[REDACTED]", text)


def sanitize(value):
    if isinstance(value, Mapping):
        return {sanitize_text(key): "[REDACTED]" if _KEY.fullmatch(str(key)) else sanitize(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_text(value)

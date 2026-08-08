"""Deterministic redaction for logs and diagnostic bundles."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = {
    "authorization",
    "browser_profile",
    "cookie",
    "environment",
    "headers",
    "local_storage",
    "openai_api_key",
    "password",
    "raw_response",
    "request_headers",
    "secret",
    "signed_url",
    "token",
}
_SENSITIVE_QUERY_PARTS = ("auth", "key", "signature", "signed", "token")
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")


def _redact_url(match: re.Match[str]) -> str:
    url = match.group(0)
    trailing = ""
    while url and url[-1] in ".,);]":
        trailing = url[-1] + trailing
        url = url[:-1]
    parsed = urlsplit(url)
    redacted_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if any(part in key.lower() for part in _SENSITIVE_QUERY_PARTS):
            redacted_query.append((key, REDACTED))
        else:
            redacted_query.append((key, value))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(redacted_query), parsed.fragment)
    ) + trailing


def redact_text(text: str) -> str:
    """Remove common credentials, signed query values, and private identities."""
    result = re.sub(
        r"(?im)\b(cookie|set-cookie)\s*:\s*[^\r\n]+",
        lambda match: f"{match.group(1)}: {REDACTED}",
        text,
    )
    result = re.sub(
        r"(?im)\b(authorization|proxy-authorization)\s*:\s*[^\r\n]+",
        lambda match: f"{match.group(1)}: {REDACTED}",
        result,
    )
    result = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*", f"Bearer {REDACTED}", result)
    result = re.sub(r"\bsk-[A-Za-z0-9_-]{10,}\b", REDACTED, result)
    result = _URL_PATTERN.sub(_redact_url, result)
    result = re.sub(r"/(Users|home)/[^/\s]+", rf"/\1/{REDACTED}", result)
    result = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", REDACTED, result)
    result = re.sub(r"(?<!\d)(?:\+?\d[\d -]{8,}\d)(?!\d)", REDACTED, result)
    return result


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(sensitive == normalized or sensitive in normalized for sensitive in _SENSITIVE_KEYS)


def redact_mapping(value: object) -> Any:
    """Recursively sanitize a structured value without preserving raw response containers."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            result[key_text] = REDACTED if _is_sensitive_key(key_text) else redact_mapping(nested)
        return result
    if isinstance(value, list):
        return [redact_mapping(nested) for nested in value]
    if isinstance(value, tuple):
        return [redact_mapping(nested) for nested in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return REDACTED


class StructuredLogger:
    """Serialize only redacted structured events."""

    def event(self, code: str, fields: Mapping[str, object]) -> str:
        payload = {"code": code, "fields": redact_mapping(fields)}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


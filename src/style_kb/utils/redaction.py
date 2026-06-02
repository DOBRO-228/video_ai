from __future__ import annotations

import re

REDACTED = "[redacted]"

_SENSITIVE_EXACT_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "bearer_token",
    "auth_token",
}
_SENSITIVE_KEY_PARTS = ("api_key", "authorization", "cookie", "secret")
_SAFE_SECRET_METADATA_SUFFIXES = ("_present", "_fingerprint")
_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)\b(authorization\s*[:=]\s*)(bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)[^\r\n]+"),
    re.compile(
        r"(?i)\b((?:api[_-]?key|token|secret|access[_-]?token|refresh[_-]?token|auth[_-]?token)\s*[:=]\s*)"
        r"['\"]?[^'\"\s,;]+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-proj-[A-Za-z0-9_-]{16,}\b"),
)


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized.endswith(_SAFE_SECRET_METADATA_SUFFIXES):
        return False
    return normalized in _SENSITIVE_EXACT_KEYS or any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact_sensitive_text(value: str) -> str:
    redacted = value
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        redacted = pattern.sub(_redact_match, redacted)
    return redacted


def _redact_match(match: re.Match[str]) -> str:
    if match.lastindex:
        return "".join(group or "" for group in match.groups()) + REDACTED
    return REDACTED


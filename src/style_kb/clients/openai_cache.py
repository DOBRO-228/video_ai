from __future__ import annotations

import hashlib

_PROMPT_CACHE_KEY_MAX_LENGTH = 64
_PROMPT_CACHE_KEY_PREFIX = "style-kb"
_PROMPT_CACHE_NAMESPACE_LENGTH = 16
_PROMPT_CACHE_MODEL_LENGTH = 12
_PROMPT_CACHE_FINGERPRINT_LENGTH = 24


def openai_prompt_cache_key(*, namespace: str, model: str, fingerprint: str) -> str:
    normalized_namespace = _compact_key_part(
        _safe_key_part(namespace),
        max_length=_PROMPT_CACHE_NAMESPACE_LENGTH,
    )
    normalized_model = _compact_key_part(
        _safe_key_part(model),
        max_length=_PROMPT_CACHE_MODEL_LENGTH,
    )
    normalized_fingerprint = _safe_key_part(fingerprint)[:_PROMPT_CACHE_FINGERPRINT_LENGTH]
    key = f"{_PROMPT_CACHE_KEY_PREFIX}:{normalized_namespace}:{normalized_model}:{normalized_fingerprint}"
    if len(key) <= _PROMPT_CACHE_KEY_MAX_LENGTH:
        return key

    fallback_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"{_PROMPT_CACHE_KEY_PREFIX}:{fallback_digest}"


def openai_prompt_cache_fingerprint(*values: str) -> str:
    payload = "\n\n".join(values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_key_part(value: str) -> str:
    cleaned = []
    for character in str(value).strip().lower():
        if character.isalnum() or character in {"-", "_", "."}:
            cleaned.append(character)
        else:
            cleaned.append("-")
    return "".join(cleaned).strip("-") or "default"


def _compact_key_part(value: str, *, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    if max_length <= 9:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:max_length]

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    head_length = max_length - len(digest) - 1
    head = value[:head_length].rstrip("-_.") or value[:head_length]
    return f"{head}-{digest}"

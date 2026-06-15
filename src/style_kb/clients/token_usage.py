from __future__ import annotations

from typing import Any


def openai_responses_usage(raw_payload: dict[str, Any]) -> dict[str, int]:
    usage = _mapping(raw_payload.get("usage"))
    input_details = _mapping(usage.get("input_tokens_details"))
    output_details = _mapping(usage.get("output_tokens_details"))
    return {
        "input_tokens": _int_or_zero(usage.get("input_tokens")),
        "cached_input_tokens": _int_or_zero(input_details.get("cached_tokens")),
        "output_tokens": _int_or_zero(usage.get("output_tokens")),
        "reasoning_tokens": _int_or_zero(output_details.get("reasoning_tokens")),
        "total_tokens": _int_or_zero(usage.get("total_tokens")),
    }


def gemini_usage(raw_payload: dict[str, Any], *, model: str | None = None) -> dict[str, int]:
    model_name = _model_name(model or raw_payload.get("model_version") or raw_payload.get("modelVersion"))
    if model_name.startswith("gemini-3-flash"):
        return _gemini_3_flash_usage(raw_payload)
    if model_name.startswith("gemini-2.5-flash"):
        return _gemini_2_5_flash_usage(raw_payload)
    return _generic_gemini_usage(raw_payload)


def _gemini_3_flash_usage(raw_payload: dict[str, Any]) -> dict[str, int]:
    return _generic_gemini_usage(raw_payload)


def _gemini_2_5_flash_usage(raw_payload: dict[str, Any]) -> dict[str, int]:
    usage = _gemini_usage_metadata(raw_payload)
    prompt_tokens = _int_or_none_from_mapping(usage, "prompt_token_count", "promptTokenCount")
    cached_tokens = _int_or_none_from_mapping(usage, "cached_content_token_count", "cachedContentTokenCount")
    tool_prompt_tokens = _int_or_none_from_mapping(usage, "tool_use_prompt_token_count", "toolUsePromptTokenCount")
    output_tokens = _int_or_zero_from_mapping(usage, "candidates_token_count", "candidatesTokenCount")
    reasoning_tokens = _int_or_zero_from_mapping(usage, "thoughts_token_count", "thoughtsTokenCount")
    reported_total = _int_or_none_from_mapping(usage, "total_token_count", "totalTokenCount")

    input_tokens = prompt_tokens if prompt_tokens is not None else (cached_tokens or 0) + (tool_prompt_tokens or 0)
    known_total = input_tokens + output_tokens + reasoning_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": reported_total if reported_total is not None else known_total,
    }


def _generic_gemini_usage(raw_payload: dict[str, Any]) -> dict[str, int]:
    usage = _gemini_usage_metadata(raw_payload)
    input_tokens = _int_or_zero_from_mapping(usage, "prompt_token_count", "promptTokenCount")
    output_tokens = _int_or_zero_from_mapping(usage, "candidates_token_count", "candidatesTokenCount")
    reasoning_tokens = _int_or_zero_from_mapping(usage, "thoughts_token_count", "thoughtsTokenCount")
    total_tokens = _int_or_none_from_mapping(usage, "total_token_count", "totalTokenCount")
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens + reasoning_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def _gemini_usage_metadata(raw_payload: dict[str, Any]) -> dict[str, Any]:
    return _mapping(raw_payload.get("usage_metadata") or raw_payload.get("usageMetadata"))


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int_or_zero_from_mapping(value: dict[str, Any], *keys: str) -> int:
    found = _int_or_none_from_mapping(value, *keys)
    return found if found is not None else 0


def _int_or_none_from_mapping(value: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        parsed = _int_or_none(value.get(key))
        if parsed is not None:
            return parsed
    return None


def _int_or_zero(value: object) -> int:
    parsed = _int_or_none(value)
    return parsed if parsed is not None else 0


def _int_or_none(value: object) -> int | None:
    try:
        if value is not None:
            return int(value)
    except (TypeError, ValueError):
        return None
    return None


def _model_name(value: object) -> str:
    return str(value or "").strip().lower()

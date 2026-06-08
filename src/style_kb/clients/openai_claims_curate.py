from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI

from style_kb.clients._retry import OnRetry, RetryPolicy, call_with_retry
from style_kb.clients.provider_diagnostics import (
    ProviderCallDiagnostics,
    cached_openai_diagnostics,
    openai_response_diagnostics,
    start_operation,
)
from style_kb.errors import MissingApiKeyError, ProviderError
from style_kb.models import ConfidenceLevel
from style_kb.utils.files import read_json, write_json_atomic

CONFIDENCE_LEVELS = ConfidenceLevel.values()


@dataclass(slots=True)
class ClaimsCurateResult:
    payload: dict[str, Any]
    raw_payload: dict[str, Any]
    usage: dict[str, int]
    model: str | None
    diagnostics: ProviderCallDiagnostics


class OpenAIClaimsCurateClient:
    def __init__(
        self,
        api_key: str | None,
        *,
        model: str,
        reasoning_effort: str,
        retry_policy: RetryPolicy | None = None,
        on_retry: OnRetry | None = None,
    ) -> None:
        if not api_key:
            raise MissingApiKeyError(
                "OPENAI_API_KEY is required before claims curation",
                error_code="missing_openai_api_key",
            )
        self.client = OpenAI(api_key=api_key, max_retries=0)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.retry_policy = retry_policy or RetryPolicy()
        self.on_retry = on_retry

    def curate_claims(
        self,
        *,
        system_prompt: str,
        claims_payload: list[dict[str, Any]],
        constraints_payload: dict[str, Any],
        request_metadata: dict[str, Any],
        raw_output_path: Path,
    ) -> ClaimsCurateResult:
        request_text = "\n\n".join(
            [
                system_prompt.strip(),
                "Ограничения проверки claims:",
                json.dumps(constraints_payload, ensure_ascii=False, indent=2, sort_keys=True),
                "Claims для проверки:",
                json.dumps(claims_payload, ensure_ascii=False, indent=2, sort_keys=True),
            ]
        )
        timer = start_operation()
        try:
            response = call_with_retry(
                lambda: self.client.responses.create(
                    model=self.model,
                    reasoning={"effort": self.reasoning_effort},
                    input=[{"role": "user", "content": [{"type": "input_text", "text": request_text}]}],
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "menswear_style_claims_curation",
                            "strict": True,
                            "schema": _curate_response_schema(len(claims_payload)),
                        }
                    },
                ),
                policy=self.retry_policy,
                on_retry=self.on_retry,
            )
        except Exception as error:  # pragma: no cover - SDK exception surface depends on installed version
            raise ProviderError(str(error), error_code="openai_claims_curate_failed", details=str(error)) from error

        raw_payload = response.model_dump(mode="json")
        diagnostics = openai_response_diagnostics(
            response,
            raw_payload,
            timer=timer,
            raw_output_path=raw_output_path,
        )
        raw_payload["_style_kb_diagnostics"] = diagnostics.to_dict()
        cached_payload = {"request": request_metadata, "diagnostics": diagnostics.to_dict(), "response": raw_payload}
        write_json_atomic(raw_output_path, cached_payload)
        return _result_from_cached_payload(cached_payload, fallback_output_text=response.output_text)


def load_cached_claims_curate_result(raw_output_path: Path) -> ClaimsCurateResult:
    return _result_from_cached_payload(read_json(raw_output_path))


def _curate_response_schema(claims_count: int) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decisions": {
                "type": "array",
                "minItems": claims_count,
                "maxItems": claims_count,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "claim_id": {"type": "string"},
                        "keep": {"type": "boolean"},
                        "merged_into": {"type": "string"},
                        "confidence_revised": {"type": "string", "enum": CONFIDENCE_LEVELS},
                        "confidence_reason": {"type": "string"},
                        "split_candidate": {"type": "boolean"},
                        "split_suggestion": {"type": "array", "items": {"type": "string"}},
                        "rewrite_suggestion": {"type": "string"},
                        "applies_to_note": {"type": "string"},
                    },
                    "required": [
                        "claim_id",
                        "keep",
                        "merged_into",
                        "confidence_revised",
                        "confidence_reason",
                        "split_candidate",
                        "split_suggestion",
                        "rewrite_suggestion",
                        "applies_to_note",
                    ],
                },
            }
        },
        "required": ["decisions"],
    }


def _result_from_cached_payload(
    cached_payload: dict[str, Any],
    *,
    fallback_output_text: str | None = None,
) -> ClaimsCurateResult:
    raw_payload = cached_payload.get("response")
    if not isinstance(raw_payload, dict):
        raise ProviderError(
            "cached OpenAI claims curation response has invalid shape",
            error_code="openai_claims_curate_cache_invalid",
        )
    output_text = fallback_output_text or _extract_output_text(raw_payload)
    try:
        payload = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise ProviderError(
            "OpenAI claims curation response is not valid JSON",
            error_code="openai_claims_curate_json_parse_failed",
            details=str(error),
        ) from error
    usage = raw_payload.get("usage") or {}
    output_details = usage.get("output_tokens_details") or {}
    usage_payload = {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
    model = str(raw_payload.get("model")) if raw_payload.get("model") is not None else None
    return ClaimsCurateResult(
        payload=payload,
        raw_payload=cached_payload,
        usage=usage_payload,
        model=model,
        diagnostics=cached_openai_diagnostics(cached_payload, raw_payload, model=model, usage=usage_payload),
    )


def _extract_output_text(raw_payload: dict[str, Any]) -> str:
    for item in raw_payload.get("output") or []:
        if item.get("type") != "message":
            continue
        for content_item in item.get("content") or []:
            if content_item.get("type") == "output_text" and content_item.get("text"):
                return str(content_item["text"])
    raise ProviderError(
        "OpenAI claims curation response does not contain output_text",
        error_code="openai_claims_curate_output_missing",
    )

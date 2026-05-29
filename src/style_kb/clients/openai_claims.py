from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI

from style_kb.clients._retry import OnRetry, RetryPolicy, call_with_retry
from style_kb.errors import MissingApiKeyError, ProviderError
from style_kb.utils.files import read_json, write_json_atomic

CLAIM_TYPES = ["rule", "recommendation", "warning", "definition", "example", "exception"]
CONFIDENCE_LEVELS = ["low", "medium", "high"]


@dataclass(slots=True)
class ClaimsAnalysisResult:
    payload: dict[str, Any]
    raw_payload: dict[str, Any]
    usage: dict[str, int]
    model: str | None


class OpenAIClaimsClient:
    def __init__(
        self,
        api_key: str | None,
        *,
        model: str,
        retry_policy: RetryPolicy | None = None,
        on_retry: OnRetry | None = None,
    ) -> None:
        if not api_key:
            raise MissingApiKeyError(
                "OPENAI_API_KEY is required before stage 13_extract_style_claims",
                error_code="missing_openai_api_key",
            )
        self.client = OpenAI(api_key=api_key, max_retries=0)
        self.model = model
        self.retry_policy = retry_policy or RetryPolicy()
        self.on_retry = on_retry

    def extract_claims(
        self,
        *,
        system_prompt: str,
        chunk_payload: dict[str, Any],
        constraints_payload: dict[str, Any],
        request_metadata: dict[str, Any],
        raw_output_path: Path,
        max_claims_per_chunk: int,
    ) -> ClaimsAnalysisResult:
        request_text = "\n\n".join(
            [
                system_prompt.strip(),
                "Ограничения извлечения:",
                json.dumps(constraints_payload, ensure_ascii=False, indent=2, sort_keys=True),
                "Chunk для анализа:",
                json.dumps(chunk_payload, ensure_ascii=False, indent=2, sort_keys=True),
            ]
        )
        try:
            response = call_with_retry(
                lambda: self.client.responses.create(
                    model=self.model,
                    input=[{"role": "user", "content": [{"type": "input_text", "text": request_text}]}],
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "menswear_style_claims",
                            "strict": True,
                            "schema": _claim_response_schema(max_claims_per_chunk),
                        }
                    },
                ),
                policy=self.retry_policy,
                on_retry=self.on_retry,
            )
        except Exception as error:  # pragma: no cover - SDK exception surface depends on installed version
            raise ProviderError(str(error), error_code="openai_claims_failed", details=str(error)) from error

        raw_payload = response.model_dump(mode="json")
        cached_payload = {"request": request_metadata, "response": raw_payload}
        write_json_atomic(raw_output_path, cached_payload)
        return _result_from_cached_payload(cached_payload, fallback_output_text=response.output_text)


def load_cached_claims_result(raw_output_path: Path) -> ClaimsAnalysisResult:
    return _result_from_cached_payload(read_json(raw_output_path))


def _claim_response_schema(max_claims_per_chunk: int) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "claims": {
                "type": "array",
                "maxItems": max_claims_per_chunk,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "claim_type": {"type": "string", "enum": CLAIM_TYPES},
                        "subject": {"type": "string"},
                        "claim": {"type": "string"},
                        "rationale": {"type": "string"},
                        "conditions": {"type": "array", "items": {"type": "string"}},
                        "applies_to": {"type": "array", "items": {"type": "string"}},
                        "avoid": {"type": "array", "items": {"type": "string"}},
                        "prefer": {"type": "array", "items": {"type": "string"}},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                        "topics": {"type": "array", "items": {"type": "string"}},
                        "confidence": {"type": "string", "enum": CONFIDENCE_LEVELS},
                    },
                    "required": [
                        "claim_type",
                        "subject",
                        "claim",
                        "rationale",
                        "conditions",
                        "applies_to",
                        "avoid",
                        "prefer",
                        "evidence",
                        "topics",
                        "confidence",
                    ],
                },
            }
        },
        "required": ["claims"],
    }


def _result_from_cached_payload(
    cached_payload: dict[str, Any],
    *,
    fallback_output_text: str | None = None,
) -> ClaimsAnalysisResult:
    raw_payload = cached_payload.get("response")
    if not isinstance(raw_payload, dict):
        raise ProviderError("cached OpenAI claims response has invalid shape", error_code="openai_claims_cache_invalid")
    output_text = fallback_output_text or _extract_output_text(raw_payload)
    try:
        payload = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise ProviderError(
            "OpenAI claims response is not valid JSON",
            error_code="openai_claims_json_parse_failed",
            details=str(error),
        ) from error
    usage = raw_payload.get("usage") or {}
    output_details = usage.get("output_tokens_details") or {}
    return ClaimsAnalysisResult(
        payload=payload,
        raw_payload=cached_payload,
        usage={
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
        model=str(raw_payload.get("model")) if raw_payload.get("model") is not None else None,
    )


def _extract_output_text(raw_payload: dict[str, Any]) -> str:
    for item in raw_payload.get("output") or []:
        if item.get("type") != "message":
            continue
        for content_item in item.get("content") or []:
            if content_item.get("type") == "output_text" and content_item.get("text"):
                return str(content_item["text"])
    raise ProviderError(
        "OpenAI claims response does not contain output_text",
        error_code="openai_claims_output_missing",
    )

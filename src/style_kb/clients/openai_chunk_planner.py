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
from style_kb.utils.files import read_json, write_json_atomic

CHUNK_PLAN_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "chunks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "speech_segment_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "title": {"type": "string"},
                    "boundary_reason": {"type": "string"},
                    "topics": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "notes": {"type": "string"},
                },
                "required": ["speech_segment_ids", "title", "boundary_reason", "topics", "notes"],
            },
        }
    },
    "required": ["chunks"],
}


@dataclass(slots=True)
class ChunkPlanAnalysisResult:
    payload: dict[str, Any]
    raw_payload: dict[str, Any]
    usage: dict[str, int]
    model: str | None
    diagnostics: ProviderCallDiagnostics


class OpenAIChunkPlannerClient:
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
                "OPENAI_API_KEY is required before stage 12_build_chunks",
                error_code="missing_openai_api_key",
            )
        self.client = OpenAI(api_key=api_key, max_retries=0)
        self.model = model
        self.retry_policy = retry_policy or RetryPolicy()
        self.on_retry = on_retry

    def plan_chunks(
        self,
        *,
        system_prompt: str,
        planner_payload: dict[str, Any],
        constraints_payload: dict[str, Any],
        raw_output_path: Path,
    ) -> ChunkPlanAnalysisResult:
        request_text = "\n\n".join(
            [
                system_prompt.strip(),
                "Ограничения планирования:",
                json.dumps(constraints_payload, ensure_ascii=False, indent=2, sort_keys=True),
                "Данные для планирования:",
                json.dumps(planner_payload, ensure_ascii=False, indent=2, sort_keys=True),
            ]
        )
        timer = start_operation()
        try:
            response = call_with_retry(
                lambda: self.client.responses.create(
                    model=self.model,
                    input=[{"role": "user", "content": [{"type": "input_text", "text": request_text}]}],
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "menswear_chunk_plan",
                            "strict": True,
                            "schema": CHUNK_PLAN_RESPONSE_SCHEMA,
                        }
                    },
                ),
                policy=self.retry_policy,
                on_retry=self.on_retry,
            )
        except Exception as error:  # pragma: no cover - SDK exception surface depends on installed version
            raise ProviderError(str(error), error_code="openai_chunk_planner_failed", details=str(error)) from error

        raw_payload = response.model_dump(mode="json")
        diagnostics = openai_response_diagnostics(
            response,
            raw_payload,
            timer=timer,
            raw_output_path=raw_output_path,
        )
        raw_payload["_style_kb_diagnostics"] = diagnostics.to_dict()
        cached_payload = {
            "request": {
                "constraints": constraints_payload,
                "planner_input": planner_payload,
            },
            "diagnostics": diagnostics.to_dict(),
            "response": raw_payload,
        }
        write_json_atomic(raw_output_path, cached_payload)
        return _result_from_cached_payload(cached_payload, fallback_output_text=response.output_text)


def load_cached_chunk_plan_result(raw_output_path: Path) -> ChunkPlanAnalysisResult:
    return _result_from_cached_payload(read_json(raw_output_path))


def _result_from_cached_payload(
    cached_payload: dict[str, Any],
    *,
    fallback_output_text: str | None = None,
) -> ChunkPlanAnalysisResult:
    raw_payload = cached_payload.get("response") or cached_payload
    output_text = fallback_output_text or _extract_output_text(raw_payload)
    try:
        payload = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise ProviderError(
            "OpenAI chunk planner response is not valid JSON",
            error_code="openai_chunk_planner_json_parse_failed",
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
    return ChunkPlanAnalysisResult(
        payload=payload,
        raw_payload=raw_payload,
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
        "OpenAI chunk planner response does not contain output_text",
        error_code="openai_chunk_planner_output_missing",
    )

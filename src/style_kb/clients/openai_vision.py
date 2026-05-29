from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI

from style_kb.clients._retry import OnRetry, RetryPolicy, call_with_retry
from style_kb.errors import MissingApiKeyError, ProviderError
from style_kb.utils.files import read_json, write_json_atomic

PRESENTER_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "present": {"type": "boolean"},
        "role": {"type": "string", "enum": ["none", "primary_presenter", "other_person"]},
        "is_recurring": {"type": "boolean"},
        "relevance": {"type": "string", "enum": ["none", "background", "brief", "primary_example"]},
        "baseline_summary": {"type": "string"},
        "scene_deltas": {"type": "array", "items": {"type": "string"}},
        "narrative_brief": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": [
        "present",
        "role",
        "is_recurring",
        "relevance",
        "baseline_summary",
        "scene_deltas",
        "narrative_brief",
        "confidence",
    ],
}

VISUAL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "visual_summary": {"type": "string"},
        "observations": {"type": "array", "items": {"type": "string"}},
        "interpretations": {"type": "array", "items": {"type": "string"}},
        "on_screen_text": {"type": "array", "items": {"type": "string"}},
        "items": {"type": "array", "items": {"type": "string"}},
        "colors": {"type": "array", "items": {"type": "string"}},
        "style_topics": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "notes": {"type": "string"},
        "presenter_context": PRESENTER_CONTEXT_SCHEMA,
    },
    "required": [
        "visual_summary",
        "observations",
        "interpretations",
        "on_screen_text",
        "items",
        "colors",
        "style_topics",
        "confidence",
        "notes",
        "presenter_context",
    ],
}

PRESENTER_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "has_primary_presenter": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "baseline_summary": {"type": "string"},
        "recurring_visual_markers": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": [
        "has_primary_presenter",
        "confidence",
        "baseline_summary",
        "recurring_visual_markers",
        "notes",
    ],
}


@dataclass(slots=True)
class VisionAnalysisResult:
    payload: dict[str, Any]
    raw_payload: dict[str, Any]
    usage: dict[str, int]
    model: str | None
    remote_duration_seconds: float | None


class OpenAIVisionClient:
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
                "OPENAI_API_KEY is required before stage 10_describe_visuals",
                error_code="missing_openai_api_key",
            )
        self.client = OpenAI(api_key=api_key, max_retries=0)
        self.model = model
        self.retry_policy = retry_policy or RetryPolicy()
        self.on_retry = on_retry

    def describe_scene(
        self,
        *,
        system_prompt: str,
        transcript_context: str,
        image_paths: list[Path],
        detail: str,
        raw_output_path: Path,
    ) -> VisionAnalysisResult:
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": "\n\n".join(
                    [
                        system_prompt.strip(),
                        "Контекст транскрипта:",
                        transcript_context.strip() or "(пусто)",
                    ]
                ),
            }
        ]
        content.extend(_image_content(image_paths, detail=detail))
        return self._create_structured_response(
            content=content,
            schema=VISUAL_RESPONSE_SCHEMA,
            schema_name="menswear_visual_analysis",
            raw_output_path=raw_output_path,
            error_code="openai_vision_failed",
        )

    def build_presenter_profile(
        self,
        *,
        system_prompt: str,
        image_paths: list[Path],
        detail: str,
        raw_output_path: Path,
    ) -> VisionAnalysisResult:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": system_prompt.strip()}]
        content.extend(_image_content(image_paths, detail=detail))
        return self._create_structured_response(
            content=content,
            schema=PRESENTER_PROFILE_SCHEMA,
            schema_name="menswear_presenter_profile",
            raw_output_path=raw_output_path,
            error_code="openai_presenter_profile_failed",
        )

    def _create_structured_response(
        self,
        *,
        content: list[dict[str, Any]],
        schema: dict[str, Any],
        schema_name: str,
        raw_output_path: Path,
        error_code: str,
    ) -> VisionAnalysisResult:
        try:
            response = call_with_retry(
                lambda: self.client.responses.create(
                    model=self.model,
                    input=[{"role": "user", "content": content}],
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": schema_name,
                            "strict": True,
                            "schema": schema,
                        }
                    },
                ),
                policy=self.retry_policy,
                on_retry=self.on_retry,
            )
        except Exception as error:  # pragma: no cover - SDK exception surface depends on installed version
            raise ProviderError(str(error), error_code=error_code, details=str(error)) from error

        raw_payload = response.model_dump(mode="json")
        write_json_atomic(raw_output_path, raw_payload)
        return _result_from_raw_payload(raw_payload, fallback_output_text=response.output_text)


def load_cached_visual_result(raw_output_path: Path) -> VisionAnalysisResult:
    return _result_from_raw_payload(read_json(raw_output_path))


def _image_content(image_paths: list[Path], *, detail: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "input_image",
            "image_url": _data_url(image_path),
            "detail": detail,
        }
        for image_path in image_paths
    ]


def _data_url(image_path: Path) -> str:
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _result_from_raw_payload(
    raw_payload: dict[str, Any],
    *,
    fallback_output_text: str | None = None,
) -> VisionAnalysisResult:
    output_text = fallback_output_text or _extract_output_text(raw_payload)
    try:
        payload = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise ProviderError(
            "OpenAI vision response is not valid JSON",
            error_code="openai_vision_json_parse_failed",
            details=str(error),
        ) from error
    usage = raw_payload.get("usage") or {}
    output_details = usage.get("output_tokens_details") or {}
    remote_duration = None
    created_at = raw_payload.get("created_at")
    completed_at = raw_payload.get("completed_at")
    if isinstance(created_at, (int, float)) and isinstance(completed_at, (int, float)):
        remote_duration = max(0.0, float(completed_at) - float(created_at))
    return VisionAnalysisResult(
        payload=payload,
        raw_payload=raw_payload,
        usage={
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
        model=str(raw_payload.get("model")) if raw_payload.get("model") is not None else None,
        remote_duration_seconds=remote_duration,
    )


def _extract_output_text(raw_payload: dict[str, Any]) -> str:
    for item in raw_payload.get("output") or []:
        if item.get("type") != "message":
            continue
        for content_item in item.get("content") or []:
            if content_item.get("type") == "output_text" and content_item.get("text"):
                return str(content_item["text"])
    raise ProviderError(
        "OpenAI vision response does not contain output_text",
        error_code="openai_vision_output_missing",
    )

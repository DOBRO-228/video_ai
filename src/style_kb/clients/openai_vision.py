from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from openai import OpenAI

from style_kb.clients._retry import OnRetry, RetryPolicy, call_with_retry
from style_kb.clients.provider_diagnostics import (
    cached_openai_diagnostics,
    openai_response_diagnostics,
    start_operation,
)
from style_kb.clients.vision import PRESENTER_PROFILE_SCHEMA, VISUAL_RESPONSE_SCHEMA, VisionAnalysisResult
from style_kb.errors import MissingApiKeyError, ProviderError
from style_kb.utils.files import read_json, write_json_atomic


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
        transcript_context: dict[str, Any],
        image_paths: list[Path],
        detail: str | None,
        raw_output_path: Path,
    ) -> VisionAnalysisResult:
        if not detail:
            raise ProviderError("OpenAI vision detail is required", error_code="openai_vision_detail_missing")
        transcript_context_json = json.dumps(transcript_context, ensure_ascii=False, indent=2, sort_keys=True)
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": "\n\n".join(
                    [
                        system_prompt.strip(),
                        "Структурированный контекст транскрипта для текущей визуальной сцены:",
                        transcript_context_json,
                        "Используй current_scene_context только как смысловой контекст. previous_context и next_context нужны только для ориентира на границах сцены. Не описывай содержимое previous_context или next_context как визуально присутствующее в текущих кадрах.",
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
        detail: str | None,
        raw_output_path: Path,
    ) -> VisionAnalysisResult:
        if not detail:
            raise ProviderError("OpenAI vision detail is required", error_code="openai_vision_detail_missing")
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
        timer = start_operation()
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
        diagnostics = openai_response_diagnostics(
            response,
            raw_payload,
            timer=timer,
            raw_output_path=raw_output_path,
        )
        raw_payload["_style_kb_diagnostics"] = diagnostics.to_dict()
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
    usage_payload = {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
    model = str(raw_payload.get("model")) if raw_payload.get("model") is not None else None
    return VisionAnalysisResult(
        payload=payload,
        raw_payload=raw_payload,
        usage=usage_payload,
        model=model,
        remote_duration_seconds=remote_duration,
        diagnostics=cached_openai_diagnostics(raw_payload, raw_payload, model=model, usage=usage_payload),
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

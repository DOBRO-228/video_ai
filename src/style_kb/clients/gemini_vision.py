from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from style_kb.clients._retry import OnRetry, RetryPolicy, call_with_retry
from style_kb.clients.provider_diagnostics import cached_gemini_diagnostics, gemini_response_diagnostics, start_operation
from style_kb.clients.vision import PRESENTER_PROFILE_SCHEMA, VISUAL_RESPONSE_SCHEMA, VisionAnalysisResult
from style_kb.errors import MissingApiKeyError, ProviderError
from style_kb.utils.files import read_json, write_json_atomic

_OUTPUT_TEXT_KEY = "_style_kb_output_text"
_GEMINI_API_VERSION = "v1alpha"


class GeminiVisionClient:
    def __init__(
        self,
        api_key: str | None,
        *,
        model: str,
        media_resolution: str | None,
        thinking_level: str | None,
        retry_policy: RetryPolicy | None = None,
        on_retry: OnRetry | None = None,
    ) -> None:
        if not api_key:
            raise MissingApiKeyError(
                "GEMINI_API_KEY is required before stage 10_describe_visuals",
                error_code="missing_gemini_api_key",
            )
        genai, types = _google_genai_modules()
        self.client = genai.Client(api_key=api_key, http_options={"api_version": _GEMINI_API_VERSION})
        self.types = types
        self.model = model
        self.media_resolution = _normalize_media_resolution(media_resolution)
        self.thinking_level = _normalize_thinking_level(thinking_level)
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
        transcript_context_json = json.dumps(transcript_context, ensure_ascii=False, indent=2, sort_keys=True)
        text = "\n\n".join(
            [
                system_prompt.strip(),
                "Структурированный контекст транскрипта для текущей визуальной сцены:",
                transcript_context_json,
                "Используй current_scene_context только как смысловой контекст. previous_context и next_context нужны только для ориентира на границах сцены. Не описывай содержимое previous_context или next_context как визуально присутствующее в текущих кадрах.",
            ]
        )
        content = _content_parts(self.types, text=text, image_paths=image_paths, media_resolution=self.media_resolution)
        return self._create_structured_response(
            content=content,
            schema=VISUAL_RESPONSE_SCHEMA,
            raw_output_path=raw_output_path,
            error_code="gemini_vision_failed",
        )

    def build_presenter_profile(
        self,
        *,
        system_prompt: str,
        image_paths: list[Path],
        detail: str | None,
        raw_output_path: Path,
    ) -> VisionAnalysisResult:
        content = _content_parts(
            self.types,
            text=system_prompt.strip(),
            image_paths=image_paths,
            media_resolution=self.media_resolution,
        )
        return self._create_structured_response(
            content=content,
            schema=PRESENTER_PROFILE_SCHEMA,
            raw_output_path=raw_output_path,
            error_code="gemini_presenter_profile_failed",
        )

    def _create_structured_response(
        self,
        *,
        content: list[Any],
        schema: dict[str, Any],
        raw_output_path: Path,
        error_code: str,
    ) -> VisionAnalysisResult:
        timer = start_operation()
        try:
            response = call_with_retry(
                lambda: self.client.models.generate_content(
                    model=self.model,
                    contents=[self.types.Content(role="user", parts=content)],
                    config=self.types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_json_schema=schema,
                        thinking_config=self.types.ThinkingConfig(thinking_level=self.thinking_level)
                        if self.thinking_level
                        else None,
                    ),
                ),
                policy=self.retry_policy,
                on_retry=self.on_retry,
            )
        except Exception as error:  # pragma: no cover - SDK exception surface depends on installed version
            raise ProviderError(str(error), error_code=error_code, details=str(error)) from error

        output_text = str(getattr(response, "text", "") or "")
        raw_payload = _model_dump(response)
        raw_payload[_OUTPUT_TEXT_KEY] = output_text
        diagnostics = gemini_response_diagnostics(
            response,
            raw_payload,
            timer=timer,
            raw_output_path=raw_output_path,
            model=self.model,
        )
        raw_payload["_style_kb_diagnostics"] = diagnostics.to_dict()
        write_json_atomic(raw_output_path, raw_payload)
        return _result_from_raw_payload(raw_payload, fallback_output_text=output_text)


def load_cached_gemini_visual_result(raw_output_path: Path) -> VisionAnalysisResult:
    return _result_from_raw_payload(read_json(raw_output_path))


def _google_genai_modules() -> tuple[Any, Any]:
    try:
        from google import genai
        from google.genai import types
    except ImportError as error:
        raise ProviderError(
            "google-genai is required for Gemini vision provider",
            error_code="gemini_sdk_missing",
            details=str(error),
        ) from error
    return genai, types


def _content_parts(
    types: Any,
    *,
    text: str,
    image_paths: list[Path],
    media_resolution: str | None,
) -> list[Any]:
    parts = [types.Part(text=text)]
    for image_path in image_paths:
        kwargs = {"inline_data": types.Blob(mime_type="image/jpeg", data=image_path.read_bytes())}
        if media_resolution:
            kwargs["media_resolution"] = {"level": media_resolution}
        parts.append(types.Part(**kwargs))
    return parts


def _normalize_media_resolution(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower().removeprefix("media_resolution_")
    if normalized not in {"low", "medium", "high", "ultra_high"}:
        raise ProviderError(
            f"Unsupported Gemini media_resolution: {value}",
            error_code="gemini_media_resolution_invalid",
        )
    return f"media_resolution_{normalized}"


def _normalize_thinking_level(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in {"minimal", "low", "medium", "high"}:
        raise ProviderError(
            f"Unsupported Gemini thinking_level: {value}",
            error_code="gemini_thinking_level_invalid",
        )
    return normalized


def _model_dump(response: Any) -> dict[str, Any]:
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {"response": dumped}
    to_json_dict = getattr(response, "to_json_dict", None)
    if callable(to_json_dict):
        dumped = to_json_dict()
        return dumped if isinstance(dumped, dict) else {"response": dumped}
    return {"text": str(getattr(response, "text", "") or "")}


def _result_from_raw_payload(
    raw_payload: dict[str, Any],
    *,
    fallback_output_text: str | None = None,
) -> VisionAnalysisResult:
    output_text = fallback_output_text or str(raw_payload.get(_OUTPUT_TEXT_KEY) or "") or _extract_output_text(raw_payload)
    try:
        payload = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise ProviderError(
            "Gemini vision response is not valid JSON",
            error_code="gemini_vision_json_parse_failed",
            details=str(error),
        ) from error
    usage_payload = _usage_from_raw_payload(raw_payload)
    model = _string_value(raw_payload.get("model_version")) or _string_value(raw_payload.get("modelVersion"))
    return VisionAnalysisResult(
        payload=payload,
        raw_payload=raw_payload,
        usage=usage_payload,
        model=model,
        remote_duration_seconds=None,
        diagnostics=cached_gemini_diagnostics(raw_payload, model=model, usage=usage_payload),
    )


def _usage_from_raw_payload(raw_payload: dict[str, Any]) -> dict[str, int]:
    usage = raw_payload.get("usage_metadata") or raw_payload.get("usageMetadata") or {}
    if not isinstance(usage, dict):
        usage = {}
    return {
        "input_tokens": _int_from_mapping(usage, "prompt_token_count", "promptTokenCount"),
        "output_tokens": _int_from_mapping(usage, "candidates_token_count", "candidatesTokenCount"),
        "reasoning_tokens": _int_from_mapping(usage, "thoughts_token_count", "thoughtsTokenCount"),
        "total_tokens": _int_from_mapping(usage, "total_token_count", "totalTokenCount"),
    }


def _extract_output_text(raw_payload: dict[str, Any]) -> str:
    candidates = raw_payload.get("candidates") or []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content") or {}
        parts = content.get("parts") or []
        texts = [str(part.get("text")) for part in parts if isinstance(part, dict) and part.get("text")]
        if texts:
            return "\n".join(texts)
    raise ProviderError(
        "Gemini vision response does not contain output text",
        error_code="gemini_vision_output_missing",
    )


def _int_from_mapping(value: dict[str, Any], *keys: str) -> int:
    for key in keys:
        try:
            if value.get(key) is not None:
                return int(value[key])
        except (TypeError, ValueError):
            continue
    return 0


def _string_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

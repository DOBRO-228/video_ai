from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openai import OpenAI

from style_kb.clients._retry import OnRetry, RetryPolicy, call_with_retry
from style_kb.errors import MissingApiKeyError, ProviderError
from style_kb.utils.files import write_json_atomic

SEGMENT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "unit_start_index": {"type": "integer", "minimum": 1},
                    "unit_end_index": {"type": "integer", "minimum": 1},
                },
                "required": ["unit_start_index", "unit_end_index"],
            },
        }
    },
    "required": ["segments"],
}

SEGMENT_RETRY_ADVISOR_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "error_summary": {"type": "string"},
        "repair_instruction": {"type": "string"},
        "hard_rules": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["error_summary", "repair_instruction", "hard_rules"],
}


class OpenAISegmenterClient:
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
                "OPENAI_API_KEY is required before stage 07_build_speech_segments",
                error_code="missing_openai_api_key",
            )
        self.client = OpenAI(api_key=api_key, max_retries=0)
        self.model = model
        self.retry_policy = retry_policy or RetryPolicy()
        self.on_retry = on_retry

    def segment_transcript(
        self,
        *,
        system_prompt: str,
        transcript_text: str,
        units_payload: list[dict[str, Any]],
        constraints_payload: dict[str, Any],
        raw_output_path: Path,
    ) -> dict[str, Any]:
        request_text = "\n\n".join(
            [
                system_prompt.strip(),
                "Ограничения сегментации:",
                json.dumps(constraints_payload, ensure_ascii=False, indent=2),
                "Полный транскрипт:",
                transcript_text.strip() or "(пусто)",
                "Атомарные units для разбиения:",
                json.dumps(units_payload, ensure_ascii=False, indent=2),
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
                            "name": "semantic_speech_segments",
                            "strict": True,
                            "schema": SEGMENT_RESPONSE_SCHEMA,
                        }
                    },
                ),
                policy=self.retry_policy,
                on_retry=self.on_retry,
            )
        except Exception as error:  # pragma: no cover - SDK exception surface depends on installed version
            raise ProviderError(str(error), error_code="openai_segmenter_failed", details=str(error)) from error

        raw_payload = response.model_dump(mode="json")
        write_json_atomic(raw_output_path, raw_payload)

        try:
            return json.loads(response.output_text)
        except json.JSONDecodeError as error:
            raise ProviderError(
                "OpenAI speech segmentation response is not valid JSON",
                error_code="openai_segmenter_json_parse_failed",
                details=str(error),
            ) from error

    def analyze_segmentation_errors(
        self,
        *,
        system_prompt: str,
        repair_payload: dict[str, Any],
        raw_output_path: Path,
    ) -> dict[str, Any]:
        request_text = "\n\n".join(
            [
                system_prompt.strip(),
                "Данные для анализа:",
                json.dumps(repair_payload, ensure_ascii=False, indent=2, sort_keys=True),
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
                            "name": "semantic_speech_retry_advice",
                            "strict": True,
                            "schema": SEGMENT_RETRY_ADVISOR_RESPONSE_SCHEMA,
                        }
                    },
                ),
                policy=self.retry_policy,
                on_retry=self.on_retry,
            )
        except Exception as error:  # pragma: no cover - SDK exception surface depends on installed version
            raise ProviderError(
                "OpenAI speech segmentation retry advisor failed",
                error_code="openai_segmenter_retry_advisor_failed",
                details=str(error),
            ) from error

        raw_payload = response.model_dump(mode="json")
        write_json_atomic(
            raw_output_path,
            {
                "request": repair_payload,
                "response": raw_payload,
            },
        )

        try:
            return json.loads(response.output_text)
        except json.JSONDecodeError as error:
            raise ProviderError(
                "OpenAI speech segmentation retry advisor response is not valid JSON",
                error_code="openai_segmenter_retry_advisor_json_parse_failed",
                details=str(error),
            ) from error

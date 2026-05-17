from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from openai import OpenAI

from style_kb.errors import MissingApiKeyError, ProviderError
from style_kb.utils.files import write_json_atomic

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
    ],
}


class OpenAIVisionClient:
    def __init__(self, api_key: str | None, *, model: str) -> None:
        if not api_key:
            raise MissingApiKeyError(
                "OPENAI_API_KEY is required before stage 10_describe_visuals",
                error_code="missing_openai_api_key",
            )
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def describe_scene(
        self,
        *,
        system_prompt: str,
        transcript_context: str,
        image_paths: list[Path],
        detail: str,
        raw_output_path: Path,
    ) -> dict[str, Any]:
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
        for image_path in image_paths:
            content.append(
                {
                    "type": "input_image",
                    "image_url": _data_url(image_path),
                    "detail": detail,
                }
            )

        try:
            response = self.client.responses.create(
                model=self.model,
                input=[{"role": "user", "content": content}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "menswear_visual_analysis",
                        "strict": True,
                        "schema": VISUAL_RESPONSE_SCHEMA,
                    }
                },
            )
        except Exception as error:  # pragma: no cover - SDK exception surface depends on installed version
            raise ProviderError(str(error), error_code="openai_vision_failed", details=str(error)) from error

        raw_payload = response.model_dump(mode="json")
        write_json_atomic(raw_output_path, raw_payload)

        try:
            return json.loads(response.output_text)
        except json.JSONDecodeError as error:
            raise ProviderError(
                "OpenAI vision response is not valid JSON",
                error_code="openai_vision_json_parse_failed",
                details=str(error),
            ) from error


def _data_url(image_path: Path) -> str:
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


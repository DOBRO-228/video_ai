from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from style_kb.pipeline.base import StageContext
from style_kb.utils.files import append_text
from style_kb.utils.json import pretty_json
from style_kb.utils.redaction import REDACTED, is_sensitive_key, redact_sensitive_text

_MAX_PREVIEW_CHARS = 200
_BASE64_CHUNK_RE = re.compile(r"^[A-Za-z0-9+/=_-]{256,}$")


def append_stage_summary(context: StageContext, stage_name: str, section: str, payload: dict[str, Any]) -> None:
    lines = [
        "",
        f"[{section}]",
        f"run_id: {context.run_id or '-'}",
        f"timestamp: {datetime.now(tz=UTC).isoformat()}",
        pretty_json(payload),
        "",
    ]
    append_text(context.paths.stage_log(stage_name), "\n".join(lines), encoding="utf-8")


def file_size(path: Path) -> int | None:
    return path.stat().st_size if path.exists() else None


def stream_summary(ffprobe_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "index": stream.get("index"),
            "codec_type": stream.get("codec_type"),
            "codec_name": stream.get("codec_name"),
            "duration": stream.get("duration"),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "avg_frame_rate": stream.get("avg_frame_rate"),
        }
        for stream in ffprobe_payload.get("streams", [])
    ]


def validation_preview(value: Any, *, max_chars: int = _MAX_PREVIEW_CHARS) -> str:
    redacted = _redact_preview_value(value)
    if redacted is None:
        return ""
    if isinstance(redacted, str):
        text = redacted
    else:
        try:
            text = json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)
        except TypeError:
            text = str(redacted)
    text = " ".join(text.split())
    if text.startswith("data:") and ";base64," in text[:80]:
        return "[omitted base64 data url]"
    if _BASE64_CHUNK_RE.fullmatch(text):
        return "[omitted base64 content]"
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def _redact_preview_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_key(str(key)) else _redact_preview_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_preview_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_preview_value(item) for item in value]
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("data:") and ";base64," in text[:80]:
        return "[omitted base64 data url]"
    if _BASE64_CHUNK_RE.fullmatch(text):
        return "[omitted base64 content]"
    text = re.sub(
        r"data:[^;,\s]+;base64,[A-Za-z0-9+/=_-]+",
        "data:[omitted base64 data url]",
        value,
        flags=re.IGNORECASE,
    )
    return redact_sensitive_text(text)

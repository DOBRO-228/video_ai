from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from style_kb.clients.provider_diagnostics import ProviderCallDiagnostics
from style_kb.models import ConfidenceLevel, PresenterRelevance, PresenterRole

PRESENTER_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "present": {"type": "boolean"},
        "role": {"type": "string", "enum": PresenterRole.values()},
        "is_recurring": {"type": "boolean"},
        "relevance": {"type": "string", "enum": PresenterRelevance.values()},
        "baseline_summary": {"type": "string"},
        "scene_deltas": {"type": "array", "items": {"type": "string"}},
        "narrative_brief": {"type": "string"},
        "confidence": {"type": "string", "enum": ConfidenceLevel.values()},
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
        "style_topics": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ConfidenceLevel.values()},
        "notes": {"type": "string"},
        "presenter_context": PRESENTER_CONTEXT_SCHEMA,
    },
    "required": [
        "visual_summary",
        "observations",
        "interpretations",
        "on_screen_text",
        "items",
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
        "confidence": {"type": "string", "enum": ConfidenceLevel.values()},
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
    diagnostics: ProviderCallDiagnostics

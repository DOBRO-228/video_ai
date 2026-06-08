from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from style_kb.clients._retry import OnRetry
from style_kb.clients.provider_diagnostics import ProviderCallDiagnostics, ProviderName
from style_kb.clients.openai_claims import ClaimsAnalysisResult, OpenAIClaimsClient, load_cached_claims_result
from style_kb.clients.openai_claims_curate import (
    ClaimsCurateResult,
    OpenAIClaimsCurateClient,
    load_cached_claims_curate_result,
)
from style_kb.diagnostics import PipelineEvent
from style_kb.errors import ProviderError, StageExecutionError
from style_kb.models import (
    Chunk,
    ClaimType,
    ConfidenceLevel,
    ProviderSource,
    SourceRef,
    StyleClaim,
    TimelineEvent,
)
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import (
    ProviderOperation,
    emit_stage_validation_failed,
    emit_provider_event,
    load_chunks,
    load_style_claims,
    load_timeline_events,
    log_openai_retry,
    provider_error_extra,
    relative_artifact_path,
    request_id_from_error,
    read_payload,
)
from style_kb.stages.diagnostics import validation_preview
from style_kb.utils.collections import stable_unique
from style_kb.utils.files import append_text, write_json_atomic
from style_kb.utils.ids import style_claim_id
from style_kb.utils.pydantic_io import write_models_jsonl

_SCHEMA_VERSION = 3
_CLAIM_TYPES = set(ClaimType.values())
_CONFIDENCE_LEVELS = set(ConfidenceLevel.values())
_RECOVERABLE_PROVIDER_ERROR_CODES = {
    "openai_claims_invalid_payload",
    "openai_claims_count_exceeded",
    "openai_claims_invalid_claim",
    "openai_claims_empty_required_field",
    "openai_claims_invalid_enum",
    "openai_claims_metadata_leak",
    "openai_claims_service_claim",
    "openai_claims_json_parse_failed",
    "openai_claims_output_missing",
}
_SERVICE_SUBJECTS = {
    "chunk id",
    "chunk_id",
    "event id",
    "event_id",
    "grounding",
    "grounding ids",
    "grounding_ids",
    "metadata",
    "provenance",
    "source",
    "source ref",
    "source refs",
    "sources",
    "timeline event ids",
    "timeline_event_ids",
}
_SERVICE_TOPICS = {
    "event_id",
    "grounding",
    "metadata",
    "provenance",
    "source",
    "source_ref",
    "source_refs",
    "sources",
    "timeline",
    "timeline_event_ids",
}
_TECHNICAL_MARKERS = {
    "chunk_id",
    "event_id",
    "timeline_event_ids",
    "source_ref",
    "source_refs",
    "timestamp_url",
    "video_id",
    "raw",
    "grounding",
    "provenance",
    "metadata",
    "schema",
    "fingerprint",
}
_TECHNICAL_PHRASES = {
    "источник и временной диапазон",
    "связанные события таймлайна",
}
_EVIDENCE_META_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"(?:в\s+)?chunk|"
    r"on[- ]screen|"
    r"в\s+видео|"
    r"в\s+кадре|"
    r"ведущий|"
    r"визуально"
    r")\s*(?:[:：-]|\s+говорится\s*,?\s*(?:что\s*)?|\s+)?",
    re.IGNORECASE,
)
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_LATIN_HOMOGLYPH_RE = re.compile(r"[AaEeOoPpCcXxYyKkMm]")
_LATIN_TO_CYRILLIC_HOMOGLYPHS = str.maketrans(
    {
        "A": "А",
        "a": "а",
        "E": "Е",
        "e": "е",
        "O": "О",
        "o": "о",
        "P": "Р",
        "p": "р",
        "C": "С",
        "c": "с",
        "X": "Х",
        "x": "х",
        "Y": "У",
        "y": "у",
        "K": "К",
        "k": "к",
        "M": "М",
        "m": "м",
    }
)
_MAX_TOPICS_PER_CLAIM = 8


@dataclass(slots=True)
class _ChunkClaimsResult:
    claims: list[StyleClaim]
    raw_files: list[Path]
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    cache_hit: bool
    diagnostics: ProviderCallDiagnostics | None = None


@dataclass(slots=True)
class _ClaimsCleanupResult:
    claims: list[StyleClaim]
    changed_claims_count: int
    evidence_meta_prefix_cleaned_count: int
    homoglyph_cleaned_fields_count: int
    topics_truncated_count: int


@dataclass(slots=True)
class _CuratedClaimsResult:
    claims: list[StyleClaim]
    raw_files: list[Path]
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    metrics: dict[str, Any]


@dataclass(slots=True)
class _CurateApplication:
    claims: list[StyleClaim]
    decisions: list[dict[str, Any]]
    invalid_decisions: list[dict[str, Any]]
    missing_decision_ids: list[str]
    merged_claim_ids: list[str]
    confidence_changes: list[dict[str, str]]
    split_candidates: list[dict[str, Any]]
    rewrite_suggestions: list[dict[str, str]]
    applies_to_notes: list[dict[str, str]]


class _RecoverableClaimsOutputError(ProviderError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        validation_errors: list[str],
        structured_errors: list[dict[str, Any]] | None = None,
        details: str | None = None,
    ) -> None:
        super().__init__(message, error_code=error_code, details=details)
        self.validation_errors = validation_errors
        self.structured_errors = structured_errors or []


class Stage13ExtractStyleClaims(Stage):
    name = "13_extract_style_claims"
    ordinal = 13

    def input_files(self, context: StageContext) -> list:
        inputs = [
            context.paths.timeline_events_jsonl,
            context.paths.chunks_jsonl,
            _prompt_path(context),
        ]
        if context.config.style_claims.curate.enabled:
            inputs.append(_curate_prompt_path(context))
        return inputs

    def output_files(self, context: StageContext) -> list:
        outputs = [context.paths.style_claims_jsonl, context.paths.style_claims_raw]
        if context.config.style_claims.curate.enabled:
            outputs.append(context.paths.style_claims_curate_raw)
        if context.paths.style_claims_errors.exists():
            outputs.append(context.paths.style_claims_errors)
        return outputs

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.style_claims_jsonl.exists() or not context.paths.style_claims_raw.exists():
            return False
        if context.config.style_claims.curate.enabled and not context.paths.style_claims_curate_raw.exists():
            return False
        summary = read_payload(context.paths.style_claims_raw)
        if not _summary_matches_config(summary, context):
            return False
        chunks = load_chunks(context.paths.chunks_jsonl) if context.paths.chunks_jsonl.exists() else []
        claims = load_style_claims(context.paths.style_claims_jsonl)
        return _claims_are_valid(claims, chunks, summary, context)

    def run(self, context: StageContext) -> StageResult:
        chunks = load_chunks(context.paths.chunks_jsonl)
        if not chunks:
            raise StageExecutionError("cannot extract style claims from empty chunks", error_code="claims_empty_chunks")
        if context.paths.style_claims_errors.exists():
            context.paths.style_claims_errors.unlink()

        prompt_text = _prompt_path(context).read_text(encoding="utf-8")
        prompt_sha = _sha256_text(prompt_text)
        timeline_events = load_timeline_events(context.paths.timeline_events_jsonl)
        event_map = {event.event_id: event for event in timeline_events}
        client: OpenAIClaimsClient | None = None
        all_candidates: list[StyleClaim] = []
        canonical_raw_files: list[Path] = []
        raw_output_files: list[Path] = []
        claim_errors: list[dict[str, Any]] = []
        legacy_raw_cache_removed_count = _remove_legacy_raw_caches(context, chunks)
        cached_chunks = 0
        api_chunks = 0
        input_tokens = 0
        output_tokens = 0
        reasoning_tokens = 0
        total_tokens = 0

        for chunk in chunks:
            missing_event_ids = [event_id for event_id in chunk.timeline_event_ids if event_id not in event_map]
            if missing_event_ids:
                raise StageExecutionError(
                    f"chunk {chunk.chunk_id} references missing timeline events",
                    error_code="claims_chunk_timeline_events_missing",
                    details=", ".join(missing_event_ids),
                )
            chunk_events = [event_map[event_id] for event_id in chunk.timeline_event_ids if event_id in event_map]
            chunk_payload = _chunk_payload(chunk, chunk_events)
            request_metadata = _request_metadata(context, chunk, chunk_payload, prompt_sha)
            raw_path = context.paths.style_claims_raw_chunk(chunk.chunk_id)
            chunk_result = _load_cached_chunk_result(raw_path, request_metadata, chunk, context)
            if chunk_result is None:
                if client is None:
                    client = OpenAIClaimsClient(
                        os.environ.get("OPENAI_API_KEY"),
                        model=context.config.style_claims.model,
                        on_retry=_claims_retry_logger(context),
                    )
                chunk_result = _extract_chunk_claims_with_retries(
                    context=context,
                    client=client,
                    chunk=chunk,
                    chunk_payload=chunk_payload,
                    request_metadata=request_metadata,
                    system_prompt=prompt_text,
                    canonical_raw_path=raw_path,
                    claim_errors=claim_errors,
                )
                api_chunks += 1
            else:
                cached_chunks += 1

            raw_output_files.extend(chunk_result.raw_files)
            canonical_raw_files.append(raw_path)
            all_candidates.extend(chunk_result.claims)
            input_tokens += chunk_result.input_tokens
            output_tokens += chunk_result.output_tokens
            reasoning_tokens += chunk_result.reasoning_tokens
            total_tokens += chunk_result.total_tokens

        cleanup_result = _cleanup_claims(all_candidates)
        cleaned_candidates = cleanup_result.claims
        style_claims_after_exact_dedupe = _dedupe_claims(cleaned_candidates, context.job.video_id)
        curate_result = _curate_claims_if_enabled(context, style_claims_after_exact_dedupe)
        style_claims = curate_result.claims
        raw_output_files.extend(curate_result.raw_files)
        input_tokens += curate_result.input_tokens
        output_tokens += curate_result.output_tokens
        reasoning_tokens += curate_result.reasoning_tokens
        total_tokens += curate_result.total_tokens
        summary = _summary_payload(
            context=context,
            chunks=chunks,
            raw_files=canonical_raw_files,
            prompt_sha=prompt_sha,
            style_claims=style_claims,
            claims_before_dedupe=len(all_candidates),
            claims_after_exact_dedupe=len(style_claims_after_exact_dedupe),
            claims_after_dedupe=len(style_claims),
            cleanup_result=cleanup_result,
            curate_metrics=curate_result.metrics,
            legacy_raw_cache_removed_count=legacy_raw_cache_removed_count,
        )
        write_models_jsonl(context.paths.style_claims_jsonl, style_claims)
        write_json_atomic(context.paths.style_claims_raw, summary)
        if context.paths.style_claims_errors.exists():
            context.paths.style_claims_errors.unlink()
        return StageResult(
            output_files=[*self.output_files(context), *raw_output_files],
            metrics={
                "style_claims_count": len(style_claims),
                "claims_before_dedupe": len(all_candidates),
                "claims_after_exact_dedupe": len(style_claims_after_exact_dedupe),
                "cached_chunks_count": cached_chunks,
                "api_chunks_count": api_chunks,
                "legacy_raw_cache_removed_count": legacy_raw_cache_removed_count,
                "claim_retry_errors_count": len(claim_errors),
                "deterministic_cleanup_changed_claims_count": cleanup_result.changed_claims_count,
                "curate_merged_count": curate_result.metrics.get("curate_merged_count", 0),
                "curate_confidence_changed_count": curate_result.metrics.get("curate_confidence_changed_count", 0),
                "curate_split_candidates_count": curate_result.metrics.get("curate_split_candidates_count", 0),
                "curate_rewrite_suggestions_count": curate_result.metrics.get("curate_rewrite_suggestions_count", 0),
                "input_tokens_total": input_tokens,
                "output_tokens_total": output_tokens,
                "reasoning_tokens_total": reasoning_tokens,
                "total_tokens_total": total_tokens,
            },
        )


def _claims_from_payload(payload: Any, chunk: Chunk, context: StageContext) -> list[StyleClaim]:
    if not isinstance(payload, dict):
        raise _RecoverableClaimsOutputError(
            f"OpenAI claims response for chunk {chunk.chunk_id} is not an object",
            error_code="openai_claims_invalid_payload",
            validation_errors=["response is not an object"],
            structured_errors=[
                _validation_entry(
                    code="openai_claims_invalid_payload",
                    message="response is not an object",
                    field="response",
                    preview=payload,
                )
            ],
        )
    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list):
        raise _RecoverableClaimsOutputError(
            f"OpenAI claims response for chunk {chunk.chunk_id} has no claims array",
            error_code="openai_claims_invalid_payload",
            validation_errors=["response has no claims array"],
            structured_errors=[
                _validation_entry(
                    code="openai_claims_invalid_payload",
                    message="response has no claims array",
                    field="claims",
                    preview=payload,
                )
            ],
        )
    if len(raw_claims) > context.config.style_claims.max_claims_per_chunk:
        raise _RecoverableClaimsOutputError(
            f"OpenAI claims response for chunk {chunk.chunk_id} exceeded max_claims_per_chunk",
            error_code="openai_claims_count_exceeded",
            validation_errors=[
                "claims_count="
                f"{len(raw_claims)} exceeds max_claims_per_chunk="
                f"{context.config.style_claims.max_claims_per_chunk}"
            ],
            structured_errors=[
                _validation_entry(
                    code="openai_claims_count_exceeded",
                    message="claims_count exceeds max_claims_per_chunk",
                    field="claims",
                    preview={"claims_count": len(raw_claims)},
                )
            ],
        )

    validation_errors: list[str] = []
    structured_errors: list[dict[str, Any]] = []
    claims: list[StyleClaim] = []
    for index, raw_claim in enumerate(raw_claims, start=1):
        if not isinstance(raw_claim, dict):
            message = f"claim {index} is not an object"
            validation_errors.append(message)
            structured_errors.append(
                _validation_entry(
                    code="openai_claims_invalid_claim",
                    message=message,
                    claim_index=index,
                    field="claim",
                    preview=raw_claim,
                )
            )
            continue
        claim_errors, claim_structured_errors = _raw_claim_validation_errors(raw_claim, context, index=index)
        if claim_errors:
            validation_errors.extend(claim_errors)
            structured_errors.extend(claim_structured_errors)
            continue
        try:
            claims.append(_claim_from_raw(raw_claim, chunk=chunk, context=context, index=index))
        except _RecoverableClaimsOutputError as error:
            validation_errors.extend(error.validation_errors)
            structured_errors.extend(error.structured_errors)
        except PydanticValidationError as error:
            message = f"claim {index} cannot be materialized: {type(error).__name__}: {error}"
            validation_errors.append(message)
            structured_errors.append(
                _validation_entry(
                    code="openai_claims_invalid_claim",
                    message=message,
                    claim_index=index,
                    field="claim",
                    preview=raw_claim,
                )
            )

    if validation_errors:
        raise _RecoverableClaimsOutputError(
            f"OpenAI claims response for chunk {chunk.chunk_id} failed validation",
            error_code=_claim_validation_error_code(validation_errors),
            validation_errors=validation_errors,
            structured_errors=structured_errors,
            details="\n".join(validation_errors),
        )
    return claims


def _raw_claim_validation_errors(
    raw_claim: dict[str, Any],
    context: StageContext,
    *,
    index: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    structured_errors: list[dict[str, Any]] = []
    claim_type = _clean_string(raw_claim.get("claim_type"))
    subject = _clean_string(raw_claim.get("subject"))
    claim_text = _clean_string(raw_claim.get("claim"))
    rationale = _clean_string(raw_claim.get("rationale"))
    evidence = _clean_list(raw_claim.get("evidence"))
    confidence = _clean_string(raw_claim.get("confidence")) or ConfidenceLevel.MEDIUM.value

    if claim_type not in _CLAIM_TYPES:
        message = f"claim {index} has invalid claim_type={claim_type!r}"
        errors.append(message)
        structured_errors.append(
            _validation_entry(
                code="openai_claims_invalid_enum",
                message=message,
                claim_index=index,
                field="claim_type",
                preview=claim_type,
            )
        )
    if confidence not in _CONFIDENCE_LEVELS:
        message = f"claim {index} has invalid confidence={confidence!r}"
        errors.append(message)
        structured_errors.append(
            _validation_entry(
                code="openai_claims_invalid_enum",
                message=message,
                claim_index=index,
                field="confidence",
                preview=confidence,
            )
        )
    if not subject or not claim_text or not rationale or not evidence:
        message = f"claim {index} contains an empty required field"
        errors.append(message)
        structured_errors.append(
            _validation_entry(
                code="openai_claims_empty_required_field",
                message=message,
                claim_index=index,
                field="required_fields",
                preview={
                    "subject": subject,
                    "claim": claim_text,
                    "rationale": rationale,
                    "evidence": evidence,
                },
            )
        )

    normalized_subject = _normalize_key(subject)
    if normalized_subject in _SERVICE_SUBJECTS:
        message = f"claim {index} has service subject={subject!r}"
        errors.append(message)
        structured_errors.append(
            _validation_entry(
                code="openai_claims_service_claim",
                message=message,
                claim_index=index,
                field="subject",
                preview=subject,
            )
        )

    checked_fields = {
        "subject": [subject],
        "claim": [claim_text],
        "rationale": [rationale],
        "conditions": _clean_list(raw_claim.get("conditions")),
        "applies_to": _clean_list(raw_claim.get("applies_to")),
        "avoid": _clean_list(raw_claim.get("avoid")),
        "prefer": _clean_list(raw_claim.get("prefer")),
        "evidence": evidence,
        "topics": _clean_list(raw_claim.get("topics")),
    }
    for field_name, values in checked_fields.items():
        for value in values:
            marker = _technical_marker(value, context.job.video_id)
            if marker:
                message = f"claim {index} field {field_name} contains technical marker {marker!r}"
                errors.append(message)
                structured_errors.append(
                    _validation_entry(
                        code="openai_claims_metadata_leak",
                        message=message,
                        claim_index=index,
                        field=field_name,
                        marker=marker,
                        preview=value,
                    )
                )

    topic_keys = [_normalize_key(topic) for topic in checked_fields["topics"] if topic.strip()]
    if topic_keys:
        service_topics_count = sum(1 for topic in topic_keys if topic in _SERVICE_TOPICS)
        if service_topics_count >= max(1, (len(topic_keys) + 1) // 2):
            message = f"claim {index} topics are mostly service metadata: {topic_keys}"
            errors.append(message)
            structured_errors.append(
                _validation_entry(
                    code="openai_claims_metadata_leak",
                    message=message,
                    claim_index=index,
                    field="topics",
                    preview=checked_fields["topics"],
                )
            )

    return errors, structured_errors


def _validation_entry(
    *,
    code: str,
    message: str,
    field: str,
    preview: Any,
    claim_index: int | None = None,
    marker: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "code": code,
        "message": message,
        "field": field,
        "preview": validation_preview(preview),
    }
    if claim_index is not None:
        entry["claim_index"] = claim_index
    if marker is not None:
        entry["marker"] = marker
    return entry


def _claim_validation_error_code(errors: list[str]) -> str:
    if any("service subject" in error for error in errors):
        return "openai_claims_service_claim"
    if any("technical marker" in error or "service metadata" in error for error in errors):
        return "openai_claims_metadata_leak"
    if any("empty required field" in error for error in errors):
        return "openai_claims_empty_required_field"
    if any("invalid claim_type" in error or "invalid confidence" in error for error in errors):
        return "openai_claims_invalid_enum"
    return "openai_claims_invalid_claim"


def _technical_marker(value: str, video_id: str) -> str | None:
    normalized = value.casefold()
    if f"{video_id.casefold()}_" in normalized:
        return f"{video_id}_"
    for phrase in _TECHNICAL_PHRASES:
        if phrase in normalized:
            return phrase
    for marker in _TECHNICAL_MARKERS:
        pattern = rf"(?<![a-z0-9_]){re.escape(marker)}(?![a-z0-9_])"
        if re.search(pattern, normalized):
            return marker
    return None


def _raise_empty_required_field(chunk: Chunk) -> None:
    raise _RecoverableClaimsOutputError(
        f"OpenAI claims response for chunk {chunk.chunk_id} contains an empty required field",
        error_code="openai_claims_empty_required_field",
        validation_errors=["claim contains an empty required field"],
        structured_errors=[
            _validation_entry(
                code="openai_claims_empty_required_field",
                message="claim contains an empty required field",
                field="required_fields",
                preview={"chunk_id": chunk.chunk_id},
            )
        ],
    )


def _claim_from_raw(raw_claim: dict[str, Any], *, chunk: Chunk, context: StageContext, index: int) -> StyleClaim:
    claim_type = _clean_string(raw_claim.get("claim_type"))
    subject = _clean_string(raw_claim.get("subject"))
    claim_text = _clean_string(raw_claim.get("claim"))
    rationale = _clean_string(raw_claim.get("rationale"))
    evidence = _clean_list(raw_claim.get("evidence"))
    if not claim_type or not subject or not claim_text or not rationale or not evidence:
        _raise_empty_required_field(chunk)
    return StyleClaim(
        claim_id=f"{chunk.chunk_id}_pending_claim_{index:02d}",
        video_id=context.job.video_id,
        chunk_id=chunk.chunk_id,
        timeline_event_ids=stable_unique(chunk.timeline_event_ids),
        claim_type=ClaimType(claim_type),
        subject=subject,
        claim=claim_text,
        rationale=rationale,
        conditions=_clean_list(raw_claim.get("conditions")),
        applies_to=_clean_list(raw_claim.get("applies_to")),
        avoid=_clean_list(raw_claim.get("avoid")),
        prefer=_clean_list(raw_claim.get("prefer")),
        evidence=evidence,
        topics=_clean_list(raw_claim.get("topics")) or chunk.topics[:8],
        confidence=ConfidenceLevel(_clean_string(raw_claim.get("confidence")) or ConfidenceLevel.MEDIUM.value),
        source=ProviderSource(
            provider=context.config.style_claims.provider,
            model=context.config.style_claims.model,
        ),
        start=chunk.start,
        end=chunk.end,
        timestamp_url=chunk.timestamp_url,
        source_refs=chunk.source_refs,
    )


def _dedupe_claims(candidates: list[StyleClaim], video_id: str) -> list[StyleClaim]:
    claims_by_key: dict[tuple[str, str, str], StyleClaim] = {}
    for candidate in candidates:
        key = (
            candidate.claim_type,
            _normalize_key(candidate.subject),
            _normalize_key(candidate.claim),
        )
        existing = claims_by_key.get(key)
        if existing is None:
            claims_by_key[key] = candidate
            continue
        claims_by_key[key] = _merge_claims(existing, candidate)

    ordered = sorted(claims_by_key.values(), key=lambda claim: (claim.start, claim.chunk_id, claim.claim))
    return [
        claim.model_copy(update={"claim_id": style_claim_id(video_id, index)})
        for index, claim in enumerate(ordered, start=1)
    ]


def _merge_claims(existing: StyleClaim, candidate: StyleClaim) -> StyleClaim:
    return existing.model_copy(
        update={
            "timeline_event_ids": stable_unique([*existing.timeline_event_ids, *candidate.timeline_event_ids]),
            "conditions": stable_unique([*existing.conditions, *candidate.conditions]),
            "applies_to": stable_unique([*existing.applies_to, *candidate.applies_to]),
            "avoid": stable_unique([*existing.avoid, *candidate.avoid]),
            "prefer": stable_unique([*existing.prefer, *candidate.prefer]),
            "evidence": stable_unique([*existing.evidence, *candidate.evidence]),
            "topics": stable_unique([*existing.topics, *candidate.topics]),
            "source_refs": _merge_source_refs(existing.source_refs, candidate.source_refs),
        }
    )


def _merge_source_refs(left: list[SourceRef], right: list[SourceRef]) -> list[SourceRef]:
    seen: set[str] = set()
    merged: list[SourceRef] = []
    for source_ref in [*left, *right]:
        key = json.dumps(source_ref.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        merged.append(source_ref)
    return merged


def _cleanup_claims(claims: list[StyleClaim]) -> _ClaimsCleanupResult:
    cleaned_claims: list[StyleClaim] = []
    changed_claims_count = 0
    evidence_meta_prefix_cleaned_count = 0
    homoglyph_cleaned_fields_count = 0
    topics_truncated_count = 0

    for claim in claims:
        update: dict[str, Any] = {}
        scalar_fields = {
            "subject": claim.subject,
            "claim": claim.claim,
            "rationale": claim.rationale,
        }
        for field_name, value in scalar_fields.items():
            cleaned, homoglyph_changed = _cleanup_text(value)
            if cleaned != value:
                update[field_name] = cleaned
                if homoglyph_changed:
                    homoglyph_cleaned_fields_count += 1

        list_fields = {
            "conditions": claim.conditions,
            "applies_to": claim.applies_to,
            "avoid": claim.avoid,
            "prefer": claim.prefer,
            "evidence": claim.evidence,
            "topics": claim.topics,
        }
        for field_name, values in list_fields.items():
            cleaned_values: list[str] = []
            for value in values:
                if field_name == "evidence":
                    stripped = _strip_evidence_meta_prefix(value)
                    if stripped != value:
                        evidence_meta_prefix_cleaned_count += 1
                    value = stripped
                cleaned, homoglyph_changed = _cleanup_text(value)
                if homoglyph_changed:
                    homoglyph_cleaned_fields_count += 1
                if cleaned:
                    cleaned_values.append(cleaned)
            cleaned_values = stable_unique(cleaned_values)
            if field_name == "topics" and len(cleaned_values) > _MAX_TOPICS_PER_CLAIM:
                cleaned_values = cleaned_values[:_MAX_TOPICS_PER_CLAIM]
                topics_truncated_count += 1
            if cleaned_values != values:
                update[field_name] = cleaned_values

        if update:
            changed_claims_count += 1
            cleaned_claims.append(claim.model_copy(update=update))
        else:
            cleaned_claims.append(claim)

    return _ClaimsCleanupResult(
        claims=cleaned_claims,
        changed_claims_count=changed_claims_count,
        evidence_meta_prefix_cleaned_count=evidence_meta_prefix_cleaned_count,
        homoglyph_cleaned_fields_count=homoglyph_cleaned_fields_count,
        topics_truncated_count=topics_truncated_count,
    )


def _cleanup_text(value: str) -> tuple[str, bool]:
    normalized = _clean_string(value)
    cleaned = _replace_cyrillic_homoglyphs(normalized)
    return cleaned, cleaned != normalized


def _strip_evidence_meta_prefix(value: str) -> str:
    cleaned = _EVIDENCE_META_PREFIX_RE.sub("", value).strip()
    cleaned = re.sub(r"^\s*говорится\s*,?\s*(?:что\s*)?", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def _replace_cyrillic_homoglyphs(value: str) -> str:
    def replace_token(match: re.Match[str]) -> str:
        token = match.group(0)
        if not _CYRILLIC_RE.search(token) or not _LATIN_HOMOGLYPH_RE.search(token):
            return token
        return token.translate(_LATIN_TO_CYRILLIC_HOMOGLYPHS)

    return re.sub(r"[\w-]+", replace_token, value)


def _renumber_claims(claims: list[StyleClaim], video_id: str) -> list[StyleClaim]:
    return [
        claim.model_copy(update={"claim_id": style_claim_id(video_id, index)})
        for index, claim in enumerate(claims, start=1)
    ]


def _curate_claims_if_enabled(context: StageContext, claims: list[StyleClaim]) -> _CuratedClaimsResult:
    curate_config = context.config.style_claims.curate
    if not curate_config.enabled:
        return _CuratedClaimsResult(
            claims=_renumber_claims(claims, context.job.video_id),
            raw_files=[],
            input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
            total_tokens=0,
            metrics=_disabled_curate_metrics(len(claims)),
        )

    prompt_text = _curate_prompt_path(context).read_text(encoding="utf-8")
    prompt_sha = _sha256_text(prompt_text)
    claims_payload = _curate_claims_payload(claims)
    request_metadata = _curate_request_metadata(context, claims_payload, prompt_sha)
    raw_path = context.paths.style_claims_raw_curate
    analysis, cache_hit = _load_cached_curate_analysis(raw_path, request_metadata, context)
    if analysis is None:
        analysis = _run_claims_curate_request(
            context=context,
            claims_payload=claims_payload,
            request_metadata=request_metadata,
            system_prompt=prompt_text,
            raw_path=raw_path,
        )
        cache_hit = False

    application = _apply_curate_payload(claims, analysis.payload, context.job.video_id)
    metrics = _curate_metrics(
        context=context,
        prompt_sha=prompt_sha,
        claims_before=len(claims),
        application=application,
        analysis=analysis,
        cache_hit=cache_hit,
        raw_path=raw_path,
    )
    write_json_atomic(
        context.paths.style_claims_curate_raw,
        _curate_audit_payload(
            context=context,
            request_metadata=request_metadata,
            raw_path=raw_path,
            analysis=analysis,
            application=application,
            metrics=metrics,
        ),
    )
    return _CuratedClaimsResult(
        claims=application.claims,
        raw_files=[raw_path],
        input_tokens=analysis.usage["input_tokens"],
        output_tokens=analysis.usage["output_tokens"],
        reasoning_tokens=analysis.usage["reasoning_tokens"],
        total_tokens=analysis.usage["total_tokens"],
        metrics=metrics,
    )


def _load_cached_curate_analysis(
    raw_path: Path,
    request_metadata: dict[str, Any],
    context: StageContext,
) -> tuple[ClaimsCurateResult | None, bool]:
    if not raw_path.exists():
        return None, False
    try:
        analysis = load_cached_claims_curate_result(raw_path)
    except Exception as error:
        _log_claims_curate_cache(
            context,
            cache_path=raw_path,
            cache_hit=False,
            validation_errors=[f"{type(error).__name__}: {error}"],
        )
        return None, False
    if analysis.raw_payload.get("request") != request_metadata:
        _log_claims_curate_cache(
            context,
            cache_path=raw_path,
            cache_hit=False,
            validation_errors=["request metadata mismatch"],
            analysis=analysis,
        )
        return None, False
    _log_claims_curate_cache(context, cache_path=raw_path, cache_hit=True, validation_errors=[], analysis=analysis)
    return analysis, True


def _run_claims_curate_request(
    *,
    context: StageContext,
    claims_payload: list[dict[str, Any]],
    request_metadata: dict[str, Any],
    system_prompt: str,
    raw_path: Path,
) -> ClaimsCurateResult:
    event_extra = {
        "claims_count": len(claims_payload),
        "max_retries": context.config.style_claims.curate.max_retries,
    }
    emit_provider_event(
        context,
        PipelineEvent.PROVIDER_REQUEST_STARTED,
        stage_name=Stage13ExtractStyleClaims.name,
        ordinal=Stage13ExtractStyleClaims.ordinal,
        operation=ProviderOperation.CLAIMS_CURATE,
        diagnostics=ProviderCallDiagnostics(
            provider=ProviderName.OPENAI,
            model=context.config.style_claims.curate.model,
            raw_output_path=str(raw_path),
        ),
        attempt=1,
        message="claims curation request started",
        extra=event_extra,
    )
    client = OpenAIClaimsCurateClient(
        os.environ.get("OPENAI_API_KEY"),
        model=context.config.style_claims.curate.model,
        reasoning_effort=context.config.style_claims.curate.reasoning_effort,
        on_retry=_claims_curate_retry_logger(context),
    )
    try:
        analysis = client.curate_claims(
            system_prompt=system_prompt,
            claims_payload=claims_payload,
            constraints_payload=_curate_constraints_payload(context),
            request_metadata=request_metadata,
            raw_output_path=raw_path,
        )
    except ProviderError as error:
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_FAILED,
            stage_name=Stage13ExtractStyleClaims.name,
            ordinal=Stage13ExtractStyleClaims.ordinal,
            operation=ProviderOperation.CLAIMS_CURATE,
            diagnostics=ProviderCallDiagnostics(
                provider=ProviderName.OPENAI,
                model=context.config.style_claims.curate.model,
                raw_output_path=str(raw_path),
                request_id=request_id_from_error(error),
            ),
            attempt=1,
            message="claims curation request failed",
            extra={**provider_error_extra(error), **event_extra},
        )
        raise
    emit_provider_event(
        context,
        PipelineEvent.PROVIDER_REQUEST_COMPLETED,
        stage_name=Stage13ExtractStyleClaims.name,
        ordinal=Stage13ExtractStyleClaims.ordinal,
        operation=ProviderOperation.CLAIMS_CURATE,
        diagnostics=analysis.diagnostics,
        attempt=1,
        message="claims curation request completed",
        extra=event_extra,
    )
    return analysis


def _apply_curate_payload(
    claims: list[StyleClaim],
    payload: dict[str, Any],
    video_id: str,
) -> _CurateApplication:
    claim_by_id = {claim.claim_id: claim for claim in claims}
    raw_decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(raw_decisions, list):
        return _CurateApplication(
            claims=_renumber_claims(claims, video_id),
            decisions=[],
            invalid_decisions=[
                {
                    "claim_id": "",
                    "reason": "curation payload has no decisions array",
                }
            ],
            missing_decision_ids=[claim.claim_id for claim in claims],
            merged_claim_ids=[],
            confidence_changes=[],
            split_candidates=[],
            rewrite_suggestions=[],
            applies_to_notes=[],
        )

    decisions_by_id: dict[str, dict[str, Any]] = {}
    normalized_decisions: list[dict[str, Any]] = []
    invalid_decisions: list[dict[str, Any]] = []
    for index, raw_decision in enumerate(raw_decisions, start=1):
        decision, decision_errors = _normalize_curate_decision(raw_decision, claim_by_id, index=index)
        if decision_errors:
            invalid_decisions.extend(decision_errors)
        if decision is None:
            continue
        claim_id = decision["claim_id"]
        if claim_id in decisions_by_id:
            invalid_decisions.append({"claim_id": claim_id, "reason": "duplicate decision"})
            continue
        decisions_by_id[claim_id] = decision
        normalized_decisions.append(decision)

    missing_decision_ids = [claim.claim_id for claim in claims if claim.claim_id not in decisions_by_id]
    valid_merges: dict[str, str] = {}
    for claim_id, decision in decisions_by_id.items():
        if claim_id not in claim_by_id:
            continue
        merged_into = decision["merged_into"]
        if decision["keep"] or not merged_into:
            continue
        source_claim = claim_by_id[claim_id]
        target_claim = claim_by_id.get(merged_into)
        target_decision = decisions_by_id.get(merged_into)
        if target_claim is None:
            invalid_decisions.append({"claim_id": claim_id, "reason": f"merged_into unknown claim_id={merged_into!r}"})
            continue
        if target_claim.claim_type != source_claim.claim_type:
            invalid_decisions.append({"claim_id": claim_id, "reason": "merged_into claim_type mismatch"})
            continue
        if target_decision is not None and not target_decision["keep"]:
            invalid_decisions.append({"claim_id": claim_id, "reason": "merged_into target is not kept"})
            continue
        valid_merges[claim_id] = merged_into

    merged_targets: dict[str, StyleClaim] = {}
    for source_id, target_id in valid_merges.items():
        target = merged_targets.get(target_id) or claim_by_id[target_id]
        merged_targets[target_id] = _merge_claims(target, claim_by_id[source_id])

    final_claims: list[StyleClaim] = []
    confidence_changes: list[dict[str, str]] = []
    split_candidates: list[dict[str, Any]] = []
    rewrite_suggestions: list[dict[str, str]] = []
    applies_to_notes: list[dict[str, str]] = []
    for claim in claims:
        if claim.claim_id in valid_merges:
            continue
        decision = decisions_by_id.get(claim.claim_id)
        updated_claim = merged_targets.get(claim.claim_id, claim)
        if decision is not None:
            revised_confidence = ConfidenceLevel(decision["confidence_revised"])
            if revised_confidence != updated_claim.confidence:
                confidence_changes.append(
                    {
                        "claim_id": claim.claim_id,
                        "before": updated_claim.confidence.value,
                        "after": revised_confidence.value,
                    }
                )
                updated_claim = updated_claim.model_copy(update={"confidence": revised_confidence})
            if decision["split_candidate"]:
                split_candidates.append(
                    {
                        "claim_id": claim.claim_id,
                        "split_suggestion": decision["split_suggestion"],
                    }
                )
            if decision["rewrite_suggestion"]:
                rewrite_suggestions.append(
                    {
                        "claim_id": claim.claim_id,
                        "rewrite_suggestion": decision["rewrite_suggestion"],
                    }
                )
            if decision["applies_to_note"]:
                applies_to_notes.append(
                    {
                        "claim_id": claim.claim_id,
                        "applies_to_note": decision["applies_to_note"],
                    }
                )
        final_claims.append(updated_claim)

    return _CurateApplication(
        claims=_renumber_claims(final_claims, video_id),
        decisions=normalized_decisions,
        invalid_decisions=invalid_decisions,
        missing_decision_ids=missing_decision_ids,
        merged_claim_ids=list(valid_merges.keys()),
        confidence_changes=confidence_changes,
        split_candidates=split_candidates,
        rewrite_suggestions=rewrite_suggestions,
        applies_to_notes=applies_to_notes,
    )


def _normalize_curate_decision(
    raw_decision: object,
    claim_by_id: dict[str, StyleClaim],
    *,
    index: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not isinstance(raw_decision, dict):
        return None, [{"claim_id": "", "reason": f"decision {index} is not an object"}]
    claim_id = _clean_string(raw_decision.get("claim_id"))
    errors: list[dict[str, Any]] = []
    if claim_id not in claim_by_id:
        errors.append({"claim_id": claim_id, "reason": f"decision {index} references unknown claim_id"})
    keep = bool(raw_decision.get("keep"))
    merged_into = _clean_string(raw_decision.get("merged_into"))
    if merged_into == claim_id:
        errors.append({"claim_id": claim_id, "reason": "merged_into points to itself"})
        merged_into = ""
    if keep and merged_into:
        errors.append({"claim_id": claim_id, "reason": "kept decision includes merged_into"})
        merged_into = ""
    if not keep and not merged_into:
        errors.append({"claim_id": claim_id, "reason": "dropped decision has empty merged_into"})
    confidence_revised = _clean_string(raw_decision.get("confidence_revised"))
    if confidence_revised not in _CONFIDENCE_LEVELS:
        errors.append({"claim_id": claim_id, "reason": f"invalid confidence_revised={confidence_revised!r}"})
        confidence_revised = claim_by_id[claim_id].confidence.value if claim_id in claim_by_id else ConfidenceLevel.MEDIUM.value
    decision = {
        "claim_id": claim_id,
        "keep": keep,
        "merged_into": merged_into,
        "confidence_revised": confidence_revised,
        "confidence_reason": _clean_string(raw_decision.get("confidence_reason")),
        "split_candidate": bool(raw_decision.get("split_candidate")),
        "split_suggestion": _clean_list(raw_decision.get("split_suggestion")),
        "rewrite_suggestion": _clean_string(raw_decision.get("rewrite_suggestion")),
        "applies_to_note": _clean_string(raw_decision.get("applies_to_note")),
    }
    return decision, errors


def _curate_claims_payload(claims: list[StyleClaim]) -> list[dict[str, Any]]:
    return [
        {
            "claim_id": claim.claim_id,
            "subject": claim.subject,
            "claim_type": claim.claim_type.value,
            "claim": claim.claim,
            "rationale": claim.rationale,
            "conditions": claim.conditions,
            "applies_to": claim.applies_to,
            "avoid": claim.avoid,
            "prefer": claim.prefer,
            "evidence": claim.evidence,
            "topics": claim.topics,
            "confidence": claim.confidence.value,
        }
        for claim in claims
    ]


def _curate_request_metadata(
    context: StageContext,
    claims_payload: list[dict[str, Any]],
    prompt_sha: str,
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "video_id": context.job.video_id,
        "provider": context.config.style_claims.curate.provider,
        "model": context.config.style_claims.curate.model,
        "reasoning_effort": context.config.style_claims.curate.reasoning_effort,
        "prompt_file": context.config.style_claims.curate.prompt_file,
        "prompt_sha256": prompt_sha,
        "max_retries": context.config.style_claims.curate.max_retries,
        "claims_count": len(claims_payload),
        "input_fingerprint": _fingerprint(claims_payload),
    }


def _curate_constraints_payload(context: StageContext) -> dict[str, Any]:
    return {
        "output_language": "ru",
        "claim_ids_are_required": True,
        "decisions_count": "exactly one decision per input claim",
        "safe_auto_applied_fields": ["keep/merged_into for valid semantic duplicates", "confidence_revised"],
        "audit_only_fields": ["split_suggestion", "rewrite_suggestion", "applies_to_note", "confidence_reason"],
        "merge_policy": "merge only if same meaning and same claim_type; keep both when unsure",
        "do_not_output": [
            "new claims",
            "rewritten canonical claim text",
            "technical source refs, chunk ids, timeline ids, timestamps, or metadata",
        ],
    }


def _disabled_curate_metrics(claims_count: int) -> dict[str, Any]:
    return {
        "enabled": False,
        "provider": None,
        "model": None,
        "reasoning_effort": None,
        "prompt_file": None,
        "prompt_sha256": None,
        "max_retries": None,
        "cache_hit": False,
        "raw_file": None,
        "claims_before_curate": claims_count,
        "claims_after_curate": claims_count,
        "curate_merged_count": 0,
        "curate_confidence_changed_count": 0,
        "curate_split_candidates_count": 0,
        "curate_rewrite_suggestions_count": 0,
        "curate_applies_to_notes_count": 0,
        "curate_invalid_decisions_count": 0,
        "curate_missing_decisions_count": 0,
    }


def _curate_metrics(
    *,
    context: StageContext,
    prompt_sha: str,
    claims_before: int,
    application: _CurateApplication,
    analysis: ClaimsCurateResult,
    cache_hit: bool,
    raw_path: Path,
) -> dict[str, Any]:
    return {
        "enabled": True,
        "provider": context.config.style_claims.curate.provider,
        "model": context.config.style_claims.curate.model,
        "reasoning_effort": context.config.style_claims.curate.reasoning_effort,
        "prompt_file": context.config.style_claims.curate.prompt_file,
        "prompt_sha256": prompt_sha,
        "max_retries": context.config.style_claims.curate.max_retries,
        "cache_hit": cache_hit,
        "raw_file": relative_artifact_path(context.paths.job_dir, raw_path),
        "claims_before_curate": claims_before,
        "claims_after_curate": len(application.claims),
        "curate_merged_count": len(application.merged_claim_ids),
        "curate_confidence_changed_count": len(application.confidence_changes),
        "curate_split_candidates_count": len(application.split_candidates),
        "curate_rewrite_suggestions_count": len(application.rewrite_suggestions),
        "curate_applies_to_notes_count": len(application.applies_to_notes),
        "curate_invalid_decisions_count": len(application.invalid_decisions),
        "curate_missing_decisions_count": len(application.missing_decision_ids),
        "input_tokens": analysis.usage["input_tokens"],
        "output_tokens": analysis.usage["output_tokens"],
        "reasoning_tokens": analysis.usage["reasoning_tokens"],
        "total_tokens": analysis.usage["total_tokens"],
    }


def _curate_audit_payload(
    *,
    context: StageContext,
    request_metadata: dict[str, Any],
    raw_path: Path,
    analysis: ClaimsCurateResult,
    application: _CurateApplication,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "video_id": context.job.video_id,
        "stage": Stage13ExtractStyleClaims.name,
        "artifact_role": "audit_only_not_for_import",
        "request": request_metadata,
        "raw_file": relative_artifact_path(context.paths.job_dir, raw_path),
        "diagnostics": analysis.diagnostics.to_dict(),
        "metrics": metrics,
        "decisions": application.decisions,
        "applied": {
            "merged_claim_ids": application.merged_claim_ids,
            "confidence_changes": application.confidence_changes,
        },
        "audit_only": {
            "split_candidates": application.split_candidates,
            "rewrite_suggestions": application.rewrite_suggestions,
            "applies_to_notes": application.applies_to_notes,
        },
        "ignored": {
            "invalid_decisions": application.invalid_decisions,
            "missing_decision_ids": application.missing_decision_ids,
        },
    }


def _claims_are_valid(
    claims: list[StyleClaim],
    chunks: list[Chunk],
    summary: dict[str, Any],
    context: StageContext,
) -> bool:
    if summary.get("claims_after_dedupe") != len(claims):
        return False
    if summary.get("chunks_count") != len(chunks):
        return False
    if summary.get("raw_files_count") != len(chunks):
        return False
    claims_before_dedupe = summary.get("claims_before_dedupe")
    if not isinstance(claims_before_dedupe, int) or claims_before_dedupe < len(claims):
        return False
    chunk_ids = {chunk.chunk_id for chunk in chunks}
    timeline_event_ids = {event_id for chunk in chunks for event_id in chunk.timeline_event_ids}
    seen_keys: set[tuple[str, str, str]] = set()
    for index, claim in enumerate(claims, start=1):
        if claim.claim_id != style_claim_id(context.job.video_id, index):
            return False
        if claim.video_id != context.job.video_id:
            return False
        if claim.chunk_id not in chunk_ids:
            return False
        if not set(claim.timeline_event_ids).issubset(timeline_event_ids):
            return False
        if claim.source.provider != context.config.style_claims.provider:
            return False
        if claim.source.model != context.config.style_claims.model:
            return False
        if not claim.subject.strip() or not claim.claim.strip() or not claim.rationale.strip():
            return False
        if not claim.evidence or not claim.source_refs:
            return False
        if _style_claim_content_validation_errors(claim, index=index, context=context):
            return False
        key = (claim.claim_type, _normalize_key(claim.subject), _normalize_key(claim.claim))
        if key in seen_keys:
            return False
        seen_keys.add(key)
    return True


def _summary_matches_config(summary: dict[str, Any], context: StageContext) -> bool:
    prompt_path = _prompt_path(context)
    if not prompt_path.exists():
        return False
    prompt_sha = _sha256_text(prompt_path.read_text(encoding="utf-8"))
    curate_config = context.config.style_claims.curate
    if curate_config.enabled:
        curate_prompt_path = _curate_prompt_path(context)
        if not curate_prompt_path.exists():
            return False
        curate_prompt_sha: str | None = _sha256_text(curate_prompt_path.read_text(encoding="utf-8"))
    else:
        curate_prompt_sha = None
    curate_summary = summary.get("curate") if isinstance(summary.get("curate"), dict) else {}
    return (
        summary.get("schema_version") == _SCHEMA_VERSION
        and summary.get("video_id") == context.job.video_id
        and summary.get("provider") == context.config.style_claims.provider
        and summary.get("model") == context.config.style_claims.model
        and summary.get("prompt_file") == context.config.style_claims.prompt_file
        and summary.get("prompt_sha256") == prompt_sha
        and summary.get("max_claims_per_chunk") == context.config.style_claims.max_claims_per_chunk
        and summary.get("max_retries") == context.config.style_claims.max_retries
        and curate_summary.get("enabled") == curate_config.enabled
        and curate_summary.get("provider") == (curate_config.provider if curate_config.enabled else None)
        and curate_summary.get("model") == (curate_config.model if curate_config.enabled else None)
        and curate_summary.get("reasoning_effort") == (
            curate_config.reasoning_effort if curate_config.enabled else None
        )
        and curate_summary.get("prompt_file") == (curate_config.prompt_file if curate_config.enabled else None)
        and curate_summary.get("prompt_sha256") == curate_prompt_sha
        and curate_summary.get("max_retries") == (curate_config.max_retries if curate_config.enabled else None)
    )


def _summary_payload(
    *,
    context: StageContext,
    chunks: list[Chunk],
    raw_files: list[Path],
    prompt_sha: str,
    style_claims: list[StyleClaim],
    claims_before_dedupe: int,
    claims_after_exact_dedupe: int,
    claims_after_dedupe: int,
    cleanup_result: _ClaimsCleanupResult,
    curate_metrics: dict[str, Any],
    legacy_raw_cache_removed_count: int,
) -> dict[str, Any]:
    applies_to_counts: dict[str, int] = {}
    for claim in style_claims:
        for value in claim.applies_to:
            applies_to_counts[value] = applies_to_counts.get(value, 0) + 1
    return {
        "schema_version": _SCHEMA_VERSION,
        "video_id": context.job.video_id,
        "provider": context.config.style_claims.provider,
        "model": context.config.style_claims.model,
        "prompt_file": context.config.style_claims.prompt_file,
        "prompt_sha256": prompt_sha,
        "max_claims_per_chunk": context.config.style_claims.max_claims_per_chunk,
        "max_retries": context.config.style_claims.max_retries,
        "chunks_count": len(chunks),
        "claims_before_dedupe": claims_before_dedupe,
        "claims_after_exact_dedupe": claims_after_exact_dedupe,
        "claims_after_dedupe": claims_after_dedupe,
        "deterministic_cleanup": {
            "changed_claims_count": cleanup_result.changed_claims_count,
            "evidence_meta_prefix_cleaned_count": cleanup_result.evidence_meta_prefix_cleaned_count,
            "homoglyph_cleaned_fields_count": cleanup_result.homoglyph_cleaned_fields_count,
            "topics_truncated_count": cleanup_result.topics_truncated_count,
        },
        "applies_to_counts": dict(sorted(applies_to_counts.items())),
        "applies_to_unique_count": len(applies_to_counts),
        "curate": curate_metrics,
        "legacy_raw_cache_removed_count": legacy_raw_cache_removed_count,
        "raw_files_count": len(raw_files),
        "raw_files_sample": [relative_artifact_path(context.paths.job_dir, path) for path in raw_files[:20]],
    }


def _load_cached_chunk_result(
    raw_path: Path,
    request_metadata: dict[str, Any],
    chunk: Chunk,
    context: StageContext,
) -> _ChunkClaimsResult | None:
    if not raw_path.exists():
        return None
    try:
        result = load_cached_claims_result(raw_path)
    except Exception as error:
        _log_claim_cache(
            context,
            chunk=chunk,
            cache_path=raw_path,
            cache_hit=False,
            validation_errors=[f"{type(error).__name__}: {error}"],
        )
        return None
    if result.raw_payload.get("request") != request_metadata:
        _log_claim_cache(
            context,
            chunk=chunk,
            cache_path=raw_path,
            cache_hit=False,
            validation_errors=["request metadata mismatch"],
        )
        return None
    try:
        claims = _claims_from_payload(result.payload, chunk, context)
    except _RecoverableClaimsOutputError as error:
        _log_claim_cache(
            context,
            chunk=chunk,
            cache_path=raw_path,
            cache_hit=False,
            validation_errors=error.validation_errors,
            structured_errors=error.structured_errors,
        )
        error_entry = _claim_error_entry(
            context,
            chunk=chunk,
            attempt=0,
            raw_attempt=0,
            max_retries=context.config.style_claims.max_retries,
            error_code=error.error_code,
            validation_errors=error.validation_errors,
            structured_errors=error.structured_errors,
            raw_output_path=raw_path,
            analysis=result,
        )
        _write_claim_errors(context, [error_entry])
        emit_stage_validation_failed(
            context,
            stage_name=Stage13ExtractStyleClaims.name,
            ordinal=Stage13ExtractStyleClaims.ordinal,
            error_code=error.error_code,
            message=f"cached claims validation failed for chunk {chunk.chunk_id}",
            validation_errors=error.validation_errors,
            structured_errors=error.structured_errors,
            raw_output_path=raw_path,
            attempt=0,
            extra={"chunk_id": chunk.chunk_id, "cache_hit": True},
        )
        return None
    _log_claim_cache(context, chunk=chunk, cache_path=raw_path, cache_hit=True, validation_errors=[], analysis=result)
    return _ChunkClaimsResult(
        claims=claims,
        raw_files=[raw_path],
        input_tokens=result.usage["input_tokens"],
        output_tokens=result.usage["output_tokens"],
        reasoning_tokens=result.usage["reasoning_tokens"],
        total_tokens=result.usage["total_tokens"],
        cache_hit=True,
        diagnostics=result.diagnostics.with_updates(raw_output_path=str(raw_path), cached=True),
    )


def _extract_chunk_claims_with_retries(
    *,
    context: StageContext,
    client: OpenAIClaimsClient,
    chunk: Chunk,
    chunk_payload: dict[str, Any],
    request_metadata: dict[str, Any],
    system_prompt: str,
    canonical_raw_path: Path,
    claim_errors: list[dict[str, Any]],
) -> _ChunkClaimsResult:
    retry_feedback: list[str] = []
    attempt_files: list[Path] = []
    max_retries = context.config.style_claims.max_retries
    first_raw_attempt = _next_claim_raw_attempt_number(context, chunk)
    for attempt in range(1, max_retries + 1):
        raw_attempt = first_raw_attempt + attempt - 1
        attempt_path = context.paths.style_claims_raw_attempt(chunk.chunk_id, raw_attempt)
        attempt_files.append(attempt_path)
        validation_errors: list[str] = []
        structured_validation_errors: list[dict[str, Any]] = []
        error_code = "openai_claims_invalid_claim"
        analysis: ClaimsAnalysisResult | None = None
        claims: list[StyleClaim] = []
        event_extra = {
            "chunk_id": chunk.chunk_id,
            "attempt": attempt,
            "raw_attempt": raw_attempt,
            "max_retries": max_retries,
        }
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_STARTED,
            stage_name=Stage13ExtractStyleClaims.name,
            ordinal=Stage13ExtractStyleClaims.ordinal,
            operation=ProviderOperation.CLAIMS_EXTRACT,
            diagnostics=ProviderCallDiagnostics(
                provider=ProviderName.OPENAI,
                model=context.config.style_claims.model,
                raw_output_path=str(attempt_path),
            ),
            attempt=attempt,
            message="claims request started",
            extra=event_extra,
        )
        try:
            analysis = client.extract_claims(
                system_prompt=system_prompt,
                chunk_payload=chunk_payload,
                constraints_payload=_constraints_payload(context, retry_feedback),
                request_metadata=request_metadata,
                raw_output_path=attempt_path,
                max_claims_per_chunk=context.config.style_claims.max_claims_per_chunk,
            )
        except ProviderError as error:
            emit_provider_event(
                context,
                PipelineEvent.PROVIDER_REQUEST_FAILED,
                stage_name=Stage13ExtractStyleClaims.name,
                ordinal=Stage13ExtractStyleClaims.ordinal,
                operation=ProviderOperation.CLAIMS_EXTRACT,
                diagnostics=ProviderCallDiagnostics(
                    provider=ProviderName.OPENAI,
                    model=context.config.style_claims.model,
                    raw_output_path=str(attempt_path),
                    request_id=request_id_from_error(error),
                ),
                attempt=attempt,
                message="claims request failed",
                extra={**provider_error_extra(error), **event_extra},
            )
            if error.error_code not in _RECOVERABLE_PROVIDER_ERROR_CODES:
                raise
            error_code = error.error_code
            validation_errors = [error.details or str(error)]
            structured_validation_errors = [
                _validation_entry(
                    code=error_code,
                    message=validation_errors[0],
                    field="response",
                    preview=validation_errors[0],
                )
            ]
        else:
            emit_provider_event(
                context,
                PipelineEvent.PROVIDER_REQUEST_COMPLETED,
                stage_name=Stage13ExtractStyleClaims.name,
                ordinal=Stage13ExtractStyleClaims.ordinal,
                operation=ProviderOperation.CLAIMS_EXTRACT,
                diagnostics=analysis.diagnostics,
                attempt=attempt,
                message="claims request completed",
                extra=event_extra,
            )
            try:
                claims = _claims_from_payload(analysis.payload, chunk, context)
            except _RecoverableClaimsOutputError as error:
                error_code = error.error_code
                validation_errors = error.validation_errors
                structured_validation_errors = error.structured_errors

        if not validation_errors:
            if analysis is None:
                raise ProviderError(
                    f"OpenAI claims response for chunk {chunk.chunk_id} produced no analysis result",
                    error_code="openai_claims_output_missing",
                )
            write_json_atomic(canonical_raw_path, analysis.raw_payload)
            _remove_duplicate_success_attempt(canonical_raw_path, attempt_path)
            _log_claim_attempt(
                context,
                chunk=chunk,
                attempt=attempt,
                raw_attempt=raw_attempt,
                max_retries=max_retries,
                raw_output_path=canonical_raw_path,
                validation_errors=[],
                error_code=None,
                analysis=analysis,
            )
            return _ChunkClaimsResult(
                claims=claims,
                raw_files=[canonical_raw_path, *[path for path in attempt_files if path.exists()]],
                input_tokens=analysis.usage["input_tokens"],
                output_tokens=analysis.usage["output_tokens"],
                reasoning_tokens=analysis.usage["reasoning_tokens"],
                total_tokens=analysis.usage["total_tokens"],
                cache_hit=False,
                diagnostics=analysis.diagnostics.with_updates(raw_output_path=str(canonical_raw_path)),
            )

        error_entry = _claim_error_entry(
            context,
            chunk=chunk,
            attempt=attempt,
            raw_attempt=raw_attempt,
            max_retries=max_retries,
            error_code=error_code,
            validation_errors=validation_errors,
            structured_errors=structured_validation_errors,
            raw_output_path=attempt_path,
            analysis=analysis,
        )
        claim_errors.append(error_entry)
        _write_claim_errors(context, claim_errors)
        _log_claim_attempt(
            context,
            chunk=chunk,
            attempt=attempt,
            raw_attempt=raw_attempt,
            max_retries=max_retries,
            raw_output_path=attempt_path,
            validation_errors=validation_errors,
            error_code=error_code,
            analysis=analysis,
            structured_errors=structured_validation_errors,
        )
        emit_stage_validation_failed(
            context,
            stage_name=Stage13ExtractStyleClaims.name,
            ordinal=Stage13ExtractStyleClaims.ordinal,
            error_code=error_code,
            message=f"claims validation failed for chunk {chunk.chunk_id}",
            validation_errors=validation_errors,
            structured_errors=structured_validation_errors,
            raw_output_path=attempt_path,
            attempt=attempt,
            extra={"chunk_id": chunk.chunk_id, "raw_attempt": raw_attempt},
        )
        _emit_claim_progress(
            context,
            f"{'retry' if attempt < max_retries else 'failed'} chunk={chunk.chunk_id} "
            f"attempt={attempt}/{max_retries} error_code={error_code}: {validation_errors[0]}",
        )
        retry_feedback = validation_errors

    raise ProviderError(
        f"OpenAI claims response for chunk {chunk.chunk_id} stayed invalid after {max_retries} attempts",
        error_code="openai_claims_invalid_after_retries",
        details="\n".join(retry_feedback[:20]),
    )


def _remove_legacy_raw_caches(context: StageContext, chunks: list[Chunk]) -> int:
    removed: list[tuple[Path, str]] = []
    for chunk in chunks:
        raw_path = context.paths.style_claims_raw_chunk(chunk.chunk_id)
        if not raw_path.exists():
            continue
        reason = _legacy_raw_cache_reason(raw_path, context)
        if reason is None:
            continue
        raw_path.unlink()
        removed.append((raw_path, reason))
    if removed:
        _log_legacy_raw_cache_removal(context, removed)
        _emit_claim_progress(context, f"removed legacy claims raw cache files: {len(removed)}")
    return len(removed)


def _legacy_raw_cache_reason(raw_path: Path, context: StageContext) -> str | None:
    try:
        payload = read_payload(raw_path)
    except Exception as error:
        return f"unreadable cache: {type(error).__name__}: {error}"
    request = payload.get("request") if isinstance(payload, dict) else None
    if not isinstance(request, dict):
        return "missing request metadata"
    if request.get("schema_version") != _SCHEMA_VERSION:
        return f"schema_version={request.get('schema_version')!r}"
    if request.get("max_retries") != context.config.style_claims.max_retries:
        return f"max_retries={request.get('max_retries')!r}"
    return None


def _style_claim_content_validation_errors(
    claim: StyleClaim,
    *,
    index: int,
    context: StageContext,
) -> list[str]:
    errors: list[str] = []
    normalized_subject = _normalize_key(claim.subject)
    if normalized_subject in _SERVICE_SUBJECTS:
        errors.append(f"claim {index} has service subject={claim.subject!r}")

    checked_fields = {
        "subject": [claim.subject],
        "claim": [claim.claim],
        "rationale": [claim.rationale],
        "conditions": claim.conditions,
        "applies_to": claim.applies_to,
        "avoid": claim.avoid,
        "prefer": claim.prefer,
        "evidence": claim.evidence,
        "topics": claim.topics,
    }
    for field_name, values in checked_fields.items():
        for value in values:
            marker = _technical_marker(value, context.job.video_id)
            if marker:
                errors.append(f"claim {index} field {field_name} contains technical marker {marker!r}")
            if field_name == "evidence" and _strip_evidence_meta_prefix(value) != value:
                errors.append(f"claim {index} evidence contains meta prefix")

    topic_keys = [_normalize_key(topic) for topic in claim.topics if topic.strip()]
    if topic_keys:
        service_topics_count = sum(1 for topic in topic_keys if topic in _SERVICE_TOPICS)
        if service_topics_count >= max(1, (len(topic_keys) + 1) // 2):
            errors.append(f"claim {index} topics are mostly service metadata: {topic_keys}")
    return errors


def _claim_error_entry(
    context: StageContext,
    *,
    chunk: Chunk,
    attempt: int,
    raw_attempt: int,
    max_retries: int,
    error_code: str,
    validation_errors: list[str],
    structured_errors: list[dict[str, Any]],
    raw_output_path: Path,
    analysis: ClaimsAnalysisResult | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "video_id": context.job.video_id,
        "chunk_id": chunk.chunk_id,
        "attempt": attempt,
        "raw_attempt": raw_attempt,
        "max_retries": max_retries,
        "will_retry": attempt < max_retries,
        "error_code": error_code,
        "errors": validation_errors,
        "structured_errors": structured_errors,
        "raw_output": str(raw_output_path) if raw_output_path.exists() else None,
        "diagnostics": analysis.diagnostics.to_dict() if analysis is not None else None,
    }


def _write_claim_errors(context: StageContext, errors: list[dict[str, Any]]) -> None:
    write_json_atomic(
        context.paths.style_claims_errors,
        {
            "schema_version": _SCHEMA_VERSION,
            "video_id": context.job.video_id,
            "stage": Stage13ExtractStyleClaims.name,
            "max_retries": context.config.style_claims.max_retries,
            "errors": errors,
        },
    )


def _log_claim_attempt(
    context: StageContext,
    *,
    chunk: Chunk,
    attempt: int,
    raw_attempt: int,
    max_retries: int,
    raw_output_path: Path,
    validation_errors: list[str],
    error_code: str | None,
    analysis: ClaimsAnalysisResult | None = None,
    structured_errors: list[dict[str, Any]] | None = None,
) -> None:
    structured_errors = structured_errors or []
    diagnostics = analysis.diagnostics if analysis is not None else None
    lines = [
        "",
        "[claims-attempt]",
        f"run_id: {context.run_id or '-'}",
        f"chunk_id: {chunk.chunk_id}",
        f"attempt: {attempt}",
        f"raw_attempt: {raw_attempt}",
        f"max_retries: {max_retries}",
        f"raw_output: {raw_output_path}",
        f"error_code: {error_code or '-'}",
        f"model: {diagnostics.model if diagnostics is not None and diagnostics.model else '-'}",
        f"request_id: {diagnostics.request_id if diagnostics is not None and diagnostics.request_id else '-'}",
        f"response_id: {diagnostics.response_id if diagnostics is not None and diagnostics.response_id else '-'}",
        f"started_at: {diagnostics.started_at if diagnostics is not None and diagnostics.started_at else '-'}",
        f"finished_at: {diagnostics.finished_at if diagnostics is not None and diagnostics.finished_at else '-'}",
        f"duration_seconds: {_format_seconds(diagnostics.duration_seconds if diagnostics is not None else None)}",
        f"validation_errors_count: {len(validation_errors)}",
        f"structured_errors_count: {len(structured_errors)}",
        "validation_errors:",
        *[f"  - {error}" for error in validation_errors[:20]],
        *([f"  - ... {len(validation_errors) - 20} more"] if len(validation_errors) > 20 else []),
        "structured_error_previews:",
        *[
            "  - "
            f"{entry.get('code', '-')}"
            f" field={entry.get('field', '-')}"
            f" preview={entry.get('preview', '-')}"
            for entry in structured_errors[:5]
        ],
        "",
    ]
    append_text(context.paths.stage_log(Stage13ExtractStyleClaims.name), "\n".join(lines), encoding="utf-8")


def _log_claim_cache(
    context: StageContext,
    *,
    chunk: Chunk,
    cache_path: Path,
    cache_hit: bool,
    validation_errors: list[str],
    analysis: ClaimsAnalysisResult | None = None,
    structured_errors: list[dict[str, Any]] | None = None,
) -> None:
    structured_errors = structured_errors or []
    diagnostics = analysis.diagnostics if analysis is not None else None
    lines = [
        "",
        "[claims-cache]",
        f"run_id: {context.run_id or '-'}",
        f"chunk_id: {chunk.chunk_id}",
        f"cache_hit: {cache_hit}",
        f"cache_path: {cache_path}",
        f"model: {diagnostics.model if diagnostics is not None and diagnostics.model else '-'}",
        f"request_id: {diagnostics.request_id if diagnostics is not None and diagnostics.request_id else '-'}",
        f"response_id: {diagnostics.response_id if diagnostics is not None and diagnostics.response_id else '-'}",
        f"started_at: {diagnostics.started_at if diagnostics is not None and diagnostics.started_at else '-'}",
        f"finished_at: {diagnostics.finished_at if diagnostics is not None and diagnostics.finished_at else '-'}",
        f"duration_seconds: {_format_seconds(diagnostics.duration_seconds if diagnostics is not None else None)}",
        f"validation_errors_count: {len(validation_errors)}",
        f"structured_errors_count: {len(structured_errors)}",
        "validation_errors:",
        *[f"  - {error}" for error in validation_errors[:20]],
        *([f"  - ... {len(validation_errors) - 20} more"] if len(validation_errors) > 20 else []),
        "structured_error_previews:",
        *[
            "  - "
            f"{entry.get('code', '-')}"
            f" field={entry.get('field', '-')}"
            f" preview={entry.get('preview', '-')}"
            for entry in structured_errors[:5]
        ],
        "",
    ]
    append_text(context.paths.stage_log(Stage13ExtractStyleClaims.name), "\n".join(lines), encoding="utf-8")


def _log_claims_curate_cache(
    context: StageContext,
    *,
    cache_path: Path,
    cache_hit: bool,
    validation_errors: list[str],
    analysis: ClaimsCurateResult | None = None,
) -> None:
    diagnostics = analysis.diagnostics if analysis is not None else None
    lines = [
        "",
        "[claims-curate-cache]",
        f"run_id: {context.run_id or '-'}",
        f"cache_hit: {cache_hit}",
        f"cache_path: {cache_path}",
        f"model: {diagnostics.model if diagnostics is not None and diagnostics.model else '-'}",
        f"request_id: {diagnostics.request_id if diagnostics is not None and diagnostics.request_id else '-'}",
        f"response_id: {diagnostics.response_id if diagnostics is not None and diagnostics.response_id else '-'}",
        f"started_at: {diagnostics.started_at if diagnostics is not None and diagnostics.started_at else '-'}",
        f"finished_at: {diagnostics.finished_at if diagnostics is not None and diagnostics.finished_at else '-'}",
        f"duration_seconds: {_format_seconds(diagnostics.duration_seconds if diagnostics is not None else None)}",
        f"validation_errors_count: {len(validation_errors)}",
        "validation_errors:",
        *[f"  - {error}" for error in validation_errors[:20]],
        *([f"  - ... {len(validation_errors) - 20} more"] if len(validation_errors) > 20 else []),
        "",
    ]
    append_text(context.paths.stage_log(Stage13ExtractStyleClaims.name), "\n".join(lines), encoding="utf-8")


def _log_legacy_raw_cache_removal(context: StageContext, removed: list[tuple[Path, str]]) -> None:
    lines = [
        "",
        "[claims-legacy-cache]",
        f"run_id: {context.run_id or '-'}",
        f"removed_count: {len(removed)}",
        "removed_files:",
        *[f"  - {path} ({reason})" for path, reason in removed],
        "",
    ]
    append_text(context.paths.stage_log(Stage13ExtractStyleClaims.name), "\n".join(lines), encoding="utf-8")


def _emit_claim_progress(context: StageContext, message: str) -> None:
    if context.progress_callback is None:
        return
    context.progress_callback(f"[13 {Stage13ExtractStyleClaims.name}] {message}")


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def _claims_retry_logger(context: StageContext) -> OnRetry:
    def _log_retry(attempt: int, delay_seconds: float, error: BaseException) -> None:
        log_openai_retry(
            context.paths.stage_log(Stage13ExtractStyleClaims.name),
            attempt=attempt,
            delay_seconds=delay_seconds,
            error=error,
            context_lines=[f"run_id: {context.run_id or '-'}", "operation: claims_extract"],
        )

    return _log_retry


def _claims_curate_retry_logger(context: StageContext) -> OnRetry:
    def _log_retry(attempt: int, delay_seconds: float, error: BaseException) -> None:
        log_openai_retry(
            context.paths.stage_log(Stage13ExtractStyleClaims.name),
            attempt=attempt,
            delay_seconds=delay_seconds,
            error=error,
            context_lines=[f"run_id: {context.run_id or '-'}", "operation: claims_curate"],
        )

    return _log_retry


def _next_claim_raw_attempt_number(context: StageContext, chunk: Chunk) -> int:
    pattern = re.compile(rf"^{re.escape(chunk.chunk_id)}_attempt_(\d+)\.json$")
    highest = 0
    for path in context.paths.claims_raw_dir.glob(f"{chunk.chunk_id}_attempt_*.json"):
        match = pattern.match(path.name)
        if match is None:
            continue
        highest = max(highest, int(match.group(1)))
    return highest + 1


def _remove_duplicate_success_attempt(canonical_raw_path: Path, attempt_path: Path) -> None:
    if not canonical_raw_path.exists() or not attempt_path.exists():
        return
    try:
        if canonical_raw_path.read_bytes() == attempt_path.read_bytes():
            attempt_path.unlink()
    except OSError:
        return


def _request_metadata(
    context: StageContext,
    chunk: Chunk,
    chunk_payload: dict[str, Any],
    prompt_sha: str,
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "video_id": context.job.video_id,
        "chunk_id": chunk.chunk_id,
        "provider": context.config.style_claims.provider,
        "model": context.config.style_claims.model,
        "prompt_file": context.config.style_claims.prompt_file,
        "prompt_sha256": prompt_sha,
        "max_claims_per_chunk": context.config.style_claims.max_claims_per_chunk,
        "max_retries": context.config.style_claims.max_retries,
        "input_fingerprint": _fingerprint(chunk_payload),
    }


def _constraints_payload(context: StageContext, retry_feedback: list[str] | None = None) -> dict[str, Any]:
    payload = {
        "output_language": "ru",
        "max_claims_per_chunk": context.config.style_claims.max_claims_per_chunk,
        "claim_types": ClaimType.values(),
        "grounding_is_added_by_python": [
            "chunk_id",
            "timeline_event_ids",
            "timestamp_url",
            "source_refs",
        ],
        "do_not_output": [
            "claims about grounding, source, metadata, provenance, schema, raw payloads, ids, or timestamps",
            "technical identifiers such as chunk_id, event_id, timeline_event_ids, "
            "source_refs, timestamp_url, video_id",
            "any field value whose main subject is source, grounding, metadata, provenance, or timeline ids",
        ],
        "dedupe": "exact deterministic merge after extraction",
    }
    if retry_feedback:
        payload["retry_feedback"] = retry_feedback
    return payload


def _chunk_payload(chunk: Chunk, timeline_events: list[TimelineEvent]) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "chunk_title": chunk.chunk_title,
        "boundary_reason": chunk.boundary_reason,
        "speech_segment_ids": chunk.speech_segment_ids,
        "video_id": chunk.video_id,
        "title": chunk.title,
        "channel": chunk.channel,
        "start": chunk.start,
        "end": chunk.end,
        "timestamp_url": chunk.timestamp_url,
        "speech_text": chunk.speech_text,
        "dialogue_text": chunk.dialogue_text,
        "visual_text": chunk.visual_text,
        "on_screen_text": chunk.on_screen_text,
        "topics": chunk.topics,
        "entities": chunk.entities,
        "timeline_events": [
            {
                "event_id": event.event_id,
                "start": event.start,
                "end": event.end,
                "speech_text": event.speech_text,
                "visual_summary": event.visual_summary,
                "on_screen_text": event.on_screen_text,
                "topics": event.topics,
                "items": event.items,
            }
            for event in timeline_events
        ],
    }


def _prompt_path(context: StageContext) -> Path:
    return Path(__file__).resolve().parents[1] / "prompts" / context.config.style_claims.prompt_file


def _curate_prompt_path(context: StageContext) -> Path:
    return Path(__file__).resolve().parents[1] / "prompts" / context.config.style_claims.curate.prompt_file


def _clean_string(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return stable_unique(_clean_string(item) for item in value)


def _normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(payload)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

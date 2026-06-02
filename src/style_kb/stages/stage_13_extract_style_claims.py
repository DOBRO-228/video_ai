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

_SCHEMA_VERSION = 2
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
        return [
            context.paths.timeline_events_jsonl,
            context.paths.chunks_jsonl,
            _prompt_path(context),
        ]

    def output_files(self, context: StageContext) -> list:
        outputs = [context.paths.style_claims_jsonl, context.paths.style_claims_raw]
        if context.paths.style_claims_errors.exists():
            outputs.append(context.paths.style_claims_errors)
        return outputs

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.style_claims_jsonl.exists() or not context.paths.style_claims_raw.exists():
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

        style_claims = _dedupe_claims(all_candidates, context.job.video_id)
        summary = _summary_payload(
            context=context,
            chunks=chunks,
            raw_files=canonical_raw_files,
            claims_before_dedupe=len(all_candidates),
            claims_after_dedupe=len(style_claims),
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
                "cached_chunks_count": cached_chunks,
                "api_chunks_count": api_chunks,
                "legacy_raw_cache_removed_count": legacy_raw_cache_removed_count,
                "claim_retry_errors_count": len(claim_errors),
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
    return (
        summary.get("schema_version") == _SCHEMA_VERSION
        and summary.get("video_id") == context.job.video_id
        and summary.get("provider") == context.config.style_claims.provider
        and summary.get("model") == context.config.style_claims.model
        and summary.get("prompt_file") == context.config.style_claims.prompt_file
        and summary.get("max_claims_per_chunk") == context.config.style_claims.max_claims_per_chunk
        and summary.get("max_retries") == context.config.style_claims.max_retries
    )


def _summary_payload(
    *,
    context: StageContext,
    chunks: list[Chunk],
    raw_files: list[Path],
    claims_before_dedupe: int,
    claims_after_dedupe: int,
    legacy_raw_cache_removed_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "video_id": context.job.video_id,
        "provider": context.config.style_claims.provider,
        "model": context.config.style_claims.model,
        "prompt_file": context.config.style_claims.prompt_file,
        "max_claims_per_chunk": context.config.style_claims.max_claims_per_chunk,
        "max_retries": context.config.style_claims.max_retries,
        "chunks_count": len(chunks),
        "claims_before_dedupe": claims_before_dedupe,
        "claims_after_dedupe": claims_after_dedupe,
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

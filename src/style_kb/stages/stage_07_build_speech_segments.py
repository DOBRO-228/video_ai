from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from style_kb.clients.openai_cache import openai_prompt_cache_fingerprint, openai_prompt_cache_key
from style_kb.clients.openai_segmenter import OpenAISegmenterClient
from style_kb.config import default_config_path
from style_kb.errors import ProviderError
from style_kb.models import ProviderSource, SpeechSegment, SpeechToken
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import emit_stage_validation_failed, load_speech_segments, load_speech_tokens, read_payload, youtube_source_ref
from style_kb.stages.diagnostics import append_stage_summary
from style_kb.utils.files import append_text, copy_file_atomic
from style_kb.utils.ids import speech_segment_id
from style_kb.utils.pydantic_io import write_models_jsonl
from style_kb.utils.time import build_timestamp_url

_DURATION_EPSILON_SECONDS = 0.25
_SENTENCE_END_CHARS = frozenset({".", "!", "?", "…"})
_STRONG_CLAUSE_END_CHARS = frozenset({";", ":"})
_TRAILING_WRAPPER_CHARS = frozenset({'"', "'", "»", "”", ")", "]", "}"})
_UNIT_MAX_SECONDS = 12.0
_UNIT_MAX_WORDS = 40
_UNIT_CLAUSE_MIN_SECONDS = 8.0
_UNIT_CLAUSE_MIN_WORDS = 24
_SEGMENTATION_MAX_ATTEMPTS = 3
_SOFT_MAX_SEMANTIC_SECONDS = 40.0
_SOFT_MAX_SEMANTIC_WORDS = 90
_SOFT_MAX_SEMANTIC_SENTENCES = 4
_NON_TERMINAL_END_CHARS = frozenset({",", ";", ":"})
_REQUEST_METADATA_KEY = "_style_kb_request"
_CONTINUATION_START_WORDS = frozenset(
    {
        "а",
        "но",
        "и",
        "или",
        "либо",
        "потому",
        "поэтому",
        "соответственно",
        "также",
        "то",
        "пожалуйста",
    }
)


@dataclass(slots=True)
class _TranscriptUnit:
    unit_index: int
    token_start_position: int
    token_end_position: int
    start: float
    end: float
    start_ms: int
    end_ms: int
    text: str
    word_count: int
    speaker: str | None
    speaker_role: str | None
    boundary_reason: str
    can_end_segment: bool
    must_end_segment: bool


@dataclass(slots=True)
class _SegmentationAttemptFeedback:
    attempt: int
    error_code: str
    message: str
    validation_errors: list[str]
    structured_errors: list[dict[str, Any]]
    raw_output_path: Path | None
    candidate_plan: list[dict[str, Any]] | None = None
    advisor_raw_output_path: Path | None = None
    advisor_instruction: str | None = None


class _SegmentationValidationError(ProviderError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        validation_errors: list[str],
        structured_errors: list[dict[str, Any]],
    ) -> None:
        super().__init__(message, error_code=error_code, details="\n".join(validation_errors[:20]))
        self.validation_errors = validation_errors
        self.structured_errors = structured_errors


class Stage07BuildSpeechSegments(Stage):
    name = "07_build_speech_segments"
    ordinal = 7

    def input_files(self, context: StageContext) -> list:
        return [
            context.paths.stt_speech_tokens,
            _prompt_path(context),
            _retry_advisor_prompt_path(context),
            default_config_path(),
        ]

    def output_files(self, context: StageContext) -> list:
        return [context.paths.stt_speech_segments, context.paths.stt_speech_segments_raw]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.stt_speech_segments.exists() or not context.paths.stt_speech_segments_raw.exists():
            return False
        raw_payload = read_payload(context.paths.stt_speech_segments_raw)
        if _openai_response_reasoning_effort(raw_payload) != context.config.speech_segmentation.reasoning_effort:
            return False

        segments = load_speech_segments(context.paths.stt_speech_segments)
        tokens = load_speech_tokens(context.paths.stt_speech_tokens)
        if not segments or not tokens:
            return False
        units = _build_transcript_units(tokens, context)
        if not units:
            return False

        token_positions = {token.token_index: index for index, token in enumerate(tokens)}
        unit_by_end_position = {unit.token_end_position: unit for unit in units}
        max_duration = context.config.speech_segmentation.max_segment_seconds
        max_words = context.config.speech_segmentation.max_segment_words
        previous_end_position = -1

        if any(segment.source.provider != context.config.speech_segmentation.provider for segment in segments):
            return False
        if any(segment.source.model != context.config.speech_segmentation.model for segment in segments):
            return False
        if any(segment.start_ms > segment.end_ms for segment in segments):
            return False
        if any(current.start_ms < previous.start_ms for previous, current in zip(segments, segments[1:])):
            return False

        for segment in segments:
            duration = segment.end - segment.start
            if duration > max_duration + _DURATION_EPSILON_SECONDS:
                return False
            if not segment.speaker and duration < context.config.speech_segmentation.min_segment_seconds - _DURATION_EPSILON_SECONDS:
                return False

            start_position = token_positions.get(segment.token_start_index)
            end_position = token_positions.get(segment.token_end_index)
            if start_position is None or end_position is None or start_position > end_position:
                return False
            if start_position != previous_end_position + 1:
                return False
            if not _segment_boundary_is_valid_for_units(
                units=units,
                unit_by_end_position=unit_by_end_position,
                start_position=start_position,
                end_position=end_position,
                tokens_count=len(tokens),
            ):
                return False

            group = tokens[start_position : end_position + 1]
            if not group:
                return False
            if _is_whitespace_token(group[0]):
                return False
            if segment.start_ms != group[0].start_ms or segment.end_ms != group[-1].end_ms:
                return False
            if segment.text != _join_tokens(group):
                return False
            speaker, speaker_role = _single_content_speaker(group)
            if segment.speaker != speaker or segment.speaker_role != speaker_role:
                return False
            if _word_count_from_tokens(group) > max_words:
                return False
            if end_position + 1 < len(tokens) and _is_whitespace_token(tokens[end_position + 1]):
                return False
            previous_end_position = end_position

        if previous_end_position != len(tokens) - 1:
            return False
        expected_segments = _merge_short_same_speaker_segments(segments, tokens, context)
        if [segment.model_dump(mode="json") for segment in segments] != [
            segment.model_dump(mode="json") for segment in expected_segments
        ]:
            return False
        return not _semantic_boundary_violations(segments, tokens=tokens, units=units)

    def run(self, context: StageContext) -> StageResult:
        tokens = load_speech_tokens(context.paths.stt_speech_tokens)
        units = _build_transcript_units(tokens, context)
        if not units:
            raise ProviderError("semantic transcript units are empty", error_code="semantic_units_empty")

        base_prompt_text = _prompt_path(context).read_text(encoding="utf-8")
        retry_advisor_prompt_text = _retry_advisor_prompt_path(context).read_text(encoding="utf-8")
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        client = OpenAISegmenterClient(
            openai_api_key,
            model=context.config.speech_segmentation.model,
            reasoning_effort=context.config.speech_segmentation.reasoning_effort,
        )
        advisor_client = OpenAISegmenterClient(
            openai_api_key,
            model=context.config.speech_segmentation.retry_advisor_model,
            reasoning_effort=context.config.speech_segmentation.retry_advisor_reasoning_effort,
        )
        segments, attempts_used, raw_attempt_paths, raw_advisor_paths, retry_feedback = _segment_with_retries(
            context=context,
            client=client,
            advisor_client=advisor_client,
            base_prompt_text=base_prompt_text,
            retry_advisor_prompt_text=retry_advisor_prompt_text,
            tokens=tokens,
            units=units,
        )
        write_models_jsonl(context.paths.stt_speech_segments, segments)
        durations = [round(segment.end - segment.start, 3) for segment in segments]
        words = [_word_count_from_tokens(tokens[_token_position(tokens, segment.token_start_index) : _token_position(tokens, segment.token_end_index) + 1]) for segment in segments]
        unit_boundary_reasons = Counter(unit.boundary_reason for unit in units)
        segmentable_unit_boundaries = sum(1 for unit in units if unit.can_end_segment)
        raw_payload = read_payload(context.paths.stt_speech_segments_raw)
        openai_response_id = raw_payload.get("id")
        append_stage_summary(
            context,
            self.name,
            "speech-segmentation-summary",
            {
                "raw_output_path": str(context.paths.stt_speech_segments_raw),
                "accepted_raw_attempt_output_path": str(
                    context.paths.stt_speech_segments_raw_attempt(attempts_used)
                ),
                "raw_attempt_output_paths": [str(path) for path in raw_attempt_paths],
                "openai_response_id": openai_response_id,
                "provider": context.config.speech_segmentation.provider,
                "model": context.config.speech_segmentation.model,
                "reasoning_effort": context.config.speech_segmentation.reasoning_effort,
                "retry_advisor_model": context.config.speech_segmentation.retry_advisor_model,
                "retry_advisor_reasoning_effort": context.config.speech_segmentation.retry_advisor_reasoning_effort,
                "accepted_attempt": attempts_used,
                "segments_count": len(segments),
                "semantic_units_count": len(units),
                "unit_boundary_reasons": dict(unit_boundary_reasons),
                "segmentable_unit_boundaries_count": segmentable_unit_boundaries,
                "non_segmentable_unit_boundaries_count": len(units) - segmentable_unit_boundaries,
                "duration_seconds": _distribution(durations),
                "word_count": _distribution(words),
                "semantic_boundary_violations": _semantic_boundary_violations(segments, tokens=tokens, units=units)[:5],
                "raw_retry_advisor_output_paths": [str(path) for path in raw_advisor_paths],
                "retry_feedback": [_feedback_payload(feedback) for feedback in retry_feedback],
            },
        )
        return StageResult(
            output_files=[*self.output_files(context), *raw_attempt_paths, *raw_advisor_paths],
            remote_refs={"openai_response_id": openai_response_id} if openai_response_id else {},
            metrics={
                "segments_count": len(segments),
                "semantic_units_count": len(units),
                "segmentation_attempts": attempts_used,
                "retry_advisor_attempts": len(raw_advisor_paths),
                "semantic_retry_events_count": len(retry_feedback),
                "retry_advisor_used_count": len(raw_advisor_paths),
                "semantic_retry_resolved_count": 1 if retry_feedback else 0,
                "max_segment_duration": max(durations) if durations else 0,
                "max_segment_words": max(words) if words else 0,
                "segmentable_unit_boundaries_count": segmentable_unit_boundaries,
                "non_segmentable_unit_boundaries_count": len(units) - segmentable_unit_boundaries,
            },
        )


def _segment_with_retries(
    *,
    context: StageContext,
    client: OpenAISegmenterClient,
    advisor_client: OpenAISegmenterClient,
    base_prompt_text: str,
    retry_advisor_prompt_text: str,
    tokens: list[SpeechToken],
    units: list[_TranscriptUnit],
) -> tuple[list[SpeechSegment], int, list[Path], list[Path], list[_SegmentationAttemptFeedback]]:
    retry_feedback: list[_SegmentationAttemptFeedback] = []
    next_retry_instruction: str | None = None
    raw_attempt_paths: list[Path] = []
    raw_advisor_paths: list[Path] = []
    prompt_cache_key, prompt_cache_retention = _openai_prompt_cache_settings(
        context,
        namespace="speech-segmentation",
        model=context.config.speech_segmentation.model,
        fingerprint=openai_prompt_cache_fingerprint(_sha256_text(base_prompt_text)),
    )
    for attempt in range(1, _SEGMENTATION_MAX_ATTEMPTS + 1):
        raw_attempt_path = context.paths.stt_speech_segments_raw_attempt(attempt)
        raw_attempt_paths.append(raw_attempt_path)
        payload = client.segment_transcript(
            system_prompt=_build_prompt(
                base_prompt_text,
                retry_feedback=retry_feedback,
                retry_instruction=next_retry_instruction,
                attempt=attempt,
            ),
            transcript_text=_join_tokens(tokens),
            units_payload=[_unit_payload(unit) for unit in units],
            constraints_payload={
                "provider": context.config.speech_segmentation.provider,
                "model": context.config.speech_segmentation.model,
                "reasoning_effort": context.config.speech_segmentation.reasoning_effort,
                "retry_advisor_model": context.config.speech_segmentation.retry_advisor_model,
                "retry_advisor_reasoning_effort": context.config.speech_segmentation.retry_advisor_reasoning_effort,
                "min_segment_seconds": context.config.speech_segmentation.min_segment_seconds,
                "max_segment_seconds": context.config.speech_segmentation.max_segment_seconds,
                "max_segment_words": context.config.speech_segmentation.max_segment_words,
                "speaker_turn_min_duration_rule": "speaker-labeled segments may be shorter than min_segment_seconds",
                "preferred_max_segment_seconds": _SOFT_MAX_SEMANTIC_SECONDS,
                "preferred_max_segment_words": _SOFT_MAX_SEMANTIC_WORDS,
                "preferred_max_segment_sentences": _SOFT_MAX_SEMANTIC_SENTENCES,
                "semantic_density_failure_rule": (
                    "A segment fails semantic-boundary validation when duration_seconds > "
                    "preferred_max_segment_seconds AND words_count >= preferred_max_segment_words "
                    "AND sentences_count >= preferred_max_segment_sentences."
                ),
                "units_count": len(units),
                "retry_feedback": [_feedback_payload(feedback) for feedback in retry_feedback],
                "retry_advisor_instruction": next_retry_instruction,
            },
            raw_output_path=raw_attempt_path,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_retention=prompt_cache_retention,
        )
        try:
            segments = _segments_from_payload(payload, units, tokens, context)
        except _SegmentationValidationError as error:
            feedback = _feedback_from_validation_error(
                attempt=attempt,
                error=error,
                raw_output_path=raw_attempt_path,
            )
            feedback.candidate_plan = _candidate_plan_for_retry(
                attempt_payload=payload,
                units=units,
                tokens=tokens,
                candidate_segments=None,
            )
            _log_retry_attempt(context, feedback=feedback)
            emit_stage_validation_failed(
                context,
                stage_name=Stage07BuildSpeechSegments.name,
                ordinal=Stage07BuildSpeechSegments.ordinal,
                error_code=error.error_code,
                message=error.message,
                validation_errors=error.validation_errors,
                structured_errors=error.structured_errors,
                raw_output_path=raw_attempt_path,
                attempt=attempt,
                extra={"retryable": attempt < _SEGMENTATION_MAX_ATTEMPTS},
            )
            if attempt >= _SEGMENTATION_MAX_ATTEMPTS:
                retry_feedback.append(feedback)
                raise _final_retry_error(error=error, retry_feedback=retry_feedback) from error
            next_retry_instruction = _build_retry_advice(
                context=context,
                advisor_client=advisor_client,
                retry_advisor_prompt_text=retry_advisor_prompt_text,
                attempt_feedback=feedback,
                attempt_payload=payload,
                units=units,
                tokens=tokens,
                candidate_segments=None,
            )
            if feedback.advisor_raw_output_path is not None:
                raw_advisor_paths.append(feedback.advisor_raw_output_path)
            retry_feedback.append(feedback)
            _emit_retry_progress(context, attempt=attempt, error_count=len(feedback.validation_errors))
            continue

        segments = _merge_short_same_speaker_segments(segments, tokens, context)
        violations = _semantic_boundary_violations(segments, tokens=tokens, units=units)
        if not violations:
            copy_file_atomic(raw_attempt_path, context.paths.stt_speech_segments_raw)
            return segments, attempt, raw_attempt_paths, raw_advisor_paths, retry_feedback

        structured_errors = _semantic_structured_errors(violations)
        feedback = _SegmentationAttemptFeedback(
            attempt=attempt,
            error_code="openai_segmenter_semantic_boundary_failed",
            message="OpenAI speech segmentation returned poor semantic boundaries",
            validation_errors=violations,
            structured_errors=structured_errors,
            raw_output_path=raw_attempt_path,
            candidate_plan=_candidate_plan_for_retry(
                attempt_payload=payload,
                units=units,
                tokens=tokens,
                candidate_segments=segments,
            ),
        )
        _log_retry_attempt(context, feedback=feedback)
        emit_stage_validation_failed(
            context,
            stage_name=Stage07BuildSpeechSegments.name,
            ordinal=Stage07BuildSpeechSegments.ordinal,
            error_code=feedback.error_code,
            message=feedback.message,
            validation_errors=feedback.validation_errors,
            structured_errors=feedback.structured_errors,
            raw_output_path=raw_attempt_path,
            attempt=attempt,
            extra={"retryable": attempt < _SEGMENTATION_MAX_ATTEMPTS},
        )
        if attempt >= _SEGMENTATION_MAX_ATTEMPTS:
            retry_feedback.append(feedback)
            raise ProviderError(
                "OpenAI speech segmentation returned poor semantic boundaries after retries",
                error_code="openai_segmenter_semantic_boundary_failed",
                details=_retry_failure_details(retry_feedback),
            )

        next_retry_instruction = _build_retry_advice(
            context=context,
            advisor_client=advisor_client,
            retry_advisor_prompt_text=retry_advisor_prompt_text,
            attempt_feedback=feedback,
            attempt_payload=payload,
            units=units,
            tokens=tokens,
            candidate_segments=segments,
        )
        if feedback.advisor_raw_output_path is not None:
            raw_advisor_paths.append(feedback.advisor_raw_output_path)
        retry_feedback.append(feedback)
        _emit_retry_progress(context, attempt=attempt, error_count=len(feedback.validation_errors))
    raise ProviderError(
        "OpenAI speech segmentation failed without producing an accepted attempt",
        error_code="openai_segmenter_failed",
        details=_retry_failure_details(retry_feedback),
    )


def _segments_from_payload(
    payload: dict,
    units: list[_TranscriptUnit],
    tokens: list[SpeechToken],
    context: StageContext,
) -> list[SpeechSegment]:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise _segmentation_validation_error(
            [
                {
                    "code": "openai_segmenter_empty_segments",
                    "message": "OpenAI speech segmentation returned no segments",
                    "segments_count": 0 if isinstance(raw_segments, list) else None,
                }
            ]
        )

    raw_validation_errors = _collect_raw_segment_validation_errors(raw_segments, units, tokens, context)
    if raw_validation_errors:
        raise _segmentation_validation_error(raw_validation_errors)

    provider = ProviderSource(
        provider=context.config.speech_segmentation.provider,
        model=context.config.speech_segmentation.model,
    )
    max_duration = context.config.speech_segmentation.max_segment_seconds
    max_words = context.config.speech_segmentation.max_segment_words
    expected_start = 1
    results: list[SpeechSegment] = []

    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            raise ProviderError(
                "OpenAI speech segmentation returned an invalid segment payload",
                error_code="openai_segmenter_invalid_segment",
            )
        unit_start_index = int(raw_segment.get("unit_start_index") or 0)
        unit_end_index = int(raw_segment.get("unit_end_index") or 0)
        if unit_start_index != expected_start:
            raise ProviderError(
                "OpenAI speech segmentation returned non-contiguous unit ranges",
                error_code="openai_segmenter_non_contiguous_ranges",
            )
        if unit_start_index < 1 or unit_end_index < unit_start_index or unit_end_index > len(units):
            raise ProviderError(
                "OpenAI speech segmentation returned an out-of-range unit index",
                error_code="openai_segmenter_unit_range_invalid",
            )

        start_unit = units[unit_start_index - 1]
        end_unit = units[unit_end_index - 1]
        group = tokens[start_unit.token_start_position : end_unit.token_end_position + 1]
        if not group:
            raise ProviderError(
                "OpenAI speech segmentation returned an empty token group",
                error_code="openai_segmenter_empty_group",
        )

        duration = group[-1].end - group[0].start
        words_count = _word_count_from_tokens(group)
        if duration > max_duration + _DURATION_EPSILON_SECONDS:
            raise ProviderError(
                f"OpenAI speech segmentation exceeded max_segment_seconds for range {unit_start_index}-{unit_end_index}",
                error_code="openai_segmenter_duration_exceeded",
            )
        if not _has_speaker_label(group) and duration < context.config.speech_segmentation.min_segment_seconds - _DURATION_EPSILON_SECONDS:
            raise ProviderError(
                f"OpenAI speech segmentation violated min_segment_seconds for range {unit_start_index}-{unit_end_index}",
                error_code="openai_segmenter_duration_too_short",
            )
        if words_count > max_words:
            raise ProviderError(
                f"OpenAI speech segmentation exceeded max_segment_words for range {unit_start_index}-{unit_end_index}",
                error_code="openai_segmenter_words_exceeded",
            )

        text = _join_tokens(group)
        languages = [token.language for token in group if token.language]
        language = Counter(languages).most_common(1)[0][0] if languages else None
        speaker, speaker_role = _single_content_speaker(group, fail_on_mixed=True)
        start = group[0].start
        end = group[-1].end
        results.append(
            SpeechSegment(
                segment_id=speech_segment_id(context.job.video_id, start, end),
                video_id=context.job.video_id,
                start=start,
                end=end,
                start_ms=group[0].start_ms,
                end_ms=group[-1].end_ms,
                text=text,
                token_start_index=group[0].token_index,
                token_end_index=group[-1].token_index,
                tokens_count=len(group),
                speaker=speaker,
                speaker_role=speaker_role,
                language=language,
                timestamp_url=build_timestamp_url(context.job.video_id, start),
                source=provider,
                source_refs=[youtube_source_ref(context.job.video_id, start, end, modality="audio")],
            )
        )
        expected_start = unit_end_index + 1

    if expected_start != len(units) + 1:
        raise ProviderError(
            "OpenAI speech segmentation did not cover all transcript units",
            error_code="openai_segmenter_incomplete_coverage",
        )
    return results


def _collect_raw_segment_validation_errors(
    raw_segments: list[Any],
    units: list[_TranscriptUnit],
    tokens: list[SpeechToken],
    context: StageContext,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    expected_start = 1
    max_duration = context.config.speech_segmentation.max_segment_seconds
    max_words = context.config.speech_segmentation.max_segment_words

    for segment_index, raw_segment in enumerate(raw_segments, start=1):
        if not isinstance(raw_segment, dict):
            errors.append(
                {
                    "code": "openai_segmenter_invalid_segment",
                    "message": f"segment {segment_index} is not an object",
                    "segment_index": segment_index,
                }
            )
            continue

        unit_start_index = _raw_unit_index(raw_segment, "unit_start_index")
        unit_end_index = _raw_unit_index(raw_segment, "unit_end_index")
        if unit_start_index is None or unit_end_index is None:
            errors.append(
                {
                    "code": "openai_segmenter_invalid_segment",
                    "message": f"segment {segment_index} has invalid unit indexes",
                    "segment_index": segment_index,
                    "raw_segment": raw_segment,
                }
            )
            continue

        if unit_start_index != expected_start:
            errors.append(
                {
                    "code": "openai_segmenter_non_contiguous_ranges",
                    "message": (
                        f"segment {segment_index} starts at unit {unit_start_index}, "
                        f"expected unit {expected_start}"
                    ),
                    "segment_index": segment_index,
                    "unit_start_index": unit_start_index,
                    "unit_end_index": unit_end_index,
                    "expected_unit_start_index": expected_start,
                }
            )

        if unit_end_index < unit_start_index or unit_end_index > len(units):
            errors.append(
                {
                    "code": "openai_segmenter_unit_range_invalid",
                    "message": (
                        f"segment {segment_index} has invalid unit range "
                        f"{unit_start_index}-{unit_end_index}"
                    ),
                    "segment_index": segment_index,
                    "unit_start_index": unit_start_index,
                    "unit_end_index": unit_end_index,
                    "units_count": len(units),
                }
            )
            expected_start = unit_end_index + 1
            continue

        start_unit = units[unit_start_index - 1]
        end_unit = units[unit_end_index - 1]
        group = tokens[start_unit.token_start_position : end_unit.token_end_position + 1]
        if not group:
            errors.append(
                {
                    "code": "openai_segmenter_empty_group",
                    "message": f"segment {segment_index} maps to an empty token group",
                    "segment_index": segment_index,
                    "unit_start_index": unit_start_index,
                    "unit_end_index": unit_end_index,
                }
            )
            expected_start = unit_end_index + 1
            continue

        text = _join_tokens(group)
        duration = group[-1].end - group[0].start
        words_count = _word_count_from_tokens(group)
        sentences_count = _sentence_boundary_count(text)
        speakers = _content_speaker_identities(group)
        common_payload: dict[str, Any] = {
            "segment_index": segment_index,
            "unit_start_index": unit_start_index,
            "unit_end_index": unit_end_index,
            "start": group[0].start,
            "end": group[-1].end,
            "duration_seconds": round(duration, 3),
            "words_count": words_count,
            "sentences_count": sentences_count,
            "speakers": [
                {"speaker": speaker, "speaker_role": speaker_role}
                for speaker, speaker_role in speakers
            ],
            "text_head": _clip_text(text, limit=160),
            "text_tail": _clip_text(text[-160:], limit=160),
        }
        if duration > max_duration + _DURATION_EPSILON_SECONDS:
            errors.append(
                {
                    **common_payload,
                    "code": "openai_segmenter_duration_exceeded",
                    "message": (
                        f"segment {segment_index} range {unit_start_index}-{unit_end_index} "
                        f"duration {duration:.3f}s exceeds max_segment_seconds {max_duration:.3f}s"
                    ),
                    "max_segment_seconds": max_duration,
                }
            )
        if not _has_speaker_label(group) and duration < context.config.speech_segmentation.min_segment_seconds - _DURATION_EPSILON_SECONDS:
            errors.append(
                {
                    **common_payload,
                    "code": "openai_segmenter_duration_too_short",
                    "message": (
                        f"segment {segment_index} range {unit_start_index}-{unit_end_index} "
                        f"duration {duration:.3f}s is shorter than min_segment_seconds "
                        f"{context.config.speech_segmentation.min_segment_seconds:.3f}s"
                    ),
                    "min_segment_seconds": context.config.speech_segmentation.min_segment_seconds,
                }
            )
        if words_count > max_words:
            errors.append(
                {
                    **common_payload,
                    "code": "openai_segmenter_words_exceeded",
                    "message": (
                        f"segment {segment_index} range {unit_start_index}-{unit_end_index} "
                        f"has {words_count} words, exceeds max_segment_words {max_words}"
                    ),
                    "max_segment_words": max_words,
                }
            )
        if (
            duration > _SOFT_MAX_SEMANTIC_SECONDS
            and words_count >= _SOFT_MAX_SEMANTIC_WORDS
            and sentences_count >= _SOFT_MAX_SEMANTIC_SENTENCES
        ):
            errors.append(
                {
                    **common_payload,
                    "code": "openai_segmenter_semantic_boundary_violation",
                    "message": (
                        f"segment {segment_index} range {unit_start_index}-{unit_end_index} "
                        "is semantically too dense: "
                        f"duration={duration:.2f}s words={words_count} sentences={sentences_count}"
                    ),
                    "preferred_max_segment_seconds": _SOFT_MAX_SEMANTIC_SECONDS,
                    "preferred_max_segment_words": _SOFT_MAX_SEMANTIC_WORDS,
                    "preferred_max_segment_sentences": _SOFT_MAX_SEMANTIC_SENTENCES,
                }
            )
        if len(speakers) > 1:
            errors.append(
                {
                    **common_payload,
                    "code": "openai_segmenter_mixed_speakers",
                    "message": (
                        f"segment {segment_index} range {unit_start_index}-{unit_end_index} "
                        "contains multiple speakers"
                    ),
                }
            )
        crossed_must_boundaries = [
            unit
            for unit in units[unit_start_index - 1 : unit_end_index - 1]
            if unit.must_end_segment
        ]
        if crossed_must_boundaries:
            first_boundary = crossed_must_boundaries[0]
            errors.append(
                {
                    **common_payload,
                    "code": "openai_segmenter_crossed_must_boundary",
                    "message": (
                        f"segment {segment_index} range {unit_start_index}-{unit_end_index} "
                        f"crosses required boundary after unit {first_boundary.unit_index}"
                    ),
                    "boundary_unit_index": first_boundary.unit_index,
                    "boundary_reason": first_boundary.boundary_reason,
                }
            )
        if unit_end_index < len(units):
            end_unit = units[unit_end_index - 1]
            if not end_unit.can_end_segment:
                next_unit = units[unit_end_index]
                errors.append(
                    {
                        **common_payload,
                        "code": "openai_segmenter_unsafe_unit_boundary",
                        "message": (
                            f"segment {segment_index} ends at non-segmentable unit boundary "
                            f"after unit {unit_end_index}"
                        ),
                        "boundary_unit_index": unit_end_index,
                        "boundary_reason": end_unit.boundary_reason,
                        "text_tail": _clip_text(text[-160:], limit=160),
                        "next_text_head": _clip_text(next_unit.text, limit=160),
                    }
                )
        expected_start = unit_end_index + 1

    if expected_start != len(units) + 1:
        errors.append(
            {
                "code": "openai_segmenter_incomplete_coverage",
                "message": f"segmentation ended at expected unit {expected_start}, units_count={len(units)}",
                "expected_unit_start_index": expected_start,
                "units_count": len(units),
            }
        )
    return errors


def _segmentation_validation_error(errors: list[dict[str, Any]]) -> _SegmentationValidationError:
    first_error = errors[0]
    error_code = str(first_error.get("code") or "openai_segmenter_invalid_segments")
    validation_errors = [_structured_error_message(error) for error in errors]
    return _SegmentationValidationError(
        str(first_error.get("message") or "OpenAI speech segmentation failed validation"),
        error_code=error_code,
        validation_errors=validation_errors,
        structured_errors=errors,
    )


def _raw_unit_index(raw_segment: dict[str, Any], key: str) -> int | None:
    value = raw_segment.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _segment_boundary_is_valid_for_units(
    *,
    units: list[_TranscriptUnit],
    unit_by_end_position: dict[int, _TranscriptUnit],
    start_position: int,
    end_position: int,
    tokens_count: int,
) -> bool:
    end_unit = unit_by_end_position.get(end_position)
    if end_unit is None:
        return False
    for unit in units:
        if unit.must_end_segment and start_position <= unit.token_end_position < end_position:
            return False
    return end_position == tokens_count - 1 or end_unit.can_end_segment


def _merge_short_same_speaker_segments(
    segments: list[SpeechSegment],
    tokens: list[SpeechToken],
    context: StageContext,
) -> list[SpeechSegment]:
    if not segments:
        return []

    token_positions = {token.token_index: index for index, token in enumerate(tokens)}
    merged: list[SpeechSegment] = []
    current = segments[0]
    for next_segment in segments[1:]:
        if _should_merge_same_speaker_segments(current, next_segment, token_positions, tokens, context):
            current = _merge_segments(current, next_segment, token_positions, tokens, context)
            continue
        merged.append(current)
        current = next_segment
    merged.append(current)
    return merged


def _should_merge_same_speaker_segments(
    current: SpeechSegment,
    next_segment: SpeechSegment,
    token_positions: dict[int, int],
    tokens: list[SpeechToken],
    context: StageContext,
) -> bool:
    if not current.speaker or current.speaker != next_segment.speaker:
        return False
    if current.speaker_role != next_segment.speaker_role:
        return False
    if current.end - current.start >= context.config.speech_segmentation.min_segment_seconds:
        if next_segment.end - next_segment.start >= context.config.speech_segmentation.min_segment_seconds:
            return False

    start_position = token_positions.get(current.token_start_index)
    current_end_position = token_positions.get(current.token_end_index)
    next_start_position = token_positions.get(next_segment.token_start_index)
    end_position = token_positions.get(next_segment.token_end_index)
    if (
        start_position is None
        or current_end_position is None
        or next_start_position is None
        or end_position is None
        or start_position > end_position
        or current_end_position + 1 != next_start_position
    ):
        return False
    group = tokens[start_position : end_position + 1]
    duration = group[-1].end - group[0].start
    if duration > context.config.speech_segmentation.max_segment_seconds + _DURATION_EPSILON_SECONDS:
        return False
    return _word_count_from_tokens(group) <= context.config.speech_segmentation.max_segment_words


def _merge_segments(
    current: SpeechSegment,
    next_segment: SpeechSegment,
    token_positions: dict[int, int],
    tokens: list[SpeechToken],
    context: StageContext,
) -> SpeechSegment:
    start_position = token_positions[current.token_start_index]
    end_position = token_positions[next_segment.token_end_index]
    group = tokens[start_position : end_position + 1]
    languages = [token.language for token in group if token.language]
    language = Counter(languages).most_common(1)[0][0] if languages else None
    start = group[0].start
    end = group[-1].end
    return SpeechSegment(
        segment_id=speech_segment_id(context.job.video_id, start, end),
        video_id=context.job.video_id,
        start=start,
        end=end,
        start_ms=group[0].start_ms,
        end_ms=group[-1].end_ms,
        text=_join_tokens(group),
        token_start_index=group[0].token_index,
        token_end_index=group[-1].token_index,
        tokens_count=len(group),
        speaker=current.speaker,
        speaker_role=current.speaker_role,
        language=language,
        timestamp_url=build_timestamp_url(context.job.video_id, start),
        source=current.source,
        source_refs=[youtube_source_ref(context.job.video_id, start, end, modality="audio")],
    )


def _build_prompt(
    base_prompt_text: str,
    *,
    retry_feedback: list[_SegmentationAttemptFeedback],
    retry_instruction: str | None,
    attempt: int,
) -> str:
    if attempt == 1 and not retry_feedback and not retry_instruction:
        return base_prompt_text

    feedback_lines: list[str] = []
    for feedback in retry_feedback[-2:]:
        feedback_lines.append(f"- Попытка {feedback.attempt}: {feedback.error_code}: {feedback.message}")
        feedback_lines.extend(f"  - {error}" for error in feedback.validation_errors[:8])
        if feedback.advisor_instruction:
            feedback_lines.append(f"  - Инструкция advisor: {feedback.advisor_instruction}")

    retry_instruction_lines = []
    if retry_instruction:
        retry_instruction_lines = [
            "",
            "Инструкция retry-advisor для следующей попытки:",
            retry_instruction.strip(),
        ]

    latest_candidate_plan = retry_feedback[-1].candidate_plan if retry_feedback else None
    candidate_plan_lines = []
    if latest_candidate_plan:
        candidate_plan_lines = [
            "",
            "Предыдущий candidate plan. Используй его как основу для локальных правок, но верни полный JSON со всеми ranges:",
            json.dumps(latest_candidate_plan, ensure_ascii=False, indent=2),
        ]

    feedback_lines = [
        "",
        "Нарушения предыдущих попыток, которые нужно исправить:",
        *(feedback_lines or ["- Нет детализированных ошибок, но предыдущая попытка не прошла валидацию."]),
        *retry_instruction_lines,
        *candidate_plan_lines,
        "",
        "Сделай новое полное разбиение. Не повторяй эти ошибки.",
        "Жёсткие лимиты из блока `Ограничения сегментации` важнее идеальной смысловой границы: нельзя превышать max_segment_seconds или max_segment_words.",
        "Нельзя заканчивать сегмент на unit с can_end_segment=false. Unit с must_end_segment=true обязан быть концом сегмента.",
        "Soft semantic density rule также обязателен для финальной валидации: не оставляй сегмент, где одновременно duration_seconds > preferred_max_segment_seconds, words_count >= preferred_max_segment_words и sentences_count >= preferred_max_segment_sentences.",
        "Если retry-feedback запрещает dense range X-Y, нельзя заменить его соседним или overlapping dense range, который всё ещё нарушает semantic density thresholds.",
        "Если semantic boundary выглядит сомнительно, сдвигай только локальную границу к ближайшему завершённому предложению.",
        "Не исправляй одну плохую границу объединением большого блока, если такой блок нарушает лимиты.",
        "Никогда не объединяй units разных speaker.",
    ]
    return base_prompt_text.rstrip() + "\n" + "\n".join(feedback_lines)


def _semantic_boundary_violations(
    segments: list[SpeechSegment],
    *,
    tokens: list[SpeechToken] | None = None,
    units: list[_TranscriptUnit] | None = None,
) -> list[str]:
    violations: list[str] = []
    unit_ranges = _segment_unit_ranges(segments, tokens=tokens, units=units)
    for index, segment in enumerate(segments, start=1):
        duration = segment.end - segment.start
        word_count = len(segment.text.split())
        sentence_count = _sentence_boundary_count(segment.text)
        if (
            duration > _SOFT_MAX_SEMANTIC_SECONDS
            and word_count >= _SOFT_MAX_SEMANTIC_WORDS
            and sentence_count >= _SOFT_MAX_SEMANTIC_SENTENCES
        ):
            unit_range = unit_ranges.get(index)
            range_text = f" range {unit_range[0]}-{unit_range[1]}" if unit_range else ""
            violations.append(
                f"segment {index}{range_text} is semantically too dense: "
                f"duration={duration:.2f}s words={word_count} sentences={sentence_count}"
            )

    for index, (previous, current) in enumerate(zip(segments, segments[1:]), start=1):
        if _segments_have_speaker_change(previous, current):
            continue
        previous_terminal = _terminal_char(previous.text)
        current_first_word = _first_word(current.text)
        if previous_terminal in _NON_TERMINAL_END_CHARS:
            violations.append(
                f"boundary {index}->{index + 1} ends on non-terminal punctuation {previous_terminal!r}: "
                f"prev_tail={previous.text[-60:]!r} next_head={current.text[:60]!r}"
            )
        if _starts_with_lowercase(current.text) and previous_terminal not in _SENTENCE_END_CHARS:
            violations.append(
                f"boundary {index}->{index + 1} starts with lowercase continuation: "
                f"prev_tail={previous.text[-60:]!r} next_head={current.text[:60]!r}"
            )
        if current_first_word in _CONTINUATION_START_WORDS and previous_terminal not in _SENTENCE_END_CHARS:
            violations.append(
                f"boundary {index}->{index + 1} starts with continuation word {current_first_word!r}: "
                f"prev_tail={previous.text[-60:]!r} next_head={current.text[:60]!r}"
            )
    return violations


def _segment_unit_ranges(
    segments: list[SpeechSegment],
    *,
    tokens: list[SpeechToken] | None,
    units: list[_TranscriptUnit] | None,
) -> dict[int, tuple[int, int]]:
    if tokens is None or units is None:
        return {}
    token_start_to_unit = {
        tokens[unit.token_start_position].token_index: unit.unit_index
        for unit in units
        if 0 <= unit.token_start_position < len(tokens)
    }
    token_end_to_unit = {
        tokens[unit.token_end_position].token_index: unit.unit_index
        for unit in units
        if 0 <= unit.token_end_position < len(tokens)
    }
    results: dict[int, tuple[int, int]] = {}
    for segment_index, segment in enumerate(segments, start=1):
        unit_start_index = token_start_to_unit.get(segment.token_start_index)
        unit_end_index = token_end_to_unit.get(segment.token_end_index)
        if unit_start_index is not None and unit_end_index is not None:
            results[segment_index] = (unit_start_index, unit_end_index)
    return results


def _semantic_structured_errors(violations: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "code": "openai_segmenter_semantic_boundary_violation",
            "message": violation,
            "violation_index": index,
        }
        for index, violation in enumerate(violations, start=1)
    ]


def _feedback_from_validation_error(
    *,
    attempt: int,
    error: _SegmentationValidationError,
    raw_output_path: Path,
) -> _SegmentationAttemptFeedback:
    return _SegmentationAttemptFeedback(
        attempt=attempt,
        error_code=error.error_code,
        message=error.message,
        validation_errors=error.validation_errors,
        structured_errors=error.structured_errors,
        raw_output_path=raw_output_path,
    )


def _build_retry_advice(
    *,
    context: StageContext,
    advisor_client: OpenAISegmenterClient,
    retry_advisor_prompt_text: str,
    attempt_feedback: _SegmentationAttemptFeedback,
    attempt_payload: dict,
    units: list[_TranscriptUnit],
    tokens: list[SpeechToken],
    candidate_segments: list[SpeechSegment] | None,
) -> str | None:
    raw_advice_path = context.paths.stt_speech_segments_retry_advice_attempt(attempt_feedback.attempt)
    repair_payload = {
        "video_id": context.job.video_id,
        "attempt": attempt_feedback.attempt,
        "main_model": context.config.speech_segmentation.model,
        "main_reasoning_effort": context.config.speech_segmentation.reasoning_effort,
        "retry_advisor_model": context.config.speech_segmentation.retry_advisor_model,
        "retry_advisor_reasoning_effort": context.config.speech_segmentation.retry_advisor_reasoning_effort,
        "constraints": {
            "min_segment_seconds": context.config.speech_segmentation.min_segment_seconds,
            "max_segment_seconds": context.config.speech_segmentation.max_segment_seconds,
            "max_segment_words": context.config.speech_segmentation.max_segment_words,
            "preferred_max_segment_seconds": _SOFT_MAX_SEMANTIC_SECONDS,
            "preferred_max_segment_words": _SOFT_MAX_SEMANTIC_WORDS,
            "preferred_max_segment_sentences": _SOFT_MAX_SEMANTIC_SENTENCES,
            "semantic_density_failure_rule": (
                "A segment fails semantic-boundary validation when duration_seconds > "
                "preferred_max_segment_seconds AND words_count >= preferred_max_segment_words "
                "AND sentences_count >= preferred_max_segment_sentences."
            ),
            "speaker_turn_rule": "do not combine different speakers",
        },
        "errors": attempt_feedback.structured_errors[:20],
        "candidate_segments": _candidate_segments_for_advisor(
            attempt_payload=attempt_payload,
            units=units,
            tokens=tokens,
            candidate_segments=candidate_segments,
        ),
        "candidate_plan": _candidate_plan_for_retry(
            attempt_payload=attempt_payload,
            units=units,
            tokens=tokens,
            candidate_segments=candidate_segments,
        ),
    }
    try:
        prompt_cache_key, prompt_cache_retention = _openai_prompt_cache_settings(
            context,
            namespace="speech-segmentation-retry-advisor",
            model=context.config.speech_segmentation.retry_advisor_model,
            fingerprint=openai_prompt_cache_fingerprint(_sha256_text(retry_advisor_prompt_text)),
        )
        advisor_payload = advisor_client.analyze_segmentation_errors(
            system_prompt=retry_advisor_prompt_text,
            repair_payload=repair_payload,
            raw_output_path=raw_advice_path,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_retention=prompt_cache_retention,
        )
    except ProviderError as error:
        _log_retry_advisor_failure(context, attempt=attempt_feedback.attempt, error=error)
        return None

    instruction = str(advisor_payload.get("repair_instruction") or "").strip()
    if not instruction:
        _log_retry_advisor_failure(
            context,
            attempt=attempt_feedback.attempt,
            error=ProviderError(
                "OpenAI speech segmentation retry advisor returned an empty repair instruction",
                error_code="openai_segmenter_retry_advisor_empty_instruction",
            ),
        )
        return None

    attempt_feedback.advisor_raw_output_path = raw_advice_path
    attempt_feedback.advisor_instruction = instruction
    _log_retry_advisor_success(
        context,
        attempt=attempt_feedback.attempt,
        raw_output_path=raw_advice_path,
        advisor_payload=advisor_payload,
    )
    return instruction


def _candidate_segments_for_advisor(
    *,
    attempt_payload: dict,
    units: list[_TranscriptUnit],
    tokens: list[SpeechToken],
    candidate_segments: list[SpeechSegment] | None,
) -> list[dict[str, Any]]:
    if candidate_segments is not None:
        return _speech_segments_for_advisor(candidate_segments, tokens)
    return _raw_segments_for_advisor(attempt_payload, units, tokens)


def _candidate_plan_for_retry(
    *,
    attempt_payload: dict,
    units: list[_TranscriptUnit],
    tokens: list[SpeechToken],
    candidate_segments: list[SpeechSegment] | None,
) -> list[dict[str, Any]]:
    if candidate_segments is not None:
        return _speech_segments_candidate_plan(candidate_segments, units, tokens)
    return _raw_candidate_plan_for_retry(attempt_payload, units, tokens)


def _raw_candidate_plan_for_retry(
    attempt_payload: dict,
    units: list[_TranscriptUnit],
    tokens: list[SpeechToken],
) -> list[dict[str, Any]]:
    raw_segments = attempt_payload.get("segments")
    if not isinstance(raw_segments, list):
        return []
    results: list[dict[str, Any]] = []
    for segment_index, raw_segment in enumerate(raw_segments[:160], start=1):
        if not isinstance(raw_segment, dict):
            results.append({"segment_index": segment_index, "raw_segment": raw_segment})
            continue
        unit_start_index = _raw_unit_index(raw_segment, "unit_start_index")
        unit_end_index = _raw_unit_index(raw_segment, "unit_end_index")
        payload = _candidate_plan_item(
            segment_index=segment_index,
            unit_start_index=unit_start_index,
            unit_end_index=unit_end_index,
            units=units,
            tokens=tokens,
        )
        results.append(payload)
    return results


def _speech_segments_candidate_plan(
    segments: list[SpeechSegment],
    units: list[_TranscriptUnit],
    tokens: list[SpeechToken],
) -> list[dict[str, Any]]:
    token_start_to_unit = {
        tokens[unit.token_start_position].token_index: unit.unit_index
        for unit in units
        if 0 <= unit.token_start_position < len(tokens)
    }
    token_end_to_unit = {
        tokens[unit.token_end_position].token_index: unit.unit_index
        for unit in units
        if 0 <= unit.token_end_position < len(tokens)
    }
    return [
        _candidate_plan_item(
            segment_index=segment_index,
            unit_start_index=token_start_to_unit.get(segment.token_start_index),
            unit_end_index=token_end_to_unit.get(segment.token_end_index),
            units=units,
            tokens=tokens,
        )
        for segment_index, segment in enumerate(segments[:160], start=1)
    ]


def _candidate_plan_item(
    *,
    segment_index: int,
    unit_start_index: int | None,
    unit_end_index: int | None,
    units: list[_TranscriptUnit],
    tokens: list[SpeechToken],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "segment_index": segment_index,
        "unit_start_index": unit_start_index,
        "unit_end_index": unit_end_index,
    }
    if (
        unit_start_index is None
        or unit_end_index is None
        or not 1 <= unit_start_index <= unit_end_index <= len(units)
    ):
        return payload
    start_unit = units[unit_start_index - 1]
    end_unit = units[unit_end_index - 1]
    group = tokens[start_unit.token_start_position : end_unit.token_end_position + 1]
    if not group:
        return payload
    text = _join_tokens(group)
    payload.update(
        {
            "duration_seconds": round(group[-1].end - group[0].start, 3),
            "words_count": _word_count_from_tokens(group),
            "sentences_count": _sentence_boundary_count(text),
            "end_boundary_reason": end_unit.boundary_reason,
            "can_end_segment": end_unit.can_end_segment,
            "must_end_segment": end_unit.must_end_segment,
        }
    )
    return payload


def _raw_segments_for_advisor(
    attempt_payload: dict,
    units: list[_TranscriptUnit],
    tokens: list[SpeechToken],
) -> list[dict[str, Any]]:
    raw_segments = attempt_payload.get("segments")
    if not isinstance(raw_segments, list):
        return []
    results: list[dict[str, Any]] = []
    for segment_index, raw_segment in enumerate(raw_segments[:120], start=1):
        if not isinstance(raw_segment, dict):
            results.append({"segment_index": segment_index, "raw_segment": raw_segment})
            continue
        unit_start_index = _raw_unit_index(raw_segment, "unit_start_index")
        unit_end_index = _raw_unit_index(raw_segment, "unit_end_index")
        payload: dict[str, Any] = {
            "segment_index": segment_index,
            "unit_start_index": unit_start_index,
            "unit_end_index": unit_end_index,
        }
        if (
            unit_start_index is not None
            and unit_end_index is not None
            and 1 <= unit_start_index <= unit_end_index <= len(units)
        ):
            start_unit = units[unit_start_index - 1]
            end_unit = units[unit_end_index - 1]
            group = tokens[start_unit.token_start_position : end_unit.token_end_position + 1]
            if group:
                text = _join_tokens(group)
                payload.update(
                    {
                        "start": group[0].start,
                        "end": group[-1].end,
                        "duration_seconds": round(group[-1].end - group[0].start, 3),
                        "words_count": _word_count_from_tokens(group),
                        "sentences_count": _sentence_boundary_count(text),
                        "end_boundary_reason": end_unit.boundary_reason,
                        "can_end_segment": end_unit.can_end_segment,
                        "must_end_segment": end_unit.must_end_segment,
                        "speakers": [
                            {"speaker": speaker, "speaker_role": speaker_role}
                            for speaker, speaker_role in _content_speaker_identities(group)
                        ],
                        "text_head": _clip_text(text, limit=160),
                        "text_tail": _clip_text(text[-160:], limit=160),
                    }
                )
        results.append(payload)
    return results


def _speech_segments_for_advisor(
    segments: list[SpeechSegment],
    tokens: list[SpeechToken],
) -> list[dict[str, Any]]:
    token_positions = {token.token_index: index for index, token in enumerate(tokens)}
    results = []
    for segment_index, segment in enumerate(segments[:120], start=1):
        start_position = token_positions.get(segment.token_start_index)
        end_position = token_positions.get(segment.token_end_index)
        group = tokens[start_position : end_position + 1] if start_position is not None and end_position is not None else []
        words_count = _word_count_from_tokens(group) if group else len(segment.text.split())
        results.append(
            {
                "segment_index": segment_index,
                "start": segment.start,
                "end": segment.end,
                "duration_seconds": round(segment.end - segment.start, 3),
                "words_count": words_count,
                "sentences_count": _sentence_boundary_count(segment.text),
                "speaker": segment.speaker,
                "speaker_role": segment.speaker_role,
                "text_head": _clip_text(segment.text, limit=160),
                "text_tail": _clip_text(segment.text[-160:], limit=160),
            }
        )
    return results


def _log_retry_attempt(context: StageContext, *, feedback: _SegmentationAttemptFeedback) -> None:
    lines = [
        "",
        "[semantic-retry]",
        f"attempt: {feedback.attempt}",
        f"error_code: {feedback.error_code}",
        f"message: {feedback.message}",
        f"raw_output_path: {feedback.raw_output_path}",
        f"violations_count: {len(feedback.validation_errors)}",
        "violations:",
        *[f"  - {violation}" for violation in feedback.validation_errors[:20]],
    ]
    if feedback.structured_errors:
        lines.append("structured_errors:")
        lines.extend(f"  - {_structured_error_message(error)}" for error in feedback.structured_errors[:20])
    lines.append("")
    append_text(context.paths.stage_log(Stage07BuildSpeechSegments.name), "\n".join(lines), encoding="utf-8")


def _log_retry_advisor_success(
    context: StageContext,
    *,
    attempt: int,
    raw_output_path: Path,
    advisor_payload: dict[str, Any],
) -> None:
    lines = [
        "",
        "[semantic-retry-advisor]",
        f"attempt: {attempt}",
        f"raw_output_path: {raw_output_path}",
        f"error_summary: {advisor_payload.get('error_summary') or ''}",
        f"repair_instruction: {advisor_payload.get('repair_instruction') or ''}",
    ]
    hard_rules = advisor_payload.get("hard_rules")
    if isinstance(hard_rules, list):
        lines.append("hard_rules:")
        lines.extend(f"  - {rule}" for rule in hard_rules[:10])
    lines.append("")
    append_text(context.paths.stage_log(Stage07BuildSpeechSegments.name), "\n".join(lines), encoding="utf-8")


def _log_retry_advisor_failure(context: StageContext, *, attempt: int, error: ProviderError) -> None:
    lines = [
        "",
        "[semantic-retry-advisor-failed]",
        f"attempt: {attempt}",
        f"error_code: {error.error_code}",
        f"error: {error}",
        "",
    ]
    append_text(context.paths.stage_log(Stage07BuildSpeechSegments.name), "\n".join(lines), encoding="utf-8")


def _emit_retry_progress(context: StageContext, *, attempt: int, error_count: int) -> None:
    if context.progress_callback is None or attempt >= _SEGMENTATION_MAX_ATTEMPTS:
        return
    context.progress_callback(
        f"[07 {Stage07BuildSpeechSegments.name}] retry {attempt + 1}/{_SEGMENTATION_MAX_ATTEMPTS} segmentation-errors={error_count}"
    )


def _final_retry_error(
    *,
    error: _SegmentationValidationError,
    retry_feedback: list[_SegmentationAttemptFeedback],
) -> ProviderError:
    return ProviderError(
        f"OpenAI speech segmentation failed validation after retries: {error.message}",
        error_code=error.error_code,
        details=_retry_failure_details(retry_feedback),
    )


def _retry_failure_details(retry_feedback: list[_SegmentationAttemptFeedback]) -> str:
    lines: list[str] = []
    for feedback in retry_feedback:
        lines.append(f"attempt {feedback.attempt}: {feedback.error_code}: {feedback.message}")
        if feedback.raw_output_path is not None:
            lines.append(f"  raw_output_path: {feedback.raw_output_path}")
        if feedback.advisor_raw_output_path is not None:
            lines.append(f"  advisor_raw_output_path: {feedback.advisor_raw_output_path}")
        if feedback.advisor_instruction:
            lines.append(f"  advisor_instruction: {feedback.advisor_instruction}")
        lines.extend(f"  - {error}" for error in feedback.validation_errors[:12])
    return "\n".join(lines)


def _feedback_payload(feedback: _SegmentationAttemptFeedback) -> dict[str, Any]:
    return {
        "attempt": feedback.attempt,
        "error_code": feedback.error_code,
        "message": feedback.message,
        "validation_errors": feedback.validation_errors[:12],
        "structured_errors": feedback.structured_errors[:12],
        "raw_output_path": str(feedback.raw_output_path) if feedback.raw_output_path is not None else None,
        "advisor_raw_output_path": (
            str(feedback.advisor_raw_output_path) if feedback.advisor_raw_output_path is not None else None
        ),
        "advisor_instruction": feedback.advisor_instruction,
    }


def _structured_error_message(error: dict[str, Any]) -> str:
    message = str(error.get("message") or error.get("code") or "segmentation validation error")
    details = []
    if error.get("unit_start_index") is not None and error.get("unit_end_index") is not None:
        details.append(f"range={error['unit_start_index']}-{error['unit_end_index']}")
    if error.get("duration_seconds") is not None:
        details.append(f"duration={error['duration_seconds']}s")
    if error.get("words_count") is not None:
        details.append(f"words={error['words_count']}")
    if error.get("sentences_count") is not None:
        details.append(f"sentences={error['sentences_count']}")
    if error.get("max_segment_seconds") is not None:
        details.append(f"max_seconds={error['max_segment_seconds']}")
    if error.get("max_segment_words") is not None:
        details.append(f"max_words={error['max_segment_words']}")
    if error.get("preferred_max_segment_seconds") is not None:
        details.append(f"preferred_seconds={error['preferred_max_segment_seconds']}")
    if error.get("preferred_max_segment_words") is not None:
        details.append(f"preferred_words={error['preferred_max_segment_words']}")
    if error.get("preferred_max_segment_sentences") is not None:
        details.append(f"preferred_sentences={error['preferred_max_segment_sentences']}")
    if not details:
        return message
    return f"{message} ({', '.join(details)})"


def _build_transcript_units(tokens: list[SpeechToken], context: StageContext) -> list[_TranscriptUnit]:
    content_positions = [position for position, token in enumerate(tokens) if not _is_whitespace_token(token)]
    if not content_positions:
        return []

    units: list[_TranscriptUnit] = []
    start_content_index = 0
    current_words = 0

    for content_index, position in enumerate(content_positions):
        previous_position = content_positions[content_index - 1] if content_index > start_content_index else None
        if _content_token_starts_word(tokens, position, previous_position):
            current_words += 1
        next_content_position = content_positions[content_index + 1] if content_index + 1 < len(content_positions) else None
        boundary_reason = _unit_boundary_reason(
            tokens=tokens,
            start_position=content_positions[start_content_index],
            end_position=position,
            next_content_position=next_content_position,
            current_words=current_words,
            context=context,
        )
        if boundary_reason is None:
            continue
        unit_end_position = _extend_through_trailing_whitespace(tokens, position, next_content_position)
        unit_start_position = content_positions[start_content_index]
        unit_tokens = tokens[unit_start_position : unit_end_position + 1]
        unit_speaker, unit_speaker_role = _single_content_speaker(unit_tokens)
        can_end_segment = _can_end_segment_at_unit_boundary(
            tokens=tokens,
            end_position=position,
            next_content_position=next_content_position,
            boundary_reason=boundary_reason,
        )
        units.append(
            _TranscriptUnit(
                unit_index=len(units) + 1,
                token_start_position=unit_start_position,
                token_end_position=unit_end_position,
                start=unit_tokens[0].start,
                end=unit_tokens[-1].end,
                start_ms=unit_tokens[0].start_ms,
                end_ms=unit_tokens[-1].end_ms,
                text=_join_tokens(unit_tokens),
                word_count=current_words,
                speaker=unit_speaker,
                speaker_role=unit_speaker_role,
                boundary_reason=boundary_reason,
                can_end_segment=can_end_segment,
                must_end_segment=boundary_reason in {"end", "speaker_change"},
            )
        )
        start_content_index = content_index + 1
        current_words = 0
    return units


def _unit_boundary_reason(
    *,
    tokens: list[SpeechToken],
    start_position: int,
    end_position: int,
    next_content_position: int | None,
    current_words: int,
    context: StageContext,
) -> str | None:
    current_token = tokens[end_position]
    duration = current_token.end - tokens[start_position].start
    terminal_char = _terminal_char(current_token.text)

    if next_content_position is None:
        return "end"

    next_content_token = tokens[next_content_position]
    if current_token.speaker and next_content_token.speaker and current_token.speaker != next_content_token.speaker:
        return "speaker_change"
    gap_ms = max(0, next_content_token.start_ms - current_token.end_ms)
    if gap_ms >= context.config.speech_segmentation.pause_break_ms:
        return "pause"
    if terminal_char in _SENTENCE_END_CHARS:
        return "sentence_end"
    if terminal_char in _STRONG_CLAUSE_END_CHARS and (
        duration >= _UNIT_CLAUSE_MIN_SECONDS or current_words >= _UNIT_CLAUSE_MIN_WORDS
    ):
        return "strong_clause"
    if duration >= _UNIT_MAX_SECONDS and _is_safe_soft_unit_boundary(
        tokens=tokens,
        end_position=end_position,
        next_content_position=next_content_position,
    ):
        return "soft_max_seconds"
    if current_words >= _UNIT_MAX_WORDS and _is_safe_soft_unit_boundary(
        tokens=tokens,
        end_position=end_position,
        next_content_position=next_content_position,
    ):
        return "soft_max_words"
    return None


def _can_end_segment_at_unit_boundary(
    *,
    tokens: list[SpeechToken],
    end_position: int,
    next_content_position: int | None,
    boundary_reason: str,
) -> bool:
    if boundary_reason in {"end", "speaker_change"}:
        return True
    return _is_semantically_safe_unit_boundary(
        tokens=tokens,
        end_position=end_position,
        next_content_position=next_content_position,
    )


def _is_safe_soft_unit_boundary(
    *,
    tokens: list[SpeechToken],
    end_position: int,
    next_content_position: int | None,
) -> bool:
    return _is_semantically_safe_unit_boundary(
        tokens=tokens,
        end_position=end_position,
        next_content_position=next_content_position,
    )


def _is_semantically_safe_unit_boundary(
    *,
    tokens: list[SpeechToken],
    end_position: int,
    next_content_position: int | None,
) -> bool:
    if next_content_position is None:
        return True
    current_token = tokens[end_position]
    terminal_char = _terminal_char(current_token.text)
    if terminal_char in _SENTENCE_END_CHARS:
        return True
    if terminal_char in _NON_TERMINAL_END_CHARS:
        return False
    if not _content_token_starts_word(tokens, next_content_position, end_position):
        return False
    next_text = _boundary_next_text(tokens, next_content_position)
    if _starts_with_lowercase(next_text):
        return False
    return _first_word(next_text) not in _CONTINUATION_START_WORDS


def _boundary_next_text(tokens: list[SpeechToken], next_content_position: int, *, limit: int = 8) -> str:
    end_position = next_content_position
    content_count = 0
    while end_position < len(tokens):
        if not _is_whitespace_token(tokens[end_position]):
            content_count += 1
        end_position += 1
        if content_count >= limit:
            break
    return _join_tokens(tokens[next_content_position:end_position])


def _prompt_path(context: StageContext) -> Path:
    return Path(__file__).resolve().parents[1] / "prompts" / context.config.speech_segmentation.prompt_file


def _retry_advisor_prompt_path(context: StageContext) -> Path:
    return Path(__file__).resolve().parents[1] / "prompts" / context.config.speech_segmentation.retry_advisor_prompt_file


def _unit_payload(unit: _TranscriptUnit) -> dict[str, object]:
    return {
        "unit_index": unit.unit_index,
        "start": unit.start,
        "end": unit.end,
        "duration": round(unit.end - unit.start, 3),
        "word_count": unit.word_count,
        "speaker": unit.speaker,
        "speaker_role": unit.speaker_role,
        "boundary_reason": unit.boundary_reason,
        "can_end_segment": unit.can_end_segment,
        "must_end_segment": unit.must_end_segment,
        "text": unit.text,
    }


def _token_position(tokens: list[SpeechToken], token_index: int) -> int:
    for position, token in enumerate(tokens):
        if token.token_index == token_index:
            return position
    return 0


def _distribution(values: list[float | int]) -> dict[str, float | int]:
    if not values:
        return {"min": 0, "max": 0, "avg": 0}
    return {
        "min": min(values),
        "max": max(values),
        "avg": round(sum(values) / len(values), 3),
    }


def _join_tokens(tokens: list[SpeechToken]) -> str:
    text = "".join(token.text for token in tokens)
    text = re.sub(r"\s+", " ", text, flags=re.UNICODE)
    return text.strip()


def _word_count_from_tokens(tokens: list[SpeechToken]) -> int:
    count = 0
    previous_content_position: int | None = None
    for position, token in enumerate(tokens):
        if _is_whitespace_token(token):
            continue
        if _content_token_starts_word(tokens, position, previous_content_position):
            count += 1
        previous_content_position = position
    return count


def _has_speaker_label(tokens: list[SpeechToken]) -> bool:
    return any(token.speaker for token in tokens if not _is_whitespace_token(token))


def _content_speaker_identities(tokens: list[SpeechToken]) -> list[tuple[str | None, str | None]]:
    identities = [
        _speaker_identity(token)
        for token in tokens
        if not _is_whitespace_token(token) and token.speaker
    ]
    unique: list[tuple[str | None, str | None]] = []
    for identity in identities:
        if identity not in unique:
            unique.append(identity)
    return unique


def _single_content_speaker(
    tokens: list[SpeechToken],
    *,
    fail_on_mixed: bool = False,
) -> tuple[str | None, str | None]:
    unique = _content_speaker_identities(tokens)
    if not unique:
        return None, None
    if len(unique) == 1:
        return unique[0]
    if fail_on_mixed:
        speakers = ", ".join(speaker or "unknown" for speaker, _ in unique)
        raise ProviderError(
            f"OpenAI speech segmentation mixed speakers in one segment: {speakers}",
            error_code="openai_segmenter_mixed_speakers",
        )
    identities = [
        _speaker_identity(token)
        for token in tokens
        if not _is_whitespace_token(token) and token.speaker
    ]
    return Counter(identities).most_common(1)[0][0]


def _speaker_identity(token: SpeechToken) -> tuple[str | None, str | None]:
    return token.speaker, token.speaker_role


def _segments_have_speaker_change(previous: SpeechSegment, current: SpeechSegment) -> bool:
    return bool(previous.speaker and current.speaker and previous.speaker != current.speaker)


def _starts_new_word(token: SpeechToken) -> bool:
    return bool(token.text[:1].isspace())


def _token_starts_word(token: SpeechToken, *, is_first: bool) -> bool:
    stripped = token.text.strip()
    if not stripped:
        return False
    return is_first or _starts_new_word(token)


def _is_whitespace_token(token: SpeechToken) -> bool:
    return not token.text.strip()


def _content_token_starts_word(
    tokens: list[SpeechToken],
    position: int,
    previous_content_position: int | None,
) -> bool:
    token = tokens[position]
    if _is_whitespace_token(token):
        return False
    if previous_content_position is None:
        return True
    if _starts_new_word(token):
        return True
    return any(_is_whitespace_token(item) for item in tokens[previous_content_position + 1 : position])


def _extend_through_trailing_whitespace(
    tokens: list[SpeechToken],
    end_position: int,
    next_content_position: int | None,
) -> int:
    if next_content_position is None:
        return len(tokens) - 1
    trailing_end = next_content_position - 1
    for position in range(end_position + 1, next_content_position):
        if not _is_whitespace_token(tokens[position]):
            return position - 1
    return trailing_end


def _terminal_char(text: str) -> str | None:
    stripped = text.strip()
    while stripped and stripped[-1] in _TRAILING_WRAPPER_CHARS:
        stripped = stripped[:-1].rstrip()
    return stripped[-1] if stripped else None


def _sentence_boundary_count(text: str) -> int:
    return len(re.findall(r"[.!?…]+", text))


def _starts_with_lowercase(text: str) -> bool:
    stripped = text.lstrip()
    return bool(stripped) and stripped[0].isalpha() and stripped[0].islower()


def _first_word(text: str) -> str:
    match = re.search(r"[A-Za-zА-Яа-яЁё-]+", text)
    if match is None:
        return ""
    return match.group(0).strip("-").lower()


def _clip_text(text: str, *, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _openai_prompt_cache_settings(
    context: StageContext,
    *,
    namespace: str,
    model: str,
    fingerprint: str,
) -> tuple[str | None, str | None]:
    cache_config = context.config.openai.prompt_cache
    if not cache_config.enabled:
        return None, None
    return (
        openai_prompt_cache_key(namespace=namespace, model=model, fingerprint=fingerprint),
        cache_config.retention,
    )


def _openai_response_reasoning_effort(raw_payload: dict[str, Any]) -> str | None:
    request_metadata = raw_payload.get(_REQUEST_METADATA_KEY)
    if isinstance(request_metadata, dict) and "reasoning_effort" in request_metadata:
        effort = request_metadata.get("reasoning_effort")
        if isinstance(effort, str):
            return effort
        return None

    reasoning = raw_payload.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    effort = reasoning.get("effort")
    if not isinstance(effort, str):
        return None
    return effort


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

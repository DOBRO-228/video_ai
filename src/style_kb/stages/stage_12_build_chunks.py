from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from style_kb.clients._retry import OnRetry
from style_kb.clients.openai_cache import openai_prompt_cache_fingerprint, openai_prompt_cache_key
from style_kb.clients.provider_diagnostics import ProviderCallDiagnostics, ProviderName
from style_kb.clients.openai_chunk_planner import (
    ChunkPlanAnalysisResult,
    OpenAIChunkPlannerClient,
    load_cached_chunk_plan_result,
)
from style_kb.diagnostics import PipelineEvent
from style_kb.errors import ProviderError, StageExecutionError
from style_kb.models import (
    Chunk,
    ChunkPlan,
    ChunkPlanItem,
    PresenterRelevance,
    SourceRef,
    SpeakerRole,
    SpeechSegment,
    TimelineEvent,
    VideoInfo,
)
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import (
    load_chunks,
    load_speech_segments,
    load_timeline_events,
    load_video_info,
    log_openai_retry,
    emit_stage_validation_failed,
    emit_provider_event,
    provider_error_extra,
    provider_event_data,
    ProviderOperation,
    request_id_from_error,
    youtube_source_ref,
)
from style_kb.stages.diagnostics import validation_preview
from style_kb.stages.stage_10_describe_visuals import visual_field_noise_markers
from style_kb.utils.collections import stable_unique
from style_kb.utils.files import append_text, read_json, write_json_atomic
from style_kb.utils.ids import chunk_id
from style_kb.utils.pydantic_io import read_model, write_model, write_models_jsonl
from style_kb.utils.text import compact_join, word_count
from style_kb.utils.time import build_timestamp_url

_PLAN_SCHEMA_VERSION = 2
_RETRYABLE_PLANNER_ERROR_CODES = {
    "openai_chunk_planner_empty_chunks",
    "openai_chunk_planner_invalid_chunk",
    "openai_chunk_planner_json_parse_failed",
    "openai_chunk_planner_output_missing",
}


class _PlannerWarningSeverity(StrEnum):
    WARNING = "warning"


@dataclass(slots=True)
class _PlannerWindow:
    window_index: int
    core_segments: list[SpeechSegment]
    context_before: list[SpeechSegment]
    context_after: list[SpeechSegment]


@dataclass(slots=True)
class _ValidatedChunkPlan:
    plan: ChunkPlan
    chunks: list[Chunk]
    warnings: list[dict[str, Any]] = field(default_factory=list)
    cached_windows_count: int = 0
    api_windows_count: int = 0
    stale_raw_removed_count: int = 0


@dataclass(slots=True)
class _WindowPlanResult:
    window_index: int
    items: list[ChunkPlanItem]
    attempts: int
    errors: list[dict[str, Any]]
    cache_hit: bool
    diagnostics: ProviderCallDiagnostics | None = None


class _WindowPlanError(ProviderError):
    def __init__(self, message: str, *, error_code: str, details: str | None, errors: list[dict[str, Any]]) -> None:
        super().__init__(message, error_code=error_code, details=details)
        self.errors = errors


class Stage12BuildChunks(Stage):
    name = "12_build_chunks"
    ordinal = 12

    def input_files(self, context: StageContext) -> list:
        return [
            context.paths.metadata_video_info,
            context.paths.stt_speech_segments,
            context.paths.timeline_events_jsonl,
            _prompt_path(context),
            _retry_advisor_prompt_path(context),
        ]

    def output_files(self, context: StageContext) -> list:
        outputs = [context.paths.chunks_jsonl, context.paths.chunk_plan]
        if context.paths.chunk_plan_errors.exists():
            outputs.append(context.paths.chunk_plan_errors)
        if context.paths.chunk_plan_warnings.exists():
            outputs.append(context.paths.chunk_plan_warnings)
        return outputs

    def validate_outputs(self, context: StageContext) -> bool:
        if context.config.chunking.mode != "llm_speech_plan":
            return False
        if not (
            context.paths.chunks_jsonl.exists()
            and context.paths.chunk_plan.exists()
            and context.paths.metadata_video_info.exists()
            and context.paths.stt_speech_segments.exists()
            and context.paths.timeline_events_jsonl.exists()
        ):
            return False
        try:
            plan = read_model(context.paths.chunk_plan, ChunkPlan)
            video_info = load_video_info(context.paths.metadata_video_info)
            speech_segments = load_speech_segments(context.paths.stt_speech_segments)
            timeline_events = load_timeline_events(context.paths.timeline_events_jsonl)
        except Exception:
            return False
        errors, expected_chunks = _validate_plan_and_materialize(
            plan,
            speech_segments,
            timeline_events,
            context,
            video_info,
        )
        if errors:
            return False
        warnings = _question_answer_boundary_warnings(plan.chunks, speech_segments, context)
        if not _warnings_artifact_matches(context, warnings):
            return False
        try:
            actual_chunks = load_chunks(context.paths.chunks_jsonl)
        except Exception:
            return False
        return bool(actual_chunks) and [
            chunk.model_dump(mode="json") for chunk in actual_chunks
        ] == [
            chunk.model_dump(mode="json") for chunk in expected_chunks
        ]

    def run(self, context: StageContext) -> StageResult:
        _ensure_supported_mode(context)
        video_info = load_video_info(context.paths.metadata_video_info)
        speech_segments = load_speech_segments(context.paths.stt_speech_segments)
        timeline_events = load_timeline_events(context.paths.timeline_events_jsonl)
        if not speech_segments:
            raise StageExecutionError("cannot build chunks from empty speech segments", error_code="empty_speech_segments")
        if not timeline_events:
            raise StageExecutionError("cannot build chunks from empty timeline", error_code="empty_timeline")

        validated_plan = _load_reusable_plan(context, speech_segments, timeline_events, video_info)
        plan_reused = validated_plan is not None
        if validated_plan is None:
            validated_plan = _build_chunk_plan(context, speech_segments, timeline_events, video_info)
            write_model(context.paths.chunk_plan, validated_plan.plan)
        elif validated_plan.warnings:
            write_model(context.paths.chunk_plan, validated_plan.plan)

        write_models_jsonl(context.paths.chunks_jsonl, validated_plan.chunks)
        if context.paths.chunk_plan_errors.exists():
            context.paths.chunk_plan_errors.unlink()
        _write_or_clear_plan_warnings(context, validated_plan.warnings)
        redundancy_metrics = _combined_text_redundancy_metrics(validated_plan.chunks)
        return StageResult(
            output_files=self.output_files(context),
            metrics={
                "chunks_count": len(validated_plan.chunks),
                "chunk_plan_reused": plan_reused,
                "chunk_plan_attempts": validated_plan.plan.attempts,
                "chunk_plan_windows_count": validated_plan.plan.windows_count,
                "chunk_plan_attempts_per_window": validated_plan.plan.attempts_per_window,
                "chunk_plan_max_attempts_in_any_window": validated_plan.plan.max_attempts_in_any_window,
                "chunk_plan_cached_windows_count": validated_plan.cached_windows_count,
                "chunk_plan_api_windows_count": validated_plan.api_windows_count,
                "chunk_plan_parallel_requests": context.config.chunking.planner_parallel_requests,
                "chunk_plan_warnings_count": len(validated_plan.warnings),
                "chunk_plan_stale_raw_removed_count": validated_plan.stale_raw_removed_count,
                "chunk_presentation_final_chunks_count": _chunk_contamination_chunks_count(validated_plan.chunks),
                **redundancy_metrics,
            },
        )


def _load_reusable_plan(
    context: StageContext,
    speech_segments: list[SpeechSegment],
    timeline_events: list[TimelineEvent],
    video_info: VideoInfo,
) -> _ValidatedChunkPlan | None:
    if not context.paths.chunk_plan.exists():
        return None
    try:
        plan = read_model(context.paths.chunk_plan, ChunkPlan)
    except Exception:
        return None
    errors, chunks = _validate_plan_and_materialize(plan, speech_segments, timeline_events, context, video_info)
    if errors:
        return None
    annotated_items, warnings = _annotate_question_answer_warnings(plan.chunks, speech_segments, context)
    if warnings:
        plan = plan.model_copy(update={"chunks": annotated_items})
    return _ValidatedChunkPlan(plan=plan, chunks=chunks, warnings=warnings)


def _build_chunk_plan(
    context: StageContext,
    speech_segments: list[SpeechSegment],
    timeline_events: list[TimelineEvent],
    video_info: VideoInfo,
) -> _ValidatedChunkPlan:
    prompt_text = _prompt_path(context).read_text(encoding="utf-8")
    retry_advisor_prompt_text = _retry_advisor_prompt_path(context).read_text(encoding="utf-8")
    prompt_sha = _sha256_text(prompt_text)
    retry_advisor_prompt_sha = _sha256_text(retry_advisor_prompt_text)
    windows = _planner_windows(speech_segments, context)
    stale_raw_removed_count = _remove_stale_chunk_plan_raw_files(context, windows_count=len(windows))
    window_results = _plan_windows(
        context=context,
        system_prompt=prompt_text,
        retry_advisor_prompt_text=retry_advisor_prompt_text,
        prompt_sha=prompt_sha,
        retry_advisor_prompt_sha=retry_advisor_prompt_sha,
        windows=windows,
        timeline_events=timeline_events,
    )
    attempts_per_window = [result.attempts for result in window_results]
    attempts_used = sum(attempts_per_window)
    validation_errors = [error for result in window_results for error in result.errors]
    planned_items = [item for result in window_results for item in result.items]
    cached_windows_count = sum(1 for result in window_results if result.cache_hit)
    api_windows_count = len(window_results) - cached_windows_count

    planned_items = _merge_question_answer_splits(planned_items, speech_segments, context)
    planned_items, _ = _annotate_question_answer_warnings(planned_items, speech_segments, context)
    plan = ChunkPlan(
        video_id=context.job.video_id,
        visual_enabled=context.config.pipeline.visual_enabled,
        provider=context.config.chunking.provider,
        model=context.config.chunking.model,
        retry_advisor_model=context.config.chunking.retry_advisor_model,
        mode=context.config.chunking.mode,
        prompt_file=context.config.chunking.prompt_file,
        prompt_sha256=prompt_sha,
        retry_advisor_prompt_file=context.config.chunking.retry_advisor_prompt_file,
        retry_advisor_prompt_sha256=retry_advisor_prompt_sha,
        max_words=context.config.chunking.max_words,
        max_speech_segments_per_chunk=context.config.chunking.max_speech_segments_per_chunk,
        question_answer_merge_seconds=context.config.chunking.question_answer_merge_seconds,
        visual_attach_seconds=context.config.chunking.visual_attach_seconds,
        max_planner_segments_per_call=context.config.chunking.max_planner_segments_per_call,
        planner_context_segments=context.config.chunking.planner_context_segments,
        title_max_chars=context.config.chunking.title_max_chars,
        boundary_reason_max_chars=context.config.chunking.boundary_reason_max_chars,
        notes_max_chars=context.config.chunking.notes_max_chars,
        topic_max_chars=context.config.chunking.topic_max_chars,
        max_topics=context.config.chunking.max_topics,
        windows_count=len(windows),
        attempts=attempts_used,
        attempts_per_window=attempts_per_window,
        max_attempts_in_any_window=max(attempts_per_window, default=0),
        chunks=[
            item.model_copy(update={"chunk_index": index})
            for index, item in enumerate(planned_items, start=1)
        ],
    )
    final_errors, chunks = _validate_plan_and_materialize(plan, speech_segments, timeline_events, context, video_info)
    if final_errors:
        structured_errors = _global_plan_structured_errors(final_errors, plan, speech_segments, context)
        raw_output_paths = [
            result.diagnostics.raw_output_path
            for result in window_results
            if result.diagnostics is not None and result.diagnostics.raw_output_path is not None
        ]
        validation_errors.append({"scope": "global", "errors": final_errors, "structured_errors": structured_errors})
        _write_plan_errors(context, validation_errors)
        emit_stage_validation_failed(
            context,
            stage_name=Stage12BuildChunks.name,
            ordinal=Stage12BuildChunks.ordinal,
            error_code="openai_chunk_planner_invalid_plan",
            message="chunk plan global validation failed",
            validation_errors=final_errors,
            structured_errors=structured_errors,
            extra={"scope": "global", "raw_output_paths": raw_output_paths},
            raw_output_path=context.paths.chunk_plan,
        )
        raise ProviderError(
            "OpenAI chunk planner returned an invalid global plan",
            error_code="openai_chunk_planner_invalid_plan",
            details="\n".join(final_errors[:20]),
        )
    if context.paths.chunk_plan_errors.exists():
        context.paths.chunk_plan_errors.unlink()
    plan_warnings = _question_answer_boundary_warnings(plan.chunks, speech_segments, context)
    return _ValidatedChunkPlan(
        plan=plan,
        chunks=chunks,
        warnings=plan_warnings,
        cached_windows_count=cached_windows_count,
        api_windows_count=api_windows_count,
        stale_raw_removed_count=stale_raw_removed_count,
    )


def _plan_windows(
    *,
    context: StageContext,
    system_prompt: str,
    retry_advisor_prompt_text: str,
    prompt_sha: str,
    retry_advisor_prompt_sha: str,
    windows: list[_PlannerWindow],
    timeline_events: list[TimelineEvent],
) -> list[_WindowPlanResult]:
    if not windows:
        return []

    max_workers = min(context.config.chunking.planner_parallel_requests, len(windows))
    results_by_window: dict[int, _WindowPlanResult] = {}
    validation_errors: list[dict[str, Any]] = []
    api_key = os.environ.get("OPENAI_API_KEY")

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="style-kb-chunk-plan") as executor:
        futures: dict[Future[_WindowPlanResult], _PlannerWindow] = {
            executor.submit(
                _plan_window_with_cache,
                context=context,
                api_key=api_key,
                system_prompt=system_prompt,
                retry_advisor_prompt_text=retry_advisor_prompt_text,
                prompt_sha=prompt_sha,
                retry_advisor_prompt_sha=retry_advisor_prompt_sha,
                window=window,
                timeline_events=timeline_events,
            ): window
            for window in windows
        }
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                window = futures.pop(future)
                try:
                    result = future.result()
                except _WindowPlanError as error:
                    validation_errors.extend(error.errors)
                    _abort_window_planning(
                        context,
                        futures=futures,
                        failed_window=window,
                        error=error,
                        validation_errors=validation_errors,
                        completed_window_indexes=sorted(results_by_window),
                    )
                    raise
                except ProviderError as error:
                    validation_errors.append(
                        {
                            "window_index": window.window_index,
                            "attempt": None,
                            "errors": [str(error)],
                            "structured_errors": [
                                _chunk_validation_entry(
                                    code=error.error_code,
                                    message=str(error),
                                    window=window,
                                    field="response",
                                    preview=str(error),
                                )
                            ],
                            "raw_output": None,
                        }
                    )
                    _abort_window_planning(
                        context,
                        futures=futures,
                        failed_window=window,
                        error=error,
                        validation_errors=validation_errors,
                        completed_window_indexes=sorted(results_by_window),
                    )
                    raise
                except Exception as error:
                    validation_errors.append(
                        {
                            "window_index": window.window_index,
                            "attempt": None,
                            "errors": [f"{type(error).__name__}: {error}"],
                            "structured_errors": [
                                _chunk_validation_entry(
                                    code="openai_chunk_planner_unexpected_error",
                                    message=f"{type(error).__name__}: {error}",
                                    window=window,
                                    field="response",
                                    preview=str(error),
                                )
                            ],
                            "raw_output": None,
                        }
                    )
                    _abort_window_planning(
                        context,
                        futures=futures,
                        failed_window=window,
                        error=error,
                        validation_errors=validation_errors,
                        completed_window_indexes=sorted(results_by_window),
                    )
                    raise
                else:
                    results_by_window[result.window_index] = result

    return [results_by_window[window.window_index] for window in windows]


def _remove_stale_chunk_plan_raw_files(context: StageContext, *, windows_count: int) -> int:
    if not context.paths.chunks_raw_dir.exists():
        return 0
    pattern = re.compile(r"^chunk_plan_window_(\d{3})_(?:cache|attempt_\d{2}|retry_advice_attempt_\d{2})\.json$")
    removed_paths: list[Path] = []
    for path in sorted(context.paths.chunks_raw_dir.glob("chunk_plan_window_*.json")):
        match = pattern.match(path.name)
        if match is None or int(match.group(1)) <= windows_count:
            continue
        path.unlink()
        removed_paths.append(path)
    if removed_paths:
        _log_stale_chunk_plan_raw_removal(context, removed_paths, windows_count=windows_count)
    return len(removed_paths)


def _plan_window_with_cache(
    *,
    context: StageContext,
    api_key: str | None,
    system_prompt: str,
    retry_advisor_prompt_text: str,
    prompt_sha: str,
    retry_advisor_prompt_sha: str,
    window: _PlannerWindow,
    timeline_events: list[TimelineEvent],
) -> _WindowPlanResult:
    planner_payload = _planner_payload(window, timeline_events, context)
    request_metadata = _window_request_metadata(
        context,
        window,
        planner_payload,
        prompt_sha,
        retry_advisor_prompt_sha,
    )
    cached_result = _load_cached_window_result(context, window, request_metadata)
    if cached_result is not None:
        return cached_result

    client = OpenAIChunkPlannerClient(
        api_key,
        model=context.config.chunking.model,
        on_retry=_chunk_retry_logger(context),
    )
    return _plan_window_with_retries(
        context=context,
        client=client,
        api_key=api_key,
        system_prompt=system_prompt,
        retry_advisor_prompt_text=retry_advisor_prompt_text,
        prompt_sha=prompt_sha,
        window=window,
        planner_payload=planner_payload,
        request_metadata=request_metadata,
    )


def _plan_window_with_retries(
    *,
    context: StageContext,
    client: OpenAIChunkPlannerClient,
    api_key: str | None,
    system_prompt: str,
    retry_advisor_prompt_text: str,
    prompt_sha: str,
    window: _PlannerWindow,
    planner_payload: dict[str, Any],
    request_metadata: dict[str, Any],
) -> _WindowPlanResult:
    feedback: list[str] = []
    retry_advisor_instruction: str | None = None
    errors: list[dict[str, Any]] = []
    advisor_client = OpenAIChunkPlannerClient(
        api_key,
        model=context.config.chunking.retry_advisor_model,
        on_retry=_chunk_retry_logger(context),
    )
    prompt_cache_key, prompt_cache_retention = _openai_prompt_cache_settings(
        context,
        namespace="chunk-plan",
        model=context.config.chunking.model,
        fingerprint=openai_prompt_cache_fingerprint(prompt_sha),
    )
    for attempt in range(1, context.config.chunking.max_retries + 1):
        constraints_payload = _constraints_payload(
            context,
            window,
            prompt_sha,
            feedback,
            retry_advisor_instruction=retry_advisor_instruction,
        )
        items: list[ChunkPlanItem] = []
        result: ChunkPlanAnalysisResult | None = None
        raw_output_path = context.paths.chunk_plan_raw_attempt(window.window_index, attempt)
        event_extra = {
            "window_index": window.window_index,
            "attempt": attempt,
            "max_retries": context.config.chunking.max_retries,
            "core_segments_count": len(window.core_segments),
        }
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_STARTED,
            stage_name=Stage12BuildChunks.name,
            ordinal=Stage12BuildChunks.ordinal,
            operation=ProviderOperation.CHUNK_PLAN,
            diagnostics=ProviderCallDiagnostics(
                provider=ProviderName.OPENAI,
                model=context.config.chunking.model,
                raw_output_path=str(raw_output_path),
            ),
            attempt=attempt,
            message="chunk plan request started",
            extra=event_extra,
        )
        try:
            result = client.plan_chunks(
                system_prompt=system_prompt,
                planner_payload=planner_payload,
                constraints_payload=constraints_payload,
                raw_output_path=raw_output_path,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
        except ProviderError as error:
            emit_provider_event(
                context,
                PipelineEvent.PROVIDER_REQUEST_FAILED,
                stage_name=Stage12BuildChunks.name,
                ordinal=Stage12BuildChunks.ordinal,
                operation=ProviderOperation.CHUNK_PLAN,
                diagnostics=ProviderCallDiagnostics(
                    provider=ProviderName.OPENAI,
                    model=context.config.chunking.model,
                    raw_output_path=str(raw_output_path),
                    request_id=request_id_from_error(error),
                ),
                attempt=attempt,
                message="chunk plan request failed",
                extra={**provider_error_extra(error), **event_extra},
            )
            if error.error_code not in _RETRYABLE_PLANNER_ERROR_CODES:
                raise
            validation_errors = [str(error)]
            structured_validation_errors = [
                _chunk_validation_entry(
                    code=error.error_code,
                    message=validation_errors[0],
                    window=window,
                    field="response",
                    preview=validation_errors[0],
                )
            ]
        else:
            emit_provider_event(
                context,
                PipelineEvent.PROVIDER_REQUEST_COMPLETED,
                stage_name=Stage12BuildChunks.name,
                ordinal=Stage12BuildChunks.ordinal,
                operation=ProviderOperation.CHUNK_PLAN,
                diagnostics=result.diagnostics,
                attempt=attempt,
                message="chunk plan request completed",
                extra=event_extra,
            )
            try:
                items = _plan_items_from_payload(result.payload, context)
                validation_errors = _window_plan_validation_errors(items, window, context)
            except ProviderError as error:
                if error.error_code not in _RETRYABLE_PLANNER_ERROR_CODES:
                    raise
                validation_errors = [str(error)]
            structured_validation_errors = _window_plan_structured_errors(
                validation_errors,
                items=items,
                window=window,
                context=context,
            )
        if not validation_errors:
            _log_plan_attempt(
                context,
                window=window,
                attempt=attempt,
                items=items,
                validation_errors=[],
                analysis=result,
                structured_errors=[],
            )
            if result is None:
                raise ProviderError(
                    "OpenAI chunk planner produced no analysis result",
                    error_code="openai_chunk_planner_output_missing",
                )
            _write_window_cache(
                context.paths.chunk_plan_window_cache(window.window_index),
                request_metadata=request_metadata,
                analysis=result,
                planner_payload=planner_payload,
                constraints_payload=constraints_payload,
            )
            return _WindowPlanResult(
                window_index=window.window_index,
                items=items,
                attempts=attempt,
                errors=errors,
                cache_hit=False,
                diagnostics=result.diagnostics,
            )
        diagnostics = result.diagnostics if result is not None else None
        advisor_raw_output_path: Path | None = None
        advisor_instruction: str | None = None
        if attempt < context.config.chunking.max_retries:
            advisor_raw_output_path = context.paths.chunk_plan_retry_advice_attempt(window.window_index, attempt)
            advisor_instruction = _build_chunk_plan_retry_advice(
                context=context,
                advisor_client=advisor_client,
                system_prompt=retry_advisor_prompt_text,
                window=window,
                planner_payload=planner_payload,
                candidate_items=items,
                validation_errors=validation_errors,
                structured_errors=structured_validation_errors,
                raw_output_path=advisor_raw_output_path,
            )
            retry_advisor_instruction = advisor_instruction
        errors.append(
            {
                "window_index": window.window_index,
                "attempt": attempt,
                "errors": validation_errors,
                "structured_errors": structured_validation_errors,
                "raw_output": str(raw_output_path),
                "retry_advisor_raw_output": str(advisor_raw_output_path) if advisor_raw_output_path is not None else None,
                "retry_advisor_instruction": advisor_instruction,
                "diagnostics": diagnostics.to_dict() if diagnostics is not None else None,
            }
        )
        _log_plan_attempt(
            context,
            window=window,
            attempt=attempt,
            items=items,
            validation_errors=validation_errors,
            analysis=result,
            structured_errors=structured_validation_errors,
        )
        emit_stage_validation_failed(
            context,
            stage_name=Stage12BuildChunks.name,
            ordinal=Stage12BuildChunks.ordinal,
            error_code="openai_chunk_planner_invalid_plan",
            message=f"chunk plan validation failed for window {window.window_index}",
            validation_errors=validation_errors,
            structured_errors=structured_validation_errors,
            raw_output_path=raw_output_path,
            attempt=attempt,
            extra={"window_index": window.window_index},
        )
        feedback = _chunk_retry_feedback(validation_errors, advisor_instruction)

    raise _WindowPlanError(
        f"OpenAI chunk planner returned an invalid plan for window {window.window_index}",
        error_code="openai_chunk_planner_invalid_plan",
        details="\n".join(feedback[:20]),
        errors=errors,
    )


def _load_cached_window_result(
    context: StageContext,
    window: _PlannerWindow,
    request_metadata: dict[str, Any],
) -> _WindowPlanResult | None:
    cache_path = context.paths.chunk_plan_window_cache(window.window_index)
    if not cache_path.exists():
        return None
    try:
        cached_payload = read_json(cache_path)
    except Exception:
        return None
    if cached_payload.get("cache_metadata") != request_metadata:
        _log_window_cache(
            context,
            window=window,
            cache_path=cache_path,
            cache_hit=False,
            validation_errors=_cache_metadata_mismatch_errors(
                cached_payload.get("cache_metadata"),
                request_metadata,
            ),
        )
        return None
    try:
        analysis = load_cached_chunk_plan_result(cache_path)
        items = _plan_items_from_payload(analysis.payload, context)
        validation_errors = _window_plan_validation_errors(items, window, context)
    except Exception as error:
        _log_window_cache(
            context,
            window=window,
            cache_path=cache_path,
            cache_hit=False,
            validation_errors=[f"{type(error).__name__}: {error}"],
        )
        return None
    if validation_errors:
        structured_errors = _window_plan_structured_errors(
            validation_errors,
            items=items,
            window=window,
            context=context,
        )
        _log_window_cache(
            context,
            window=window,
            cache_path=cache_path,
            cache_hit=False,
            validation_errors=validation_errors,
            structured_errors=structured_errors,
            analysis=analysis,
        )
        emit_stage_validation_failed(
            context,
            stage_name=Stage12BuildChunks.name,
            ordinal=Stage12BuildChunks.ordinal,
            error_code="openai_chunk_planner_invalid_plan",
            message=f"cached chunk plan validation failed for window {window.window_index}",
            validation_errors=validation_errors,
            structured_errors=structured_errors,
            raw_output_path=cache_path,
            attempt=0,
            extra={"window_index": window.window_index, "cache_hit": True},
        )
        return None

    _log_window_cache(context, window=window, cache_path=cache_path, cache_hit=True, validation_errors=[], analysis=analysis)
    return _WindowPlanResult(
        window_index=window.window_index,
        items=items,
        attempts=0,
        errors=[],
        cache_hit=True,
        diagnostics=analysis.diagnostics.with_updates(raw_output_path=str(cache_path), cached=True),
    )


def _write_window_cache(
    cache_path: Path,
    *,
    request_metadata: dict[str, Any],
    analysis: ChunkPlanAnalysisResult,
    planner_payload: dict[str, Any],
    constraints_payload: dict[str, Any],
) -> None:
    write_json_atomic(
        cache_path,
        {
            "cache_metadata": request_metadata,
            "request": {
                "constraints": constraints_payload,
                "planner_input": planner_payload,
            },
            "response": analysis.raw_payload,
            "diagnostics": provider_event_data(
                operation=ProviderOperation.CHUNK_PLAN,
                diagnostics=analysis.diagnostics.with_updates(raw_output_path=str(cache_path), cached=False),
            ),
        },
    )


def _build_chunk_plan_retry_advice(
    *,
    context: StageContext,
    advisor_client: OpenAIChunkPlannerClient,
    system_prompt: str,
    window: _PlannerWindow,
    planner_payload: dict[str, Any],
    candidate_items: list[ChunkPlanItem],
    validation_errors: list[str],
    structured_errors: list[dict[str, Any]],
    raw_output_path: Path,
) -> str | None:
    repair_payload = {
        "video_id": context.job.video_id,
        "window_index": window.window_index,
        "main_model": context.config.chunking.model,
        "retry_advisor_model": context.config.chunking.retry_advisor_model,
        "constraints": {
            "max_words": context.config.chunking.max_words,
            "max_speech_segments_per_chunk": context.config.chunking.max_speech_segments_per_chunk,
            "question_answer_merge_seconds": context.config.chunking.question_answer_merge_seconds,
            "title_max_chars": context.config.chunking.title_max_chars,
            "boundary_reason_max_chars": context.config.chunking.boundary_reason_max_chars,
            "notes_max_chars": context.config.chunking.notes_max_chars,
            "topic_max_chars": context.config.chunking.topic_max_chars,
            "max_topics": context.config.chunking.max_topics,
            "planning_segment_ids": [segment.segment_id for segment in window.core_segments],
            "context_segment_ids_not_for_output": [
                segment.segment_id for segment in [*window.context_before, *window.context_after]
            ],
        },
        "validation_errors": validation_errors[:30],
        "structured_errors": structured_errors[:30],
        "candidate_chunks": [
            _chunk_plan_item_payload(item, index=index)
            for index, item in enumerate(candidate_items, start=1)
        ],
        "planner_input": planner_payload,
    }
    try:
        prompt_cache_key, prompt_cache_retention = _openai_prompt_cache_settings(
            context,
            namespace="chunk-plan-retry-advisor",
            model=context.config.chunking.retry_advisor_model,
            fingerprint=openai_prompt_cache_fingerprint(_sha256_text(system_prompt)),
        )
        advisor_payload = advisor_client.analyze_plan_errors(
            system_prompt=system_prompt,
            repair_payload=repair_payload,
            raw_output_path=raw_output_path,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_retention=prompt_cache_retention,
        )
    except ProviderError as error:
        _log_chunk_plan_retry_advisor_failure(context, window=window, raw_output_path=raw_output_path, error=error)
        return None

    instruction = str(advisor_payload.get("repair_instruction") or "").strip()
    if not instruction:
        _log_chunk_plan_retry_advisor_failure(
            context,
            window=window,
            raw_output_path=raw_output_path,
            error=ProviderError(
                "OpenAI chunk planner retry advisor returned an empty repair instruction",
                error_code="openai_chunk_planner_retry_advisor_empty_instruction",
            ),
        )
        return None

    _log_chunk_plan_retry_advisor_success(
        context,
        window=window,
        raw_output_path=raw_output_path,
        advisor_payload=advisor_payload,
    )
    return instruction


def _chunk_plan_item_payload(item: ChunkPlanItem, *, index: int) -> dict[str, Any]:
    return {
        "chunk_index": index,
        "speech_segment_ids": item.speech_segment_ids,
        "title": item.title,
        "boundary_reason": item.boundary_reason,
        "topics": item.topics,
        "notes": item.notes,
    }


def _chunk_retry_feedback(validation_errors: list[str], advisor_instruction: str | None) -> list[str]:
    if not advisor_instruction:
        return validation_errors
    return [*validation_errors, f"retry_advisor_instruction: {advisor_instruction}"]


def _cache_metadata_mismatch_errors(cached_metadata: object, expected_metadata: dict[str, Any]) -> list[str]:
    if not isinstance(cached_metadata, dict):
        return [f"cache metadata has invalid shape: {type(cached_metadata).__name__}"]
    changed_keys = [
        key
        for key in sorted(set(cached_metadata) | set(expected_metadata))
        if cached_metadata.get(key) != expected_metadata.get(key)
    ]
    if not changed_keys:
        return ["cache metadata mismatch"]
    return [
        "cache metadata mismatch changed_keys="
        + ", ".join(changed_keys[:12])
        + (" ..." if len(changed_keys) > 12 else "")
    ]


def _window_request_metadata(
    context: StageContext,
    window: _PlannerWindow,
    planner_payload: dict[str, Any],
    prompt_sha: str,
    retry_advisor_prompt_sha: str,
) -> dict[str, Any]:
    return {
        "schema_version": _PLAN_SCHEMA_VERSION,
        "video_id": context.job.video_id,
        "visual_enabled": context.config.pipeline.visual_enabled,
        "provider": context.config.chunking.provider,
        "model": context.config.chunking.model,
        "retry_advisor_model": context.config.chunking.retry_advisor_model,
        "mode": context.config.chunking.mode,
        "prompt_file": context.config.chunking.prompt_file,
        "prompt_sha256": prompt_sha,
        "retry_advisor_prompt_file": context.config.chunking.retry_advisor_prompt_file,
        "retry_advisor_prompt_sha256": retry_advisor_prompt_sha,
        "window_index": window.window_index,
        "planning_segment_ids": [segment.segment_id for segment in window.core_segments],
        "context_before_segment_ids": [segment.segment_id for segment in window.context_before],
        "context_after_segment_ids": [segment.segment_id for segment in window.context_after],
        "max_words": context.config.chunking.max_words,
        "max_speech_segments_per_chunk": context.config.chunking.max_speech_segments_per_chunk,
        "question_answer_merge_seconds": context.config.chunking.question_answer_merge_seconds,
        "visual_attach_seconds": context.config.chunking.visual_attach_seconds,
        "max_planner_segments_per_call": context.config.chunking.max_planner_segments_per_call,
        "planner_context_segments": context.config.chunking.planner_context_segments,
        "title_max_chars": context.config.chunking.title_max_chars,
        "boundary_reason_max_chars": context.config.chunking.boundary_reason_max_chars,
        "notes_max_chars": context.config.chunking.notes_max_chars,
        "topic_max_chars": context.config.chunking.topic_max_chars,
        "max_topics": context.config.chunking.max_topics,
        "input_fingerprint": _fingerprint(planner_payload),
    }


def _plan_items_from_payload(payload: dict[str, Any], context: StageContext) -> list[ChunkPlanItem]:
    raw_chunks = payload.get("chunks")
    if not isinstance(raw_chunks, list) or not raw_chunks:
        raise ProviderError("OpenAI chunk planner returned no chunks", error_code="openai_chunk_planner_empty_chunks")
    items: list[ChunkPlanItem] = []
    for index, raw_chunk in enumerate(raw_chunks, start=1):
        if not isinstance(raw_chunk, dict):
            raise ProviderError("OpenAI chunk planner returned an invalid chunk", error_code="openai_chunk_planner_invalid_chunk")
        items.append(
            ChunkPlanItem(
                chunk_index=index,
                speech_segment_ids=[str(segment_id) for segment_id in raw_chunk.get("speech_segment_ids") or []],
                title=_clip_string(_compact_string(raw_chunk.get("title")), context.config.chunking.title_max_chars),
                boundary_reason=_clip_string(
                    _compact_string(raw_chunk.get("boundary_reason")),
                    context.config.chunking.boundary_reason_max_chars,
                ),
                topics=stable_unique(_compact_string(topic).lower() for topic in raw_chunk.get("topics") or []),
                notes=_clip_string(_compact_string(raw_chunk.get("notes")), context.config.chunking.notes_max_chars),
            )
        )
    return items


def _window_plan_validation_errors(
    items: list[ChunkPlanItem],
    window: _PlannerWindow,
    context: StageContext,
) -> list[str]:
    core_ids = [segment.segment_id for segment in window.core_segments]
    core_positions = {segment_id: index for index, segment_id in enumerate(core_ids)}
    errors: list[str] = []
    seen: list[str] = []
    for item in items:
        if not item.speech_segment_ids:
            errors.append(f"chunk {item.chunk_index} has no speech_segment_ids")
            continue
        duplicated_ids = _duplicated_values(item.speech_segment_ids)
        if duplicated_ids:
            errors.append(f"chunk {item.chunk_index} has duplicated speech_segment_ids: {duplicated_ids}")
        unknown = [segment_id for segment_id in item.speech_segment_ids if segment_id not in core_positions]
        if unknown:
            errors.append(f"chunk {item.chunk_index} references non-core segment ids: {unknown}")
            continue
        positions = [core_positions[segment_id] for segment_id in item.speech_segment_ids]
        if positions != list(range(min(positions), max(positions) + 1)):
            errors.append(f"chunk {item.chunk_index} segment ids are not contiguous")
        if len(item.speech_segment_ids) > context.config.chunking.max_speech_segments_per_chunk:
            errors.append(f"chunk {item.chunk_index} exceeds max_speech_segments_per_chunk")
        words = _speech_segments_word_count(_segments_by_ids(window.core_segments, item.speech_segment_ids))
        if words > context.config.chunking.max_words:
            errors.append(f"chunk {item.chunk_index} exceeds max_words: {words}")
        errors.extend(_chunk_plan_item_metadata_errors(item, context))
        seen.extend(item.speech_segment_ids)

    if seen != core_ids:
        missing = [segment_id for segment_id in core_ids if segment_id not in seen]
        duplicated = [segment_id for segment_id in seen if seen.count(segment_id) > 1]
        errors.append(f"window coverage mismatch missing={missing} duplicated={stable_unique(duplicated)}")
    errors.extend(_question_answer_boundary_errors(items, window.core_segments, context))
    return errors


def _window_plan_structured_errors(
    errors: list[str],
    *,
    items: list[ChunkPlanItem],
    window: _PlannerWindow,
    context: StageContext,
) -> list[dict[str, Any]]:
    return [
        _chunk_validation_entry(
            code=_chunk_validation_code(error),
            message=error,
            window=window,
            field=_chunk_validation_field(error),
            preview=_chunk_validation_preview(error, items, window, context),
        )
        for error in errors
    ]


def _global_plan_structured_errors(
    errors: list[str],
    plan: ChunkPlan,
    speech_segments: list[SpeechSegment],
    context: StageContext,
) -> list[dict[str, Any]]:
    windows = _planner_windows(speech_segments, context)
    fallback_window = windows[0] if windows else _PlannerWindow(window_index=0, core_segments=speech_segments, context_before=[], context_after=[])
    structured_errors: list[dict[str, Any]] = []
    for error in errors:
        chunk_index = _chunk_index_from_error(error)
        item = next((candidate for candidate in plan.chunks if candidate.chunk_index == chunk_index), None)
        preview: Any
        field = _chunk_validation_field(error)
        if item is not None:
            preview = getattr(item, field) if hasattr(item, field) else item.model_dump(mode="json")
        elif "coverage mismatch" in error:
            preview = {"expected_segment_ids": [segment.segment_id for segment in speech_segments], "message": error}
        else:
            preview = error
        entry = _chunk_validation_entry(
            code=_chunk_validation_code(error),
            message=error,
            window=fallback_window,
            field=field,
            preview=preview,
        )
        entry["scope"] = "global"
        structured_errors.append(entry)
    return structured_errors


def _chunk_validation_entry(
    *,
    code: str,
    message: str,
    window: _PlannerWindow,
    field: str,
    preview: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "code": code,
        "message": message,
        "window_index": window.window_index,
        "field": field,
        "preview": validation_preview(preview),
    }
    chunk_index = _chunk_index_from_error(message)
    if chunk_index is not None:
        entry["chunk_index"] = chunk_index
    return entry


def _chunk_validation_code(error: str) -> str:
    if "speech_segment_ids" in error:
        return "openai_chunk_planner_segment_ids"
    if "max_words" in error or "max_speech_segments" in error:
        return "openai_chunk_planner_chunk_limits"
    if "title" in error or "boundary_reason" in error or "topics" in error or "notes" in error:
        return "openai_chunk_planner_metadata"
    if "coverage mismatch" in error:
        return "openai_chunk_planner_coverage"
    if "offscreen question" in error:
        return "openai_chunk_planner_question_answer_split"
    return "openai_chunk_planner_invalid_plan"


def _chunk_validation_field(error: str) -> str:
    if "speech_segment_ids" in error or "segment ids" in error:
        return "speech_segment_ids"
    if "title" in error:
        return "title"
    if "boundary_reason" in error:
        return "boundary_reason"
    if "topics" in error:
        return "topics"
    if "notes" in error:
        return "notes"
    if "coverage mismatch" in error:
        return "coverage"
    return "chunk_plan"


def _chunk_validation_preview(
    error: str,
    items: list[ChunkPlanItem],
    window: _PlannerWindow,
    context: StageContext,
) -> Any:
    chunk_index = _chunk_index_from_error(error)
    if chunk_index is not None:
        item = next((candidate for candidate in items if candidate.chunk_index == chunk_index), None)
        if item is not None:
            field = _chunk_validation_field(error)
            if field == "speech_segment_ids":
                return item.speech_segment_ids
            if hasattr(item, field):
                return getattr(item, field)
            return item.model_dump(mode="json")
    if "offscreen question" in error:
        ids = re.findall(r"seg_[A-Za-z0-9_-]+|[A-Za-z0-9_-]+_seg_\d+", error)
        segment_map = {segment.segment_id: segment for segment in window.core_segments}
        return [
            {"segment_id": segment_id, "text": segment_map[segment_id].text}
            for segment_id in ids
            if segment_id in segment_map
        ] or error
    if "coverage mismatch" in error:
        return {"expected_segment_ids": [segment.segment_id for segment in window.core_segments], "message": error}
    return error


def _chunk_index_from_error(error: str) -> int | None:
    match = re.search(r"chunk (\d+)", error)
    return int(match.group(1)) if match else None


def _plan_validation_errors(
    plan: ChunkPlan,
    speech_segments: list[SpeechSegment],
    context: StageContext,
) -> list[str]:
    expected_metadata = _plan_metadata(context)
    actual_metadata = {
        "video_id": plan.video_id,
        "visual_enabled": plan.visual_enabled,
        "provider": plan.provider,
        "model": plan.model,
        "retry_advisor_model": plan.retry_advisor_model,
        "mode": plan.mode,
        "prompt_file": plan.prompt_file,
        "prompt_sha256": plan.prompt_sha256,
        "retry_advisor_prompt_file": plan.retry_advisor_prompt_file,
        "retry_advisor_prompt_sha256": plan.retry_advisor_prompt_sha256,
        "max_words": plan.max_words,
        "max_speech_segments_per_chunk": plan.max_speech_segments_per_chunk,
        "question_answer_merge_seconds": plan.question_answer_merge_seconds,
        "visual_attach_seconds": plan.visual_attach_seconds,
        "max_planner_segments_per_call": plan.max_planner_segments_per_call,
        "planner_context_segments": plan.planner_context_segments,
        "title_max_chars": plan.title_max_chars,
        "boundary_reason_max_chars": plan.boundary_reason_max_chars,
        "notes_max_chars": plan.notes_max_chars,
        "topic_max_chars": plan.topic_max_chars,
        "max_topics": plan.max_topics,
    }
    errors = [
        f"plan metadata mismatch: expected {expected_metadata}, got {actual_metadata}"
    ] if actual_metadata != expected_metadata else []
    expected_windows_count = len(_planner_windows(speech_segments, context))
    if plan.windows_count != expected_windows_count:
        errors.append(f"plan windows_count mismatch: expected {expected_windows_count}, got {plan.windows_count}")
    if plan.windows_count != len(plan.attempts_per_window):
        errors.append("plan attempts_per_window length does not match windows_count")
    if plan.attempts != sum(plan.attempts_per_window):
        errors.append("plan attempts does not equal sum(attempts_per_window)")
    if plan.max_attempts_in_any_window != max(plan.attempts_per_window, default=0):
        errors.append("plan max_attempts_in_any_window does not match attempts_per_window")

    if not plan.chunks:
        errors.append("plan has no chunks")
        return errors

    segment_ids = [segment.segment_id for segment in speech_segments]
    positions = {segment_id: index for index, segment_id in enumerate(segment_ids)}
    seen: list[str] = []
    segments_by_id = {segment.segment_id: segment for segment in speech_segments}
    for expected_index, item in enumerate(plan.chunks, start=1):
        if item.chunk_index != expected_index:
            errors.append(f"chunk index mismatch at position {expected_index}")
        if not item.speech_segment_ids:
            errors.append(f"chunk {item.chunk_index} has no speech_segment_ids")
            continue
        duplicated_ids = _duplicated_values(item.speech_segment_ids)
        if duplicated_ids:
            errors.append(f"chunk {item.chunk_index} has duplicated speech_segment_ids: {duplicated_ids}")
        unknown = [segment_id for segment_id in item.speech_segment_ids if segment_id not in positions]
        if unknown:
            errors.append(f"chunk {item.chunk_index} references unknown segment ids: {unknown}")
            continue
        item_positions = [positions[segment_id] for segment_id in item.speech_segment_ids]
        if item_positions != list(range(min(item_positions), max(item_positions) + 1)):
            errors.append(f"chunk {item.chunk_index} segment ids are not globally contiguous")
        if len(item.speech_segment_ids) > context.config.chunking.max_speech_segments_per_chunk:
            errors.append(f"chunk {item.chunk_index} exceeds max_speech_segments_per_chunk")
        item_segments = [segments_by_id[segment_id] for segment_id in item.speech_segment_ids]
        words = _speech_segments_word_count(item_segments)
        if words > context.config.chunking.max_words:
            errors.append(f"chunk {item.chunk_index} exceeds max_words: {words}")
        errors.extend(_chunk_plan_item_metadata_errors(item, context))
        seen.extend(item.speech_segment_ids)

    if seen != segment_ids:
        missing = [segment_id for segment_id in segment_ids if segment_id not in seen]
        duplicated = [segment_id for segment_id in seen if seen.count(segment_id) > 1]
        errors.append(f"global coverage mismatch missing={missing} duplicated={stable_unique(duplicated)}")
    errors.extend(_question_answer_boundary_errors(plan.chunks, speech_segments, context))

    return errors


def _validate_plan_and_materialize(
    plan: ChunkPlan,
    speech_segments: list[SpeechSegment],
    timeline_events: list[TimelineEvent],
    context: StageContext,
    video_info: VideoInfo,
) -> tuple[list[str], list[Chunk]]:
    errors = _plan_validation_errors(plan, speech_segments, context)
    if errors:
        return errors, []
    try:
        chunks = _materialize_chunks(plan, speech_segments, timeline_events, context, video_info)
        chunks = [_sanitize_chunk_for_kb(chunk) for chunk in chunks]
    except Exception as error:
        return [f"chunk materialization failed: {type(error).__name__}: {error}"], []
    contamination_errors = _chunk_contamination_errors(chunks)
    if contamination_errors:
        return contamination_errors, []
    return [], chunks


def _materialize_chunks(
    plan: ChunkPlan,
    speech_segments: list[SpeechSegment],
    timeline_events: list[TimelineEvent],
    context: StageContext,
    video_info: VideoInfo,
) -> list[Chunk]:
    segments_by_id = {segment.segment_id: segment for segment in speech_segments}
    chunks: list[Chunk] = []
    for item in plan.chunks:
        item_segments = [segments_by_id[segment_id] for segment_id in item.speech_segment_ids]
        start = item_segments[0].start
        end = item_segments[-1].end
        chunk_events = _chunk_timeline_events(start, end, timeline_events, context)
        if not chunk_events:
            raise StageExecutionError(
                f"chunk {item.chunk_index} has no timeline evidence for {start:.3f}..{end:.3f}",
                error_code="chunk_timeline_evidence_missing",
            )
        speech_text = " ".join(segment.text for segment in item_segments if segment.text).strip()
        dialogue_text = _dialogue_text_from_segments(item_segments)
        presenter_brief = _safe_chunk_text(_presenter_brief(chunk_events))
        visual_text = compact_join(
            [
                " ".join(_safe_chunk_text(event.visual_summary) for event in chunk_events if event.visual_summary).strip(),
                "\n".join(
                    _safe_chunk_text(text)
                    for event in chunk_events
                    for text in event.on_screen_text
                    if _safe_chunk_text(text)
                ).strip(),
                "; ".join(
                    _safe_chunk_text(value)
                    for event in chunk_events
                    for value in [*event.items, *event.topics]
                    if _safe_chunk_text(value)
                ).strip(),
            ]
        )
        combined_text = compact_join([dialogue_text or speech_text, presenter_brief, visual_text])
        topics = stable_unique(_safe_chunk_text(topic) for topic in [*item.topics, *(topic for event in chunk_events for topic in event.topics)] if _safe_chunk_text(topic))
        entities = stable_unique(_safe_chunk_text(item_name) for event in chunk_events for item_name in event.items if _safe_chunk_text(item_name))
        on_screen_text = stable_unique(_safe_chunk_text(text) for event in chunk_events for text in event.on_screen_text if _safe_chunk_text(text))
        speaker_roles = stable_unique(segment.speaker_role for segment in item_segments if segment.speaker_role)
        modality = []
        if speech_text:
            modality.append("audio")
        if visual_text or on_screen_text:
            modality.append("visual")
        chunks.append(
            Chunk(
                chunk_id=chunk_id(context.job.video_id, start, end),
                video_id=context.job.video_id,
                speech_segment_ids=item.speech_segment_ids,
                chunk_title=item.title,
                boundary_reason=item.boundary_reason,
                title=video_info.title,
                channel=video_info.channel,
                url=video_info.url,
                presenter_brief=presenter_brief,
                start=start,
                end=end,
                timestamp_url=build_timestamp_url(context.job.video_id, start),
                speech_text=speech_text,
                dialogue_text=dialogue_text,
                visual_text=visual_text,
                combined_text=combined_text,
                on_screen_text=on_screen_text,
                topics=topics,
                entities=entities,
                modality=modality,
                speaker_roles=speaker_roles,
                timeline_event_ids=[event.event_id for event in chunk_events],
                source_refs=_chunk_source_refs(
                    context.job.video_id,
                    start,
                    end,
                    chunk_events,
                    title=video_info.title,
                    include_visual_refs=context.config.pipeline.visual_enabled,
                ),
            )
        )
    return chunks


def _sanitize_chunk_for_kb(chunk: Chunk) -> Chunk:
    presenter_brief = _safe_chunk_text(chunk.presenter_brief)
    visual_text = _safe_chunk_text(chunk.visual_text)
    topics = stable_unique(_safe_chunk_text(topic) for topic in chunk.topics if _safe_chunk_text(topic))
    entities = stable_unique(_safe_chunk_text(entity) for entity in chunk.entities if _safe_chunk_text(entity))
    on_screen_text = stable_unique(_safe_chunk_text(text) for text in chunk.on_screen_text if _safe_chunk_text(text))
    combined_text = compact_join([chunk.dialogue_text or chunk.speech_text, presenter_brief, visual_text])
    return chunk.model_copy(
        update={
            "presenter_brief": presenter_brief,
            "visual_text": visual_text,
            "combined_text": combined_text,
            "on_screen_text": on_screen_text,
            "topics": topics,
            "entities": entities,
        }
    )


def _chunk_contamination_errors(chunks: list[Chunk]) -> list[str]:
    errors: list[str] = []
    for chunk in chunks:
        fields = {
            "visual_text": visual_field_noise_markers(chunk.visual_text),
            "presenter_brief": visual_field_noise_markers(chunk.presenter_brief),
            "topics": visual_field_noise_markers(chunk.topics),
            "entities": visual_field_noise_markers(chunk.entities),
            "combined_text": visual_field_noise_markers(_combined_visual_component(chunk)),
        }
        fields = {field: markers for field, markers in fields.items() if markers}
        if not fields:
            continue
        for field, markers in fields.items():
            errors.append(
                f"chunk {chunk.chunk_id} field {field} contains presentation/technical markers: {markers[:5]}"
            )
    return errors


def _chunk_contamination_chunks_count(chunks: list[Chunk]) -> int:
    return sum(
        1
        for chunk in chunks
        if any(
            visual_field_noise_markers(value)
            for value in [
                chunk.visual_text,
                chunk.presenter_brief,
                chunk.topics,
                chunk.entities,
                _combined_visual_component(chunk),
            ]
        )
    )


def _safe_chunk_text(value: object) -> str:
    text = _compact_string(value)
    if not text:
        return ""
    if visual_field_noise_markers(text):
        return ""
    return text


def _timeline_event_style_evidence_text(event: TimelineEvent) -> str:
    return compact_join(
        [
            _safe_chunk_text(event.visual_summary),
            "; ".join(_safe_chunk_text(item) for item in event.items if _safe_chunk_text(item)),
            "; ".join(_safe_chunk_text(topic) for topic in event.topics if _safe_chunk_text(topic)),
            "; ".join(_safe_chunk_text(text) for text in event.on_screen_text if _safe_chunk_text(text)),
        ]
    )


def _combined_visual_component(chunk: Chunk) -> str:
    return compact_join([chunk.presenter_brief, chunk.visual_text])


def _combined_text_redundancy_metrics(chunks: list[Chunk]) -> dict[str, int]:
    combined_tokens_total = 0
    redundant_tokens_total = 0
    chunks_with_redundancy = 0
    max_redundancy_bps = 0
    for chunk in chunks:
        combined_tokens = _redundancy_tokens(chunk.combined_text)
        if not combined_tokens:
            continue
        repeated_source_tokens = set(
            _redundancy_tokens(
                compact_join(
                    [
                        chunk.visual_text,
                        "\n".join(chunk.topics),
                        "\n".join(chunk.entities),
                    ]
                )
            )
        )
        redundant_tokens = sum(1 for token in combined_tokens if token in repeated_source_tokens)
        combined_tokens_total += len(combined_tokens)
        redundant_tokens_total += redundant_tokens
        redundancy_bps = round(redundant_tokens / len(combined_tokens) * 10000)
        max_redundancy_bps = max(max_redundancy_bps, redundancy_bps)
        if redundant_tokens:
            chunks_with_redundancy += 1
    return {
        "combined_text_tokens_total": combined_tokens_total,
        "combined_text_redundant_tokens_total": redundant_tokens_total,
        "combined_text_redundancy_ratio_bps": round(redundant_tokens_total / max(combined_tokens_total, 1) * 10000),
        "combined_text_redundancy_chunks_count": chunks_with_redundancy,
        "combined_text_redundancy_max_chunk_ratio_bps": max_redundancy_bps,
    }


def _redundancy_tokens(value: str) -> list[str]:
    return re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE)


def _planner_windows(segments: list[SpeechSegment], context: StageContext) -> list[_PlannerWindow]:
    windows: list[_PlannerWindow] = []
    max_segments = context.config.chunking.max_planner_segments_per_call
    context_segments = context.config.chunking.planner_context_segments
    start_index = 0
    window_index = 1
    while start_index < len(segments):
        end_index = min(start_index + max_segments, len(segments))
        end_index = _qa_safe_window_end(segments, start_index, end_index, context)
        windows.append(
            _PlannerWindow(
                window_index=window_index,
                core_segments=segments[start_index:end_index],
                context_before=segments[max(0, start_index - context_segments) : start_index],
                context_after=segments[end_index : min(len(segments), end_index + context_segments)],
            )
        )
        start_index = end_index
        window_index += 1
    return windows


def _qa_safe_window_end(
    segments: list[SpeechSegment],
    start_index: int,
    end_index: int,
    context: StageContext,
) -> int:
    if end_index >= len(segments) or end_index <= start_index:
        return end_index
    if not _is_question_answer_pair(segments[end_index - 1], segments[end_index], context):
        return end_index
    if end_index - start_index > 1:
        return end_index - 1
    return min(end_index + 1, len(segments))


def _planner_payload(window: _PlannerWindow, timeline_events: list[TimelineEvent], context: StageContext) -> dict[str, Any]:
    return {
        "window_index": window.window_index,
        "visual_enabled": context.config.pipeline.visual_enabled,
        "context_before": [
            _segment_payload(
                segment,
                timeline_events,
                planning_allowed=False,
                visual_enabled=context.config.pipeline.visual_enabled,
            )
            for segment in window.context_before
        ],
        "segments": [
            _segment_payload(
                segment,
                timeline_events,
                planning_allowed=True,
                visual_enabled=context.config.pipeline.visual_enabled,
            )
            for segment in window.core_segments
        ],
        "context_after": [
            _segment_payload(
                segment,
                timeline_events,
                planning_allowed=False,
                visual_enabled=context.config.pipeline.visual_enabled,
            )
            for segment in window.context_after
        ],
    }


def _segment_payload(
    segment: SpeechSegment,
    timeline_events: list[TimelineEvent],
    *,
    planning_allowed: bool,
    visual_enabled: bool,
) -> dict[str, Any]:
    overlapping_events = _overlapping_timeline_events(segment.start, segment.end, timeline_events)
    return {
        "segment_id": segment.segment_id,
        "planning_allowed": planning_allowed,
        "start": segment.start,
        "end": segment.end,
        "speaker_role": segment.speaker_role,
        "text": segment.text,
        "timeline_event_ids": [event.event_id for event in overlapping_events],
        "visual_hints": [
            {
                "event_id": event.event_id,
                "start": event.start,
                "end": event.end,
                "style_evidence_text": _timeline_event_style_evidence_text(event),
                "visual_summary": _safe_chunk_text(event.visual_summary),
                "on_screen_text": [_safe_chunk_text(text) for text in event.on_screen_text if _safe_chunk_text(text)],
                "topics": [_safe_chunk_text(topic) for topic in event.topics if _safe_chunk_text(topic)],
                "items": [_safe_chunk_text(item) for item in event.items if _safe_chunk_text(item)],
                "presenter_relevance": event.presenter_context.relevance,
            }
            for event in overlapping_events
        ] if visual_enabled else [],
    }


def _constraints_payload(
    context: StageContext,
    window: _PlannerWindow,
    prompt_sha: str,
    feedback: list[str],
    *,
    retry_advisor_instruction: str | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": _PLAN_SCHEMA_VERSION,
        "video_id": context.job.video_id,
        "visual_enabled": context.config.pipeline.visual_enabled,
        "mode": context.config.chunking.mode,
        "provider": context.config.chunking.provider,
        "model": context.config.chunking.model,
        "retry_advisor_model": context.config.chunking.retry_advisor_model,
        "prompt_file": context.config.chunking.prompt_file,
        "prompt_sha256": prompt_sha,
        "planning_segment_ids": [segment.segment_id for segment in window.core_segments],
        "context_segment_ids_not_for_output": [
            segment.segment_id for segment in [*window.context_before, *window.context_after]
        ],
        "hard_rules": [
            "Cover every planning_segment_id exactly once.",
            "Preserve order.",
            "Every chunk must contain contiguous speech_segment_ids.",
            "Do not include context_segment_ids_not_for_output in chunks.",
            "Return only chunk plan metadata, not final chunk text.",
            "Merge offscreen_questioner with nearby host answer when it is part of one QA exchange.",
        ],
        "max_words": context.config.chunking.max_words,
        "max_speech_segments_per_chunk": context.config.chunking.max_speech_segments_per_chunk,
        "question_answer_merge_seconds": context.config.chunking.question_answer_merge_seconds,
        "planner_context_segments": context.config.chunking.planner_context_segments,
        "title_max_chars": context.config.chunking.title_max_chars,
        "boundary_reason_max_chars": context.config.chunking.boundary_reason_max_chars,
        "notes_max_chars": context.config.chunking.notes_max_chars,
        "topic_max_chars": context.config.chunking.topic_max_chars,
        "max_topics": context.config.chunking.max_topics,
        "retry_feedback": feedback,
    }
    if retry_advisor_instruction:
        payload["retry_advisor_instruction"] = retry_advisor_instruction
    return payload


def _plan_metadata(context: StageContext) -> dict[str, Any]:
    prompt_text = _prompt_path(context).read_text(encoding="utf-8")
    retry_advisor_prompt_text = _retry_advisor_prompt_path(context).read_text(encoding="utf-8")
    return {
        "video_id": context.job.video_id,
        "visual_enabled": context.config.pipeline.visual_enabled,
        "provider": context.config.chunking.provider,
        "model": context.config.chunking.model,
        "retry_advisor_model": context.config.chunking.retry_advisor_model,
        "mode": context.config.chunking.mode,
        "prompt_file": context.config.chunking.prompt_file,
        "prompt_sha256": _sha256_text(prompt_text),
        "retry_advisor_prompt_file": context.config.chunking.retry_advisor_prompt_file,
        "retry_advisor_prompt_sha256": _sha256_text(retry_advisor_prompt_text),
        "max_words": context.config.chunking.max_words,
        "max_speech_segments_per_chunk": context.config.chunking.max_speech_segments_per_chunk,
        "question_answer_merge_seconds": context.config.chunking.question_answer_merge_seconds,
        "visual_attach_seconds": context.config.chunking.visual_attach_seconds,
        "max_planner_segments_per_call": context.config.chunking.max_planner_segments_per_call,
        "planner_context_segments": context.config.chunking.planner_context_segments,
        "title_max_chars": context.config.chunking.title_max_chars,
        "boundary_reason_max_chars": context.config.chunking.boundary_reason_max_chars,
        "notes_max_chars": context.config.chunking.notes_max_chars,
        "topic_max_chars": context.config.chunking.topic_max_chars,
        "max_topics": context.config.chunking.max_topics,
    }


def _ensure_supported_mode(context: StageContext) -> None:
    if context.config.chunking.mode != "llm_speech_plan":
        raise StageExecutionError(
            f"unsupported chunking.mode for stage 12: {context.config.chunking.mode}",
            error_code="unsupported_chunking_mode",
        )


def _merge_question_answer_splits(
    items: list[ChunkPlanItem],
    speech_segments: list[SpeechSegment],
    context: StageContext,
) -> list[ChunkPlanItem]:
    if len(items) < 2:
        return items

    segments_by_id = {segment.segment_id: segment for segment in speech_segments}
    positions = {segment.segment_id: index for index, segment in enumerate(speech_segments)}
    merged_items: list[ChunkPlanItem] = []
    index = 0
    while index < len(items):
        current = items[index]
        while index + 1 < len(items):
            next_item = items[index + 1]
            if not _items_split_question_answer(current, next_item, segments_by_id, positions, context):
                break
            merged = _merge_chunk_plan_items(current, next_item, segments_by_id, context)
            if merged is None:
                break
            current = merged
            index += 1
        merged_items.append(current)
        index += 1
    return [
        item.model_copy(update={"chunk_index": chunk_index})
        for chunk_index, item in enumerate(merged_items, start=1)
    ]


def _annotate_question_answer_warnings(
    items: list[ChunkPlanItem],
    speech_segments: list[SpeechSegment],
    context: StageContext,
) -> tuple[list[ChunkPlanItem], list[dict[str, Any]]]:
    warnings = _question_answer_boundary_warnings(items, speech_segments, context)
    if not warnings:
        return items, []

    notes_by_chunk_index: dict[int, list[str]] = {}
    for warning in warnings:
        note = (
            "QA split preserved because merging the question and answer chunks would exceed limits: "
            + ", ".join(warning["limit_violations"])
        )
        notes_by_chunk_index.setdefault(int(warning["left_chunk_index"]), []).append(note)
        notes_by_chunk_index.setdefault(int(warning["right_chunk_index"]), []).append(note)

    annotated: list[ChunkPlanItem] = []
    for item in items:
        notes = notes_by_chunk_index.get(item.chunk_index)
        if not notes:
            annotated.append(item)
            continue
        annotated.append(
            item.model_copy(
                update={
                    "notes": _clip_string(
                        compact_join([item.notes, *stable_unique(notes)]),
                        context.config.chunking.notes_max_chars,
                    ),
                }
            )
        )
    return annotated, warnings


def _items_split_question_answer(
    left: ChunkPlanItem,
    right: ChunkPlanItem,
    segments_by_id: dict[str, SpeechSegment],
    positions: dict[str, int],
    context: StageContext,
) -> bool:
    if not left.speech_segment_ids or not right.speech_segment_ids:
        return False
    left_last = segments_by_id.get(left.speech_segment_ids[-1])
    right_first = segments_by_id.get(right.speech_segment_ids[0])
    if left_last is None or right_first is None:
        return False
    if positions.get(right_first.segment_id) != positions.get(left_last.segment_id, -2) + 1:
        return False
    return _is_question_answer_pair(left_last, right_first, context)


def _merge_chunk_plan_items(
    left: ChunkPlanItem,
    right: ChunkPlanItem,
    segments_by_id: dict[str, SpeechSegment],
    context: StageContext,
) -> ChunkPlanItem | None:
    segment_ids = [*left.speech_segment_ids, *right.speech_segment_ids]
    limit_report = _chunk_plan_item_merge_limit_report(left, right, segments_by_id, context)
    if limit_report["limit_violations"]:
        return None
    segments = [segments_by_id[segment_id] for segment_id in segment_ids if segment_id in segments_by_id]
    notes = compact_join(
        [
            left.notes,
            right.notes,
            f"Исходные причины границ: {left.boundary_reason}; {right.boundary_reason}",
            "Склеено детерминированно: закадровый вопрос и ближайший ответ ведущего пересекли границу окна планировщика.",
        ]
    )
    return ChunkPlanItem(
        chunk_index=0,
        speech_segment_ids=segment_ids,
        title=_clip_string(left.title or right.title, context.config.chunking.title_max_chars),
        boundary_reason=_clip_string(
            "Склейка вопрос-ответ через границу окна планировщика.",
            context.config.chunking.boundary_reason_max_chars,
        ),
        topics=stable_unique([*left.topics, *right.topics]),
        notes=_clip_string(notes, context.config.chunking.notes_max_chars),
    )


def _chunk_plan_item_merge_limit_report(
    left: ChunkPlanItem,
    right: ChunkPlanItem,
    segments_by_id: dict[str, SpeechSegment],
    context: StageContext,
) -> dict[str, Any]:
    segment_ids = [*left.speech_segment_ids, *right.speech_segment_ids]
    segments = [segments_by_id[segment_id] for segment_id in segment_ids if segment_id in segments_by_id]
    merged_words = _speech_segments_word_count(segments)
    limit_violations: list[str] = []
    if len(segment_ids) > context.config.chunking.max_speech_segments_per_chunk:
        limit_violations.append(
            "max_speech_segments_per_chunk "
            f"{len(segment_ids)}>{context.config.chunking.max_speech_segments_per_chunk}"
        )
    if merged_words > context.config.chunking.max_words:
        limit_violations.append(f"max_words {merged_words}>{context.config.chunking.max_words}")
    return {
        "merged_segment_count": len(segment_ids),
        "merged_words": merged_words,
        "max_speech_segments_per_chunk": context.config.chunking.max_speech_segments_per_chunk,
        "max_words": context.config.chunking.max_words,
        "limit_violations": limit_violations,
    }


def _chunk_timeline_events(
    start: float,
    end: float,
    timeline_events: list[TimelineEvent],
    context: StageContext,
) -> list[TimelineEvent]:
    overlapping = _overlapping_timeline_events(start, end, timeline_events)
    if overlapping:
        return overlapping

    attach_seconds = context.config.chunking.visual_attach_seconds
    nearby = [
        event
        for event in timeline_events
        if _distance_to_range(event.start, event.end, start, end) <= attach_seconds
    ]
    if nearby:
        return sorted(nearby, key=lambda event: (event.start, event.end))
    return []


def _overlapping_timeline_events(start: float, end: float, timeline_events: list[TimelineEvent]) -> list[TimelineEvent]:
    return [
        event
        for event in timeline_events
        if event.end > start and event.start < end
    ]


def _distance_to_range(left_start: float, left_end: float, right_start: float, right_end: float) -> float:
    if left_end >= right_start and left_start <= right_end:
        return 0.0
    if left_end < right_start:
        return right_start - left_end
    return left_start - right_end


def _presenter_brief(events: list[TimelineEvent]) -> str:
    briefs = [
        event.presenter_context.narrative_brief
        for event in events
        if event.presenter_context.relevance in {PresenterRelevance.BRIEF, PresenterRelevance.PRIMARY_EXAMPLE}
        and event.presenter_context.narrative_brief
    ]
    return " ".join(stable_unique(briefs)).strip()


def _dialogue_text_from_segments(segments: list[SpeechSegment]) -> str:
    lines = []
    speakers_by_role: dict[SpeakerRole, set[str]] = {}
    for segment in segments:
        if segment.speaker_role and segment.speaker:
            speakers_by_role.setdefault(segment.speaker_role, set()).add(segment.speaker)

    previous_key = None
    previous_label = ""
    buffer = []
    for segment in segments:
        key = (segment.speaker_role, segment.speaker)
        label = _speaker_label(
            segment.speaker_role,
            segment.speaker,
            include_speaker=bool(
                segment.speaker_role
                and segment.speaker
                and len(speakers_by_role.get(segment.speaker_role, set())) > 1
            ),
        )
        if previous_key is not None and key != previous_key:
            lines.append(f"{previous_label}: {' '.join(buffer).strip()}")
            buffer = []
        previous_key = key
        previous_label = label
        if segment.text:
            buffer.append(segment.text)
    if previous_key is not None and buffer:
        lines.append(f"{previous_label}: {' '.join(buffer).strip()}")
    return "\n".join(line for line in lines if not line.endswith(": "))


def _speaker_label(speaker_role: SpeakerRole | None, speaker: str | None, *, include_speaker: bool = False) -> str:
    if speaker_role == SpeakerRole.HOST:
        if include_speaker and speaker:
            return f"Ведущий ({speaker})"
        return "Ведущий"
    if speaker_role == SpeakerRole.OFFSCREEN_QUESTIONER:
        if include_speaker and speaker:
            return f"Закадровый вопрос ({speaker})"
        return "Закадровый вопрос"
    return speaker or "Голос"


def _chunk_source_refs(
    video_id: str,
    start: float,
    end: float,
    events: list[TimelineEvent],
    *,
    title: str | None,
    include_visual_refs: bool,
) -> list[SourceRef]:
    refs = [youtube_source_ref(video_id, start, end, title=title, modality="audio")]
    if include_visual_refs:
        refs.extend(
            SourceRef(
                type="visual",
                url=event.timestamp_url,
                start=event.start,
                end=event.end,
                modality="visual",
            )
            for event in events
        )
    seen: set[str] = set()
    unique: list[SourceRef] = []
    for ref in refs:
        key = json.dumps(ref.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def _question_answer_boundary_errors(
    items: list[ChunkPlanItem],
    speech_segments: list[SpeechSegment],
    context: StageContext,
) -> list[str]:
    positions = {segment.segment_id: index for index, segment in enumerate(speech_segments)}
    segments_by_id = {segment.segment_id: segment for segment in speech_segments}
    errors: list[str] = []
    for left, right in zip(items, items[1:]):
        left_last = segments_by_id.get(left.speech_segment_ids[-1]) if left.speech_segment_ids else None
        right_first = segments_by_id.get(right.speech_segment_ids[0]) if right.speech_segment_ids else None
        if left_last is None or right_first is None:
            continue
        if positions[right_first.segment_id] != positions[left_last.segment_id] + 1:
            continue
        if _is_question_answer_pair(left_last, right_first, context):
            limit_report = _chunk_plan_item_merge_limit_report(left, right, segments_by_id, context)
            if limit_report["limit_violations"]:
                continue
            errors.append(
                f"offscreen question {left_last.segment_id} is split from nearby host answer {right_first.segment_id}"
            )
    return errors


def _question_answer_boundary_warnings(
    items: list[ChunkPlanItem],
    speech_segments: list[SpeechSegment],
    context: StageContext,
) -> list[dict[str, Any]]:
    positions = {segment.segment_id: index for index, segment in enumerate(speech_segments)}
    segments_by_id = {segment.segment_id: segment for segment in speech_segments}
    warnings: list[dict[str, Any]] = []
    for left, right in zip(items, items[1:]):
        left_last = segments_by_id.get(left.speech_segment_ids[-1]) if left.speech_segment_ids else None
        right_first = segments_by_id.get(right.speech_segment_ids[0]) if right.speech_segment_ids else None
        if left_last is None or right_first is None:
            continue
        if positions[right_first.segment_id] != positions[left_last.segment_id] + 1:
            continue
        if not _is_question_answer_pair(left_last, right_first, context):
            continue
        limit_report = _chunk_plan_item_merge_limit_report(left, right, segments_by_id, context)
        if not limit_report["limit_violations"]:
            continue
        warnings.append(
            {
                "code": "question_answer_split_due_to_chunk_limits",
                "severity": _PlannerWarningSeverity.WARNING.value,
                "message": (
                    f"offscreen question {left_last.segment_id} is split from nearby host answer "
                    f"{right_first.segment_id} because merging adjacent chunks would exceed chunk limits"
                ),
                "left_chunk_index": left.chunk_index,
                "right_chunk_index": right.chunk_index,
                "question_segment_id": left_last.segment_id,
                "answer_segment_id": right_first.segment_id,
                "gap_seconds": round(max(0.0, right_first.start - left_last.end), 3),
                "left_segment_ids": left.speech_segment_ids,
                "right_segment_ids": right.speech_segment_ids,
                **limit_report,
            }
        )
    return warnings


def _is_question_answer_pair(left: SpeechSegment, right: SpeechSegment, context: StageContext) -> bool:
    if left.speaker_role != SpeakerRole.OFFSCREEN_QUESTIONER or right.speaker_role != SpeakerRole.HOST:
        return False
    gap_seconds = max(0.0, right.start - left.end)
    return gap_seconds <= context.config.chunking.question_answer_merge_seconds


def _chunk_plan_item_metadata_errors(item: ChunkPlanItem, context: StageContext) -> list[str]:
    errors: list[str] = []
    if not item.title.strip():
        errors.append(f"chunk {item.chunk_index} has empty title")
    if len(item.title) > context.config.chunking.title_max_chars:
        errors.append(
            f"chunk {item.chunk_index} title exceeds "
            f"{context.config.chunking.title_max_chars} characters"
        )
    if not item.boundary_reason.strip():
        errors.append(f"chunk {item.chunk_index} has empty boundary_reason")
    if len(item.boundary_reason) > context.config.chunking.boundary_reason_max_chars:
        errors.append(
            f"chunk {item.chunk_index} boundary_reason exceeds "
            f"{context.config.chunking.boundary_reason_max_chars} characters"
        )
    if not item.topics:
        errors.append(f"chunk {item.chunk_index} has no topics")
    if len(item.topics) > context.config.chunking.max_topics:
        errors.append(f"chunk {item.chunk_index} has too many topics")
    long_topics = [topic for topic in item.topics if len(topic) > context.config.chunking.topic_max_chars]
    if long_topics:
        errors.append(
            f"chunk {item.chunk_index} has topics longer than "
            f"{context.config.chunking.topic_max_chars} characters: {long_topics}"
        )
    duplicated_topics = _duplicated_values(item.topics)
    if duplicated_topics:
        errors.append(f"chunk {item.chunk_index} has duplicated topics: {duplicated_topics}")
    if len(item.notes) > context.config.chunking.notes_max_chars:
        errors.append(
            f"chunk {item.chunk_index} notes exceeds "
            f"{context.config.chunking.notes_max_chars} characters"
        )
    return errors


def _duplicated_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicated: list[str] = []
    for value in values:
        if value in seen and value not in duplicated:
            duplicated.append(value)
        seen.add(value)
    return duplicated


def _segments_by_ids(segments: list[SpeechSegment], segment_ids: list[str]) -> list[SpeechSegment]:
    segment_map = {segment.segment_id: segment for segment in segments}
    return [segment_map[segment_id] for segment_id in segment_ids if segment_id in segment_map]


def _speech_segments_word_count(segments: list[SpeechSegment]) -> int:
    return word_count(" ".join(segment.text for segment in segments))


def _write_plan_errors(context: StageContext, errors: list[dict[str, Any]]) -> None:
    write_json_atomic(
        context.paths.chunk_plan_errors,
        {
            "schema_version": _PLAN_SCHEMA_VERSION,
            "video_id": context.job.video_id,
            "errors": errors,
        },
    )


def _warnings_artifact_matches(context: StageContext, warnings: list[dict[str, Any]]) -> bool:
    if not warnings:
        return not context.paths.chunk_plan_warnings.exists()
    if not context.paths.chunk_plan_warnings.exists():
        return False
    try:
        payload = read_json(context.paths.chunk_plan_warnings)
    except Exception:
        return False
    return (
        payload.get("schema_version") == _PLAN_SCHEMA_VERSION
        and payload.get("video_id") == context.job.video_id
        and payload.get("warnings") == warnings
    )


def _write_or_clear_plan_warnings(context: StageContext, warnings: list[dict[str, Any]]) -> None:
    if not warnings:
        if context.paths.chunk_plan_warnings.exists():
            context.paths.chunk_plan_warnings.unlink()
        return
    write_json_atomic(
        context.paths.chunk_plan_warnings,
        {
            "schema_version": _PLAN_SCHEMA_VERSION,
            "video_id": context.job.video_id,
            "stage": Stage12BuildChunks.name,
            "warnings": warnings,
        },
    )
    _log_plan_warnings(context, warnings)
    _emit_plan_warnings_progress(context, warnings)


def _abort_window_planning(
    context: StageContext,
    *,
    futures: dict[Future[_WindowPlanResult], _PlannerWindow],
    failed_window: _PlannerWindow,
    error: BaseException,
    validation_errors: list[dict[str, Any]],
    completed_window_indexes: list[int],
) -> None:
    if validation_errors:
        _write_plan_errors(context, validation_errors)

    cancelled_windows: list[_PlannerWindow] = []
    in_flight_map: dict[Future[_WindowPlanResult], _PlannerWindow] = {}
    for pending_future, pending_window in list(futures.items()):
        if pending_future.cancel():
            cancelled_windows.append(pending_window)
        else:
            in_flight_map[pending_future] = pending_window
    futures.clear()

    _log_window_planning_abort(
        context,
        failed_window=failed_window,
        error=error,
        validation_errors=validation_errors,
        completed_window_indexes=completed_window_indexes,
        cancelled_windows=cancelled_windows,
        in_flight_windows=list(in_flight_map.values()),
    )
    _emit_window_planning_abort_progress(
        context,
        failed_window=failed_window,
        completed_count=len(completed_window_indexes),
        cancelled_count=len(cancelled_windows),
        in_flight_count=len(in_flight_map),
    )

    if in_flight_map:
        wait(in_flight_map)
        _log_window_in_flight_finalization(context, in_flight_map)


def _log_window_planning_abort(
    context: StageContext,
    *,
    failed_window: _PlannerWindow,
    error: BaseException,
    validation_errors: list[dict[str, Any]],
    completed_window_indexes: list[int],
    cancelled_windows: list[_PlannerWindow],
    in_flight_windows: list[_PlannerWindow],
) -> None:
    lines = [
        "",
        "[chunk-plan-concurrent-abort]",
        f"run_id: {context.run_id or '-'}",
        f"failed_window_index: {failed_window.window_index}",
        f"error: {type(error).__name__}: {error}",
        f"validation_errors_count: {len(validation_errors)}",
        f"completed_windows_count: {len(completed_window_indexes)}",
        "completed_window_indexes:",
        *[f"  - {window_index}" for window_index in completed_window_indexes[:20]],
        *([f"  - ... {len(completed_window_indexes) - 20} more"] if len(completed_window_indexes) > 20 else []),
        f"cancelled_count: {len(cancelled_windows)}",
        "cancelled_windows:",
        *[f"  - {window.window_index}" for window in cancelled_windows[:20]],
        *([f"  - ... {len(cancelled_windows) - 20} more"] if len(cancelled_windows) > 20 else []),
        f"in_flight_count: {len(in_flight_windows)}",
        "in_flight_windows:",
        *[f"  - {window.window_index}" for window in in_flight_windows[:20]],
        *([f"  - ... {len(in_flight_windows) - 20} more"] if len(in_flight_windows) > 20 else []),
        "",
    ]
    append_text(context.paths.stage_log(Stage12BuildChunks.name), "\n".join(lines), encoding="utf-8")


def _log_stale_chunk_plan_raw_removal(
    context: StageContext,
    removed_paths: list[Path],
    *,
    windows_count: int,
) -> None:
    lines = [
        "",
        "[chunk-plan-stale-raw-cleanup]",
        f"run_id: {context.run_id or '-'}",
        f"windows_count: {windows_count}",
        f"removed_count: {len(removed_paths)}",
        "removed_files:",
        *[f"  - {path}" for path in removed_paths],
        "",
    ]
    append_text(context.paths.stage_log(Stage12BuildChunks.name), "\n".join(lines), encoding="utf-8")


def _log_window_in_flight_finalization(
    context: StageContext,
    in_flight_map: dict[Future[_WindowPlanResult], _PlannerWindow],
) -> None:
    lines = [
        "",
        "[chunk-plan-in-flight-finalization]",
        f"run_id: {context.run_id or '-'}",
        f"in_flight_count: {len(in_flight_map)}",
        "in_flight_windows:",
    ]
    for index, (future, window) in enumerate(sorted(in_flight_map.items(), key=lambda item: item[1].window_index)):
        if index >= 20:
            lines.append(f"  - ... {len(in_flight_map) - 20} more")
            break
        if future.cancelled():
            lines.append(f"  - {window.window_index} status=cancelled_after_abort")
            continue
        future_error = future.exception()
        if future_error is None:
            result = future.result()
            diagnostics = result.diagnostics
            lines.append(f"  - {window.window_index} status=completed_after_abort")
            lines.append(f"    request_id: {diagnostics.request_id if diagnostics is not None else '-'}")
            lines.append(f"    response_id: {diagnostics.response_id if diagnostics is not None else '-'}")
            lines.append(f"    started_at: {diagnostics.started_at if diagnostics is not None else '-'}")
            lines.append(f"    finished_at: {diagnostics.finished_at if diagnostics is not None else '-'}")
            lines.append(f"    duration_seconds: {_format_seconds(diagnostics.duration_seconds if diagnostics is not None else None)}")
            lines.append(f"    raw_output: {diagnostics.raw_output_path if diagnostics is not None else '-'}")
        else:
            lines.append(
                f"  - {window.window_index} status=failed_after_abort "
                f"error={type(future_error).__name__}: {future_error}"
            )
            lines.append(f"    request_id: {request_id_from_error(future_error) or '-'}")
    lines.append("")
    append_text(context.paths.stage_log(Stage12BuildChunks.name), "\n".join(lines), encoding="utf-8")


def _emit_window_planning_abort_progress(
    context: StageContext,
    *,
    failed_window: _PlannerWindow,
    completed_count: int,
    cancelled_count: int,
    in_flight_count: int,
) -> None:
    if context.progress_callback is None:
        return
    context.progress_callback(
        f"[12 {Stage12BuildChunks.name}] aborting window planning after "
        f"window={failed_window.window_index} failed; completed={completed_count} "
        f"cancelled={cancelled_count} in_flight={in_flight_count}"
    )


def _log_plan_warnings(context: StageContext, warnings: list[dict[str, Any]]) -> None:
    lines = [
        "",
        "[chunk-plan-warnings]",
        f"run_id: {context.run_id or '-'}",
        f"warnings_count: {len(warnings)}",
        "warnings:",
        *[
            f"  - {warning['code']}: {warning['message']} "
            f"({', '.join(warning['limit_violations'])})"
            for warning in warnings
        ],
        "",
    ]
    append_text(context.paths.stage_log(Stage12BuildChunks.name), "\n".join(lines), encoding="utf-8")


def _emit_plan_warnings_progress(context: StageContext, warnings: list[dict[str, Any]]) -> None:
    if context.progress_callback is None:
        return
    context.progress_callback(
        f"[12 {Stage12BuildChunks.name}] warnings={len(warnings)} "
        "question-answer splits preserved due to chunk limits"
    )


def _log_plan_attempt(
    context: StageContext,
    *,
    window: _PlannerWindow,
    attempt: int,
    items: list[ChunkPlanItem],
    validation_errors: list[str],
    analysis: ChunkPlanAnalysisResult | None = None,
    structured_errors: list[dict[str, Any]] | None = None,
) -> None:
    structured_errors = structured_errors or []
    diagnostics = analysis.diagnostics if analysis is not None else None
    lines = [
        "",
        "[chunk-plan-attempt]",
        f"run_id: {context.run_id or '-'}",
        f"window_index: {window.window_index}",
        f"attempt: {attempt}",
        f"planned_chunks_count: {len(items)}",
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
    append_text(context.paths.stage_log(Stage12BuildChunks.name), "\n".join(lines), encoding="utf-8")


def _log_chunk_plan_retry_advisor_success(
    context: StageContext,
    *,
    window: _PlannerWindow,
    raw_output_path: Path,
    advisor_payload: dict[str, Any],
) -> None:
    lines = [
        "",
        "[chunk-plan-retry-advisor]",
        f"run_id: {context.run_id or '-'}",
        f"window_index: {window.window_index}",
        f"raw_output_path: {raw_output_path}",
        f"error_summary: {advisor_payload.get('error_summary') or ''}",
        f"repair_instruction: {advisor_payload.get('repair_instruction') or ''}",
    ]
    hard_rules = advisor_payload.get("hard_rules")
    if isinstance(hard_rules, list):
        lines.append("hard_rules:")
        lines.extend(f"  - {rule}" for rule in hard_rules[:10])
    lines.append("")
    append_text(context.paths.stage_log(Stage12BuildChunks.name), "\n".join(lines), encoding="utf-8")


def _log_chunk_plan_retry_advisor_failure(
    context: StageContext,
    *,
    window: _PlannerWindow,
    raw_output_path: Path,
    error: ProviderError,
) -> None:
    lines = [
        "",
        "[chunk-plan-retry-advisor-failed]",
        f"run_id: {context.run_id or '-'}",
        f"window_index: {window.window_index}",
        f"raw_output_path: {raw_output_path}",
        f"error_code: {error.error_code}",
        f"error: {error}",
        "",
    ]
    append_text(context.paths.stage_log(Stage12BuildChunks.name), "\n".join(lines), encoding="utf-8")


def _log_window_cache(
    context: StageContext,
    *,
    window: _PlannerWindow,
    cache_path: Path,
    cache_hit: bool,
    validation_errors: list[str],
    analysis: ChunkPlanAnalysisResult | None = None,
    structured_errors: list[dict[str, Any]] | None = None,
) -> None:
    structured_errors = structured_errors or []
    diagnostics = analysis.diagnostics if analysis is not None else None
    lines = [
        "",
        "[chunk-plan-cache]",
        f"run_id: {context.run_id or '-'}",
        f"window_index: {window.window_index}",
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
    append_text(context.paths.stage_log(Stage12BuildChunks.name), "\n".join(lines), encoding="utf-8")


def _chunk_retry_logger(context: StageContext) -> OnRetry:
    def _log_retry(attempt: int, delay_seconds: float, error: BaseException) -> None:
        log_openai_retry(
            context.paths.stage_log(Stage12BuildChunks.name),
            attempt=attempt,
            delay_seconds=delay_seconds,
            error=error,
            context_lines=[f"run_id: {context.run_id or '-'}", "operation: chunk_plan"],
        )

    return _log_retry


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def _prompt_path(context: StageContext) -> Path:
    return Path(__file__).resolve().parents[1] / "prompts" / context.config.chunking.prompt_file


def _retry_advisor_prompt_path(context: StageContext) -> Path:
    return Path(__file__).resolve().parents[1] / "prompts" / context.config.chunking.retry_advisor_prompt_file


def _compact_string(value: object) -> str:
    return " ".join(str(value or "").split())


def _clip_string(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    clipped = value[:max_chars].rstrip()
    if " " not in clipped:
        return clipped
    word_boundary = clipped.rsplit(" ", 1)[0].rstrip()
    return word_boundary or clipped


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(payload)


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


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

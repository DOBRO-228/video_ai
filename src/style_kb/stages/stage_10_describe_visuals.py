from __future__ import annotations

import json
import os
import re
import hashlib
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from time import perf_counter

from style_kb.clients._retry import OnRetry
from style_kb.clients.provider_diagnostics import ProviderCallDiagnostics, ProviderName
from style_kb.clients.gemini_vision import GeminiVisionClient, load_cached_gemini_visual_result
from style_kb.clients.openai_vision import OpenAIVisionClient, load_cached_visual_result as load_cached_openai_visual_result
from style_kb.clients.vision import PRESENTER_PROFILE_SCHEMA, VISUAL_RESPONSE_SCHEMA, VisionAnalysisResult
from style_kb.diagnostics import PipelineEvent
from style_kb.errors import StageExecutionError
from style_kb.models import (
    ConfidenceLevel,
    FrameRef,
    PresenterContext,
    PresenterProfile,
    PresenterRelevance,
    Scene,
    SpeechSegment,
    VisualEvent,
)
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import (
    load_frame_refs,
    load_scenes,
    load_speech_segments,
    load_visual_events,
    log_openai_retry,
    emit_stage_validation_failed,
    emit_provider_event,
    provider_error_extra,
    ProviderOperation,
    request_id_from_error,
    youtube_source_ref,
)
from style_kb.stages.diagnostics import validation_preview
from style_kb.utils.collections import stable_unique
from style_kb.utils.files import append_text, read_json, write_json_atomic
from style_kb.utils.ids import visual_event_id
from style_kb.utils.pydantic_io import read_model, write_model, write_models_jsonl
from style_kb.utils.time import build_timestamp_url

_SCENE_CONTENT_MAX_ATTEMPTS = 2
_CONTENT_VALIDATION_METADATA_KEY = "_style_kb_content_validation"
_CONTENT_VALIDATION_ACCEPTED_WITH_WARNING = "accepted_with_warning"
_REQUEST_METADATA_KEY = "_style_kb_request"
_BASELINE_LEAKAGE_MARKER_THRESHOLD = 1
_BASELINE_LEAKAGE_FIELDS = (
    "visual_summary",
    "observations",
    "interpretations",
    "items",
    "style_topics",
    "notes",
)
_GENERIC_BASELINE_MARKERS = frozenset(
    {
        "образ",
        "общий вид",
        "деловой стиль",
        "формальный стиль",
        "классический стиль",
        "формальный деловой стиль",
    }
)
_TECHNICAL_VISUAL_PATTERNS = (
    r"\bобразовательн\w*\s+формат\w*",
    r"\bформат\w*\s+(?:видео|контент\w*|подач\w*)",
    r"\bвизуальн\w*\s+подач\w*",
    r"\bподач\w*\s+на\s+экран\w*",
    r"\b(?:крупн\w*|средн\w*|общ\w*)\s+план\w*\b",
    r"\b(?:ракурс\w*|кадр\w*|камер\w*|съемк\w*|съёмк\w*|монтаж\w*)\b",
    r"\b(?:заставк\w*|оверле\w*|overlay|overlays|on\s+screen|screen)\b",
    r"\b(?:экран\w*|текстов\w*\s+вставк\w*|надпис\w*|слайд\w*|slide|slides)\b",
    r"\b(?:visual\s+aids?|instructional\s+aids?|presentation|presentational|formal\s+presentation)\b",
    r"\b(?:фон\w*|background|интерьер\w*|книжн\w*\s+полк\w*|полк\w*|стол\w*|камин\w*)\b",
    r"\b(?:микрофон\w*|петличк\w*|lapel\s+mic|microphone)\b",
)
_BARE_COLOR_LABELS = frozenset(
    {
        "белый",
        "белая",
        "белое",
        "белые",
        "черный",
        "черная",
        "черное",
        "черные",
        "чёрный",
        "чёрная",
        "чёрное",
        "чёрные",
        "серый",
        "серая",
        "серое",
        "серые",
        "зеленый",
        "зеленая",
        "зеленое",
        "зеленые",
        "зелёный",
        "зелёная",
        "зелёное",
        "зелёные",
        "синий",
        "синяя",
        "синее",
        "синие",
        "красный",
        "красная",
        "красное",
        "красные",
        "коричневый",
        "коричневая",
        "коричневое",
        "коричневые",
        "бежевый",
        "бежевая",
        "бежевое",
        "бежевые",
        "оранжевый",
        "оранжевая",
        "оранжевое",
        "оранжевые",
        "желтый",
        "желтая",
        "желтое",
        "желтые",
        "жёлтый",
        "жёлтая",
        "жёлтое",
        "жёлтые",
        "white",
        "black",
        "gray",
        "grey",
        "green",
        "red",
        "blue",
        "brown",
        "beige",
        "orange",
        "yellow",
        "mustard",
    }
)


class _VisualListField(StrEnum):
    ITEMS = "items"
    STYLE_TOPICS = "style_topics"


class _SceneValidationStatus(StrEnum):
    CACHE_INVALID = "cache-invalid"
    RETRY = "retry"
    ACCEPTED_WITH_WARNING = "accepted-with-warning"


class _SceneProgressStatus(StrEnum):
    CACHED = "cached"
    CACHED_WITH_WARNING = "cached-with-warning"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNING = "completed-with-warning"


@dataclass(slots=True)
class _SceneTask:
    order: int
    scene: Scene
    frames: list[FrameRef]
    image_paths: list[Path]
    transcript_context: dict[str, object]
    transcript_words: int
    raw_output_path: Path


@dataclass(slots=True)
class _SceneResult:
    task: _SceneTask
    analysis: VisionAnalysisResult
    cached: bool
    wall_seconds: float
    raw_output_paths: list[Path]
    technical_leakage: _VisualContentLeakage | None = None


@dataclass(slots=True)
class _VisualContentLeakage:
    reason: str
    error_code: str
    markers: list[str]
    fields: dict[str, list[str]]
    structured_errors: list[dict[str, object]]


def empty_baseline_leakage_metrics() -> dict[str, int]:
    return {
        "baseline_leakage_scenes_count": 0,
        "baseline_leakage_markers_total": 0,
        "baseline_leakage_unique_markers_count": 0,
        "baseline_leakage_fields_count": 0,
        "baseline_leakage_structured_errors_count": 0,
        "baseline_leakage_visual_summary_count": 0,
        "baseline_leakage_observations_count": 0,
        "baseline_leakage_interpretations_count": 0,
        "baseline_leakage_items_count": 0,
        "baseline_leakage_style_topics_count": 0,
        "baseline_leakage_notes_count": 0,
    }


def empty_technical_leakage_metrics() -> dict[str, int]:
    return {
        "technical_leakage_scenes_count": 0,
        "technical_leakage_markers_total": 0,
        "technical_leakage_unique_markers_count": 0,
        "technical_leakage_fields_count": 0,
        "technical_leakage_structured_errors_count": 0,
        "technical_leakage_visual_summary_count": 0,
        "technical_leakage_observations_count": 0,
        "technical_leakage_interpretations_count": 0,
        "technical_leakage_items_count": 0,
        "technical_leakage_style_topics_count": 0,
        "technical_leakage_notes_count": 0,
    }


class Stage10DescribeVisuals(Stage):
    name = "10_describe_visuals"
    ordinal = 10

    def input_files(self, context: StageContext) -> list:
        files = [
            context.paths.frame_refs_jsonl,
            context.paths.scenes_jsonl,
            context.paths.stt_speech_segments,
            _prompt_path(context),
        ]
        if context.config.vision.presenter_bootstrap_enabled:
            files.append(_presenter_prompt_path(context))
        return files

    def output_files(self, context: StageContext) -> list:
        return [context.paths.visual_events_jsonl, context.paths.visual_presenter_profile]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.visual_events_jsonl.exists() or not context.paths.visual_presenter_profile.exists():
            return False
        try:
            scenes = load_scenes(context.paths.scenes_jsonl) if context.paths.scenes_jsonl.exists() else []
            visual_events = load_visual_events(context.paths.visual_events_jsonl)
        except Exception:
            return False
        if not visual_events or (scenes and len(visual_events) != len(scenes)):
            return False
        return True

    def run(self, context: StageContext) -> StageResult:
        prompt_text = _prompt_path(context).read_text(encoding="utf-8")
        scenes = load_scenes(context.paths.scenes_jsonl)
        frame_refs = load_frame_refs(context.paths.frame_refs_jsonl)
        speech_segments = load_speech_segments(context.paths.stt_speech_segments)
        frame_map = _frame_map(frame_refs)
        presenter_profile = _load_or_build_presenter_profile(context, scenes=scenes, frame_map=frame_map)
        effective_profile = _effective_presenter_profile(context, presenter_profile)
        scene_prompt = _scene_prompt(prompt_text, effective_profile)

        tasks = [
            _SceneTask(
                order=index,
                scene=scene,
                frames=frame_map.get(scene.scene_id, []),
                image_paths=[context.paths.job_dir / frame.path for frame in frame_map.get(scene.scene_id, [])],
                transcript_context=transcript_context,
                transcript_words=_transcript_context_word_count(transcript_context),
                raw_output_path=context.paths.visual_raw_scene(scene.scene_id),
            )
            for index, scene in enumerate(scenes, start=1)
            for transcript_context in [_scene_transcript_context(scene.start, scene.end, speech_segments, context)]
        ]
        newest_input_mtime = _newest_input_mtime(_scene_cache_input_files(context))
        total_scenes = len(tasks)
        visual_events: dict[int, VisualEvent] = {}
        output_files = [context.paths.visual_events_jsonl, context.paths.visual_presenter_profile]
        if context.paths.visual_raw_presenter_profile.exists():
            output_files.append(context.paths.visual_raw_presenter_profile)
        cached_count = 0
        api_count = 0
        total_input_tokens = 0
        total_output_tokens = 0
        total_reasoning_tokens = 0
        total_tokens = 0
        completed = 0
        pending_tasks: list[_SceneTask] = []

        for task in tasks:
            cached_result = _load_cached_result(
                context,
                task,
                newest_input_mtime,
            )
            if cached_result is None:
                pending_tasks.append(task)
                continue
            output_files.extend(cached_result.raw_output_paths)
            visual_events[task.order] = _build_visual_event(context, task, cached_result.analysis)
            completed += 1
            cached_count += 1
            total_input_tokens += cached_result.analysis.usage["input_tokens"]
            total_output_tokens += cached_result.analysis.usage["output_tokens"]
            total_reasoning_tokens += cached_result.analysis.usage["reasoning_tokens"]
            total_tokens += cached_result.analysis.usage["total_tokens"]
            _log_scene_result(context, cached_result, completed=completed, total_scenes=total_scenes)
            _emit_scene_progress(context, cached_result, completed=completed, total_scenes=total_scenes)

        if pending_tasks:
            max_workers = max(1, context.config.vision.batch_size)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(
                        _request_scene_analysis,
                        context=context,
                        task=task,
                        system_prompt=scene_prompt,
                        on_retry=_scene_retry_logger(context, task),
                    ): task
                    for task in pending_tasks
                }
                while future_map:
                    done, _ = wait(future_map, return_when=FIRST_COMPLETED)
                    for future in done:
                        task = future_map.pop(future)
                        error = future.exception()
                        if error is not None:
                            cancelled_tasks: list[_SceneTask] = []
                            in_flight_map: dict[Future, _SceneTask] = {}
                            for pending_future, pending_task in list(future_map.items()):
                                if pending_future.cancel():
                                    cancelled_tasks.append(pending_task)
                                else:
                                    in_flight_map[pending_future] = pending_task
                            future_map.clear()
                            _log_concurrent_abort(
                                context,
                                error=error,
                                completed=completed,
                                total_scenes=total_scenes,
                                cancelled_tasks=cancelled_tasks,
                                in_flight_tasks=list(in_flight_map.values()),
                            )
                            _emit_concurrent_abort_progress(
                                context,
                                completed=completed,
                                total_scenes=total_scenes,
                                cancelled_count=len(cancelled_tasks),
                                in_flight_count=len(in_flight_map),
                            )
                            if in_flight_map:
                                wait(in_flight_map)
                                _log_in_flight_finalization(context, in_flight_map)
                            raise error
                        result = future.result()
                        output_files.extend(result.raw_output_paths)
                        visual_events[task.order] = _build_visual_event(context, task, result.analysis)
                        completed += 1
                        api_count += 1
                        total_input_tokens += result.analysis.usage["input_tokens"]
                        total_output_tokens += result.analysis.usage["output_tokens"]
                        total_reasoning_tokens += result.analysis.usage["reasoning_tokens"]
                        total_tokens += result.analysis.usage["total_tokens"]
                        _log_scene_result(context, result, completed=completed, total_scenes=total_scenes)
                        _emit_scene_progress(context, result, completed=completed, total_scenes=total_scenes)

        ordered_visual_events = [visual_events[index] for index in sorted(visual_events)]
        write_models_jsonl(context.paths.visual_events_jsonl, ordered_visual_events)
        presenter_counts = _presenter_relevance_counts(ordered_visual_events)
        baseline_leakage_metrics = baseline_leakage_metrics_for_events(ordered_visual_events, effective_profile)
        technical_leakage_metrics = technical_leakage_metrics_for_events(ordered_visual_events)
        _log_baseline_leakage_summary(context, baseline_leakage_metrics)
        _log_technical_leakage_summary(context, technical_leakage_metrics)
        return StageResult(
            output_files=_dedupe_paths(output_files),
            metrics={
                "visual_events_count": len(ordered_visual_events),
                "cached_scenes_count": cached_count,
                "api_scenes_count": api_count,
                "input_tokens_total": total_input_tokens,
                "output_tokens_total": total_output_tokens,
                "reasoning_tokens_total": total_reasoning_tokens,
                "total_tokens_total": total_tokens,
                "presenter_profile_detected": presenter_profile.has_primary_presenter,
                "presenter_profile_confidence": presenter_profile.confidence,
                "presenter_background_scenes_count": presenter_counts[PresenterRelevance.BACKGROUND.value],
                "presenter_brief_scenes_count": presenter_counts[PresenterRelevance.BRIEF.value],
                "presenter_primary_example_scenes_count": presenter_counts[PresenterRelevance.PRIMARY_EXAMPLE.value],
                **baseline_leakage_metrics,
                **technical_leakage_metrics,
            },
        )


def _frame_map(frame_refs: list[FrameRef]) -> dict[str, list[FrameRef]]:
    frame_map: dict[str, list[FrameRef]] = defaultdict(list)
    for frame in frame_refs:
        frame_map[frame.scene_id].append(frame)
    for frames in frame_map.values():
        frames.sort(key=lambda frame: frame.timestamp)
    return frame_map


def _load_or_build_presenter_profile(
    context: StageContext,
    *,
    scenes: list[Scene],
    frame_map: dict[str, list[FrameRef]],
) -> PresenterProfile:
    if not context.config.vision.presenter_bootstrap_enabled:
        profile = PresenterProfile(
            has_primary_presenter=False,
            confidence=ConfidenceLevel.LOW,
            baseline_summary="",
            recurring_visual_markers=[],
            notes="Presenter bootstrap is disabled in config.",
        )
        write_model(context.paths.visual_presenter_profile, profile)
        return profile

    prompt_path = _presenter_prompt_path(context)
    image_paths = _presenter_bootstrap_image_paths(context, scenes=scenes, frame_map=frame_map)
    if not image_paths:
        raise StageExecutionError("presenter bootstrap has no representative images", error_code="presenter_bootstrap_no_images")

    newest_input = _newest_input_mtime([prompt_path, context.paths.frame_refs_jsonl, context.paths.scenes_jsonl, *image_paths])
    request_metadata = _presenter_request_metadata(context)
    if context.paths.visual_presenter_profile.exists() and context.paths.visual_raw_presenter_profile.exists():
        cached = read_model(context.paths.visual_presenter_profile, PresenterProfile)
        if (
            context.paths.visual_presenter_profile.stat().st_mtime >= newest_input
            and _raw_request_metadata_matches(context.paths.visual_raw_presenter_profile, request_metadata)
        ):
            return cached

    provider = _vision_provider(context)
    client = _build_vision_client(context, on_retry=_presenter_retry_logger(context))
    emit_provider_event(
        context,
        PipelineEvent.PROVIDER_REQUEST_STARTED,
        stage_name=Stage10DescribeVisuals.name,
        ordinal=Stage10DescribeVisuals.ordinal,
        operation=ProviderOperation.VISION_PRESENTER_PROFILE,
        diagnostics=ProviderCallDiagnostics(
            provider=provider,
            model=context.config.vision.model,
            raw_output_path=str(context.paths.visual_raw_presenter_profile),
        ),
        message="presenter profile request started",
        extra={"images_count": len(image_paths)},
    )
    try:
        analysis = client.build_presenter_profile(
            system_prompt=prompt_path.read_text(encoding="utf-8"),
            image_paths=image_paths,
            detail=context.config.vision.detail,
            raw_output_path=context.paths.visual_raw_presenter_profile,
        )
    except Exception as error:
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_FAILED,
            stage_name=Stage10DescribeVisuals.name,
            ordinal=Stage10DescribeVisuals.ordinal,
            operation=ProviderOperation.VISION_PRESENTER_PROFILE,
            diagnostics=ProviderCallDiagnostics(
                provider=provider,
                model=context.config.vision.model,
                raw_output_path=str(context.paths.visual_raw_presenter_profile),
                request_id=request_id_from_error(error),
            ),
            message="presenter profile request failed",
            extra={**provider_error_extra(error), "images_count": len(image_paths)},
        )
        raise
    emit_provider_event(
        context,
        PipelineEvent.PROVIDER_REQUEST_COMPLETED,
        stage_name=Stage10DescribeVisuals.name,
        ordinal=Stage10DescribeVisuals.ordinal,
        operation=ProviderOperation.VISION_PRESENTER_PROFILE,
        diagnostics=analysis.diagnostics,
        message="presenter profile request completed",
        extra={"images_count": len(image_paths)},
    )
    profile = PresenterProfile.model_validate(analysis.payload)
    _write_raw_request_metadata(context.paths.visual_raw_presenter_profile, request_metadata)
    write_model(context.paths.visual_presenter_profile, profile)
    _log_presenter_profile(context, profile=profile, analysis=analysis, image_paths=image_paths)
    return profile


def _effective_presenter_profile(context: StageContext, profile: PresenterProfile) -> PresenterProfile:
    if (
        context.config.vision.presenter_low_confidence_disables_recurrence
        and profile.has_primary_presenter
        and profile.confidence == ConfidenceLevel.LOW
    ):
        return PresenterProfile(
            has_primary_presenter=False,
            confidence=profile.confidence,
            baseline_summary="",
            recurring_visual_markers=[],
            notes=f"Recurrence disabled because bootstrap confidence is low. Original notes: {profile.notes}",
        )
    return profile


def _presenter_bootstrap_image_paths(
    context: StageContext,
    *,
    scenes: list[Scene],
    frame_map: dict[str, list[FrameRef]],
) -> list[Path]:
    limit = min(context.config.vision.presenter_bootstrap_scene_limit, len(scenes))
    max_images = context.config.vision.presenter_bootstrap_max_images
    selected_scenes = _presenter_bootstrap_scenes(scenes, limit=limit)
    image_paths: list[Path] = []
    for scene in selected_scenes:
        frames = frame_map.get(scene.scene_id) or []
        if not frames:
            continue
        middle_frame = frames[len(frames) // 2]
        image_paths.append(context.paths.job_dir / middle_frame.path)
        if len(image_paths) >= max_images:
            break
    return image_paths


def _presenter_bootstrap_scenes(scenes: list[Scene], *, limit: int) -> list[Scene]:
    if limit <= 0 or not scenes:
        return []
    first_half_count = max(1, len(scenes) // 2)
    first_half = scenes[:first_half_count]
    early_count = min(3, limit, len(first_half))
    selected_indices = list(range(early_count))
    remaining = limit - len(selected_indices)
    candidates = list(range(early_count, len(first_half)))
    if remaining > 0 and candidates:
        if remaining >= len(candidates):
            selected_indices.extend(candidates[:remaining])
        else:
            step = (len(candidates) - 1) / max(remaining - 1, 1)
            selected_indices.extend(candidates[round(index * step)] for index in range(remaining))
    return [first_half[index] for index in sorted(set(selected_indices))[:limit]]


def _scene_prompt(prompt_text: str, profile: PresenterProfile) -> str:
    presenter_profile_json = json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
    return "\n\n".join(
        [
            prompt_text.strip(),
            "Профиль повторяющегося ведущего для этого видео:",
            presenter_profile_json,
            "Если has_primary_presenter=true, используй этот профиль только для распознавания повторяющегося baseline. Не копируй baseline в основные поля сцены при любом relevance, включая primary_example. Основные поля должны содержать только scene-specific визуальную информацию о мужском стиле: новые примеры, демонстрации деталей одежды, смену одежды, новые предметы гардероба или реальные отличия от baseline.",
        ]
    )


def _scene_transcript_context(start: float, end: float, speech_segments: list[SpeechSegment], context: StageContext) -> dict[str, object]:
    before = context.config.vision.transcript_context_before_seconds
    after = context.config.vision.transcript_context_after_seconds
    if not context.config.vision.include_nearby_transcript:
        return {
            "scene_time": {"start": round(start, 3), "end": round(end, 3)},
            "rules": _transcript_context_rules(),
            "previous_context": _transcript_window_payload(window_start=start, window_end=start, segments=[]),
            "current_scene_context": _transcript_window_payload(window_start=start, window_end=end, segments=[]),
            "next_context": _transcript_window_payload(window_start=end, window_end=end, segments=[]),
        }
    lower = max(0.0, start - before)
    upper = end + after
    current_segments = _overlapping_segments(speech_segments, start, end)
    current_ids = {segment.segment_id for segment in current_segments}
    previous_segments = (
        [
            segment
            for segment in _overlapping_segments(speech_segments, lower, start)
            if segment.segment_id not in current_ids
        ]
        if before > 0
        else []
    )
    next_segments = (
        [
            segment
            for segment in _overlapping_segments(speech_segments, end, upper)
            if segment.segment_id not in current_ids
        ]
        if after > 0
        else []
    )
    return {
        "scene_time": {"start": round(start, 3), "end": round(end, 3)},
        "rules": _transcript_context_rules(),
        "previous_context": _transcript_window_payload(
            window_start=lower,
            window_end=start,
            segments=previous_segments,
        ),
        "current_scene_context": _transcript_window_payload(
            window_start=start,
            window_end=end,
            segments=current_segments,
        ),
        "next_context": _transcript_window_payload(
            window_start=end,
            window_end=upper,
            segments=next_segments,
        ),
    }


def _transcript_context_rules() -> dict[str, str]:
    return {
        "visual_evidence_source": "current_scene_frames_only",
        "transcript_role": "context_only_not_visual_evidence",
        "previous_context_role": "boundary_orientation_only",
        "next_context_role": "boundary_orientation_only_do_not_describe_as_current_scene",
    }


def _overlapping_segments(speech_segments: list[SpeechSegment], start: float, end: float) -> list[SpeechSegment]:
    if end <= start:
        return []
    return [segment for segment in speech_segments if segment.start < end and segment.end > start]


def _transcript_window_payload(
    *,
    window_start: float,
    window_end: float,
    segments: list[SpeechSegment],
) -> dict[str, object]:
    text = "\n".join(segment.text for segment in segments if segment.text.strip())
    return {
        "window_start": round(window_start, 3),
        "window_end": round(window_end, 3),
        "segment_count": len(segments),
        "word_count": len(text.split()),
        "text": text,
    }


def _transcript_context_word_count(transcript_context: dict[str, object]) -> int:
    total = 0
    for key in ("previous_context", "current_scene_context", "next_context"):
        value = transcript_context.get(key)
        if isinstance(value, dict):
            total += int(value.get("word_count") or 0)
    return total


def _prompt_path(context: StageContext) -> Path:
    return Path(__file__).resolve().parents[1] / "prompts" / context.config.vision.prompt_file


def _scene_cache_input_files(context: StageContext) -> list[Path]:
    files = Stage10DescribeVisuals().input_files(context)
    if context.paths.visual_presenter_profile.exists():
        files.append(context.paths.visual_presenter_profile)
    return files


def _presenter_prompt_path(context: StageContext) -> Path:
    return Path(__file__).resolve().parents[1] / "prompts" / context.config.vision.presenter_bootstrap_prompt_file


def _newest_input_mtime(paths: list[Path]) -> float:
    existing = [path.stat().st_mtime for path in paths if path.exists()]
    return max(existing) if existing else 0.0


def _vision_provider(context: StageContext) -> ProviderName:
    try:
        provider = ProviderName(context.config.vision.provider)
    except ValueError as error:
        raise StageExecutionError(
            f"Unsupported vision provider: {context.config.vision.provider}",
            error_code="unsupported_vision_provider",
        ) from error
    if provider not in {ProviderName.OPENAI, ProviderName.GEMINI}:
        raise StageExecutionError(
            f"Unsupported vision provider: {provider.value}",
            error_code="unsupported_vision_provider",
        )
    return provider


def _build_vision_client(context: StageContext, *, on_retry: OnRetry | None = None):
    provider = _vision_provider(context)
    if provider == ProviderName.OPENAI:
        return OpenAIVisionClient(
            os.environ.get("OPENAI_API_KEY"),
            model=context.config.vision.model,
            on_retry=on_retry,
        )
    if provider == ProviderName.GEMINI:
        return GeminiVisionClient(
            os.environ.get("GEMINI_API_KEY"),
            model=context.config.vision.model,
            media_resolution=context.config.vision.media_resolution,
            thinking_level=context.config.vision.thinking_level,
            on_retry=on_retry,
        )
    raise AssertionError(f"unhandled vision provider: {provider.value}")


def _load_cached_visual_result(context: StageContext, raw_output_path: Path) -> VisionAnalysisResult:
    provider = _vision_provider(context)
    if provider == ProviderName.OPENAI:
        return load_cached_openai_visual_result(raw_output_path)
    if provider == ProviderName.GEMINI:
        return load_cached_gemini_visual_result(raw_output_path)
    raise AssertionError(f"unhandled vision provider: {provider.value}")


def _presenter_request_metadata(context: StageContext) -> dict[str, object]:
    return {
        **_vision_settings_metadata(context),
        "operation": ProviderOperation.VISION_PRESENTER_PROFILE.value,
        "prompt_sha256": _file_sha256(_presenter_prompt_path(context)),
        "schema_sha256": _json_sha256(PRESENTER_PROFILE_SCHEMA),
        "presenter_bootstrap_scene_limit": context.config.vision.presenter_bootstrap_scene_limit,
        "presenter_bootstrap_max_images": context.config.vision.presenter_bootstrap_max_images,
    }


def _scene_request_metadata(context: StageContext) -> dict[str, object]:
    return {
        **_vision_settings_metadata(context),
        "operation": ProviderOperation.VISION_SCENE.value,
        "prompt_sha256": _file_sha256(_prompt_path(context)),
        "presenter_profile_sha256": _file_sha256(context.paths.visual_presenter_profile),
        "schema_sha256": _json_sha256(VISUAL_RESPONSE_SCHEMA),
        "include_nearby_transcript": context.config.vision.include_nearby_transcript,
        "transcript_context_before_seconds": context.config.vision.transcript_context_before_seconds,
        "transcript_context_after_seconds": context.config.vision.transcript_context_after_seconds,
    }


def _vision_settings_metadata(context: StageContext) -> dict[str, object]:
    return {
        "provider": context.config.vision.provider,
        "model": context.config.vision.model,
        "detail": context.config.vision.detail,
        "media_resolution": context.config.vision.media_resolution,
        "thinking_level": context.config.vision.thinking_level,
    }


def _raw_request_metadata_matches(raw_output_path: Path, expected: dict[str, object]) -> bool:
    try:
        raw_payload = read_json(raw_output_path)
    except Exception:
        return False
    return raw_payload.get(_REQUEST_METADATA_KEY) == expected


def _write_raw_request_metadata(raw_output_path: Path, request_metadata: dict[str, object]) -> None:
    payload = read_json(raw_output_path)
    payload[_REQUEST_METADATA_KEY] = request_metadata
    write_json_atomic(raw_output_path, payload)


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_cached_result(
    context: StageContext,
    task: _SceneTask,
    newest_input_mtime: float,
) -> _SceneResult | None:
    if not task.raw_output_path.exists():
        return None
    if task.raw_output_path.stat().st_mtime < newest_input_mtime:
        return None
    if not _raw_request_metadata_matches(task.raw_output_path, _scene_request_metadata(context)):
        return None
    analysis = _load_cached_visual_result(context, task.raw_output_path)
    if not isinstance(analysis.payload.get("presenter_context"), dict):
        return None
    PresenterContext.model_validate(analysis.payload["presenter_context"])
    leakage = _technical_visual_leakage_from_payload(analysis.payload)
    if leakage is not None and not _raw_payload_has_accepted_technical_warning(analysis.raw_payload):
        _log_vision_content_validation(
            context,
            task=task,
            status=_SceneValidationStatus.CACHE_INVALID,
            attempt=0,
            max_attempts=_SCENE_CONTENT_MAX_ATTEMPTS,
            leakage=leakage,
        )
        return None
    return _SceneResult(
        task=task,
        analysis=analysis,
        cached=True,
        wall_seconds=0.0,
        raw_output_paths=[task.raw_output_path],
        technical_leakage=leakage,
    )


def _raw_payload_has_accepted_technical_warning(raw_payload: dict) -> bool:
    metadata = raw_payload.get(_CONTENT_VALIDATION_METADATA_KEY)
    return isinstance(metadata, dict) and metadata.get("status") == _CONTENT_VALIDATION_ACCEPTED_WITH_WARNING


def _write_scene_canonical_raw(
    *,
    attempt_raw_path: Path,
    canonical_raw_path: Path,
    request_metadata: dict[str, object],
    attempt: int,
    max_attempts: int,
    leakage: _VisualContentLeakage | None,
) -> None:
    payload = read_json(attempt_raw_path)
    diagnostics = payload.get("_style_kb_diagnostics")
    if isinstance(diagnostics, dict):
        diagnostics["raw_output_path"] = str(canonical_raw_path)
    payload[_CONTENT_VALIDATION_METADATA_KEY] = {
        "status": _CONTENT_VALIDATION_ACCEPTED_WITH_WARNING if leakage is not None else "accepted",
        "attempt": attempt,
        "max_attempts": max_attempts,
        "attempt_raw_output_path": str(attempt_raw_path),
        "technical_leakage": _content_validation_metadata(leakage),
    }
    payload[_REQUEST_METADATA_KEY] = request_metadata
    write_json_atomic(canonical_raw_path, payload)


def _content_validation_metadata(leakage: _VisualContentLeakage | None) -> dict[str, object] | None:
    if leakage is None:
        return None
    return {
        "reason": leakage.reason,
        "error_code": leakage.error_code,
        "markers": leakage.markers,
        "fields": leakage.fields,
        "structured_errors_count": len(leakage.structured_errors),
    }


def _request_scene_analysis(
    *,
    context: StageContext,
    task: _SceneTask,
    system_prompt: str,
    on_retry: OnRetry | None = None,
) -> _SceneResult:
    started_at = perf_counter()
    provider = _vision_provider(context)
    client = _build_vision_client(context, on_retry=on_retry)
    request_metadata = _scene_request_metadata(context)
    leakage: _VisualContentLeakage | None = None
    raw_attempt_paths: list[Path] = []
    for attempt in range(1, _SCENE_CONTENT_MAX_ATTEMPTS + 1):
        raw_attempt_path = context.paths.visual_raw_scene_attempt(task.scene.scene_id, attempt)
        raw_attempt_paths.append(raw_attempt_path)
        event_extra = {
            "scene_order": task.order,
            "scene_index": task.scene.index,
            "scene_id": task.scene.scene_id,
            "attempt": attempt,
            "max_attempts": _SCENE_CONTENT_MAX_ATTEMPTS,
            "frames_count": len(task.frames),
        }
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_STARTED,
            stage_name=Stage10DescribeVisuals.name,
            ordinal=Stage10DescribeVisuals.ordinal,
            operation=ProviderOperation.VISION_SCENE,
            diagnostics=ProviderCallDiagnostics(
                provider=provider,
                model=context.config.vision.model,
                raw_output_path=str(raw_attempt_path),
            ),
            attempt=attempt,
            message="scene vision request started",
            extra=event_extra,
        )
        try:
            analysis = client.describe_scene(
                system_prompt=_scene_prompt_for_attempt(system_prompt, leakage),
                transcript_context=task.transcript_context,
                image_paths=task.image_paths,
                detail=context.config.vision.detail,
                raw_output_path=raw_attempt_path,
            )
        except Exception as error:
            emit_provider_event(
                context,
                PipelineEvent.PROVIDER_REQUEST_FAILED,
                stage_name=Stage10DescribeVisuals.name,
                ordinal=Stage10DescribeVisuals.ordinal,
                operation=ProviderOperation.VISION_SCENE,
                diagnostics=ProviderCallDiagnostics(
                    provider=provider,
                    model=context.config.vision.model,
                    raw_output_path=str(raw_attempt_path),
                    request_id=request_id_from_error(error),
                ),
                attempt=attempt,
                message="scene vision request failed",
                extra={**provider_error_extra(error), **event_extra},
            )
            raise
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_COMPLETED,
            stage_name=Stage10DescribeVisuals.name,
            ordinal=Stage10DescribeVisuals.ordinal,
            operation=ProviderOperation.VISION_SCENE,
            diagnostics=analysis.diagnostics,
            attempt=attempt,
            message="scene vision request completed",
            extra=event_extra,
        )
        leakage = _technical_visual_leakage_from_payload(analysis.payload)
        if leakage is None:
            _write_scene_canonical_raw(
                attempt_raw_path=raw_attempt_path,
                canonical_raw_path=task.raw_output_path,
                request_metadata=request_metadata,
                attempt=attempt,
                max_attempts=_SCENE_CONTENT_MAX_ATTEMPTS,
                leakage=None,
            )
            return _SceneResult(
                task=task,
                analysis=analysis,
                cached=False,
                wall_seconds=perf_counter() - started_at,
                raw_output_paths=[task.raw_output_path, *raw_attempt_paths],
            )

        status = (
            _SceneValidationStatus.RETRY
            if attempt < _SCENE_CONTENT_MAX_ATTEMPTS
            else _SceneValidationStatus.ACCEPTED_WITH_WARNING
        )
        _log_vision_content_validation(
            context,
            task=task,
            status=status,
            attempt=attempt,
            max_attempts=_SCENE_CONTENT_MAX_ATTEMPTS,
            leakage=leakage,
            raw_output_path=raw_attempt_path,
        )
        if status == _SceneValidationStatus.RETRY:
            _emit_content_validation_retry_progress(context, task=task, attempt=attempt, leakage=leakage)
            continue

        _write_scene_canonical_raw(
            attempt_raw_path=raw_attempt_path,
            canonical_raw_path=task.raw_output_path,
            request_metadata=request_metadata,
            attempt=attempt,
            max_attempts=_SCENE_CONTENT_MAX_ATTEMPTS,
            leakage=leakage,
        )
        return _SceneResult(
            task=task,
            analysis=analysis,
            cached=False,
            wall_seconds=perf_counter() - started_at,
            raw_output_paths=[task.raw_output_path, *raw_attempt_paths],
            technical_leakage=leakage,
        )

    raise StageExecutionError(
        "Vision provider response did not produce a usable scene analysis",
        error_code="vision_scene_analysis_missing",
    )


def _build_visual_event(context: StageContext, task: _SceneTask, analysis: VisionAnalysisResult) -> VisualEvent:
    payload = analysis.payload
    scene = task.scene
    presenter_context = PresenterContext.model_validate(payload.get("presenter_context"))
    return VisualEvent(
        visual_event_id=visual_event_id(context.job.video_id, scene.start, scene.end),
        video_id=context.job.video_id,
        scene_id=scene.scene_id,
        start=scene.start,
        end=scene.end,
        timestamp_url=build_timestamp_url(context.job.video_id, scene.start),
        frames=task.frames,
        presenter_context=presenter_context,
        visual_summary=str(payload.get("visual_summary") or ""),
        observations=stable_unique(payload.get("observations") or []),
        interpretations=stable_unique(payload.get("interpretations") or []),
        on_screen_text=stable_unique(payload.get("on_screen_text") or []),
        items=_sanitize_visual_labels(payload.get("items") or [], _VisualListField.ITEMS),
        style_topics=_sanitize_visual_labels(payload.get("style_topics") or [], _VisualListField.STYLE_TOPICS),
        confidence=ConfidenceLevel(str(payload.get("confidence") or ConfidenceLevel.MEDIUM.value)),
        notes=str(payload.get("notes") or ""),
        source_refs=[youtube_source_ref(context.job.video_id, scene.start, scene.end, modality="visual")],
    )


def _scene_prompt_for_attempt(system_prompt: str, leakage: _VisualContentLeakage | None) -> str:
    if leakage is None:
        return system_prompt
    feedback = [
        "",
        f"Нарушение предыдущей попытки: {leakage.reason}.",
        "Исправь ответ: основные поля сцены должны содержать только визуально подтверждённую информацию о мужском стиле.",
        "Не пиши про формат видео, экран, слайды, текстовые вставки, оверлеи, кадр, камеру, крупный/средний план, фон, интерьер или предметы съёмочной обстановки.",
        "Если нарушение связано только с экранным текстом, перенеси читаемый текст в on_screen_text и очисти остальные scene-specific поля.",
        "Найденные markers:",
        *[f"- {marker}" for marker in leakage.markers],
        "Поля с нарушениями:",
        *[f"- {field}: {', '.join(markers)}" for field, markers in leakage.fields.items()],
    ]
    return system_prompt.rstrip() + "\n" + "\n".join(feedback)


def baseline_leakage_metrics_for_events(
    visual_events: list[VisualEvent],
    presenter_profile: PresenterProfile,
) -> dict[str, int]:
    metrics = empty_baseline_leakage_metrics()
    unique_markers: set[str] = set()
    for event in visual_events:
        leakage = _baseline_leakage_from_payload(event.model_dump(mode="json"), presenter_profile)
        if leakage is None:
            continue
        metrics["baseline_leakage_scenes_count"] += 1
        metrics["baseline_leakage_markers_total"] += len(leakage.markers)
        metrics["baseline_leakage_fields_count"] += len(leakage.fields)
        metrics["baseline_leakage_structured_errors_count"] += len(leakage.structured_errors)
        unique_markers.update(leakage.markers)
        for field, markers in leakage.fields.items():
            metric_name = f"baseline_leakage_{field}_count"
            if metric_name in metrics:
                metrics[metric_name] += len(markers)
    metrics["baseline_leakage_unique_markers_count"] = len(unique_markers)
    return metrics


def technical_leakage_metrics_for_events(visual_events: list[VisualEvent]) -> dict[str, int]:
    metrics = empty_technical_leakage_metrics()
    unique_markers: set[str] = set()
    for event in visual_events:
        leakage = _technical_visual_leakage_from_payload(event.model_dump(mode="json"))
        if leakage is None:
            continue
        metrics["technical_leakage_scenes_count"] += 1
        metrics["technical_leakage_markers_total"] += len(leakage.markers)
        metrics["technical_leakage_fields_count"] += len(leakage.fields)
        metrics["technical_leakage_structured_errors_count"] += len(leakage.structured_errors)
        unique_markers.update(leakage.markers)
        for field, markers in leakage.fields.items():
            metric_name = f"technical_leakage_{field}_count"
            if metric_name in metrics:
                metrics[metric_name] += len(markers)
    metrics["technical_leakage_unique_markers_count"] = len(unique_markers)
    return metrics


def _sanitize_visual_labels(values: object, field: _VisualListField) -> list[str]:
    if not isinstance(values, list):
        return []
    return [
        value
        for value in stable_unique(_clean_visual_label(item) for item in values)
        if not _should_drop_visual_label(value, field)
    ]


def _should_drop_visual_label(value: str, field: _VisualListField) -> bool:
    normalized = _normalize_visual_label(value)
    if not normalized:
        return True
    if normalized in _BARE_COLOR_LABELS:
        return True
    if field == _VisualListField.ITEMS:
        return _matches_any_pattern(normalized, _TECHNICAL_VISUAL_PATTERNS)
    if field == _VisualListField.STYLE_TOPICS:
        return _matches_any_pattern(normalized, _TECHNICAL_VISUAL_PATTERNS)
    return False


def _clean_visual_label(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_visual_label(value: str) -> str:
    text = value.casefold().replace("ё", "е")
    text = re.sub(r"[-‐‑‒–—]+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text, flags=re.UNICODE).strip()


def _matches_any_pattern(value: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, value, flags=re.UNICODE) is not None for pattern in patterns)


def _technical_visual_leakage_from_payload(payload: dict) -> _VisualContentLeakage | None:
    fields: dict[str, list[str]] = {}
    structured_errors: list[dict[str, object]] = []
    for field in ("visual_summary", "observations", "interpretations", "items", "style_topics", "notes"):
        markers = _technical_visual_markers(payload.get(field))
        if not markers:
            continue
        fields[field] = markers
        structured_errors.extend(
            {
                "code": "openai_vision_technical_visual_label",
                "message": "scene-specific field contains technical presentation or background label",
                "field": field,
                "marker": marker,
                "preview": validation_preview(payload.get(field)),
            }
            for marker in markers
        )
    if not fields:
        return None
    markers = stable_unique(marker for field_markers in fields.values() for marker in field_markers)
    return _VisualContentLeakage(
        reason="technical_visual_label_leakage",
        error_code="openai_vision_technical_visual_label",
        markers=markers,
        fields=fields,
        structured_errors=structured_errors,
    )


def _technical_visual_markers(value: object) -> list[str]:
    values = value if isinstance(value, list) else [value]
    markers: list[str] = []
    for item in values:
        raw_text = _clean_visual_label(item)
        normalized = _normalize_visual_label(raw_text)
        if not normalized:
            continue
        if normalized in _BARE_COLOR_LABELS:
            markers.append(raw_text)
            continue
        if _matches_any_pattern(normalized, _TECHNICAL_VISUAL_PATTERNS):
            markers.append(raw_text)
    return stable_unique(markers)


def _baseline_leakage_from_payload(payload: dict, presenter_profile: PresenterProfile) -> _VisualContentLeakage | None:
    if not presenter_profile.has_primary_presenter:
        return None

    presenter_context = PresenterContext.model_validate(payload.get("presenter_context"))
    if not presenter_context.is_recurring:
        return None

    markers_by_key = _baseline_markers_by_key(presenter_profile)
    if not markers_by_key:
        return None

    leaked_fields: dict[str, list[str]] = {}
    structured_errors: list[dict[str, object]] = []
    leaked_marker_keys: set[str] = set()
    for field in _BASELINE_LEAKAGE_FIELDS:
        raw_field_text = _field_text(payload.get(field))
        normalized_text = _normalize_baseline_text(raw_field_text)
        if not normalized_text:
            continue
        field_marker_keys = [
            marker_key
            for marker_key in markers_by_key
            if marker_key in normalized_text
        ]
        if not field_marker_keys:
            continue
        leaked_fields[field] = [markers_by_key[marker_key] for marker_key in field_marker_keys]
        structured_errors.extend(
            {
                "code": "openai_vision_baseline_leakage",
                "message": "scene-specific field repeats recurring presenter baseline marker",
                "field": field,
                "marker": markers_by_key[marker_key],
                "preview": validation_preview(raw_field_text),
            }
            for marker_key in field_marker_keys
        )
        leaked_marker_keys.update(field_marker_keys)

    if len(leaked_marker_keys) < _BASELINE_LEAKAGE_MARKER_THRESHOLD:
        return None
    return _VisualContentLeakage(
        reason="recurring_presenter_baseline_leakage",
        error_code="openai_vision_baseline_leakage",
        markers=[markers_by_key[marker_key] for marker_key in sorted(leaked_marker_keys)],
        fields=leaked_fields,
        structured_errors=structured_errors,
    )


def _baseline_markers_by_key(presenter_profile: PresenterProfile) -> dict[str, str]:
    markers: dict[str, str] = {}
    for marker in presenter_profile.recurring_visual_markers:
        _add_baseline_marker(markers, marker, require_phrase=False)
    for marker in _baseline_summary_markers(presenter_profile.baseline_summary):
        _add_baseline_marker(markers, marker, require_phrase=True)
    return markers


def _add_baseline_marker(markers: dict[str, str], value: str, *, require_phrase: bool) -> None:
    marker = " ".join(str(value or "").split())
    marker_key = _normalize_baseline_text(marker)
    if not marker_key or marker_key in markers:
        return
    if _is_generic_baseline_marker(marker_key):
        return
    if require_phrase and (len(marker_key) < 14 or len(marker_key.split()) < 2):
        return
    markers[marker_key] = marker


def _is_generic_baseline_marker(marker_key: str) -> bool:
    return marker_key in _GENERIC_BASELINE_MARKERS or marker_key.startswith("общий вид ")


def _baseline_summary_markers(summary: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[,;]", summary)
        if part.strip()
    ]


def _field_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return ""


def _normalize_baseline_text(value: str) -> str:
    text = value.casefold().replace("ё", "е")
    text = re.sub(r"[-‐‑‒–—]+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text, flags=re.UNICODE).strip()


def _visual_content_leakage_details(leakage: _VisualContentLeakage) -> str:
    lines = [
        f"reason: {leakage.reason}",
        f"markers_count: {len(leakage.markers)}",
        f"structured_errors_count: {len(leakage.structured_errors)}",
        "markers:",
        *[f"  - {marker}" for marker in leakage.markers],
        "fields:",
    ]
    for field, markers in leakage.fields.items():
        lines.append(f"  - {field}: {', '.join(markers)}")
    lines.append("structured_error_previews:")
    lines.extend(
        f"  - {entry.get('field', '-')}: marker={entry.get('marker', '-')} preview={entry.get('preview', '-')}"
        for entry in leakage.structured_errors[:5]
    )
    return "\n".join(lines)


def _presenter_relevance_counts(visual_events: list[VisualEvent]) -> dict[str, int]:
    counts = {
        PresenterRelevance.BACKGROUND.value: 0,
        PresenterRelevance.BRIEF.value: 0,
        PresenterRelevance.PRIMARY_EXAMPLE.value: 0,
    }
    for event in visual_events:
        relevance = event.presenter_context.relevance.value
        if relevance in counts:
            counts[relevance] += 1
    return counts


def _log_presenter_profile(
    context: StageContext,
    *,
    profile: PresenterProfile,
    analysis: VisionAnalysisResult,
    image_paths: list[Path],
) -> None:
    diagnostics = analysis.diagnostics
    lines = [
        "",
        "[presenter-profile]",
        f"run_id: {context.run_id or '-'}",
        f"has_primary_presenter: {profile.has_primary_presenter}",
        f"confidence: {profile.confidence}",
        f"images_count: {len(image_paths)}",
        f"model: {diagnostics.model or '-'}",
        f"request_id: {diagnostics.request_id or '-'}",
        f"response_id: {diagnostics.response_id or '-'}",
        f"started_at: {diagnostics.started_at or '-'}",
        f"finished_at: {diagnostics.finished_at or '-'}",
        f"duration_seconds: {_format_seconds(diagnostics.duration_seconds)}",
        f"input_tokens: {analysis.usage['input_tokens']}",
        f"output_tokens: {analysis.usage['output_tokens']}",
        f"reasoning_tokens: {analysis.usage['reasoning_tokens']}",
        f"total_tokens: {analysis.usage['total_tokens']}",
        f"profile_output: {context.paths.visual_presenter_profile}",
        f"raw_output: {context.paths.visual_raw_presenter_profile}",
        "images:",
        *[f"  - {path}" for path in image_paths],
        "",
    ]
    append_text(context.paths.stage_log(Stage10DescribeVisuals.name), "\n".join(lines), encoding="utf-8")


def _log_scene_result(
    context: StageContext,
    result: _SceneResult,
    *,
    completed: int,
    total_scenes: int,
) -> None:
    status = _scene_progress_status(result)
    diagnostics = result.analysis.diagnostics
    lines = [
        "",
        "[scene-summary]",
        f"run_id: {context.run_id or '-'}",
        f"scene_order: {result.task.order}/{total_scenes}",
        f"completed_progress: {completed}/{total_scenes}",
        f"scene_index: {result.task.scene.index}",
        f"scene_id: {result.task.scene.scene_id}",
        f"status: {status.value}",
        f"frames_count: {len(result.task.frames)}",
        f"transcript_words: {result.task.transcript_words}",
        f"previous_transcript_words: {_transcript_window_word_count(result.task.transcript_context, 'previous_context')}",
        f"current_transcript_words: {_transcript_window_word_count(result.task.transcript_context, 'current_scene_context')}",
        f"next_transcript_words: {_transcript_window_word_count(result.task.transcript_context, 'next_context')}",
        f"wall_seconds: {_format_seconds(result.wall_seconds)}",
        f"started_at: {diagnostics.started_at or '-'}",
        f"finished_at: {diagnostics.finished_at or '-'}",
        f"duration_seconds: {_format_seconds(diagnostics.duration_seconds)}",
        f"remote_duration_seconds: {_format_seconds(result.analysis.remote_duration_seconds)}",
        f"model: {diagnostics.model or '-'}",
        f"request_id: {diagnostics.request_id or '-'}",
        f"response_id: {diagnostics.response_id or '-'}",
        f"input_tokens: {result.analysis.usage['input_tokens']}",
        f"output_tokens: {result.analysis.usage['output_tokens']}",
        f"reasoning_tokens: {result.analysis.usage['reasoning_tokens']}",
        f"total_tokens: {result.analysis.usage['total_tokens']}",
        f"raw_output: {result.task.raw_output_path}",
    ]
    if result.technical_leakage is not None:
        lines.extend(
            [
                f"technical_leakage_reason: {result.technical_leakage.reason}",
                f"technical_leakage_markers_count: {len(result.technical_leakage.markers)}",
            ]
        )
    lines.append("")
    append_text(context.paths.stage_log(Stage10DescribeVisuals.name), "\n".join(lines), encoding="utf-8")


def _log_baseline_leakage_summary(context: StageContext, metrics: dict[str, int]) -> None:
    lines = [
        "",
        "[baseline-leakage-summary]",
        f"run_id: {context.run_id or '-'}",
        f"scenes_count: {metrics['baseline_leakage_scenes_count']}",
        f"markers_total: {metrics['baseline_leakage_markers_total']}",
        f"unique_markers_count: {metrics['baseline_leakage_unique_markers_count']}",
        f"fields_count: {metrics['baseline_leakage_fields_count']}",
        f"structured_errors_count: {metrics['baseline_leakage_structured_errors_count']}",
        f"visual_summary_count: {metrics['baseline_leakage_visual_summary_count']}",
        f"observations_count: {metrics['baseline_leakage_observations_count']}",
        f"interpretations_count: {metrics['baseline_leakage_interpretations_count']}",
        f"items_count: {metrics['baseline_leakage_items_count']}",
        f"style_topics_count: {metrics['baseline_leakage_style_topics_count']}",
        f"notes_count: {metrics['baseline_leakage_notes_count']}",
        "",
    ]
    append_text(context.paths.stage_log(Stage10DescribeVisuals.name), "\n".join(lines), encoding="utf-8")


def _log_technical_leakage_summary(context: StageContext, metrics: dict[str, int]) -> None:
    lines = [
        "",
        "[technical-leakage-summary]",
        f"run_id: {context.run_id or '-'}",
        f"scenes_count: {metrics['technical_leakage_scenes_count']}",
        f"markers_total: {metrics['technical_leakage_markers_total']}",
        f"unique_markers_count: {metrics['technical_leakage_unique_markers_count']}",
        f"fields_count: {metrics['technical_leakage_fields_count']}",
        f"structured_errors_count: {metrics['technical_leakage_structured_errors_count']}",
        f"visual_summary_count: {metrics['technical_leakage_visual_summary_count']}",
        f"observations_count: {metrics['technical_leakage_observations_count']}",
        f"interpretations_count: {metrics['technical_leakage_interpretations_count']}",
        f"items_count: {metrics['technical_leakage_items_count']}",
        f"style_topics_count: {metrics['technical_leakage_style_topics_count']}",
        f"notes_count: {metrics['technical_leakage_notes_count']}",
        "",
    ]
    append_text(context.paths.stage_log(Stage10DescribeVisuals.name), "\n".join(lines), encoding="utf-8")


def _log_vision_content_validation(
    context: StageContext,
    *,
    task: _SceneTask,
    status: _SceneValidationStatus,
    attempt: int,
    max_attempts: int,
    leakage: _VisualContentLeakage,
    raw_output_path: Path | None = None,
) -> None:
    effective_raw_output_path = raw_output_path or task.raw_output_path
    lines = [
        "",
        "[vision-content-validation]",
        f"run_id: {context.run_id or '-'}",
        f"scene_order: {task.order}",
        f"scene_index: {task.scene.index}",
        f"scene_id: {task.scene.scene_id}",
        f"status: {status.value}",
        f"attempt: {attempt}/{max_attempts}",
        f"reason: {leakage.reason}",
        f"markers_count: {len(leakage.markers)}",
        "markers:",
        *[f"  - {marker}" for marker in leakage.markers],
        "fields:",
    ]
    for field, markers in leakage.fields.items():
        lines.append(f"  - {field}: {', '.join(markers)}")
    lines.append("structured_error_previews:")
    lines.extend(
        "  - "
        f"{entry.get('field', '-')}"
        f" marker={entry.get('marker', '-')}"
        f" preview={entry.get('preview', '-')}"
        for entry in leakage.structured_errors[:5]
    )
    lines.extend(
        [
            f"raw_output: {effective_raw_output_path}",
            "",
        ]
    )
    append_text(context.paths.stage_log(Stage10DescribeVisuals.name), "\n".join(lines), encoding="utf-8")
    if status == _SceneValidationStatus.ACCEPTED_WITH_WARNING:
        return
    validation_errors = [
        f"field {field} contains blocked visual content: {', '.join(markers)}"
        for field, markers in leakage.fields.items()
    ]
    emit_stage_validation_failed(
        context,
        stage_name=Stage10DescribeVisuals.name,
        ordinal=Stage10DescribeVisuals.ordinal,
        error_code=leakage.error_code,
        message="vision content validation failed",
        validation_errors=validation_errors,
        structured_errors=leakage.structured_errors,
        raw_output_path=effective_raw_output_path,
        attempt=attempt,
        extra={"scene_order": task.order, "scene_id": task.scene.scene_id, "status": status.value},
    )


def _emit_content_validation_retry_progress(
    context: StageContext,
    *,
    task: _SceneTask,
    attempt: int,
    leakage: _VisualContentLeakage,
) -> None:
    if context.progress_callback is None:
        return
    context.progress_callback(
        " ".join(
            [
                f"[10 {Stage10DescribeVisuals.name}]",
                f"scene {task.order}",
                _SceneValidationStatus.RETRY.value,
                f"attempt={attempt + 1}/{_SCENE_CONTENT_MAX_ATTEMPTS}",
                f"reason={leakage.reason}",
                f"markers={len(leakage.markers)}",
            ]
        )
    )


def _emit_scene_progress(
    context: StageContext,
    result: _SceneResult,
    *,
    completed: int,
    total_scenes: int,
) -> None:
    if context.progress_callback is None:
        return
    status = _scene_progress_status(result)
    context.progress_callback(
        " ".join(
            [
                f"[10 {Stage10DescribeVisuals.name}]",
                f"scene {result.task.order}/{total_scenes}",
                status.value,
                f"done={completed}/{total_scenes}",
                f"frames={len(result.task.frames)}",
                f"wall={_format_seconds(result.wall_seconds)}s",
                f"tokens={result.analysis.usage['total_tokens']}",
            ]
        )
    )


def _scene_progress_status(result: _SceneResult) -> _SceneProgressStatus:
    if result.cached:
        if result.technical_leakage is not None:
            return _SceneProgressStatus.CACHED_WITH_WARNING
        return _SceneProgressStatus.CACHED
    if result.technical_leakage is not None:
        return _SceneProgressStatus.COMPLETED_WITH_WARNING
    return _SceneProgressStatus.COMPLETED


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def _transcript_window_word_count(transcript_context: dict[str, object], key: str) -> int:
    value = transcript_context.get(key)
    if not isinstance(value, dict):
        return 0
    try:
        return int(value.get("word_count") or 0)
    except (TypeError, ValueError):
        return 0


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def _scene_retry_logger(context: StageContext, task: _SceneTask) -> OnRetry:
    stage_log_path = context.paths.stage_log(Stage10DescribeVisuals.name)

    def _on_retry(attempt: int, delay: float, error: BaseException) -> None:
        log_openai_retry(
            stage_log_path,
            attempt=attempt,
            delay_seconds=delay,
            error=error,
            context_lines=[
                f"run_id: {context.run_id or '-'}",
                f"scene_index: {task.scene.index}",
                f"scene_id: {task.scene.scene_id}",
            ],
        )

    return _on_retry


def _presenter_retry_logger(context: StageContext) -> OnRetry:
    stage_log_path = context.paths.stage_log(Stage10DescribeVisuals.name)

    def _on_retry(attempt: int, delay: float, error: BaseException) -> None:
        log_openai_retry(
            stage_log_path,
            attempt=attempt,
            delay_seconds=delay,
            error=error,
            context_lines=[f"run_id: {context.run_id or '-'}", "operation: presenter_profile_bootstrap"],
        )

    return _on_retry


def _log_concurrent_abort(
    context: StageContext,
    *,
    error: BaseException,
    completed: int,
    total_scenes: int,
    cancelled_tasks: list[_SceneTask],
    in_flight_tasks: list[_SceneTask],
) -> None:
    lines = [
        "",
        "[concurrent-abort]",
        f"run_id: {context.run_id or '-'}",
        f"error: {type(error).__name__}: {error}",
        f"completed: {completed}/{total_scenes}",
        f"cancelled_count: {len(cancelled_tasks)}",
        f"in_flight_count: {len(in_flight_tasks)}",
        "cancelled_scenes:",
        *[
            f"  - {task.scene.scene_id} (index {task.scene.index}) raw_output={task.raw_output_path}"
            for task in cancelled_tasks[:10]
        ],
        *([f"  - ... {len(cancelled_tasks) - 10} more"] if len(cancelled_tasks) > 10 else []),
        "in_flight_scenes:",
        *[
            f"  - {task.scene.scene_id} (index {task.scene.index}) raw_output={task.raw_output_path}"
            for task in in_flight_tasks[:10]
        ],
        *([f"  - ... {len(in_flight_tasks) - 10} more"] if len(in_flight_tasks) > 10 else []),
        "note: in_flight requests keep running until completion; their raw outputs"
        " are saved to disk and reused on the next run",
        "",
    ]
    append_text(
        context.paths.stage_log(Stage10DescribeVisuals.name),
        "\n".join(lines),
        encoding="utf-8",
    )


def _log_in_flight_finalization(
    context: StageContext,
    in_flight_map: dict[Future, _SceneTask],
) -> None:
    lines = ["", "[concurrent-abort-finalized]", f"run_id: {context.run_id or '-'}"]
    for index, (future, task) in enumerate(in_flight_map.items()):
        if index >= 10:
            lines.append(f"  - ... {len(in_flight_map) - 10} more")
            break
        if future.cancelled():
            lines.append(
                f"  - scene_id={task.scene.scene_id} index={task.scene.index}"
                " status=cancelled_after_start"
            )
            continue
        future_error = future.exception()
        if future_error is None:
            result = future.result()
            diagnostics = result.analysis.diagnostics
            lines.append(f"  - scene_id={task.scene.scene_id} index={task.scene.index} status=completed_after_abort")
            lines.append(f"    raw_output: {task.raw_output_path}")
            lines.append(f"    request_id: {diagnostics.request_id or '-'}")
            lines.append(f"    response_id: {diagnostics.response_id or '-'}")
            lines.append(f"    started_at: {diagnostics.started_at or '-'}")
            lines.append(f"    finished_at: {diagnostics.finished_at or '-'}")
            lines.append(f"    duration_seconds: {_format_seconds(diagnostics.duration_seconds)}")
        else:
            lines.append(
                f"  - scene_id={task.scene.scene_id} index={task.scene.index}"
                f" status=failed_after_abort error={type(future_error).__name__}: {future_error}"
            )
            lines.append(f"    request_id: {request_id_from_error(future_error) or '-'}")
    lines.append("")
    append_text(
        context.paths.stage_log(Stage10DescribeVisuals.name),
        "\n".join(lines),
        encoding="utf-8",
    )


def _emit_concurrent_abort_progress(
    context: StageContext,
    *,
    completed: int,
    total_scenes: int,
    cancelled_count: int,
    in_flight_count: int,
) -> None:
    if context.progress_callback is None:
        return
    context.progress_callback(
        " ".join(
            [
                f"[10 {Stage10DescribeVisuals.name}]",
                "abort",
                f"done={completed}/{total_scenes}",
                f"cancelled={cancelled_count}",
                f"in_flight={in_flight_count}",
            ]
        )
    )

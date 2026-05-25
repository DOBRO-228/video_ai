from __future__ import annotations

import json
import os
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from style_kb.clients.openai_vision import OpenAIVisionClient, VisionAnalysisResult, load_cached_visual_result
from style_kb.errors import StageExecutionError
from style_kb.models import FrameRef, PresenterContext, PresenterProfile, Scene, SpeechSegment, VisualEvent
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import load_frame_refs, load_scenes, load_speech_segments, load_visual_events, youtube_source_ref
from style_kb.utils.collections import stable_unique
from style_kb.utils.files import append_text
from style_kb.utils.ids import visual_event_id
from style_kb.utils.pydantic_io import read_model, write_model, write_models_jsonl
from style_kb.utils.time import build_timestamp_url


@dataclass(slots=True)
class _SceneTask:
    order: int
    scene: Scene
    frames: list[FrameRef]
    image_paths: list[Path]
    transcript_context: str
    transcript_words: int
    raw_output_path: Path


@dataclass(slots=True)
class _SceneResult:
    task: _SceneTask
    analysis: VisionAnalysisResult
    cached: bool
    wall_seconds: float


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
        read_model(context.paths.visual_presenter_profile, PresenterProfile)
        scenes = load_scenes(context.paths.scenes_jsonl) if context.paths.scenes_jsonl.exists() else []
        visual_events = load_visual_events(context.paths.visual_events_jsonl)
        return bool(visual_events) and (not scenes or len(visual_events) == len(scenes))

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
                transcript_context=_nearby_transcript(scene.start, scene.end, speech_segments, context),
                transcript_words=_transcript_word_count(scene.start, scene.end, speech_segments, context),
                raw_output_path=context.paths.visual_raw_scene(scene.scene_id),
            )
            for index, scene in enumerate(scenes, start=1)
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
            cached_result = _load_cached_result(task, newest_input_mtime)
            if cached_result is None:
                pending_tasks.append(task)
                continue
            output_files.append(task.raw_output_path)
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
            api_key = os.environ.get("OPENAI_API_KEY")
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(
                        _request_scene_analysis,
                        task=task,
                        system_prompt=scene_prompt,
                        detail=context.config.vision.detail,
                        api_key=api_key,
                        model=context.config.vision.model,
                    ): task
                    for task in pending_tasks
                }
                while future_map:
                    done, _ = wait(future_map, return_when=FIRST_COMPLETED)
                    for future in done:
                        task = future_map.pop(future)
                        error = future.exception()
                        if error is not None:
                            for pending_future in future_map:
                                pending_future.cancel()
                            raise error
                        result = future.result()
                        output_files.append(task.raw_output_path)
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
                "presenter_background_scenes_count": presenter_counts["background"],
                "presenter_brief_scenes_count": presenter_counts["brief"],
                "presenter_primary_example_scenes_count": presenter_counts["primary_example"],
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
            confidence="low",
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
    if context.paths.visual_presenter_profile.exists() and context.paths.visual_raw_presenter_profile.exists():
        cached = read_model(context.paths.visual_presenter_profile, PresenterProfile)
        if context.paths.visual_presenter_profile.stat().st_mtime >= newest_input:
            return cached

    client = OpenAIVisionClient(os.environ.get("OPENAI_API_KEY"), model=context.config.vision.model)
    analysis = client.build_presenter_profile(
        system_prompt=prompt_path.read_text(encoding="utf-8"),
        image_paths=image_paths,
        detail=context.config.vision.detail,
        raw_output_path=context.paths.visual_raw_presenter_profile,
    )
    profile = PresenterProfile.model_validate(analysis.payload)
    write_model(context.paths.visual_presenter_profile, profile)
    _log_presenter_profile(context, profile=profile, analysis=analysis, image_paths=image_paths)
    return profile


def _effective_presenter_profile(context: StageContext, profile: PresenterProfile) -> PresenterProfile:
    if (
        context.config.vision.presenter_low_confidence_disables_recurrence
        and profile.has_primary_presenter
        and profile.confidence == "low"
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
            "Если has_primary_presenter=true, используй этот профиль только для распознавания повторяющегося baseline. Не копируй baseline в основные поля сцены, если relevance=background.",
        ]
    )


def _nearby_transcript(start: float, end: float, speech_segments: list[SpeechSegment], context: StageContext) -> str:
    before = context.config.vision.transcript_context_before_seconds
    after = context.config.vision.transcript_context_after_seconds
    lower = max(0.0, start - before)
    upper = end + after
    texts = [segment.text for segment in speech_segments if segment.end >= lower and segment.start <= upper]
    return "\n".join(texts)


def _transcript_word_count(start: float, end: float, speech_segments: list[SpeechSegment], context: StageContext) -> int:
    transcript = _nearby_transcript(start, end, speech_segments, context)
    return len(transcript.split())


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


def _load_cached_result(task: _SceneTask, newest_input_mtime: float) -> _SceneResult | None:
    if not task.raw_output_path.exists():
        return None
    if task.raw_output_path.stat().st_mtime < newest_input_mtime:
        return None
    analysis = load_cached_visual_result(task.raw_output_path)
    if not isinstance(analysis.payload.get("presenter_context"), dict):
        return None
    PresenterContext.model_validate(analysis.payload["presenter_context"])
    return _SceneResult(task=task, analysis=analysis, cached=True, wall_seconds=0.0)


def _request_scene_analysis(
    *,
    task: _SceneTask,
    system_prompt: str,
    detail: str,
    api_key: str | None,
    model: str,
) -> _SceneResult:
    started_at = perf_counter()
    client = OpenAIVisionClient(api_key, model=model)
    analysis = client.describe_scene(
        system_prompt=system_prompt,
        transcript_context=task.transcript_context,
        image_paths=task.image_paths,
        detail=detail,
        raw_output_path=task.raw_output_path,
    )
    return _SceneResult(task=task, analysis=analysis, cached=False, wall_seconds=perf_counter() - started_at)


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
        items=stable_unique(payload.get("items") or []),
        colors=stable_unique(payload.get("colors") or []),
        style_topics=stable_unique(payload.get("style_topics") or []),
        confidence=str(payload.get("confidence") or "medium"),
        notes=str(payload.get("notes") or ""),
        source_refs=[youtube_source_ref(context.job.video_id, scene.start, scene.end, modality="visual")],
    )


def _presenter_relevance_counts(visual_events: list[VisualEvent]) -> dict[str, int]:
    counts = {"background": 0, "brief": 0, "primary_example": 0}
    for event in visual_events:
        relevance = event.presenter_context.relevance
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
    lines = [
        "",
        "[presenter-profile]",
        f"has_primary_presenter: {profile.has_primary_presenter}",
        f"confidence: {profile.confidence}",
        f"images_count: {len(image_paths)}",
        f"model: {analysis.model or '-'}",
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
    lines = [
        "",
        "[scene-summary]",
        f"scene_order: {result.task.order}/{total_scenes}",
        f"completed_progress: {completed}/{total_scenes}",
        f"scene_index: {result.task.scene.index}",
        f"scene_id: {result.task.scene.scene_id}",
        f"status: {'cached' if result.cached else 'completed'}",
        f"frames_count: {len(result.task.frames)}",
        f"transcript_words: {result.task.transcript_words}",
        f"wall_seconds: {_format_seconds(result.wall_seconds)}",
        f"remote_duration_seconds: {_format_seconds(result.analysis.remote_duration_seconds)}",
        f"model: {result.analysis.model or '-'}",
        f"input_tokens: {result.analysis.usage['input_tokens']}",
        f"output_tokens: {result.analysis.usage['output_tokens']}",
        f"reasoning_tokens: {result.analysis.usage['reasoning_tokens']}",
        f"total_tokens: {result.analysis.usage['total_tokens']}",
        f"raw_output: {result.task.raw_output_path}",
        "",
    ]
    append_text(context.paths.stage_log(Stage10DescribeVisuals.name), "\n".join(lines), encoding="utf-8")


def _emit_scene_progress(
    context: StageContext,
    result: _SceneResult,
    *,
    completed: int,
    total_scenes: int,
) -> None:
    if context.progress_callback is None:
        return
    status = "cached" if result.cached else "completed"
    context.progress_callback(
        " ".join(
            [
                f"[10 {Stage10DescribeVisuals.name}]",
                f"scene {result.task.order}/{total_scenes}",
                status,
                f"done={completed}/{total_scenes}",
                f"frames={len(result.task.frames)}",
                f"wall={_format_seconds(result.wall_seconds)}s",
                f"tokens={result.analysis.usage['total_tokens']}",
            ]
        )
    )


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique

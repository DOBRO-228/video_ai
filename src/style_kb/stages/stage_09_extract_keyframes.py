from __future__ import annotations

import json

from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Any

from style_kb.clients.image_metrics import DedupSkip, FrameStat, dedupe_frames, load_gray, phash, sharpness
from style_kb.clients.media import extract_frame
from style_kb.errors import MediaToolError
from style_kb.models import FrameRef, Scene
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import load_scenes, relative_artifact_path, youtube_source_ref
from style_kb.stages.diagnostics import append_stage_summary, file_size
from style_kb.utils.files import append_text, read_json, write_json_atomic
from style_kb.utils.pydantic_io import read_models_jsonl, write_models_jsonl
from style_kb.utils.time import build_timestamp_url, clamp


@dataclass(slots=True)
class _FrameCandidate:
    scene: Scene
    image_index: int
    timestamp: float
    frame_role: str
    path: Path
    relative_path: str
    file_size_bytes: int


class Stage09ExtractKeyframes(Stage):
    name = "09_extract_keyframes"
    ordinal = 9

    def input_files(self, context: StageContext) -> list[Path]:
        return [context.paths.downloads_video_proxy, context.paths.scenes_jsonl]

    def output_files(self, context: StageContext) -> list[Path]:
        outputs = [
            context.paths.frame_refs_jsonl,
            context.paths.frame_extraction_report,
            context.paths.frame_extraction_events_jsonl,
        ]
        report_paths = _report_frame_paths(context)
        if report_paths:
            outputs.extend(report_paths)
        elif context.paths.frame_refs_jsonl.exists():
            for frame in read_models_jsonl(context.paths.frame_refs_jsonl, FrameRef):
                outputs.append(context.paths.job_dir / frame.path)
        return _unique_paths(outputs)

    def validate_outputs(self, context: StageContext) -> bool:
        if (
            not context.paths.frame_refs_jsonl.exists()
            or not context.paths.frame_extraction_report.exists()
            or not context.paths.frame_extraction_events_jsonl.exists()
        ):
            return False
        try:
            frame_refs = read_models_jsonl(context.paths.frame_refs_jsonl, FrameRef)
            report = read_json(context.paths.frame_extraction_report)
        except Exception:
            return False
        report_frames = _report_frames(report)
        included_frames = [frame for frame in report_frames if frame.get("included_in_frame_refs") is True]
        dropped_count = len(report_frames) - len(included_frames)
        frame_ref_paths = {frame.path for frame in frame_refs}
        included_paths = {str(frame.get("path")) for frame in included_frames if frame.get("path")}
        return (
            bool(frame_refs)
            and report.get("frames_count") == len(frame_refs)
            and report.get("frames_count") == len(included_frames)
            and report.get("frames_extracted_total") == len(report_frames)
            and report.get("frames_dropped") == dropped_count
            and report.get("frame_selection") == _frame_selection_report(context)
            and report.get("dedup") == _dedup_report(context)
            and frame_ref_paths == included_paths
            and all(_artifact_exists(context, frame.get("path")) for frame in report_frames)
        )

    def run(self, context: StageContext) -> StageResult:
        scenes = load_scenes(context.paths.scenes_jsonl)
        append_stage_summary(
            context,
            self.name,
            "frame-extraction-preflight",
            {
                "scenes_count": len(scenes),
                "images_per_scene": context.config.scene_detection.images_per_scene,
                "extra_sample_every_seconds": context.config.scene_detection.extra_sample_every_seconds,
                "max_frames_per_scene": context.config.scene_detection.max_frames_per_scene,
                "video_path": str(context.paths.downloads_video_proxy),
                "frame_refs_path": str(context.paths.frame_refs_jsonl),
                "report_path": str(context.paths.frame_extraction_report),
            },
        )
        frame_refs: list[FrameRef] = []
        output_files: list[Path] = [
            context.paths.frame_refs_jsonl,
            context.paths.frame_extraction_report,
            context.paths.frame_extraction_events_jsonl,
        ]
        extraction_report: list[dict[str, Any]] = []
        frames_dropped = 0

        for scene in scenes:
            scene_candidates: list[_FrameCandidate] = []
            for image_index, (timestamp, frame_role) in enumerate(_scene_timestamps(scene, context), start=1):
                frame_path = context.paths.frame_path(scene.index, image_index)
                relative_path = relative_artifact_path(context.paths.job_dir, frame_path)
                event: dict[str, Any] = {
                    "schema_version": 1,
                    "run_id": context.run_id,
                    "video_id": context.job.video_id,
                    "stage": self.name,
                    "scene_id": scene.scene_id,
                    "scene_index": scene.index,
                    "image_index": image_index,
                    "timestamp": round(timestamp, 3),
                    "frame_role": frame_role,
                    "path": relative_path,
                }
                append_text(
                    context.paths.frame_extraction_events_jsonl,
                    json.dumps({**event, "event": "frame_extraction_started"}, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                extract_frame(
                    context.paths.downloads_video_proxy,
                    timestamp=timestamp,
                    destination=frame_path,
                    log_path=None,
                    pipeline_logger=context.pipeline_logger,
                    job_id=context.job.job_id,
                    video_id=context.job.video_id,
                    stage=self.name,
                    ordinal=self.ordinal,
                    text_log_streams=False,
                )
                if not frame_path.exists() or frame_path.stat().st_size == 0:
                    raise MediaToolError("missing keyframe after extraction", error_code="missing_keyframes")
                append_text(
                    context.paths.frame_extraction_events_jsonl,
                    json.dumps(
                        {
                            **event,
                            "event": "frame_extraction_completed",
                            "file_size_bytes": file_size(frame_path),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                scene_candidates.append(
                    _FrameCandidate(
                        scene=scene,
                        image_index=image_index,
                        timestamp=timestamp,
                        frame_role=frame_role,
                        path=frame_path,
                        relative_path=relative_path,
                        file_size_bytes=file_size(frame_path),
                    )
                )
                output_files.append(frame_path)

            scene_frame_refs, scene_report, scene_dropped = _materialize_scene_frames(context, scene_candidates)
            frame_refs.extend(scene_frame_refs)
            extraction_report.extend(scene_report)
            frames_dropped += scene_dropped

        write_models_jsonl(context.paths.frame_refs_jsonl, frame_refs)
        write_json_atomic(
            context.paths.frame_extraction_report,
            {
                "video_id": context.job.video_id,
                "scenes_count": len(scenes),
                "frames_count": len(frame_refs),
                "frames_extracted_total": len(extraction_report),
                "frames_dropped": frames_dropped,
                "dedup": _dedup_report(context),
                "frame_selection": _frame_selection_report(context),
                "events_jsonl": str(context.paths.frame_extraction_events_jsonl),
                "frames": extraction_report,
            },
        )
        append_stage_summary(
            context,
            self.name,
            "frame-extraction-summary",
            {
                "scenes_count": len(scenes),
                "frames_count": len(frame_refs),
                "frames_extracted_total": len(extraction_report),
                "frames_dropped": frames_dropped,
                "frames_per_scene_avg": round(len(frame_refs) / len(scenes), 3) if scenes else 0,
                "dedup": _dedup_report(context),
                "frame_selection": _frame_selection_report(context),
                "report_path": str(context.paths.frame_extraction_report),
                "events_jsonl": str(context.paths.frame_extraction_events_jsonl),
                "sample_frames": extraction_report[:10],
            },
        )
        return StageResult(
            output_files=output_files,
            metrics={
                "frames_count": len(frame_refs),
                "frames_extracted_total": len(extraction_report),
                "frames_dropped": frames_dropped,
                "scenes_count": len(scenes),
                "frames_per_scene_avg": round(len(frame_refs) / len(scenes), 3) if scenes else 0,
                "dedup_enabled": context.config.scene_detection.intra_scene_dedup,
            },
        )


def _materialize_scene_frames(
    context: StageContext,
    candidates: list[_FrameCandidate],
) -> tuple[list[FrameRef], list[dict[str, Any]], int]:
    kept_keys = {candidate.relative_path for candidate in candidates}
    stats_by_key: dict[str, FrameStat] = {}
    skips_by_key: dict[str, DedupSkip] = {}
    if context.config.scene_detection.intra_scene_dedup and len(candidates) > 1:
        stats = _frame_stats(candidates, context=context)
        stats_by_key = {stat.key: stat for stat in stats}
        kept, skips = dedupe_frames(
            stats,
            phash_max_distance=context.config.scene_detection.phash_max_distance,
            ssim_confirm=context.config.scene_detection.ssim_confirm,
            min_frames=context.config.scene_detection.min_frames_per_scene,
        )
        kept_keys = set(kept)
        skips_by_key = {skip.skipped_key: skip for skip in skips}

    frame_refs: list[FrameRef] = []
    report: list[dict[str, Any]] = []
    dropped = 0
    for candidate in candidates:
        included = candidate.relative_path in kept_keys
        entry = _frame_report_entry(candidate, included=included, stat=stats_by_key.get(candidate.relative_path))
        if included:
            frame_refs.append(_frame_ref(context, candidate))
        else:
            dropped += 1
            skip = skips_by_key[candidate.relative_path]
            entry["dedup"] = _dedup_skip_report(skip)
            _append_dropped_duplicate_event(context, candidate, skip)
        report.append(entry)
    return frame_refs, report, dropped


def _frame_stats(candidates: list[_FrameCandidate], *, context: StageContext) -> list[FrameStat]:
    protected_candidate = min(
        (candidate for candidate in candidates if candidate.frame_role == "keyframe"),
        key=lambda item: item.timestamp,
        default=candidates[0],
    )
    protected_key = protected_candidate.relative_path
    keep_gray = context.config.scene_detection.ssim_confirm is not None
    stats: list[FrameStat] = []
    for candidate in candidates:
        gray = load_gray(candidate.path)
        stats.append(
            FrameStat(
                key=candidate.relative_path,
                timestamp=candidate.timestamp,
                phash=phash(gray),
                sharpness=sharpness(gray),
                gray=gray if keep_gray else None,
                protected=candidate.relative_path == protected_key,
            )
        )
    return stats


def _frame_report_entry(candidate: _FrameCandidate, *, included: bool, stat: FrameStat | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "scene_id": candidate.scene.scene_id,
        "scene_index": candidate.scene.index,
        "image_index": candidate.image_index,
        "timestamp": round(candidate.timestamp, 3),
        "frame_role": candidate.frame_role,
        "path": candidate.relative_path,
        "file_size_bytes": candidate.file_size_bytes,
        "included_in_frame_refs": included,
    }
    if stat is not None:
        entry["sharpness"] = round(stat.sharpness, 3)
    return entry


def _dedup_skip_report(skip: DedupSkip) -> dict[str, Any]:
    return {
        "matched_frame": skip.matched_key,
        "matched_timestamp": round(skip.matched_timestamp, 3),
        "phash_distance": skip.phash_distance,
        "ssim": round(skip.ssim, 4) if skip.ssim is not None else None,
        "skip_reason": skip.skip_reason,
    }


def _append_dropped_duplicate_event(context: StageContext, candidate: _FrameCandidate, skip: DedupSkip) -> None:
    event = {
        "schema_version": 1,
        "run_id": context.run_id,
        "video_id": context.job.video_id,
        "stage": Stage09ExtractKeyframes.name,
        "event": "frame_extraction_dropped_duplicate",
        "scene_id": candidate.scene.scene_id,
        "scene_index": candidate.scene.index,
        "image_index": candidate.image_index,
        "timestamp": round(candidate.timestamp, 3),
        "frame_role": candidate.frame_role,
        "path": candidate.relative_path,
        **_dedup_skip_report(skip),
    }
    append_text(
        context.paths.frame_extraction_events_jsonl,
        json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _frame_ref(context: StageContext, candidate: _FrameCandidate) -> FrameRef:
    scene = candidate.scene
    return FrameRef(
        video_id=context.job.video_id,
        scene_id=scene.scene_id,
        start=scene.start,
        end=scene.end,
        path=candidate.relative_path,
        timestamp=candidate.timestamp,
        frame_role=candidate.frame_role,
        timestamp_url=build_timestamp_url(context.job.video_id, candidate.timestamp),
        source_refs=[youtube_source_ref(context.job.video_id, scene.start, scene.end, modality="visual")],
    )


def _scene_timestamps(scene: Scene, context: StageContext) -> list[tuple[float, str]]:
    timestamps: list[tuple[float, str]] = []
    seen: set[float] = set()
    target_count = _scene_frame_count(scene, context)
    keyframe_count = min(target_count, context.config.scene_detection.images_per_scene)
    for index, fraction in enumerate(_scene_frame_fractions(target_count)):
        frame_role = "keyframe" if index < keyframe_count else "sample"
        _append_scene_timestamp(timestamps, seen, scene, scene.start + (scene.duration * fraction), frame_role)
    return timestamps


def _scene_frame_count(scene: Scene, context: StageContext) -> int:
    max_frames = context.config.scene_detection.max_frames_per_scene
    if scene.duration <= 4:
        return min(1, max_frames)
    if scene.duration <= 8:
        return min(2, max_frames)
    duration_based = max(
        context.config.scene_detection.images_per_scene,
        ceil(scene.duration / context.config.scene_detection.extra_sample_every_seconds),
    )
    return max(1, min(max_frames, duration_based))


def _scene_frame_fractions(count: int) -> list[float]:
    if count <= 1:
        return [0.5]
    if count == 2:
        return [0.33, 0.67]
    if count == 3:
        return [0.15, 0.5, 0.85]
    return [(index + 0.5) / count for index in range(count)]


def _append_scene_timestamp(
    timestamps: list[tuple[float, str]],
    seen: set[float],
    scene: Scene,
    timestamp: float,
    frame_role: str,
) -> None:
    normalized = round(clamp(timestamp, scene.start, max(scene.end - 0.001, scene.start)), 3)
    if normalized in seen:
        return
    seen.add(normalized)
    timestamps.append((normalized, frame_role))


def _frame_selection_report(context: StageContext) -> dict[str, object]:
    return {
        "strategy": "adaptive_duration",
        "images_per_scene": context.config.scene_detection.images_per_scene,
        "extra_sample_every_seconds": context.config.scene_detection.extra_sample_every_seconds,
        "max_frames_per_scene": context.config.scene_detection.max_frames_per_scene,
    }


def _dedup_report(context: StageContext) -> dict[str, object]:
    return {
        "enabled": context.config.scene_detection.intra_scene_dedup,
        "phash_max_distance": context.config.scene_detection.phash_max_distance,
        "ssim_confirm": context.config.scene_detection.ssim_confirm,
        "min_frames_per_scene": context.config.scene_detection.min_frames_per_scene,
    }


def _report_frame_paths(context: StageContext) -> list[Path]:
    try:
        report = read_json(context.paths.frame_extraction_report)
    except Exception:
        return []
    return [_artifact_path(context, frame.get("path")) for frame in _report_frames(report) if frame.get("path")]


def _report_frames(report: Any) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        return []
    frames = report.get("frames")
    return [frame for frame in frames if isinstance(frame, dict)] if isinstance(frames, list) else []


def _artifact_exists(context: StageContext, raw_path: object) -> bool:
    if not raw_path:
        return False
    path = _artifact_path(context, raw_path)
    return path.exists() and path.stat().st_size > 0


def _artifact_path(context: StageContext, raw_path: object) -> Path:
    path = Path(str(raw_path))
    return path if path.is_absolute() else context.paths.job_dir / path


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique

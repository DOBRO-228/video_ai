from __future__ import annotations

import json
import shutil

from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Any

from style_kb.clients.image_metrics import (
    DedupSkip,
    FrameQualityMetrics,
    FrameStat,
    dedupe_frames,
    frame_quality,
    load_bgr,
    load_gray,
    phash,
    sharpness,
)
from style_kb.clients.media import ExtractedWindowFrame, extract_frame, extract_frame_window
from style_kb.errors import MediaToolError
from style_kb.models import FrameRef, Scene
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import load_scenes, relative_artifact_path, youtube_source_ref
from style_kb.stages.diagnostics import append_stage_summary, file_size
from style_kb.utils.files import append_text, read_json, write_json_atomic
from style_kb.utils.pydantic_io import read_models_jsonl, write_models_jsonl
from style_kb.utils.time import build_timestamp_url, clamp


@dataclass(slots=True)
class _FrameSlot:
    scene: Scene
    image_index: int
    planned_timestamp: float
    frame_role: str


@dataclass(slots=True)
class _QualityProbe:
    requested_timestamp: float
    timestamp: float
    offset_seconds: float
    path: Path | None
    relative_path: str | None
    selected: bool
    rejection_reason: str | None
    metrics: FrameQualityMetrics


@dataclass(slots=True)
class _FrameCandidate:
    scene: Scene
    image_index: int
    planned_timestamp: float
    timestamp: float
    frame_role: str
    path: Path
    relative_path: str
    file_size_bytes: int
    selection_decision: str | None = None
    quality_class: str | None = None
    quality_guard: str | None = None
    quality_drop_reason: str | None = None
    quality_metrics: FrameQualityMetrics | None = None
    quality_probes: list[_QualityProbe] | None = None


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
        quality_enabled = context.config.scene_detection.frame_quality.enabled
        frames_dropped_quality = report.get("frames_dropped_quality", 0)
        frames_dropped_dedup = report.get("frames_dropped_dedup", dropped_count)
        selected_frames_count = report.get("selected_frames_count", len(report_frames))
        summaries_valid = True
        if quality_enabled:
            selection_summary = report.get("selection_summary")
            quality_class_summary = report.get("quality_class_summary")
            summaries_valid = (
                isinstance(selection_summary, dict)
                and isinstance(quality_class_summary, dict)
                and sum(value for value in selection_summary.values() if isinstance(value, int)) == selected_frames_count
                and sum(value for value in quality_class_summary.values() if isinstance(value, int)) == selected_frames_count
            )
        return (
            bool(frame_refs)
            and report.get("frames_count") == len(frame_refs)
            and report.get("frames_count") == len(included_frames)
            and report.get("frames_extracted_total") == len(report_frames)
            and report.get("frames_dropped") == dropped_count
            and report.get("frames_dropped") == frames_dropped_quality + frames_dropped_dedup
            and report.get("selected_frames_count", len(report_frames)) == len(report_frames)
            and report.get("frame_selection") == _frame_selection_report(context)
            and report.get("frame_quality") == _frame_quality_report(context)
            and report.get("dedup") == _dedup_report(context)
            and summaries_valid
            and frame_ref_paths == included_paths
            and all(_artifact_exists(context, frame.get("path")) for frame in report_frames)
            and _quality_probe_artifacts_exist(context, report)
        )

    def run(self, context: StageContext) -> StageResult:
        scenes = load_scenes(context.paths.scenes_jsonl)
        quality_enabled = context.config.scene_detection.frame_quality.enabled
        probe_temp_dir = context.paths.frames_dir / ".quality_probes_tmp"
        preserved_probe_paths: list[Path] = []
        if quality_enabled:
            _reset_probe_temp_dir(probe_temp_dir)
            if context.config.scene_detection.frame_quality.keep_rejected_probe_files:
                _reset_quality_candidates_dir(context.paths.frames_dir / "quality_candidates")
        append_stage_summary(
            context,
            self.name,
            "frame-extraction-preflight",
            {
                "scenes_count": len(scenes),
                "images_per_scene": context.config.scene_detection.images_per_scene,
                "extra_sample_every_seconds": context.config.scene_detection.extra_sample_every_seconds,
                "max_frames_per_scene": context.config.scene_detection.max_frames_per_scene,
                "frame_quality": _frame_quality_report(context),
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
        frames_dropped_quality = 0
        frames_dropped_dedup = 0
        planned_slots_count = 0
        selected_frames_count = 0
        quality_summary = {
            "slots_count": 0,
            "probe_windows_extracted_total": 0,
            "probe_frames_extracted_total": 0,
        }
        selection_summary = {
            "planned_nearest_kept": 0,
            "replaced_by_higher_quality_probe": 0,
        }
        quality_class_summary = {
            "good": 0,
            "low_quality_kept": 0,
            "low_quality_dropped": 0,
        }

        for scene in scenes:
            scene_candidates: list[_FrameCandidate] = []
            slots = _scene_slots(scene, context)
            planned_slots_count += len(slots)
            selected_frames_count += len(slots)
            quality_summary["slots_count"] += len(slots)
            for slot in slots:
                if quality_enabled:
                    candidate, preserved_paths = _extract_quality_candidate(
                        context,
                        slot,
                        probe_temp_dir=probe_temp_dir,
                        selection_summary=selection_summary,
                        quality_summary=quality_summary,
                    )
                    preserved_probe_paths.extend(preserved_paths)
                    output_files.extend(preserved_paths)
                else:
                    candidate = _extract_legacy_candidate(context, slot)
                scene_candidates.append(candidate)
                output_files.append(candidate.path)

            _apply_quality_drop(context, scene_candidates)
            if quality_enabled:
                for candidate in scene_candidates:
                    if candidate.quality_class in quality_class_summary:
                        quality_class_summary[candidate.quality_class] += 1
            scene_frame_refs, scene_report, scene_dropped_quality, scene_dropped_dedup = _materialize_scene_frames(
                context, scene_candidates
            )
            frame_refs.extend(scene_frame_refs)
            extraction_report.extend(scene_report)
            frames_dropped_quality += scene_dropped_quality
            frames_dropped_dedup += scene_dropped_dedup
            frames_dropped += scene_dropped_quality + scene_dropped_dedup

        write_models_jsonl(context.paths.frame_refs_jsonl, frame_refs)
        write_json_atomic(
            context.paths.frame_extraction_report,
            {
                "video_id": context.job.video_id,
                "scenes_count": len(scenes),
                "planned_slots_count": planned_slots_count,
                "probe_windows_extracted_total": quality_summary["probe_windows_extracted_total"],
                "probe_frames_extracted_total": quality_summary["probe_frames_extracted_total"],
                "selected_frames_count": selected_frames_count,
                "frames_count": len(frame_refs),
                "frames_extracted_total": len(extraction_report),
                "frames_dropped": frames_dropped,
                "frames_dropped_quality": frames_dropped_quality,
                "frames_dropped_dedup": frames_dropped_dedup,
                "dedup": _dedup_report(context),
                "frame_selection": _frame_selection_report(context),
                "frame_quality": _frame_quality_report(context),
                "quality_summary": quality_summary,
                "selection_summary": selection_summary if quality_enabled else {},
                "quality_class_summary": quality_class_summary if quality_enabled else {},
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
                "frames_dropped_quality": frames_dropped_quality,
                "frames_dropped_dedup": frames_dropped_dedup,
                "frames_per_scene_avg": round(len(frame_refs) / len(scenes), 3) if scenes else 0,
                "dedup": _dedup_report(context),
                "frame_selection": _frame_selection_report(context),
                "frame_quality": _frame_quality_report(context),
                "quality_summary": quality_summary,
                "selection_summary": selection_summary if quality_enabled else {},
                "quality_class_summary": quality_class_summary if quality_enabled else {},
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
                "frames_dropped_quality": frames_dropped_quality,
                "frames_dropped_dedup": frames_dropped_dedup,
                "probe_windows_extracted_total": quality_summary["probe_windows_extracted_total"],
                "probe_frames_extracted_total": quality_summary["probe_frames_extracted_total"],
                "scenes_count": len(scenes),
                "frames_per_scene_avg": round(len(frame_refs) / len(scenes), 3) if scenes else 0,
                "dedup_enabled": context.config.scene_detection.intra_scene_dedup,
                "frame_quality_enabled": quality_enabled,
            },
        )


def _extract_legacy_candidate(context: StageContext, slot: _FrameSlot) -> _FrameCandidate:
    frame_path = context.paths.frame_path(slot.scene.index, slot.image_index)
    relative_path = relative_artifact_path(context.paths.job_dir, frame_path)
    _append_frame_extraction_event(
        context,
        slot,
        event_name="frame_extraction_started",
        timestamp=slot.planned_timestamp,
        path=relative_path,
    )
    extract_frame(
        context.paths.downloads_video_proxy,
        timestamp=slot.planned_timestamp,
        destination=frame_path,
        log_path=None,
        pipeline_logger=context.pipeline_logger,
        job_id=context.job.job_id,
        video_id=context.job.video_id,
        stage=Stage09ExtractKeyframes.name,
        ordinal=Stage09ExtractKeyframes.ordinal,
        text_log_streams=False,
    )
    if not frame_path.exists() or frame_path.stat().st_size == 0:
        raise MediaToolError("missing keyframe after extraction", error_code="missing_keyframes")
    _append_frame_extraction_event(
        context,
        slot,
        event_name="frame_extraction_completed",
        timestamp=slot.planned_timestamp,
        path=relative_path,
        file_size_bytes=file_size(frame_path),
    )
    return _FrameCandidate(
        scene=slot.scene,
        image_index=slot.image_index,
        planned_timestamp=slot.planned_timestamp,
        timestamp=slot.planned_timestamp,
        frame_role=slot.frame_role,
        path=frame_path,
        relative_path=relative_path,
        file_size_bytes=file_size(frame_path),
    )


def _extract_quality_candidate(
    context: StageContext,
    slot: _FrameSlot,
    *,
    probe_temp_dir: Path,
    selection_summary: dict[str, int],
    quality_summary: dict[str, int],
) -> tuple[_FrameCandidate, list[Path]]:
    frame_path = context.paths.frame_path(slot.scene.index, slot.image_index)
    relative_path = relative_artifact_path(context.paths.job_dir, frame_path)
    requested_timestamps = _probe_requested_timestamps(context, slot)
    window_start = min(requested_timestamps)
    window_end = max(requested_timestamps)
    prefix = f"scene_{slot.scene.index:06d}_{slot.image_index:02d}"
    _append_frame_extraction_event(
        context,
        slot,
        event_name="frame_extraction_started",
        timestamp=slot.planned_timestamp,
        path=relative_path,
        planned_timestamp=slot.planned_timestamp,
    )
    extracted_frames = extract_frame_window(
        context.paths.downloads_video_proxy,
        window_start=window_start,
        window_end=window_end,
        probe_step_seconds=context.config.scene_detection.frame_quality.probe_step_seconds,
        destination_dir=probe_temp_dir,
        filename_prefix=prefix,
        single_timestamp=slot.planned_timestamp,
        log_path=None,
        pipeline_logger=context.pipeline_logger,
        job_id=context.job.job_id,
        video_id=context.job.video_id,
        stage=Stage09ExtractKeyframes.name,
        ordinal=Stage09ExtractKeyframes.ordinal,
        text_log_streams=False,
    )

    quality_summary["probe_windows_extracted_total"] += 1
    quality_summary["probe_frames_extracted_total"] += len(extracted_frames)
    _append_quality_event(
        context,
        "frame_quality_window_extracted",
        slot,
        planned_timestamp=slot.planned_timestamp,
        window_start=window_start,
        window_end=window_end,
        probe_frames_count=len(extracted_frames),
    )

    probe_candidates = [
        _quality_probe_candidate(context, slot, frame, requested_timestamps) for frame in extracted_frames
    ]
    if not probe_candidates:
        raise MediaToolError("missing quality probes after extraction", error_code="missing_keyframes")

    planned_probe = min(probe_candidates, key=lambda probe: abs(probe.timestamp - slot.planned_timestamp))
    best_probe = max(probe_candidates, key=lambda probe: probe.metrics.score)
    ratio = context.config.scene_detection.frame_quality.prefer_planned_timestamp_score_ratio
    selected_probe = planned_probe if planned_probe.metrics.score >= best_probe.metrics.score * ratio else best_probe
    selection_decision = (
        "planned_nearest_kept" if selected_probe.path == planned_probe.path else "replaced_by_higher_quality_probe"
    )
    selection_summary[selection_decision] += 1

    if frame_path.exists():
        frame_path.unlink()
    selected_probe.path.replace(frame_path)
    if not frame_path.exists() or frame_path.stat().st_size == 0:
        raise MediaToolError("missing keyframe after quality selection", error_code="missing_keyframes")

    preserved_paths: list[Path] = []
    quality_probes: list[_QualityProbe] = []
    keep_rejected = context.config.scene_detection.frame_quality.keep_rejected_probe_files
    for probe in probe_candidates:
        selected = probe.path == selected_probe.path
        probe_path: Path | None = frame_path if selected else probe.path
        probe_relative_path: str | None = relative_path if selected else None
        rejection_reason = None if selected else "lower_quality_score"
        if not selected:
            if keep_rejected:
                probe_path = _preserve_rejected_probe(context, probe.path, slot, len(quality_probes) + 1)
                probe_relative_path = relative_artifact_path(context.paths.job_dir, probe_path)
                preserved_paths.append(probe_path)
            elif probe.path.exists():
                probe.path.unlink()
                probe_path = None
        quality_probes.append(
            _QualityProbe(
                requested_timestamp=probe.requested_timestamp,
                timestamp=probe.timestamp,
                offset_seconds=round(probe.requested_timestamp - slot.planned_timestamp, 3),
                path=probe_path,
                relative_path=probe_relative_path,
                selected=selected,
                rejection_reason=rejection_reason,
                metrics=probe.metrics,
            )
        )
        _append_quality_event(
            context,
            "frame_quality_probe_extracted",
            slot,
            requested_timestamp=probe.requested_timestamp,
            timestamp=probe.timestamp,
            quality_score=round(probe.metrics.score, 6),
            flags=list(probe.metrics.flags),
        )
        _append_quality_event(
            context,
            "frame_quality_probe_selected" if selected else "frame_quality_probe_rejected",
            slot,
            requested_timestamp=probe.requested_timestamp,
            timestamp=probe.timestamp,
            selected=selected,
            quality_score=round(probe.metrics.score, 6),
            flags=list(probe.metrics.flags),
        )

    _append_frame_extraction_event(
        context,
        slot,
        event_name="frame_extraction_completed",
        timestamp=selected_probe.timestamp,
        path=relative_path,
        planned_timestamp=slot.planned_timestamp,
        selection_decision=selection_decision,
        quality_class=_quality_class(selected_probe.metrics),
        file_size_bytes=file_size(frame_path),
    )
    return (
        _FrameCandidate(
            scene=slot.scene,
            image_index=slot.image_index,
            planned_timestamp=slot.planned_timestamp,
            timestamp=selected_probe.timestamp,
            frame_role=slot.frame_role,
            path=frame_path,
            relative_path=relative_path,
            file_size_bytes=file_size(frame_path),
            selection_decision=selection_decision,
            quality_class=_quality_class(selected_probe.metrics),
            quality_metrics=selected_probe.metrics,
            quality_probes=quality_probes,
        ),
        preserved_paths,
    )


@dataclass(frozen=True, slots=True)
class _ProbeCandidate:
    requested_timestamp: float
    timestamp: float
    path: Path
    metrics: FrameQualityMetrics


def _quality_probe_candidate(
    context: StageContext,
    slot: _FrameSlot,
    frame: ExtractedWindowFrame,
    requested_timestamps: list[float],
) -> _ProbeCandidate:
    requested_timestamp = min(requested_timestamps, key=lambda timestamp: abs(timestamp - frame.timestamp))
    metrics = frame_quality(
        load_gray(frame.path),
        grid_size=context.config.scene_detection.frame_quality.grid_size,
        central_region_weight=context.config.scene_detection.frame_quality.central_region_weight,
        bgr=load_bgr(frame.path),
    )
    return _ProbeCandidate(
        requested_timestamp=requested_timestamp,
        timestamp=frame.timestamp,
        path=frame.path,
        metrics=metrics,
    )


def _quality_class(metrics: FrameQualityMetrics) -> str:
    severe_flags = {"transition_like", "gray_transition_like", "high_central_blurred_tile_ratio"}
    return "low_quality_kept" if any(flag in severe_flags for flag in metrics.flags) else "good"


def _apply_quality_drop(context: StageContext, candidates: list[_FrameCandidate]) -> None:
    if not context.config.scene_detection.frame_quality.enabled or not context.config.scene_detection.frame_quality.drop_low_quality:
        return
    min_frames = max(
        context.config.scene_detection.min_frames_per_scene,
        context.config.scene_detection.frame_quality.min_quality_frames_per_scene,
    )
    remaining = len(candidates)
    keyframe_candidates = [candidate for candidate in candidates if candidate.frame_role == "keyframe"]
    for candidate in candidates:
        if candidate.quality_class != "low_quality_kept":
            continue
        if remaining <= min_frames:
            candidate.quality_guard = "min_scene_guard"
            continue
        if len(keyframe_candidates) <= 1 and candidate.frame_role == "keyframe":
            candidate.quality_guard = "only_keyframe_guard"
            continue
        candidate.quality_class = "low_quality_dropped"
        candidate.quality_drop_reason = "low_quality"
        remaining -= 1
        if candidate.frame_role == "keyframe":
            keyframe_candidates.remove(candidate)


def _preserve_rejected_probe(context: StageContext, probe_path: Path, slot: _FrameSlot, probe_index: int) -> Path:
    destination_dir = context.paths.frames_dir / "quality_candidates"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"scene_{slot.scene.index:06d}_{slot.image_index:02d}_probe_{probe_index:03d}.jpg"
    if destination.exists():
        destination.unlink()
    probe_path.replace(destination)
    return destination


def _materialize_scene_frames(
    context: StageContext,
    candidates: list[_FrameCandidate],
) -> tuple[list[FrameRef], list[dict[str, Any]], int, int]:
    dedupe_candidates = [candidate for candidate in candidates if candidate.quality_class != "low_quality_dropped"]
    kept_keys = {candidate.relative_path for candidate in dedupe_candidates}
    stats_by_key: dict[str, FrameStat] = {}
    skips_by_key: dict[str, DedupSkip] = {}
    if context.config.scene_detection.intra_scene_dedup and len(dedupe_candidates) > 1:
        stats = _frame_stats(dedupe_candidates, context=context)
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
    dropped_quality = 0
    dropped_dedup = 0
    for candidate in candidates:
        quality_dropped = candidate.quality_class == "low_quality_dropped"
        included = not quality_dropped and candidate.relative_path in kept_keys
        entry = _frame_report_entry(candidate, included=included, stat=stats_by_key.get(candidate.relative_path))
        if quality_dropped:
            dropped_quality += 1
            entry["quality_drop"] = {
                "skip_reason": candidate.quality_drop_reason or "low_quality",
                "quality_class": candidate.quality_class,
            }
            _append_quality_event(
                context,
                "frame_quality_low_quality_dropped",
                _FrameSlot(
                    scene=candidate.scene,
                    image_index=candidate.image_index,
                    planned_timestamp=candidate.planned_timestamp,
                    frame_role=candidate.frame_role,
                ),
                timestamp=candidate.timestamp,
                path=candidate.relative_path,
                quality_class=candidate.quality_class,
            )
        elif included:
            frame_refs.append(_frame_ref(context, candidate))
        else:
            dropped_dedup += 1
            skip = skips_by_key[candidate.relative_path]
            entry["dedup"] = _dedup_skip_report(skip)
            _append_dropped_duplicate_event(context, candidate, skip)
        report.append(entry)
    return frame_refs, report, dropped_quality, dropped_dedup


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
        "planned_timestamp": round(candidate.planned_timestamp, 3),
        "timestamp": round(candidate.timestamp, 3),
        "timestamp_delta_seconds": round(candidate.timestamp - candidate.planned_timestamp, 3),
        "frame_role": candidate.frame_role,
        "path": candidate.relative_path,
        "file_size_bytes": candidate.file_size_bytes,
        "included_in_frame_refs": included,
    }
    if candidate.selection_decision is not None:
        entry["selection_decision"] = candidate.selection_decision
    if candidate.quality_class is not None:
        entry["quality_class"] = candidate.quality_class
    if candidate.quality_guard is not None:
        entry["quality_guard"] = candidate.quality_guard
    if candidate.quality_metrics is not None:
        entry["quality"] = _quality_metrics_report(candidate.quality_metrics, compact=False)
    if candidate.quality_probes is not None:
        entry["quality_probes"] = [_quality_probe_report(probe) for probe in candidate.quality_probes]
    if stat is not None:
        entry["sharpness"] = round(stat.sharpness, 3)
    return entry


def _quality_metrics_report(metrics: FrameQualityMetrics, *, compact: bool) -> dict[str, Any]:
    report: dict[str, Any] = {
        "score": round(metrics.score, 6),
        "global_sharpness": round(metrics.global_sharpness, 3),
        "contrast": round(metrics.contrast, 3),
        "edge_density": round(metrics.edge_density, 6),
        "mean_saturation": round(metrics.mean_saturation, 3),
        "saturation_p90": round(metrics.saturation_p90, 3),
        "colorfulness": round(metrics.colorfulness, 3),
        "central_blurred_tile_ratio": round(metrics.central_blurred_tile_ratio, 6),
        "flags": list(metrics.flags),
    }
    if not compact:
        report.update(
            {
                "tile_sharpness_p10": round(metrics.tile_sharpness_p10, 3),
                "tile_sharpness_p50": round(metrics.tile_sharpness_p50, 3),
                "tile_sharpness_p90": round(metrics.tile_sharpness_p90, 3),
                "blurred_tile_ratio": round(metrics.blurred_tile_ratio, 6),
            }
        )
    return report


def _quality_probe_report(probe: _QualityProbe) -> dict[str, Any]:
    report: dict[str, Any] = {
        "requested_timestamp": round(probe.requested_timestamp, 3),
        "timestamp": round(probe.timestamp, 3),
        "timestamp_delta_seconds": round(probe.timestamp - probe.requested_timestamp, 3),
        "offset_seconds": round(probe.offset_seconds, 3),
        "path": probe.relative_path,
        "selected": probe.selected,
        "rejection_reason": probe.rejection_reason,
        "quality": _quality_metrics_report(probe.metrics, compact=not probe.selected),
    }
    return report


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


def _append_frame_extraction_event(
    context: StageContext,
    slot: _FrameSlot,
    *,
    event_name: str,
    timestamp: float,
    path: str,
    **extra: Any,
) -> None:
    event = {
        "schema_version": 1,
        "run_id": context.run_id,
        "video_id": context.job.video_id,
        "stage": Stage09ExtractKeyframes.name,
        "event": event_name,
        "scene_id": slot.scene.scene_id,
        "scene_index": slot.scene.index,
        "image_index": slot.image_index,
        "timestamp": round(timestamp, 3),
        "frame_role": slot.frame_role,
        "path": path,
        **extra,
    }
    append_text(
        context.paths.frame_extraction_events_jsonl,
        json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _append_quality_event(context: StageContext, event_name: str, slot: _FrameSlot, **extra: Any) -> None:
    event = {
        "schema_version": 1,
        "run_id": context.run_id,
        "video_id": context.job.video_id,
        "stage": Stage09ExtractKeyframes.name,
        "event": event_name,
        "scene_id": slot.scene.scene_id,
        "scene_index": slot.scene.index,
        "image_index": slot.image_index,
        "frame_role": slot.frame_role,
        **extra,
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


def _scene_slots(scene: Scene, context: StageContext) -> list[_FrameSlot]:
    return [
        _FrameSlot(scene=scene, image_index=image_index, planned_timestamp=timestamp, frame_role=frame_role)
        for image_index, (timestamp, frame_role) in enumerate(_scene_timestamps(scene, context), start=1)
    ]


def _scene_timestamps(scene: Scene, context: StageContext) -> list[tuple[float, str]]:
    timestamps: list[tuple[float, str]] = []
    seen: set[float] = set()
    target_count = _scene_frame_count(scene, context)
    keyframe_count = min(target_count, context.config.scene_detection.images_per_scene)
    for index, fraction in enumerate(_scene_frame_fractions(target_count)):
        frame_role = "keyframe" if index < keyframe_count else "sample"
        _append_scene_timestamp(context, timestamps, seen, scene, scene.start + (scene.duration * fraction), frame_role)
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
    context: StageContext,
    timestamps: list[tuple[float, str]],
    seen: set[float],
    scene: Scene,
    timestamp: float,
    frame_role: str,
) -> None:
    lower, upper = _scene_sampling_bounds(context, scene)
    normalized = round(clamp(timestamp, lower, upper), 3)
    if normalized in seen:
        return
    seen.add(normalized)
    timestamps.append((normalized, frame_role))


def _probe_requested_timestamps(context: StageContext, slot: _FrameSlot) -> list[float]:
    config = context.config.scene_detection.frame_quality
    requested: list[float] = []
    lower, upper = _scene_sampling_bounds(context, slot.scene)
    offset = -config.probe_window_seconds
    while offset <= config.probe_window_seconds + 0.0001:
        timestamp = clamp(
            slot.planned_timestamp + offset,
            lower,
            upper,
        )
        normalized = round(timestamp, 3)
        if normalized not in requested:
            requested.append(normalized)
        offset += config.probe_step_seconds
    planned = round(clamp(slot.planned_timestamp, lower, upper), 3)
    if planned not in requested:
        requested.append(planned)
    return sorted(requested)


def _scene_sampling_bounds(context: StageContext, scene: Scene) -> tuple[float, float]:
    inset = context.config.scene_detection.keyframe_edge_inset_seconds
    if inset > 0 and scene.duration > 2 * inset:
        lower = scene.start + inset
        upper = scene.end - inset
    else:
        lower = scene.start
        upper = max(scene.end - 0.001, scene.start)
    return lower, max(upper, lower)


def _frame_selection_report(context: StageContext) -> dict[str, object]:
    return {
        "strategy": "adaptive_duration",
        "images_per_scene": context.config.scene_detection.images_per_scene,
        "extra_sample_every_seconds": context.config.scene_detection.extra_sample_every_seconds,
        "max_frames_per_scene": context.config.scene_detection.max_frames_per_scene,
        "keyframe_edge_inset_seconds": context.config.scene_detection.keyframe_edge_inset_seconds,
    }


def _frame_quality_report(context: StageContext) -> dict[str, object]:
    config = context.config.scene_detection.frame_quality
    return {
        "enabled": config.enabled,
        "extraction_backend": "ffmpeg_window",
        "probe_window_seconds": config.probe_window_seconds,
        "probe_step_seconds": config.probe_step_seconds,
        "prefer_planned_timestamp_score_ratio": config.prefer_planned_timestamp_score_ratio,
        "drop_low_quality": config.drop_low_quality,
        "min_quality_frames_per_scene": config.min_quality_frames_per_scene,
        "central_region_weight": config.central_region_weight,
        "grid_size": config.grid_size,
        "keep_rejected_probe_files": config.keep_rejected_probe_files,
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
    paths = [_artifact_path(context, frame.get("path")) for frame in _report_frames(report) if frame.get("path")]
    for frame in _report_frames(report):
        for probe in frame.get("quality_probes") or []:
            if isinstance(probe, dict) and probe.get("path"):
                paths.append(_artifact_path(context, probe.get("path")))
    return paths


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


def _quality_probe_artifacts_exist(context: StageContext, report: Any) -> bool:
    frame_quality = report.get("frame_quality") if isinstance(report, dict) else None
    keep_rejected = isinstance(frame_quality, dict) and frame_quality.get("keep_rejected_probe_files") is True
    if not keep_rejected:
        return True
    for frame in _report_frames(report):
        for probe in frame.get("quality_probes") or []:
            if isinstance(probe, dict) and probe.get("path") and not _artifact_exists(context, probe.get("path")):
                return False
    return True


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


def _reset_probe_temp_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _reset_quality_candidates_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)

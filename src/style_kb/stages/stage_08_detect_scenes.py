from __future__ import annotations

from pathlib import Path
from typing import Any

from scenedetect import SceneManager, open_video
from scenedetect.detectors import AdaptiveDetector

from style_kb.clients.media import duration_seconds, fps
from style_kb.clients.scene_refinement import PaletteRefinementResult, SceneBoundary, refine_scene_boundaries
from style_kb.errors import ConfigError, MediaToolError
from style_kb.models import Scene
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import load_scenes, youtube_source_ref
from style_kb.stages.diagnostics import append_stage_summary
from style_kb.utils.files import read_json, write_json_atomic
from style_kb.utils.ids import scene_id
from style_kb.utils.pydantic_io import write_models_jsonl
from style_kb.utils.time import build_timestamp_url


class Stage08DetectScenes(Stage):
    name = "08_detect_scenes"
    ordinal = 8

    def input_files(self, context: StageContext) -> list:
        return [context.paths.downloads_video_proxy, context.paths.downloads_video_ffprobe]

    def output_files(self, context: StageContext) -> list:
        return [context.paths.scenes_jsonl, context.paths.scene_detection_report]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.scenes_jsonl.exists() or not context.paths.scene_detection_report.exists():
            return False
        try:
            scenes = load_scenes(context.paths.scenes_jsonl)
            report = read_json(context.paths.scene_detection_report)
            ffprobe_payload = read_json(context.paths.downloads_video_ffprobe)
            frame_rate = fps(ffprobe_payload)
        except Exception:
            return False
        return _valid_scenes(context, scenes) and _valid_report(context, report, scenes=scenes, frame_rate=frame_rate)

    def run(self, context: StageContext) -> StageResult:
        if context.config.scene_detection.detector != "adaptive":
            raise ConfigError(f"unsupported scene_detection.detector: {context.config.scene_detection.detector}")
        ffprobe_payload = read_json(context.paths.downloads_video_ffprobe)
        video_duration = duration_seconds(ffprobe_payload)
        frame_rate = fps(ffprobe_payload)
        source_width, source_height = _video_dimensions(ffprobe_payload)
        video = open_video(str(context.paths.downloads_video_proxy))
        scene_manager = SceneManager()
        min_scene_len_frames = _min_scene_len_frames(context, frame_rate)
        scene_manager.add_detector(AdaptiveDetector(min_scene_len=min_scene_len_frames))
        scene_manager.detect_scenes(video)
        detected = scene_manager.get_scene_list()

        fallback_used = not detected
        fallback_scene_count = 0
        if not detected:
            source_boundaries = _fallback_boundaries(context, video_duration=video_duration, frame_rate=frame_rate)
            fallback_scene_count = len(source_boundaries)
        else:
            source_boundaries = _detected_boundaries(detected)

        refinement = refine_scene_boundaries(
            context.paths.downloads_video_proxy,
            source_boundaries,
            config=context.config.scene_detection.palette_boundary_refinement,
            frame_rate=frame_rate,
            source_width=source_width,
            source_height=source_height,
        )
        scenes = _scenes_from_boundaries(context, refinement.boundaries, frame_rate=frame_rate)
        report = _scene_detection_report(
            context,
            ffprobe_payload=ffprobe_payload,
            frame_rate=frame_rate,
            video_duration=video_duration,
            video_path=context.paths.downloads_video_proxy,
            detected_scene_count=len(detected),
            source_scene_count=len(source_boundaries),
            fallback_used=fallback_used,
            fallback_scene_count=fallback_scene_count,
            refinement=refinement,
            scenes=scenes,
        )

        write_models_jsonl(context.paths.scenes_jsonl, scenes)
        write_json_atomic(context.paths.scene_detection_report, report)
        append_stage_summary(
            context,
            self.name,
            "scene-detection-summary",
            {
                "detector": "AdaptiveDetector",
                "min_scene_len_seconds": context.config.scene_detection.min_scene_len_seconds,
                "min_scene_len_frames": min_scene_len_frames,
                "video_fps": frame_rate,
                "video_duration": video_duration,
                "detected_scene_count": len(detected),
                "source_scene_count": len(source_boundaries),
                "scenes_count": len(scenes),
                "fallback_used": fallback_used,
                "fallback_scene_count": fallback_scene_count,
                "palette_boundary_refinement": report["palette_boundary_refinement"],
                "boundaries_adjusted": len(report["boundary_adjustments"]),
                "scenes_path": str(context.paths.scenes_jsonl),
                "report_path": str(context.paths.scene_detection_report),
            },
        )
        return StageResult(
            output_files=self.output_files(context),
            metrics={
                "scenes_count": len(scenes),
                "source_scene_count": len(source_boundaries),
                "boundaries_adjusted": len(report["boundary_adjustments"]),
                "fallback_used": fallback_used,
                "fps": frame_rate,
            },
        )


def _detected_boundaries(detected: list[tuple[Any, Any]]) -> list[SceneBoundary]:
    boundaries: list[SceneBoundary] = []
    for index, (start_tc, end_tc) in enumerate(detected):
        start_frame = start_tc.get_frames()
        end_frame = end_tc.get_frames()
        if end_frame > start_frame:
            boundaries.append(SceneBoundary(source_scene_index=index, start_frame=start_frame, end_frame=end_frame))
    return boundaries


def _fallback_boundaries(context: StageContext, *, video_duration: float, frame_rate: float) -> list[SceneBoundary]:
    final_frame = max(1, int(round(video_duration * frame_rate)))
    step_frames = max(1, int(round(context.config.scene_detection.fallback_scene_seconds * frame_rate)))
    boundaries: list[SceneBoundary] = []
    start_frame = 0
    index = 0
    while start_frame < final_frame:
        end_frame = min(final_frame, start_frame + step_frames)
        if end_frame > start_frame:
            boundaries.append(SceneBoundary(source_scene_index=index, start_frame=start_frame, end_frame=end_frame))
            index += 1
        start_frame = end_frame
    return boundaries


def _scenes_from_boundaries(context: StageContext, boundaries: list[SceneBoundary], *, frame_rate: float) -> list[Scene]:
    return [
        _scene_from_frames(
            context,
            index=index,
            start_frame=boundary.start_frame,
            end_frame=boundary.end_frame,
            frame_rate=frame_rate,
        )
        for index, boundary in enumerate(sorted(boundaries, key=lambda item: (item.start_frame, item.end_frame)))
        if boundary.end_frame > boundary.start_frame
    ]


def _scene_from_frames(
    context: StageContext,
    *,
    index: int,
    start_frame: int,
    end_frame: int,
    frame_rate: float,
) -> Scene:
    start = round(start_frame / frame_rate, 3)
    end = round(end_frame / frame_rate, 3)
    return Scene(
        scene_id=scene_id(context.job.video_id, index),
        video_id=context.job.video_id,
        index=index,
        start=start,
        end=end,
        start_frame=start_frame,
        end_frame=end_frame,
        duration=round(end - start, 3),
        timestamp_url=build_timestamp_url(context.job.video_id, start),
        source_refs=[youtube_source_ref(context.job.video_id, start, end, modality="visual")],
    )


def _scene_detection_report(
    context: StageContext,
    *,
    ffprobe_payload: dict[str, Any],
    frame_rate: float,
    video_duration: float,
    video_path: Path,
    detected_scene_count: int,
    source_scene_count: int,
    fallback_used: bool,
    fallback_scene_count: int,
    refinement: PaletteRefinementResult,
    scenes: list[Scene],
) -> dict[str, Any]:
    refinement_report = dict(refinement.report)
    boundary_adjustments = refinement_report.pop("boundary_adjustments", [])
    return {
        "schema_version": 1,
        "video_id": context.job.video_id,
        "stage": Stage08DetectScenes.name,
        "inputs": {
            "video_proxy": str(video_path),
            "video_proxy_size_bytes": video_path.stat().st_size if video_path.exists() else None,
            "video_duration": video_duration,
            "video_fps": frame_rate,
            "video_dimensions": list(_video_dimensions(ffprobe_payload)),
        },
        "detector": _detector_report(context, frame_rate),
        "detected_scene_count": detected_scene_count,
        "source_scene_count": source_scene_count,
        "scenes_count": len(scenes),
        "fallback_used": fallback_used,
        "fallback_scene_seconds": context.config.scene_detection.fallback_scene_seconds,
        "fallback_scene_count": fallback_scene_count,
        "palette_boundary_refinement": refinement_report,
        "boundary_adjustments": boundary_adjustments,
        "duration_stats": _duration_stats(scenes),
        "warnings": _coverage_warnings(scenes),
    }


def _detector_report(context: StageContext, frame_rate: float) -> dict[str, Any]:
    return {
        "configured": context.config.scene_detection.detector,
        "actual": "AdaptiveDetector",
        "min_scene_len_seconds": context.config.scene_detection.min_scene_len_seconds,
        "min_scene_len_frames": _min_scene_len_frames(context, frame_rate),
    }


def _palette_refinement_config_report(context: StageContext) -> dict[str, Any]:
    config = context.config.scene_detection.palette_boundary_refinement
    return {
        "enabled": config.enabled,
        "sample_step_seconds": config.sample_step_seconds,
        "min_scene_duration_seconds": config.min_scene_duration_seconds,
        "max_boundary_shift_seconds": config.max_boundary_shift_seconds,
        "min_segment_seconds": config.min_segment_seconds,
        "stable_window_seconds": config.stable_window_seconds,
        "edge_guard_seconds": config.edge_guard_seconds,
        "min_saturation_delta": config.min_saturation_delta,
        "min_colorfulness_delta": config.min_colorfulness_delta,
        "min_histogram_distance": config.min_histogram_distance,
        "min_confidence": config.min_confidence,
    }


def _valid_report(context: StageContext, report: Any, *, scenes: list[Scene], frame_rate: float) -> bool:
    if not isinstance(report, dict):
        return False
    refinement_report = report.get("palette_boundary_refinement")
    if not isinstance(refinement_report, dict):
        return False
    return (
        report.get("schema_version") == 1
        and report.get("video_id") == context.job.video_id
        and report.get("stage") == Stage08DetectScenes.name
        and report.get("detector") == _detector_report(context, frame_rate)
        and report.get("scenes_count") == len(scenes)
        and all(refinement_report.get(key) == value for key, value in _palette_refinement_config_report(context).items())
        and refinement_report.get("output_scenes_count") == len(scenes)
        and isinstance(report.get("boundary_adjustments"), list)
    )


def _valid_scenes(context: StageContext, scenes: list[Scene]) -> bool:
    if not scenes:
        return False
    for index, scene in enumerate(scenes):
        if (
            scene.video_id != context.job.video_id
            or scene.index != index
            or scene.scene_id != scene_id(context.job.video_id, index)
            or scene.start_frame >= scene.end_frame
            or scene.start >= scene.end
        ):
            return False
        if index > 0 and scenes[index - 1].end_frame != scene.start_frame:
            return False
    return True


def _duration_stats(scenes: list[Scene]) -> dict[str, float]:
    durations = [scene.duration for scene in scenes]
    if not durations:
        return {"min": 0.0, "max": 0.0, "avg": 0.0}
    return {
        "min": round(min(durations), 3),
        "max": round(max(durations), 3),
        "avg": round(sum(durations) / len(durations), 3),
    }


def _coverage_warnings(scenes: list[Scene]) -> list[str]:
    warnings: list[str] = []
    for previous, current in zip(scenes, scenes[1:]):
        if previous.end_frame != current.start_frame:
            warnings.append(f"coverage_gap_or_overlap:{previous.index}->{current.index}")
    return warnings


def _min_scene_len_frames(context: StageContext, frame_rate: float) -> int:
    return max(1, int(round(context.config.scene_detection.min_scene_len_seconds * frame_rate)))


def _video_dimensions(ffprobe_payload: dict[str, Any]) -> tuple[int, int]:
    video_streams = [stream for stream in ffprobe_payload.get("streams", []) if stream.get("codec_type") == "video"]
    if not video_streams:
        raise MediaToolError("ffprobe output has no video stream", error_code="ffprobe_video_stream_missing")
    width = video_streams[0].get("width")
    height = video_streams[0].get("height")
    if not width or not height:
        raise MediaToolError("ffprobe output has no video dimensions", error_code="ffprobe_dimensions_missing")
    return int(width), int(height)

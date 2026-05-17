from __future__ import annotations

from style_kb.clients.media import extract_frame
from style_kb.errors import MediaToolError
from style_kb.models import FrameRef
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import load_scenes, relative_artifact_path, youtube_source_ref
from style_kb.utils.pydantic_io import read_models_jsonl, write_models_jsonl
from style_kb.utils.time import build_timestamp_url, clamp


class Stage09ExtractKeyframes(Stage):
    name = "09_extract_keyframes"
    ordinal = 9

    def input_files(self, context: StageContext) -> list:
        return [context.paths.downloads_video_proxy, context.paths.scenes_jsonl]

    def output_files(self, context: StageContext) -> list:
        outputs = [context.paths.frame_refs_jsonl]
        if context.paths.frame_refs_jsonl.exists():
            for frame in read_models_jsonl(context.paths.frame_refs_jsonl, FrameRef):
                outputs.append(context.paths.job_dir / frame.path)
        return outputs

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.frame_refs_jsonl.exists():
            return False
        frame_refs = read_models_jsonl(context.paths.frame_refs_jsonl, FrameRef)
        return bool(frame_refs) and all((context.paths.job_dir / frame.path).exists() for frame in frame_refs)

    def run(self, context: StageContext) -> StageResult:
        scenes = load_scenes(context.paths.scenes_jsonl)
        frame_refs: list[FrameRef] = []
        output_files = [context.paths.frame_refs_jsonl]

        for scene in scenes:
            for image_index, (timestamp, frame_role) in enumerate(_scene_timestamps(scene, context), start=1):
                frame_path = context.paths.frame_path(scene.index, image_index)
                extract_frame(
                    context.paths.downloads_video_proxy,
                    timestamp=timestamp,
                    destination=frame_path,
                    log_path=context.paths.stage_log(f"{self.name}.scene_{scene.index:06d}"),
                )
                if not frame_path.exists() or frame_path.stat().st_size == 0:
                    raise MediaToolError("missing keyframe after extraction", error_code="missing_keyframes")
                output_files.append(frame_path)
                frame_refs.append(
                    FrameRef(
                        video_id=context.job.video_id,
                        scene_id=scene.scene_id,
                        start=scene.start,
                        end=scene.end,
                        path=relative_artifact_path(context.paths.job_dir, frame_path),
                        timestamp=timestamp,
                        frame_role=frame_role,
                        timestamp_url=build_timestamp_url(context.job.video_id, timestamp),
                        source_refs=[youtube_source_ref(context.job.video_id, scene.start, scene.end, modality="visual")],
                    )
                )

        write_models_jsonl(context.paths.frame_refs_jsonl, frame_refs)
        return StageResult(output_files=output_files, metrics={"frames_count": len(frame_refs)})


def _scene_timestamps(scene, context: StageContext) -> list[tuple[float, str]]:
    base_fractions = [0.15, 0.5, 0.85]
    timestamps: list[tuple[float, str]] = []
    for fraction in base_fractions[: context.config.scene_detection.images_per_scene]:
        timestamps.append((round(scene.start + (scene.duration * fraction), 3), "keyframe"))

    extra_every = context.config.scene_detection.extra_sample_every_seconds
    sample = scene.start + extra_every
    while sample < scene.end:
        timestamps.append((round(sample, 3), "sample"))
        sample += extra_every

    unique: list[tuple[float, str]] = []
    seen = set()
    for timestamp, frame_role in sorted(timestamps, key=lambda item: item[0]):
        normalized = round(clamp(timestamp, scene.start, max(scene.end - 0.001, scene.start)), 3)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append((normalized, frame_role))
    return unique

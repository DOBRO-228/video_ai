from __future__ import annotations

from scenedetect import SceneManager, open_video
from scenedetect.detectors import AdaptiveDetector

from style_kb.clients.media import duration_seconds, ffprobe_json, fps
from style_kb.models import Scene
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import load_scenes, youtube_source_ref
from style_kb.stages.diagnostics import append_stage_summary
from style_kb.utils.files import read_json
from style_kb.utils.ids import scene_id
from style_kb.utils.pydantic_io import write_models_jsonl
from style_kb.utils.time import build_timestamp_url


class Stage08DetectScenes(Stage):
    name = "08_detect_scenes"
    ordinal = 8

    def input_files(self, context: StageContext) -> list:
        return [context.paths.downloads_video_proxy, context.paths.downloads_video_ffprobe]

    def output_files(self, context: StageContext) -> list:
        return [context.paths.scenes_jsonl]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.scenes_jsonl.exists():
            return False
        return bool(load_scenes(context.paths.scenes_jsonl))

    def run(self, context: StageContext) -> StageResult:
        ffprobe_payload = read_json(context.paths.downloads_video_ffprobe)
        video_duration = duration_seconds(ffprobe_payload)
        frame_rate = fps(ffprobe_payload)
        video = open_video(str(context.paths.downloads_video_proxy))
        scene_manager = SceneManager()
        scene_manager.add_detector(
            AdaptiveDetector(min_scene_len=max(1, int(round(context.config.scene_detection.min_scene_len_seconds * frame_rate))))
        )
        scene_manager.detect_scenes(video)
        detected = scene_manager.get_scene_list()

        scenes: list[Scene] = []
        fallback_used = not detected
        if not detected:
            scenes.append(
                _scene_from_range(
                    context=context,
                    index=0,
                    start=0.0,
                    end=video_duration,
                    frame_rate=frame_rate,
                )
            )
        else:
            for index, (start_tc, end_tc) in enumerate(detected):
                scenes.append(
                    Scene(
                        scene_id=scene_id(context.job.video_id, index),
                        video_id=context.job.video_id,
                        index=index,
                        start=round(start_tc.get_seconds(), 3),
                        end=round(end_tc.get_seconds(), 3),
                        start_frame=start_tc.get_frames(),
                        end_frame=end_tc.get_frames(),
                        duration=round(end_tc.get_seconds() - start_tc.get_seconds(), 3),
                        timestamp_url=build_timestamp_url(context.job.video_id, start_tc.get_seconds()),
                        source_refs=[
                            youtube_source_ref(
                                context.job.video_id,
                                start_tc.get_seconds(),
                                end_tc.get_seconds(),
                                modality="visual",
                            )
                        ],
                    )
                )

        write_models_jsonl(context.paths.scenes_jsonl, scenes)
        min_scene_len_frames = max(1, int(round(context.config.scene_detection.min_scene_len_seconds * frame_rate)))
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
                "scenes_count": len(scenes),
                "fallback_used": fallback_used,
                "scenes_path": str(context.paths.scenes_jsonl),
            },
        )
        return StageResult(
            output_files=self.output_files(context),
            metrics={"scenes_count": len(scenes), "fallback_used": fallback_used, "fps": frame_rate},
        )


def _scene_from_range(context: StageContext, *, index: int, start: float, end: float, frame_rate: float) -> Scene:
    return Scene(
        scene_id=scene_id(context.job.video_id, index),
        video_id=context.job.video_id,
        index=index,
        start=round(start, 3),
        end=round(end, 3),
        start_frame=int(round(start * frame_rate)),
        end_frame=int(round(end * frame_rate)),
        duration=round(end - start, 3),
        timestamp_url=build_timestamp_url(context.job.video_id, start),
        source_refs=[youtube_source_ref(context.job.video_id, start, end, modality="visual")],
    )

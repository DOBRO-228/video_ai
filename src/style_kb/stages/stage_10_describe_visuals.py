from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

from style_kb.clients.openai_vision import OpenAIVisionClient
from style_kb.models import VisualEvent
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import load_frame_refs, load_scenes, load_speech_segments, load_visual_events, youtube_source_ref
from style_kb.utils.collections import stable_unique
from style_kb.utils.ids import visual_event_id
from style_kb.utils.pydantic_io import write_models_jsonl
from style_kb.utils.time import build_timestamp_url


class Stage10DescribeVisuals(Stage):
    name = "10_describe_visuals"
    ordinal = 10

    def input_files(self, context: StageContext) -> list:
        return [context.paths.frame_refs_jsonl, context.paths.scenes_jsonl, context.paths.stt_speech_segments]

    def output_files(self, context: StageContext) -> list:
        outputs = [context.paths.visual_events_jsonl]
        if context.paths.visual_raw_dir.exists():
            outputs.extend(sorted(context.paths.visual_raw_dir.glob("*.json")))
        return outputs

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.visual_events_jsonl.exists():
            return False
        scenes = load_scenes(context.paths.scenes_jsonl) if context.paths.scenes_jsonl.exists() else []
        visual_events = load_visual_events(context.paths.visual_events_jsonl)
        return bool(visual_events) and (not scenes or len(visual_events) == len(scenes))

    def run(self, context: StageContext) -> StageResult:
        prompt_path = Path(__file__).resolve().parents[1] / "prompts" / context.config.vision.prompt_file
        prompt_text = prompt_path.read_text(encoding="utf-8")
        client = OpenAIVisionClient(os.environ.get("OPENAI_API_KEY"), model=context.config.vision.model)

        scenes = load_scenes(context.paths.scenes_jsonl)
        frame_refs = load_frame_refs(context.paths.frame_refs_jsonl)
        speech_segments = load_speech_segments(context.paths.stt_speech_segments)
        frame_map: dict[str, list] = defaultdict(list)
        for frame in frame_refs:
            frame_map[frame.scene_id].append(frame)

        visual_events: list[VisualEvent] = []
        output_files = [context.paths.visual_events_jsonl]
        for scene in scenes:
            scene_frames = frame_map.get(scene.scene_id, [])
            image_paths = [context.paths.job_dir / frame.path for frame in scene_frames]
            transcript_context = _nearby_transcript(scene.start, scene.end, speech_segments, context)
            raw_output_path = context.paths.visual_raw_scene(scene.scene_id)
            payload = client.describe_scene(
                system_prompt=prompt_text,
                transcript_context=transcript_context,
                image_paths=image_paths,
                detail=context.config.vision.detail,
                raw_output_path=raw_output_path,
            )
            output_files.append(raw_output_path)
            visual_events.append(
                VisualEvent(
                    visual_event_id=visual_event_id(context.job.video_id, scene.start, scene.end),
                    video_id=context.job.video_id,
                    scene_id=scene.scene_id,
                    start=scene.start,
                    end=scene.end,
                    timestamp_url=build_timestamp_url(context.job.video_id, scene.start),
                    frames=scene_frames,
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
            )

        write_models_jsonl(context.paths.visual_events_jsonl, visual_events)
        return StageResult(output_files=output_files, metrics={"visual_events_count": len(visual_events)})


def _nearby_transcript(start: float, end: float, speech_segments, context: StageContext) -> str:
    before = context.config.vision.transcript_context_before_seconds
    after = context.config.vision.transcript_context_after_seconds
    lower = max(0.0, start - before)
    upper = end + after
    texts = [segment.text for segment in speech_segments if segment.end >= lower and segment.start <= upper]
    return "\n".join(texts)

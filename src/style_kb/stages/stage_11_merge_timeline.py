from __future__ import annotations

from style_kb.clients.media import duration_seconds
from style_kb.errors import MediaToolError, StageExecutionError
from style_kb.models import SourceRef, TimelineEvent
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import (
    load_scenes,
    load_speech_segments,
    load_timeline_events,
    load_video_info,
    load_visual_events,
    read_payload,
    youtube_source_ref,
)
from style_kb.utils.collections import stable_unique
from style_kb.utils.ids import timeline_event_id
from style_kb.utils.pydantic_io import write_models_jsonl
from style_kb.utils.time import build_timestamp_url


class Stage11MergeTimeline(Stage):
    name = "11_merge_timeline"
    ordinal = 11

    def input_files(self, context: StageContext) -> list:
        return [
            context.paths.metadata_video_info,
            context.paths.stt_speech_segments,
            context.paths.visual_events_jsonl,
            context.paths.scenes_jsonl,
            context.paths.downloads_audio_ffprobe,
            context.paths.downloads_video_ffprobe,
        ]

    def output_files(self, context: StageContext) -> list:
        return [context.paths.timeline_events_jsonl]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.timeline_events_jsonl.exists():
            return False
        return bool(load_timeline_events(context.paths.timeline_events_jsonl))

    def run(self, context: StageContext) -> StageResult:
        audio_duration = duration_seconds(read_payload(context.paths.downloads_audio_ffprobe))
        video_duration = duration_seconds(read_payload(context.paths.downloads_video_ffprobe))
        if abs(audio_duration - video_duration) > 1.0:
            raise MediaToolError("audio/video duration mismatch exceeds 1.0s", error_code="audio_video_duration_mismatch")

        video_info = load_video_info(context.paths.metadata_video_info)
        speech_segments = load_speech_segments(context.paths.stt_speech_segments)
        visual_events = {event.scene_id: event for event in load_visual_events(context.paths.visual_events_jsonl)}
        scenes = load_scenes(context.paths.scenes_jsonl)

        timeline_events: list[TimelineEvent] = []
        for scene in scenes:
            visual_event = visual_events.get(scene.scene_id)
            if visual_event is None:
                raise StageExecutionError(
                    f"missing visual event for scene {scene.scene_id}",
                    error_code="missing_visual_event",
                )
            overlapping_segments = [segment for segment in speech_segments if segment.end >= scene.start and segment.start <= scene.end]
            speech_text = " ".join(segment.text for segment in overlapping_segments if segment.text).strip()
            source_refs: list[SourceRef] = [youtube_source_ref(scene.video_id, scene.start, scene.end, title=video_info.title)]
            source_refs.extend(
                SourceRef(
                    type="audio",
                    url=segment.timestamp_url,
                    start=segment.start,
                    end=segment.end,
                    modality="audio",
                )
                for segment in overlapping_segments
            )
            source_refs.append(SourceRef(type="visual", url=visual_event.timestamp_url, start=scene.start, end=scene.end, modality="visual"))
            timeline_events.append(
                TimelineEvent(
                    event_id=timeline_event_id(context.job.video_id, scene.start, scene.end),
                    video_id=context.job.video_id,
                    title=video_info.title,
                    channel=video_info.channel,
                    start=scene.start,
                    end=scene.end,
                    timestamp_url=build_timestamp_url(context.job.video_id, scene.start),
                    speech_text=speech_text,
                    visual_summary=visual_event.visual_summary,
                    on_screen_text=stable_unique(visual_event.on_screen_text),
                    items=stable_unique(visual_event.items),
                    colors=stable_unique(visual_event.colors),
                    topics=stable_unique(visual_event.style_topics),
                    scene_id=scene.scene_id,
                    speech_segment_ids=[segment.segment_id for segment in overlapping_segments],
                    source_refs=source_refs,
                )
            )

        write_models_jsonl(context.paths.timeline_events_jsonl, timeline_events)
        return StageResult(output_files=self.output_files(context), metrics={"timeline_events_count": len(timeline_events)})

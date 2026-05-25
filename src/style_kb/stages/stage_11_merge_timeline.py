from __future__ import annotations

import re

from style_kb.clients.media import duration_seconds
from style_kb.errors import MediaToolError, StageExecutionError
from style_kb.models import Scene, SourceRef, SpeechSegment, SpeechToken, TimelineEvent, VideoInfo, VisualEvent
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import (
    load_scenes,
    load_speech_segments,
    load_speech_tokens,
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
            context.paths.stt_speech_tokens,
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
        try:
            actual_events = load_timeline_events(context.paths.timeline_events_jsonl)
        except Exception:
            return False
        if not actual_events:
            return False
        try:
            expected_events = _build_timeline_events(context)
        except Exception:
            return False
        if len(actual_events) != len(expected_events):
            return False
        return all(
            actual.model_dump(mode="json") == expected.model_dump(mode="json")
            for actual, expected in zip(actual_events, expected_events)
        )

    def run(self, context: StageContext) -> StageResult:
        timeline_events = _build_timeline_events(context)
        write_models_jsonl(context.paths.timeline_events_jsonl, timeline_events)
        return StageResult(output_files=self.output_files(context), metrics={"timeline_events_count": len(timeline_events)})


def _build_timeline_events(context: StageContext) -> list[TimelineEvent]:
    audio_duration = duration_seconds(read_payload(context.paths.downloads_audio_ffprobe))
    video_duration = duration_seconds(read_payload(context.paths.downloads_video_ffprobe))
    if abs(audio_duration - video_duration) > 1.0:
        raise MediaToolError("audio/video duration mismatch exceeds 1.0s", error_code="audio_video_duration_mismatch")

    video_info = load_video_info(context.paths.metadata_video_info)
    speech_tokens = load_speech_tokens(context.paths.stt_speech_tokens)
    speech_segments = load_speech_segments(context.paths.stt_speech_segments)
    visual_events = {event.scene_id: event for event in load_visual_events(context.paths.visual_events_jsonl)}
    scenes = load_scenes(context.paths.scenes_jsonl)
    token_positions = {token.token_index: index for index, token in enumerate(speech_tokens)}

    timeline_events: list[TimelineEvent] = []
    for scene_index, scene in enumerate(scenes):
        is_last_scene = scene_index == len(scenes) - 1
        visual_event = visual_events.get(scene.scene_id)
        if visual_event is None:
            raise StageExecutionError(
                f"missing visual event for scene {scene.scene_id}",
                error_code="missing_visual_event",
            )
        timeline_events.append(
            _build_scene_timeline_event(
                context=context,
                video_info=video_info,
                scene=scene,
                visual_event=visual_event,
                speech_segments=speech_segments,
                speech_tokens=speech_tokens,
                token_positions=token_positions,
                is_last_scene=is_last_scene,
            )
        )
    return timeline_events


def _build_scene_timeline_event(
    *,
    context: StageContext,
    video_info: VideoInfo,
    scene: Scene,
    visual_event: VisualEvent,
    speech_segments: list[SpeechSegment],
    speech_tokens: list[SpeechToken],
    token_positions: dict[int, int],
    is_last_scene: bool,
) -> TimelineEvent:
    scene_tokens: list[SpeechToken] = []
    scene_segment_ids: list[str] = []
    audio_source_refs: list[SourceRef] = []

    for segment in speech_segments:
        if segment.end <= scene.start or segment.start >= scene.end:
            continue
        start_position = token_positions.get(segment.token_start_index)
        end_position = token_positions.get(segment.token_end_index)
        if start_position is None or end_position is None or start_position > end_position:
            raise StageExecutionError(
                f"invalid token range for segment {segment.segment_id}",
                error_code="timeline_segment_token_range_invalid",
            )
        segment_tokens = speech_tokens[start_position : end_position + 1]
        clipped_tokens = [token for token in segment_tokens if _token_belongs_to_scene(token, scene, is_last_scene=is_last_scene)]
        if not clipped_tokens:
            continue
        scene_tokens.extend(clipped_tokens)
        scene_segment_ids.append(segment.segment_id)
        audio_source_refs.append(
            SourceRef(
                type="audio",
                url=build_timestamp_url(scene.video_id, clipped_tokens[0].start),
                start=clipped_tokens[0].start,
                end=clipped_tokens[-1].end,
                modality="audio",
            )
        )

    source_refs: list[SourceRef] = [youtube_source_ref(scene.video_id, scene.start, scene.end, title=video_info.title)]
    source_refs.extend(audio_source_refs)
    source_refs.append(SourceRef(type="visual", url=visual_event.timestamp_url, start=scene.start, end=scene.end, modality="visual"))
    return TimelineEvent(
        event_id=timeline_event_id(context.job.video_id, scene.start, scene.end),
        video_id=context.job.video_id,
        title=video_info.title,
        channel=video_info.channel,
        presenter_context=visual_event.presenter_context,
        start=scene.start,
        end=scene.end,
        timestamp_url=build_timestamp_url(context.job.video_id, scene.start),
        speech_text=_join_tokens(scene_tokens),
        visual_summary=visual_event.visual_summary,
        on_screen_text=stable_unique(visual_event.on_screen_text),
        items=stable_unique(visual_event.items),
        colors=stable_unique(visual_event.colors),
        topics=stable_unique(visual_event.style_topics),
        scene_id=scene.scene_id,
        speech_segment_ids=scene_segment_ids,
        source_refs=source_refs,
    )


def _token_belongs_to_scene(token: SpeechToken, scene: Scene, *, is_last_scene: bool) -> bool:
    midpoint = token.start + ((token.end - token.start) / 2)
    if midpoint < scene.start:
        return False
    if is_last_scene:
        return midpoint <= scene.end
    return midpoint < scene.end


def _join_tokens(tokens: list[SpeechToken]) -> str:
    text = "".join(token.text for token in tokens)
    text = re.sub(r"\s+", " ", text, flags=re.UNICODE)
    return text.strip()

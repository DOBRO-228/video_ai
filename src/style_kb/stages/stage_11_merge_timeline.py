from __future__ import annotations

import re

from style_kb.clients.media import duration_seconds
from style_kb.errors import MediaToolError, StageExecutionError
from style_kb.models import Scene, SourceRef, SpeechSegment, SpeechToken, SpeechTurn, TimelineEvent, VideoInfo, VisualEvent
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
from style_kb.stages.diagnostics import append_stage_summary
from style_kb.utils.collections import stable_unique
from style_kb.utils.files import write_json_atomic
from style_kb.utils.ids import timeline_event_id
from style_kb.utils.pydantic_io import write_models_jsonl
from style_kb.utils.time import build_timestamp_url

_DURATION_MISMATCH_LIMIT_SECONDS = 1.0


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
        return [context.paths.timeline_events_jsonl, context.paths.timeline_media_durations]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.timeline_events_jsonl.exists() or not context.paths.timeline_media_durations.exists():
            return False
        try:
            actual_media_durations = read_payload(context.paths.timeline_media_durations)
            _validate_media_durations(actual_media_durations, context.job.video_id)
            expected_media_durations = _expected_media_durations(context, actual_media_durations)
            if actual_media_durations != expected_media_durations:
                return False
            actual_events = load_timeline_events(context.paths.timeline_events_jsonl)
            expected_events = _build_timeline_events(context, media_durations=expected_media_durations)
        except Exception:
            return False
        if not actual_events:
            return False
        if len(actual_events) != len(expected_events):
            return False
        return all(
            actual.model_dump(mode="json") == expected.model_dump(mode="json")
            for actual, expected in zip(actual_events, expected_events)
        )

    def run(self, context: StageContext) -> StageResult:
        media_durations = _build_media_durations(context)
        scenes = load_scenes(context.paths.scenes_jsonl)
        visual_events = load_visual_events(context.paths.visual_events_jsonl)
        speech_segments = load_speech_segments(context.paths.stt_speech_segments)
        speech_tokens = load_speech_tokens(context.paths.stt_speech_tokens)
        append_stage_summary(
            context,
            self.name,
            "timeline-merge-preflight",
            {
                "media_durations": media_durations,
                "duration_mismatch_limit_seconds": _DURATION_MISMATCH_LIMIT_SECONDS,
                "scenes_count": len(scenes),
                "visual_events_count": len(visual_events),
                "speech_segments_count": len(speech_segments),
                "speech_tokens_count": len(speech_tokens),
                "timeline_events_path": str(context.paths.timeline_events_jsonl),
                "media_durations_path": str(context.paths.timeline_media_durations),
            },
        )
        timeline_events = _build_timeline_events(context, media_durations=media_durations)
        write_json_atomic(context.paths.timeline_media_durations, media_durations)
        write_models_jsonl(context.paths.timeline_events_jsonl, timeline_events)
        append_stage_summary(
            context,
            self.name,
            "timeline-merge-summary",
            {
                "media_durations": media_durations,
                "duration_mismatch_limit_seconds": _DURATION_MISMATCH_LIMIT_SECONDS,
                "scenes_count": len(scenes),
                "visual_events_count": len(visual_events),
                "speech_segments_count": len(speech_segments),
                "speech_tokens_count": len(speech_tokens),
                "timeline_events_count": len(timeline_events),
                "timeline_events_path": str(context.paths.timeline_events_jsonl),
                "media_durations_path": str(context.paths.timeline_media_durations),
            },
        )
        return StageResult(
            output_files=self.output_files(context),
            metrics={
                "timeline_events_count": len(timeline_events),
                "audio_video_duration_abs": media_durations["audio_video_duration_abs"],
                "scenes_count": len(scenes),
                "visual_events_count": len(visual_events),
                "speech_segments_count": len(speech_segments),
                "speech_tokens_count": len(speech_tokens),
            },
        )


def _build_timeline_events(context: StageContext, *, media_durations: dict) -> list[TimelineEvent]:
    _validate_media_durations(media_durations, context.job.video_id)
    if media_durations["audio_video_duration_abs"] > _DURATION_MISMATCH_LIMIT_SECONDS:
        raise MediaToolError("audio/video duration mismatch exceeds 1.0s", error_code="audio_video_duration_mismatch")

    video_info = load_video_info(context.paths.metadata_video_info)
    if media_durations["metadata_duration"] != video_info.duration:
        raise MediaToolError("metadata duration changed after timeline merge", error_code="metadata_duration_changed")

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
                details=_missing_visual_event_details(scene.scene_id, scenes, visual_events),
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


def _build_media_durations(context: StageContext) -> dict:
    video_info = load_video_info(context.paths.metadata_video_info)
    audio_duration = duration_seconds(read_payload(context.paths.downloads_audio_ffprobe))
    video_duration = duration_seconds(read_payload(context.paths.downloads_video_ffprobe))
    audio_video_duration_abs = round(abs(audio_duration - video_duration), 4)
    if audio_video_duration_abs > _DURATION_MISMATCH_LIMIT_SECONDS:
        raise MediaToolError("audio/video duration mismatch exceeds 1.0s", error_code="audio_video_duration_mismatch")
    return {
        "video_id": context.job.video_id,
        "audio_duration": audio_duration,
        "video_duration": video_duration,
        "metadata_duration": video_info.duration,
        "audio_video_duration_abs": audio_video_duration_abs,
    }


def _missing_visual_event_details(scene_id: str, scenes: list[Scene], visual_events: dict[str, VisualEvent]) -> str:
    available_scene_ids = [scene.scene_id for scene in scenes]
    available_visual_scene_ids = sorted(visual_events)
    return (
        f"missing_scene_id: {scene_id}\n"
        f"available_scene_ids: {available_scene_ids[:20]}\n"
        f"available_visual_scene_ids: {available_visual_scene_ids[:20]}"
    )


def _expected_media_durations(context: StageContext, existing_media_durations: dict) -> dict:
    if context.paths.downloads_audio_ffprobe.exists() and context.paths.downloads_video_ffprobe.exists():
        return _build_media_durations(context)
    return existing_media_durations


def _validate_media_durations(payload: dict, video_id: str) -> None:
    if payload.get("video_id") != video_id:
        raise MediaToolError("timeline media durations belong to a different video", error_code="timeline_media_video_mismatch")
    for key in ["audio_duration", "video_duration", "metadata_duration", "audio_video_duration_abs"]:
        value = payload.get(key)
        if not isinstance(value, int | float) or value < 0:
            raise MediaToolError("timeline media durations are invalid", error_code="timeline_media_durations_invalid")
    expected_mismatch = round(abs(payload["audio_duration"] - payload["video_duration"]), 4)
    if payload["audio_video_duration_abs"] != expected_mismatch:
        raise MediaToolError("timeline media duration mismatch value is inconsistent", error_code="timeline_media_durations_invalid")


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
    scene_token_segment_ids: list[str] = []
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
        scene_token_segment_ids.extend([segment.segment_id] * len(clipped_tokens))
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
        speech_turns=_build_speech_turns(context.job.video_id, scene_tokens, scene_token_segment_ids),
        visual_summary=visual_event.visual_summary,
        on_screen_text=stable_unique(visual_event.on_screen_text),
        items=stable_unique(visual_event.items),
        topics=stable_unique(visual_event.style_topics),
        scene_id=scene.scene_id,
        speech_segment_ids=scene_segment_ids,
        source_refs=source_refs,
    )


def _build_speech_turns(video_id: str, tokens: list[SpeechToken], segment_ids: list[str]) -> list[SpeechTurn]:
    if not tokens:
        return []
    turns: list[SpeechTurn] = []
    current_tokens: list[SpeechToken] = []
    current_segment_ids: list[str] = []
    current_identity: tuple[str | None, str | None] | None = None

    for token, segment_id in zip(tokens, segment_ids):
        identity = current_identity if not token.text.strip() and current_identity is not None else (token.speaker, token.speaker_role)
        if current_identity is not None and identity != current_identity and current_tokens:
            turns.append(_speech_turn(video_id, current_tokens, current_segment_ids, current_identity))
            current_tokens = []
            current_segment_ids = []
        current_identity = identity
        current_tokens.append(token)
        current_segment_ids.append(segment_id)

    if current_tokens and current_identity is not None:
        turns.append(_speech_turn(video_id, current_tokens, current_segment_ids, current_identity))
    return turns


def _speech_turn(
    video_id: str,
    tokens: list[SpeechToken],
    segment_ids: list[str],
    identity: tuple[str | None, str | None],
) -> SpeechTurn:
    speaker, speaker_role = identity
    start = tokens[0].start
    end = tokens[-1].end
    return SpeechTurn(
        video_id=video_id,
        start=start,
        end=end,
        timestamp_url=build_timestamp_url(video_id, start),
        text=_join_tokens(tokens),
        speaker=speaker,
        speaker_role=speaker_role,
        speech_segment_ids=stable_unique(segment_ids),
        source_refs=[SourceRef(type="audio", url=build_timestamp_url(video_id, start), start=start, end=end, modality="audio")],
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

from __future__ import annotations

from style_kb.clients.media import duration_seconds
from style_kb.errors import StageExecutionError
from style_kb.models import (
    ConfidenceLevel,
    PresenterProfile,
    PresenterRelevance,
    QualityReport,
    SpeakerDiarization,
    VisualEvent,
)
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import (
    load_chunks,
    load_frame_refs,
    load_scenes,
    load_speech_segments,
    load_speech_tokens,
    load_style_claims,
    load_timeline_events,
    load_video_info,
    load_visual_events,
    read_payload,
)
from style_kb.utils.pydantic_io import read_model, write_model


class Stage16QualityReport(Stage):
    name = "16_quality_report"
    ordinal = 16

    def input_files(self, context: StageContext) -> list:
        return [
            context.paths.metadata_video_info,
            context.paths.downloads_audio_ffprobe,
            context.paths.downloads_video_ffprobe,
            context.paths.stt_speaker_diarization,
            context.paths.stt_speech_tokens,
            context.paths.stt_speech_segments,
            context.paths.scenes_jsonl,
            context.paths.frame_refs_jsonl,
            context.paths.visual_events_jsonl,
            context.paths.visual_presenter_profile,
            context.paths.timeline_events_jsonl,
            context.paths.chunks_jsonl,
            context.paths.style_claims_jsonl,
        ]

    def output_files(self, context: StageContext) -> list:
        return [context.paths.quality_report]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.quality_report.exists():
            return False
        return bool(QualityReport.model_validate(read_payload(context.paths.quality_report)))

    def run(self, context: StageContext) -> StageResult:
        video_info = load_video_info(context.paths.metadata_video_info)
        speech_tokens = load_speech_tokens(context.paths.stt_speech_tokens)
        speaker_diarization = read_model(context.paths.stt_speaker_diarization, SpeakerDiarization)
        speech_segments = load_speech_segments(context.paths.stt_speech_segments)
        scenes = load_scenes(context.paths.scenes_jsonl)
        frame_refs = load_frame_refs(context.paths.frame_refs_jsonl)
        visual_events = load_visual_events(context.paths.visual_events_jsonl)
        timeline_events = load_timeline_events(context.paths.timeline_events_jsonl)
        chunks = load_chunks(context.paths.chunks_jsonl)
        style_claims = load_style_claims(context.paths.style_claims_jsonl)
        if not timeline_events:
            raise StageExecutionError("timeline is empty", error_code="quality_empty_timeline")
        if not chunks:
            raise StageExecutionError("chunks are empty", error_code="quality_empty_chunks")

        audio_duration = duration_seconds(read_payload(context.paths.downloads_audio_ffprobe))
        video_duration = duration_seconds(read_payload(context.paths.downloads_video_ffprobe))
        transcript_end = speech_tokens[-1].end if speech_tokens else 0.0
        timeline_end = timeline_events[-1].end
        chunks_end = chunks[-1].end
        warnings: list[str] = []
        if abs(video_info.duration - video_duration) > 1.0:
            warnings.append("metadata duration differs from proxy video duration by more than 1.0s")
        if len(scenes) == 1:
            warnings.append("scene detection produced a single scene")
        if speaker_diarization.enabled and speaker_diarization.detected_speakers == 0:
            warnings.append("speaker diarization is enabled but no speaker labels were detected")
        chunks_coverage = round(sum(chunk.end - chunk.start for chunk in chunks) / max(video_duration, 1.0), 4)
        if chunks_coverage > 2.0:
            warnings.append("chunks cover the video more than 2x; overlap or duplicated content may be too high")
        if not style_claims:
            warnings.append("style claims are empty despite non-empty chunks")
        chunks_with_claims = {claim.chunk_id for claim in style_claims}
        chunks_without_claims = [chunk for chunk in chunks if chunk.chunk_id not in chunks_with_claims]
        if chunks and len(chunks_without_claims) / len(chunks) > 0.5:
            warnings.append("more than half of chunks have no extracted style claims")
        warnings.extend(_presenter_warnings(context, visual_events, chunks))

        report = QualityReport(
            job_id=context.job.job_id,
            video_id=context.job.video_id,
            stage_counts={
                "speech_tokens": len(speech_tokens),
                "speakers": speaker_diarization.detected_speakers,
                "speech_segments": len(speech_segments),
                "scenes": len(scenes),
                "frame_refs": len(frame_refs),
                "visual_events": len(visual_events),
                "timeline_events": len(timeline_events),
                "chunks": len(chunks),
                "style_claims": len(style_claims),
            },
            durations={
                "metadata": video_info.duration,
                "audio": audio_duration,
                "video": video_duration,
                "transcript_end": transcript_end,
                "timeline_end": timeline_end,
                "chunks_end": chunks_end,
            },
            coverage={
                "speech_segments_vs_video": round(sum(segment.end - segment.start for segment in speech_segments) / max(video_duration, 1.0), 4),
                "timeline_vs_video": round(sum(event.end - event.start for event in timeline_events) / max(video_duration, 1.0), 4),
                "chunks_vs_video": chunks_coverage,
            },
            mismatches={
                "audio_vs_video_duration_abs": round(abs(audio_duration - video_duration), 4),
                "metadata_vs_video_duration_abs": round(abs(video_info.duration - video_duration), 4),
                "transcript_vs_video_end_abs": round(abs(transcript_end - video_duration), 4),
            },
            warnings=warnings,
            errors=[],
            artifacts=context.paths.artifact_summary(),
        )
        write_model(context.paths.quality_report, report)
        return StageResult(output_files=self.output_files(context), metrics={"warnings_count": len(warnings)})


def _presenter_warnings(context: StageContext, visual_events: list[VisualEvent], chunks) -> list[str]:
    warnings: list[str] = []
    if not context.paths.visual_presenter_profile.exists():
        return warnings
    profile = read_model(context.paths.visual_presenter_profile, PresenterProfile)
    if not profile.has_primary_presenter:
        return warnings

    recurring_events = [event for event in visual_events if event.presenter_context.is_recurring]
    background_events = [
        event for event in recurring_events if event.presenter_context.relevance == PresenterRelevance.BACKGROUND
    ]
    if profile.confidence == ConfidenceLevel.HIGH and not recurring_events:
        warnings.append("presenter profile detected a recurring presenter, but no visual events are marked recurring")

    baseline_values = {event.presenter_context.baseline_summary.strip() for event in recurring_events if event.presenter_context.baseline_summary.strip()}
    if len(baseline_values) > 3:
        warnings.append("presenter baseline drifts across visual events; recurring presenter context may be unstable")

    background_leaks = sum(1 for event in background_events if _event_mentions_presenter_baseline(event, profile))
    if background_events and background_leaks / max(len(background_events), 1) > 0.35:
        warnings.append("background presenter baseline appears in scene-specific visual fields too often")

    chunks_with_brief = [chunk for chunk in chunks if chunk.presenter_brief.strip()]
    if profile.confidence == ConfidenceLevel.HIGH and not chunks_with_brief:
        warnings.append("presenter_brief is empty for all chunks despite high-confidence recurring presenter")
    return warnings


def _event_mentions_presenter_baseline(event: VisualEvent, profile: PresenterProfile) -> bool:
    markers = [profile.baseline_summary, *profile.recurring_visual_markers]
    marker_words = {word.lower() for marker in markers for word in marker.replace("/", " ").split() if len(word) >= 5}
    if not marker_words:
        return False
    text = " ".join(
        [
            event.visual_summary,
            " ".join(event.observations),
            " ".join(event.items),
            " ".join(event.style_topics),
        ]
    ).lower()
    return any(word in text for word in marker_words)

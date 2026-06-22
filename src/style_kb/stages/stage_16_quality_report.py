from __future__ import annotations

from style_kb.config.models import AppConfig
from style_kb.errors import StageExecutionError
from style_kb.models import (
    Chunk,
    ConfidenceLevel,
    Job,
    PresenterProfile,
    QualityReport,
    SpeakerDiarization,
    StyleClaim,
    VisualEvent,
)
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.pipeline.paths import JobPaths
from style_kb.stages.common import (
    effective_style_claims_path,
    effective_style_claims_path_for_paths,
    load_chunks,
    load_effective_style_claims,
    load_frame_refs,
    load_scenes,
    load_speech_segments,
    load_style_claims,
    load_speech_tokens,
    load_timeline_events,
    load_video_info,
    load_visual_events,
    read_payload,
)
from style_kb.stages.stage_10_describe_visuals import (
    baseline_leakage_metrics_for_events,
    empty_baseline_leakage_metrics,
    presentation_noise_markers,
    technical_leakage_metrics_for_events,
)
from style_kb.utils.collections import stable_unique
from style_kb.utils.pydantic_io import read_model, write_model


class Stage16QualityReport(Stage):
    name = "16_quality_report"
    ordinal = 16

    def input_files(self, context: StageContext) -> list:
        inputs = [
            context.paths.metadata_video_info,
            context.paths.timeline_media_durations,
            context.paths.stt_speaker_diarization,
            context.paths.stt_speech_tokens,
            context.paths.stt_speech_segments,
            context.paths.timeline_events_jsonl,
            context.paths.chunks_jsonl,
            effective_style_claims_path(context),
        ]
        if context.config.pipeline.visual_enabled:
            inputs[5:5] = [
                context.paths.scenes_jsonl,
                context.paths.frame_refs_jsonl,
                context.paths.visual_events_jsonl,
                context.paths.visual_presenter_profile,
            ]
        return inputs

    def output_files(self, context: StageContext) -> list:
        return [context.paths.quality_report]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.quality_report.exists():
            return False
        try:
            report = QualityReport.model_validate(read_payload(context.paths.quality_report))
        except Exception:
            return False
        if report.job_id != context.job.job_id or report.video_id != context.job.video_id:
            return False
        try:
            expected_counts = _expected_stage_counts(context)
        except Exception:
            return False
        return all(report.stage_counts.get(key) == value for key, value in expected_counts.items())

    def run(self, context: StageContext) -> StageResult:
        report = build_quality_report(config=context.config, job=context.job, paths=context.paths)
        write_model(context.paths.quality_report, report)
        return StageResult(
            output_files=self.output_files(context),
            metrics=quality_report_stage_metrics(report),
        )


def build_quality_report(*, config: AppConfig, job: Job, paths: JobPaths) -> QualityReport:
    video_info = load_video_info(paths.metadata_video_info)
    speech_tokens = load_speech_tokens(paths.stt_speech_tokens)
    speaker_diarization = read_model(paths.stt_speaker_diarization, SpeakerDiarization)
    speech_segments = load_speech_segments(paths.stt_speech_segments)
    scenes = load_scenes(paths.scenes_jsonl) if config.pipeline.visual_enabled else []
    frame_refs = load_frame_refs(paths.frame_refs_jsonl) if config.pipeline.visual_enabled else []
    visual_events = load_visual_events(paths.visual_events_jsonl) if config.pipeline.visual_enabled else []
    timeline_events = load_timeline_events(paths.timeline_events_jsonl)
    chunks = load_chunks(paths.chunks_jsonl)
    style_claims = load_effective_style_claims_for_paths(paths)
    if not timeline_events:
        raise StageExecutionError("timeline is empty", error_code="quality_empty_timeline")
    if not chunks:
        raise StageExecutionError("chunks are empty", error_code="quality_empty_chunks")

    media_durations = read_payload(paths.timeline_media_durations)
    audio_duration = float(media_durations["audio_duration"])
    video_duration = float(media_durations["video_duration"])
    transcript_end = speech_tokens[-1].end if speech_tokens else 0.0
    timeline_end = timeline_events[-1].end
    chunks_end = chunks[-1].end
    warnings: list[str] = []
    if config.pipeline.visual_enabled and abs(video_info.duration - video_duration) > 1.0:
        warnings.append("metadata duration differs from proxy video duration by more than 1.0s")
    if config.pipeline.visual_enabled and len(scenes) == 1:
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
    baseline_leakage_metrics = _quality_baseline_leakage_metrics(config=config, paths=paths, visual_events=visual_events)
    technical_leakage_metrics = technical_leakage_metrics_for_events(visual_events)
    visual_presentation_noise_metrics = _visual_presentation_noise_metrics(visual_events)
    chunk_presentation_noise_metrics = _chunk_presentation_noise_metrics(chunks)
    quality_metrics = {
        "visual_enabled": int(config.pipeline.visual_enabled),
        **baseline_leakage_metrics,
        **technical_leakage_metrics,
        **visual_presentation_noise_metrics,
        **chunk_presentation_noise_metrics,
    }
    warnings.extend(
        _presenter_warnings(
            config=config,
            paths=paths,
            visual_events=visual_events,
            chunks=chunks,
            baseline_leakage_metrics=baseline_leakage_metrics,
        )
    )
    warnings.extend(_technical_visual_warnings(visual_events, technical_leakage_metrics))
    warnings.extend(
        _presentation_noise_warnings(
            visual_events,
            chunks,
            visual_presentation_noise_metrics,
            chunk_presentation_noise_metrics,
        )
    )

    return QualityReport(
        job_id=job.job_id,
        video_id=job.video_id,
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
        metrics=quality_metrics,
        warnings=warnings,
        errors=[],
        artifacts=paths.artifact_summary(),
    )


def quality_report_stage_metrics(report: QualityReport) -> dict[str, int]:
    return {"warnings_count": len(report.warnings), **report.metrics}


def load_effective_style_claims_for_paths(paths: JobPaths) -> list[StyleClaim]:
    return load_style_claims(effective_style_claims_path_for_paths(paths))


def _expected_stage_counts(context: StageContext) -> dict[str, int]:
    speaker_diarization = read_model(context.paths.stt_speaker_diarization, SpeakerDiarization)
    return {
        "speech_tokens": len(load_speech_tokens(context.paths.stt_speech_tokens)),
        "speakers": speaker_diarization.detected_speakers,
        "speech_segments": len(load_speech_segments(context.paths.stt_speech_segments)),
        "scenes": len(load_scenes(context.paths.scenes_jsonl)) if context.config.pipeline.visual_enabled else 0,
        "frame_refs": len(load_frame_refs(context.paths.frame_refs_jsonl)) if context.config.pipeline.visual_enabled else 0,
        "visual_events": len(load_visual_events(context.paths.visual_events_jsonl)) if context.config.pipeline.visual_enabled else 0,
        "timeline_events": len(load_timeline_events(context.paths.timeline_events_jsonl)),
        "chunks": len(load_chunks(context.paths.chunks_jsonl)),
        "style_claims": len(load_effective_style_claims(context)),
    }


def _quality_baseline_leakage_metrics(
    *,
    config: AppConfig,
    paths: JobPaths,
    visual_events: list[VisualEvent],
) -> dict[str, int]:
    if not config.pipeline.visual_enabled:
        return empty_baseline_leakage_metrics()
    if not paths.visual_presenter_profile.exists():
        return empty_baseline_leakage_metrics()
    profile = read_model(paths.visual_presenter_profile, PresenterProfile)
    if not profile.has_primary_presenter:
        return empty_baseline_leakage_metrics()
    return baseline_leakage_metrics_for_events(visual_events, profile)


def _presenter_warnings(
    *,
    config: AppConfig,
    paths: JobPaths,
    visual_events: list[VisualEvent],
    chunks: list[Chunk],
    baseline_leakage_metrics: dict[str, int],
) -> list[str]:
    warnings: list[str] = []
    if not config.pipeline.visual_enabled:
        return warnings
    if not paths.visual_presenter_profile.exists():
        return warnings
    profile = read_model(paths.visual_presenter_profile, PresenterProfile)
    if not profile.has_primary_presenter:
        return warnings

    recurring_events = [event for event in visual_events if event.presenter_context.is_recurring]
    if profile.confidence == ConfidenceLevel.HIGH and not recurring_events:
        warnings.append("presenter profile detected a recurring presenter, but no visual events are marked recurring")

    baseline_values = {event.presenter_context.baseline_summary.strip() for event in recurring_events if event.presenter_context.baseline_summary.strip()}
    if len(baseline_values) > 3:
        warnings.append("presenter baseline drifts across visual events; recurring presenter context may be unstable")

    baseline_leakage_scenes = baseline_leakage_metrics["baseline_leakage_scenes_count"]
    if baseline_leakage_scenes:
        warnings.append(
            f"presenter baseline appears in scene-specific visual fields in {baseline_leakage_scenes}/{len(visual_events)} visual events"
        )

    chunks_with_brief = [chunk for chunk in chunks if chunk.presenter_brief.strip()]
    if profile.confidence == ConfidenceLevel.HIGH and not chunks_with_brief:
        warnings.append("presenter_brief is empty for all chunks despite high-confidence recurring presenter")
    return warnings


def _technical_visual_warnings(
    visual_events: list[VisualEvent],
    technical_leakage_metrics: dict[str, int],
) -> list[str]:
    scenes = technical_leakage_metrics["technical_leakage_scenes_count"]
    if not scenes:
        return []
    return [
        f"technical presentation labels remain in scene-specific visual fields in {scenes}/{len(visual_events)} visual events"
    ]


def _visual_presentation_noise_metrics(visual_events: list[VisualEvent]) -> dict[str, int]:
    metrics = _empty_visual_presentation_noise_metrics()
    unique_markers: set[str] = set()
    for event in visual_events:
        fields = {
            "visual_summary": _presentation_noise_markers(event.visual_summary),
            "observations": _presentation_noise_markers(event.observations),
            "interpretations": _presentation_noise_markers(event.interpretations),
            "items": _presentation_noise_markers(event.items),
            "style_topics": _presentation_noise_markers(event.style_topics),
            "notes": _presentation_noise_markers(event.notes),
            "scene_deltas": _presentation_noise_markers(event.presenter_context.scene_deltas),
        }
        fields = {field: markers for field, markers in fields.items() if markers}
        if not fields:
            continue
        metrics["visual_presentation_noise_scenes_count"] += 1
        metrics["visual_presentation_noise_fields_count"] += len(fields)
        for field, markers in fields.items():
            metric_name = f"visual_presentation_noise_{field}_count"
            if metric_name in metrics:
                metrics[metric_name] += len(markers)
            metrics["visual_presentation_noise_markers_total"] += len(markers)
            unique_markers.update(markers)
    metrics["visual_presentation_noise_unique_markers_count"] = len(unique_markers)
    return metrics


def _chunk_presentation_noise_metrics(chunks: list[Chunk]) -> dict[str, int]:
    metrics = _empty_chunk_presentation_noise_metrics()
    unique_markers: set[str] = set()
    for chunk in chunks:
        fields = {
            "visual_text": _presentation_noise_markers(chunk.visual_text),
            "presenter_brief": _presentation_noise_markers(chunk.presenter_brief),
            "topics": _presentation_noise_markers(chunk.topics),
            "entities": _presentation_noise_markers(chunk.entities),
            "combined_text": _presentation_noise_markers(_chunk_combined_visual_component(chunk)),
        }
        fields = {field: markers for field, markers in fields.items() if markers}
        if not fields:
            continue
        metrics["chunk_presentation_noise_chunks_count"] += 1
        metrics["chunk_presentation_noise_fields_count"] += len(fields)
        for field, markers in fields.items():
            metric_name = f"chunk_presentation_noise_{field}_count"
            if metric_name in metrics:
                metrics[metric_name] += len(markers)
            metrics["chunk_presentation_noise_markers_total"] += len(markers)
            unique_markers.update(markers)
    metrics["chunk_presentation_noise_unique_markers_count"] = len(unique_markers)
    return metrics


def _presentation_noise_warnings(
    visual_events: list[VisualEvent],
    chunks: list[Chunk],
    visual_metrics: dict[str, int],
    chunk_metrics: dict[str, int],
) -> list[str]:
    warnings: list[str] = []
    visual_scenes = visual_metrics["visual_presentation_noise_scenes_count"]
    if visual_scenes:
        warnings.append(
            f"presentation-style visual wording remains in scene-specific visual fields in {visual_scenes}/{len(visual_events)} visual events"
        )
    noisy_chunks = chunk_metrics["chunk_presentation_noise_chunks_count"]
    if noisy_chunks:
        warnings.append(
            f"presentation-style visual wording reached KB chunk fields in {noisy_chunks}/{len(chunks)} chunks"
        )
    return warnings


def _empty_visual_presentation_noise_metrics() -> dict[str, int]:
    return {
        "visual_presentation_noise_scenes_count": 0,
        "visual_presentation_noise_markers_total": 0,
        "visual_presentation_noise_unique_markers_count": 0,
        "visual_presentation_noise_fields_count": 0,
        "visual_presentation_noise_visual_summary_count": 0,
        "visual_presentation_noise_observations_count": 0,
        "visual_presentation_noise_interpretations_count": 0,
        "visual_presentation_noise_items_count": 0,
        "visual_presentation_noise_style_topics_count": 0,
        "visual_presentation_noise_notes_count": 0,
        "visual_presentation_noise_scene_deltas_count": 0,
    }


def _empty_chunk_presentation_noise_metrics() -> dict[str, int]:
    return {
        "chunk_presentation_noise_chunks_count": 0,
        "chunk_presentation_noise_markers_total": 0,
        "chunk_presentation_noise_unique_markers_count": 0,
        "chunk_presentation_noise_fields_count": 0,
        "chunk_presentation_noise_visual_text_count": 0,
        "chunk_presentation_noise_presenter_brief_count": 0,
        "chunk_presentation_noise_topics_count": 0,
        "chunk_presentation_noise_entities_count": 0,
        "chunk_presentation_noise_combined_text_count": 0,
    }


def _presentation_noise_markers(value: object) -> list[str]:
    return presentation_noise_markers(value)


def _chunk_combined_visual_component(chunk: Chunk) -> str:
    return "\n".join(part for part in [chunk.presenter_brief, chunk.visual_text] if part.strip())

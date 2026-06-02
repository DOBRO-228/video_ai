from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from style_kb.clients.provider_diagnostics import ProviderCallDiagnostics, request_id_from_error, response_status_from_error
from style_kb.diagnostics import PipelineEvent
from style_kb.models import (
    Chunk,
    FrameRef,
    Scene,
    SourceRef,
    StageState,
    SpeechSegment,
    SpeechToken,
    StyleClaim,
    TimelineEvent,
    VideoInfo,
    VisualEvent,
)
from style_kb.pipeline.base import StageContext
from style_kb.utils.files import append_text, read_json
from style_kb.utils.pydantic_io import read_model, read_models_jsonl
from style_kb.utils.time import build_timestamp_url


def load_video_info(path: Path) -> VideoInfo:
    return read_model(path, VideoInfo)


def load_speech_tokens(path: Path) -> list[SpeechToken]:
    return read_models_jsonl(path, SpeechToken)


def load_speech_segments(path: Path) -> list[SpeechSegment]:
    return read_models_jsonl(path, SpeechSegment)


def load_scenes(path: Path) -> list[Scene]:
    return read_models_jsonl(path, Scene)


def load_frame_refs(path: Path) -> list[FrameRef]:
    return read_models_jsonl(path, FrameRef)


def load_visual_events(path: Path) -> list[VisualEvent]:
    return read_models_jsonl(path, VisualEvent)


def load_timeline_events(path: Path) -> list[TimelineEvent]:
    return read_models_jsonl(path, TimelineEvent)


def load_chunks(path: Path) -> list[Chunk]:
    return read_models_jsonl(path, Chunk)


def load_style_claims(path: Path) -> list[StyleClaim]:
    return read_models_jsonl(path, StyleClaim)


def youtube_source_ref(video_id: str, start: float, end: float, *, title: str | None = None, modality: str | None = None) -> SourceRef:
    return SourceRef(
        type="youtube",
        url=build_timestamp_url(video_id, start),
        start=start,
        end=end,
        title=title,
        modality=modality,
    )


def relative_artifact_path(root: Path, artifact: Path) -> str:
    return str(artifact.relative_to(root))


def read_payload(path: Path) -> dict:
    return read_json(path)


class ProviderOperation(StrEnum):
    VISION_SCENE = "vision_scene"
    VISION_PRESENTER_PROFILE = "vision_presenter_profile"
    CHUNK_PLAN = "chunk_plan"
    CLAIMS_EXTRACT = "claims_extract"
    SONIOX_UPLOAD = "soniox_upload"
    SONIOX_CREATE_TRANSCRIPTION = "soniox_create_transcription"
    SONIOX_WAIT_TRANSCRIPTION = "soniox_wait_transcription"
    SONIOX_GET_TRANSCRIPT = "soniox_get_transcript"


def provider_event_data(
    *,
    operation: ProviderOperation,
    diagnostics: ProviderCallDiagnostics,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return diagnostics.event_data(operation=operation.value, extra=extra)


def provider_error_extra(error: BaseException) -> dict[str, Any]:
    return {
        "error_type": type(error).__name__,
        "error_message": str(error),
        "status_code": response_status_from_error(error),
    }


def emit_provider_event(
    context: StageContext,
    event: PipelineEvent,
    *,
    stage_name: str,
    ordinal: int,
    operation: ProviderOperation,
    diagnostics: ProviderCallDiagnostics,
    attempt: int | None = None,
    message: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    if context.pipeline_logger is None:
        return
    status = {
        PipelineEvent.PROVIDER_REQUEST_STARTED: StageState.RUNNING,
        PipelineEvent.PROVIDER_REQUEST_COMPLETED: StageState.COMPLETED,
        PipelineEvent.PROVIDER_REQUEST_FAILED: StageState.FAILED,
    }.get(event)
    context.pipeline_logger.emit(
        event,
        job_id=context.job.job_id,
        video_id=context.job.video_id,
        stage=stage_name,
        ordinal=ordinal,
        attempt=attempt,
        status=status,
        message=message or f"{operation.value} {event.value}",
        details_path=context.paths.stage_log(stage_name),
        request_id=diagnostics.request_id,
        data=provider_event_data(
            operation=operation,
            diagnostics=diagnostics,
            extra=extra,
        ),
    )


def emit_stage_validation_failed(
    context: StageContext,
    *,
    stage_name: str,
    ordinal: int,
    error_code: str,
    message: str,
    validation_errors: list[str],
    structured_errors: list[dict[str, Any]],
    raw_output_path: Path | None = None,
    attempt: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    if context.pipeline_logger is None:
        return
    data: dict[str, Any] = {
        "error_code": error_code,
        "validation_errors": validation_errors,
        "structured_errors": structured_errors,
        "raw_output_path": str(raw_output_path) if raw_output_path is not None else None,
    }
    if extra:
        data.update(extra)
    context.pipeline_logger.emit(
        PipelineEvent.STAGE_VALIDATION_FAILED,
        job_id=context.job.job_id,
        video_id=context.job.video_id,
        stage=stage_name,
        ordinal=ordinal,
        attempt=attempt,
        status=StageState.FAILED,
        message=message,
        details_path=context.paths.stage_log(stage_name),
        data=data,
    )


def log_openai_retry(
    stage_log_path: Path,
    *,
    attempt: int,
    delay_seconds: float,
    error: BaseException,
    context_lines: list[str] | None = None,
) -> None:
    lines = [
        "",
        "[openai-retry]",
        f"attempt: {attempt}",
        f"next_delay_seconds: {delay_seconds:.2f}",
        f"error: {type(error).__name__}: {error}",
    ]
    request_id = request_id_from_error(error)
    status_code = response_status_from_error(error)
    if request_id is not None:
        lines.append(f"request_id: {request_id}")
    if status_code is not None:
        lines.append(f"status_code: {status_code}")
    if context_lines:
        lines.extend(context_lines)
    lines.append("")
    append_text(stage_log_path, "\n".join(lines), encoding="utf-8")

from __future__ import annotations

import os
from datetime import UTC, datetime
from time import perf_counter

from style_kb.clients.provider_diagnostics import ProviderCallDiagnostics, ProviderName
from style_kb.clients.soniox import SonioxClient
from style_kb.diagnostics import PipelineEvent
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import read_payload
from style_kb.stages.common import ProviderOperation, emit_provider_event, provider_error_extra
from style_kb.stages.diagnostics import append_stage_summary, file_size
from style_kb.utils.files import write_json_atomic


class Stage04SonioxUploadAudio(Stage):
    name = "04_soniox_upload_audio"
    ordinal = 4

    def input_files(self, context: StageContext) -> list:
        return [context.paths.downloads_audio]

    def output_files(self, context: StageContext) -> list:
        return [context.paths.stt_upload]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.stt_upload.exists():
            return False
        payload = read_payload(context.paths.stt_upload)
        return bool(payload.get("id"))

    def run(self, context: StageContext) -> StageResult:
        append_stage_summary(
            context,
            self.name,
            "soniox-upload-preflight",
            {
                "audio_path": str(context.paths.downloads_audio),
                "audio_exists": context.paths.downloads_audio.exists(),
                "audio_size_bytes": file_size(context.paths.downloads_audio),
                "client_reference_id": context.job.job_id,
                "upload_payload_path": str(context.paths.stt_upload),
            },
        )
        client = SonioxClient(os.environ.get("SONIOX_API_KEY"))
        started_at = datetime.now(tz=UTC).isoformat()
        started = perf_counter()
        started_diagnostics = ProviderCallDiagnostics(
            provider=ProviderName.SONIOX,
            raw_output_path=str(context.paths.stt_upload),
            request_id=context.job.job_id,
            started_at=started_at,
        )
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_STARTED,
            stage_name=self.name,
            ordinal=self.ordinal,
            operation=ProviderOperation.SONIOX_UPLOAD,
            diagnostics=started_diagnostics,
            message="Soniox upload started",
            extra={"audio_path": str(context.paths.downloads_audio)},
        )
        try:
            payload = client.upload_file(context.paths.downloads_audio, client_reference_id=context.job.job_id)
        except Exception as error:
            finished_at = datetime.now(tz=UTC).isoformat()
            duration = perf_counter() - started
            diagnostics = started_diagnostics.with_updates(
                finished_at=finished_at,
                duration_seconds=round(duration, 3),
            )
            emit_provider_event(
                context,
                PipelineEvent.PROVIDER_REQUEST_FAILED,
                stage_name=self.name,
                ordinal=self.ordinal,
                operation=ProviderOperation.SONIOX_UPLOAD,
                diagnostics=diagnostics,
                message="Soniox upload failed",
                extra={**provider_error_extra(error), "audio_path": str(context.paths.downloads_audio)},
            )
            raise
        finished_at = datetime.now(tz=UTC).isoformat()
        duration = perf_counter() - started
        diagnostics = started_diagnostics.with_updates(
            response_id=str(payload.get("id")) if payload.get("id") is not None else None,
            finished_at=finished_at,
            duration_seconds=round(duration, 3),
        )
        payload["_style_kb_diagnostics"] = diagnostics.event_data(operation=ProviderOperation.SONIOX_UPLOAD.value)
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_COMPLETED,
            stage_name=self.name,
            ordinal=self.ordinal,
            operation=ProviderOperation.SONIOX_UPLOAD,
            diagnostics=diagnostics,
            message="Soniox upload completed",
            extra={"audio_path": str(context.paths.downloads_audio)},
        )
        write_json_atomic(context.paths.stt_upload, payload)
        append_stage_summary(
            context,
            self.name,
            "soniox-upload-summary",
            {
                "audio_path": str(context.paths.downloads_audio),
                "audio_size_bytes": file_size(context.paths.downloads_audio),
                "client_reference_id": context.job.job_id,
                "soniox_file_id": payload.get("id"),
                "started_at": started_at,
                "finished_at": finished_at,
                "remote_duration_seconds": round(duration, 3),
                "upload_payload_path": str(context.paths.stt_upload),
            },
        )
        return StageResult(
            output_files=self.output_files(context),
            remote_refs={"soniox_file_id": payload["id"]},
            metrics={"audio_size_bytes": file_size(context.paths.downloads_audio), "remote_duration_seconds": round(duration, 3)},
        )

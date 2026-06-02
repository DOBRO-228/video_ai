from __future__ import annotations

import os
from datetime import UTC, datetime
from time import perf_counter

from style_kb.clients.provider_diagnostics import ProviderCallDiagnostics, ProviderName
from style_kb.clients.soniox import SonioxClient
from style_kb.diagnostics import PipelineEvent
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import ProviderOperation, emit_provider_event, provider_error_extra, read_payload
from style_kb.stages.diagnostics import append_stage_summary
from style_kb.utils.files import write_json_atomic


class Stage05SonioxCreateTranscription(Stage):
    name = "05_soniox_create_transcription"
    ordinal = 5

    def input_files(self, context: StageContext) -> list:
        return [context.paths.stt_upload]

    def output_files(self, context: StageContext) -> list:
        return [context.paths.stt_transcription]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.stt_transcription.exists():
            return False
        payload = read_payload(context.paths.stt_transcription)
        return bool(payload.get("id"))

    def run(self, context: StageContext) -> StageResult:
        upload_payload = read_payload(context.paths.stt_upload)
        append_stage_summary(
            context,
            self.name,
            "soniox-transcription-preflight",
            {
                "soniox_file_id": upload_payload.get("id"),
                "model": context.config.stt.model,
                "language_hints": context.config.stt.language_hints,
                "language_hints_strict": context.config.stt.language_hints_strict,
                "language_identification": context.config.stt.language_identification,
                "speaker_diarization": context.config.stt.speaker_diarization,
                "client_reference_id": context.job.job_id,
                "transcription_path": str(context.paths.stt_transcription),
            },
        )
        client = SonioxClient(os.environ.get("SONIOX_API_KEY"))
        started_at = datetime.now(tz=UTC).isoformat()
        started = perf_counter()
        started_diagnostics = ProviderCallDiagnostics(
            provider=ProviderName.SONIOX,
            model=context.config.stt.model,
            raw_output_path=str(context.paths.stt_transcription),
            request_id=context.job.job_id,
            started_at=started_at,
        )
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_STARTED,
            stage_name=self.name,
            ordinal=self.ordinal,
            operation=ProviderOperation.SONIOX_CREATE_TRANSCRIPTION,
            diagnostics=started_diagnostics,
            message="Soniox create transcription started",
            extra={"soniox_file_id": upload_payload.get("id")},
        )
        try:
            payload = client.create_transcription(
                file_id=upload_payload["id"],
                model=context.config.stt.model,
                language_hints=context.config.stt.language_hints,
                language_hints_strict=context.config.stt.language_hints_strict,
                enable_language_identification=context.config.stt.language_identification,
                enable_speaker_diarization=context.config.stt.speaker_diarization,
                context_domain=context.config.stt.context.domain,
                context_terms=context.config.stt.context.terms,
                client_reference_id=context.job.job_id,
            )
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
                operation=ProviderOperation.SONIOX_CREATE_TRANSCRIPTION,
                diagnostics=diagnostics,
                message="Soniox create transcription failed",
                extra={**provider_error_extra(error), "soniox_file_id": upload_payload.get("id")},
            )
            raise
        finished_at = datetime.now(tz=UTC).isoformat()
        duration = perf_counter() - started
        diagnostics = started_diagnostics.with_updates(
            response_id=str(payload.get("id")) if payload.get("id") is not None else None,
            finished_at=finished_at,
            duration_seconds=round(duration, 3),
        )
        payload["_style_kb_diagnostics"] = diagnostics.event_data(
            operation=ProviderOperation.SONIOX_CREATE_TRANSCRIPTION.value,
        )
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_COMPLETED,
            stage_name=self.name,
            ordinal=self.ordinal,
            operation=ProviderOperation.SONIOX_CREATE_TRANSCRIPTION,
            diagnostics=diagnostics,
            message="Soniox create transcription completed",
            extra={"soniox_file_id": upload_payload.get("id")},
        )
        write_json_atomic(context.paths.stt_transcription, payload)
        append_stage_summary(
            context,
            self.name,
            "soniox-transcription-summary",
            {
                "soniox_file_id": upload_payload.get("id"),
                "soniox_transcription_id": payload.get("id"),
                "model": context.config.stt.model,
                "language_hints": context.config.stt.language_hints,
                "language_hints_strict": context.config.stt.language_hints_strict,
                "language_identification": context.config.stt.language_identification,
                "speaker_diarization": context.config.stt.speaker_diarization,
                "client_reference_id": context.job.job_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "remote_duration_seconds": round(duration, 3),
                "transcription_path": str(context.paths.stt_transcription),
            },
        )
        return StageResult(
            output_files=self.output_files(context),
            remote_refs={"soniox_transcription_id": payload["id"]},
            metrics={
                "remote_duration_seconds": round(duration, 3),
                "language_hints_count": len(context.config.stt.language_hints),
                "speaker_diarization": context.config.stt.speaker_diarization,
            },
        )

from __future__ import annotations

import os

from style_kb.clients.soniox import SonioxClient
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import read_payload
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
        client = SonioxClient(os.environ.get("SONIOX_API_KEY"))
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
        write_json_atomic(context.paths.stt_transcription, payload)
        return StageResult(
            output_files=self.output_files(context),
            remote_refs={"soniox_transcription_id": payload["id"]},
        )

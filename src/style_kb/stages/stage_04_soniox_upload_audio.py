from __future__ import annotations

import os

from style_kb.clients.soniox import SonioxClient
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import read_payload
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
        client = SonioxClient(os.environ.get("SONIOX_API_KEY"))
        payload = client.upload_file(context.paths.downloads_audio, client_reference_id=context.job.job_id)
        write_json_atomic(context.paths.stt_upload, payload)
        return StageResult(output_files=self.output_files(context), remote_refs={"soniox_file_id": payload["id"]})


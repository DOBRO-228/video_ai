from __future__ import annotations

import os

from style_kb.clients.soniox import SonioxClient
from style_kb.errors import ProviderError
from style_kb.models import SpeechToken
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import load_speech_tokens, read_payload
from style_kb.utils.files import write_json_atomic
from style_kb.utils.pydantic_io import write_models_jsonl
from style_kb.utils.time import ms_to_seconds


class Stage06SonioxFetchTranscript(Stage):
    name = "06_soniox_fetch_transcript"
    ordinal = 6

    def input_files(self, context: StageContext) -> list:
        return [context.paths.stt_transcription]

    def output_files(self, context: StageContext) -> list:
        return [context.paths.stt_transcript_raw, context.paths.stt_speech_tokens]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.stt_transcript_raw.exists() or not context.paths.stt_speech_tokens.exists():
            return False
        return len(load_speech_tokens(context.paths.stt_speech_tokens)) > 0

    def run(self, context: StageContext) -> StageResult:
        transcription_payload = read_payload(context.paths.stt_transcription)
        transcription_id = transcription_payload["id"]
        client = SonioxClient(os.environ.get("SONIOX_API_KEY"))
        status_payload = client.wait_for_transcription(
            transcription_id,
            interval_sec=5.0,
            timeout_sec=3600.0,
        )
        status = status_payload.get("status")
        if status in {"error", "failed"}:
            raise ProviderError(
                status_payload.get("error_message") or "Soniox transcription failed",
                error_code="soniox_transcription_failed",
            )

        write_json_atomic(context.paths.stt_transcription, status_payload)
        transcript_payload = client.get_transcript(transcription_id)
        tokens = _normalize_tokens(context.job.video_id, transcript_payload)
        if not tokens:
            raise ProviderError("Soniox transcript is empty", error_code="empty_transcript")
        write_json_atomic(context.paths.stt_transcript_raw, transcript_payload)
        write_models_jsonl(context.paths.stt_speech_tokens, tokens)
        return StageResult(output_files=self.output_files(context), metrics={"tokens_count": len(tokens)})


def _normalize_tokens(video_id: str, transcript_payload: dict) -> list[SpeechToken]:
    raw_tokens = transcript_payload.get("tokens") or []
    tokens: list[SpeechToken] = []
    for index, raw_token in enumerate(raw_tokens):
        text = str(raw_token.get("text") or raw_token.get("token") or raw_token.get("value") or "").strip()
        if not text:
            continue
        start_ms = _coerce_ms(raw_token, ["start_ms", "start_time_ms", "startTimeMs", "start"])
        end_ms = _coerce_ms(raw_token, ["end_ms", "end_time_ms", "endTimeMs", "end"])
        tokens.append(
            SpeechToken(
                video_id=video_id,
                token_index=index,
                text=text,
                start_ms=start_ms,
                end_ms=end_ms,
                start=ms_to_seconds(start_ms),
                end=ms_to_seconds(end_ms),
                speaker=_coerce_optional(raw_token, ["speaker", "speaker_id", "speakerId"]),
                language=_coerce_optional(raw_token, ["language", "lang"]),
            )
        )
    return tokens


def _coerce_ms(payload: dict, keys: list[str]) -> int:
    for key in keys:
        if key not in payload or payload[key] is None:
            continue
        value = payload[key]
        if isinstance(value, (int, float)) and value < 10000:
            return int(round(float(value) * 1000))
        return int(round(float(value)))
    raise ProviderError("Soniox token is missing timestamp fields", error_code="soniox_token_timestamp_missing")


def _coerce_optional(payload: dict, keys: list[str]) -> str | None:
    for key in keys:
        if payload.get(key) is not None:
            return str(payload[key])
    return None

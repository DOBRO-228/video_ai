from __future__ import annotations

import os
from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter

from style_kb.clients.provider_diagnostics import ProviderCallDiagnostics, ProviderName
from style_kb.clients.soniox import SonioxClient
from style_kb.diagnostics import PipelineEvent
from style_kb.errors import ProviderError
from style_kb.models import SpeakerDiarization, SpeakerProfile, SpeakerRole, SpeechToken
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import (
    ProviderOperation,
    emit_provider_event,
    load_speech_tokens,
    provider_error_extra,
    read_payload,
)
from style_kb.stages.diagnostics import append_stage_summary
from style_kb.utils.files import write_json_atomic
from style_kb.utils.pydantic_io import write_model, write_models_jsonl
from style_kb.utils.time import ms_to_seconds


class _SonioxTranscriptionStatus(StrEnum):
    ERROR = "error"
    FAILED = "failed"


class Stage06SonioxFetchTranscript(Stage):
    name = "06_soniox_fetch_transcript"
    ordinal = 6

    def input_files(self, context: StageContext) -> list:
        return [context.paths.stt_transcription]

    def output_files(self, context: StageContext) -> list:
        return [
            context.paths.stt_transcription,
            context.paths.stt_transcript_raw,
            context.paths.stt_speech_tokens,
            context.paths.stt_speaker_diarization,
        ]

    def validate_outputs(self, context: StageContext) -> bool:
        if (
            not context.paths.stt_transcript_raw.exists()
            or not context.paths.stt_speech_tokens.exists()
            or not context.paths.stt_speaker_diarization.exists()
        ):
            return False
        tokens = load_speech_tokens(context.paths.stt_speech_tokens)
        if not tokens:
            return False
        transcript_payload = read_payload(context.paths.stt_transcript_raw)
        diarization_payload = read_payload(context.paths.stt_speaker_diarization)
        if "unassigned_tokens_count" not in diarization_payload:
            return False
        diarization = SpeakerDiarization.model_validate(diarization_payload)
        expected_tokens = _apply_speaker_roles(
            _normalize_tokens(context.job.video_id, transcript_payload),
            diarization,
        )
        if len(tokens) != len(expected_tokens):
            return False
        for actual, expected in zip(tokens, expected_tokens):
            if actual.token_index != expected.token_index:
                return False
            if actual.text != expected.text:
                return False
            if actual.start_ms != expected.start_ms or actual.end_ms != expected.end_ms:
                return False
            if actual.language != expected.language or actual.speaker != expected.speaker:
                return False
            if actual.speaker_role != expected.speaker_role:
                return False
        if any(token.start_ms > token.end_ms for token in tokens):
            return False
        if any(current.start_ms < previous.start_ms for previous, current in zip(tokens, tokens[1:])):
            return False
        transcription_payload = read_payload(context.paths.stt_transcription)
        audio_duration_ms = transcription_payload.get("audio_duration_ms")
        if audio_duration_ms is not None and tokens[-1].end_ms > int(audio_duration_ms) + 10_000:
            return False
        return True

    def run(self, context: StageContext) -> StageResult:
        transcription_payload = read_payload(context.paths.stt_transcription)
        transcription_id = transcription_payload["id"]
        append_stage_summary(
            context,
            self.name,
            "soniox-fetch-preflight",
            {
                "soniox_transcription_id": transcription_id,
                "poll_interval_seconds": 5.0,
                "timeout_seconds": 3600.0,
                "transcription_path": str(context.paths.stt_transcription),
                "transcript_raw_path": str(context.paths.stt_transcript_raw),
            },
        )
        client = SonioxClient(os.environ.get("SONIOX_API_KEY"))
        wait_started_at = datetime.now(tz=UTC).isoformat()
        started = perf_counter()
        wait_started_diagnostics = ProviderCallDiagnostics(
            provider=ProviderName.SONIOX,
            model=context.config.stt.model,
            raw_output_path=str(context.paths.stt_transcription),
            request_id=transcription_id,
            started_at=wait_started_at,
        )
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_STARTED,
            stage_name=self.name,
            ordinal=self.ordinal,
            operation=ProviderOperation.SONIOX_WAIT_TRANSCRIPTION,
            diagnostics=wait_started_diagnostics,
            message="Soniox wait transcription started",
            extra={"poll_interval_seconds": 5.0, "timeout_seconds": 3600.0},
        )
        try:
            status_payload = client.wait_for_transcription(
                transcription_id,
                interval_sec=5.0,
                timeout_sec=3600.0,
            )
        except Exception as error:
            wait_finished_at = datetime.now(tz=UTC).isoformat()
            wait_duration = perf_counter() - started
            wait_diagnostics = wait_started_diagnostics.with_updates(
                finished_at=wait_finished_at,
                duration_seconds=round(wait_duration, 3),
            )
            emit_provider_event(
                context,
                PipelineEvent.PROVIDER_REQUEST_FAILED,
                stage_name=self.name,
                ordinal=self.ordinal,
                operation=ProviderOperation.SONIOX_WAIT_TRANSCRIPTION,
                diagnostics=wait_diagnostics,
                message="Soniox wait transcription failed",
                extra={**provider_error_extra(error), "poll_interval_seconds": 5.0, "timeout_seconds": 3600.0},
            )
            raise
        wait_finished_at = datetime.now(tz=UTC).isoformat()
        wait_duration = perf_counter() - started
        wait_diagnostics = wait_started_diagnostics.with_updates(
            response_id=str(status_payload.get("id")) if status_payload.get("id") is not None else transcription_id,
            finished_at=wait_finished_at,
            duration_seconds=round(wait_duration, 3),
        )
        status_payload["_style_kb_diagnostics"] = wait_diagnostics.event_data(
            operation=ProviderOperation.SONIOX_WAIT_TRANSCRIPTION.value,
        )
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_COMPLETED,
            stage_name=self.name,
            ordinal=self.ordinal,
            operation=ProviderOperation.SONIOX_WAIT_TRANSCRIPTION,
            diagnostics=wait_diagnostics,
            message="Soniox wait transcription completed",
            extra={"final_status": status_payload.get("status")},
        )
        status = status_payload.get("status")
        if status in {_SonioxTranscriptionStatus.ERROR.value, _SonioxTranscriptionStatus.FAILED.value}:
            emit_provider_event(
                context,
                PipelineEvent.PROVIDER_REQUEST_FAILED,
                stage_name=self.name,
                ordinal=self.ordinal,
                operation=ProviderOperation.SONIOX_WAIT_TRANSCRIPTION,
                diagnostics=wait_diagnostics,
                message="Soniox transcription finished with failed status",
                extra={"final_status": status, "error_message": status_payload.get("error_message")},
            )
            raise ProviderError(
                status_payload.get("error_message") or "Soniox transcription failed",
                error_code="soniox_transcription_failed",
            )

        write_json_atomic(context.paths.stt_transcription, status_payload)
        transcript_started_at = datetime.now(tz=UTC).isoformat()
        transcript_timer_started = perf_counter()
        transcript_started_diagnostics = ProviderCallDiagnostics(
            provider=ProviderName.SONIOX,
            model=context.config.stt.model,
            raw_output_path=str(context.paths.stt_transcript_raw),
            request_id=transcription_id,
            started_at=transcript_started_at,
        )
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_STARTED,
            stage_name=self.name,
            ordinal=self.ordinal,
            operation=ProviderOperation.SONIOX_GET_TRANSCRIPT,
            diagnostics=transcript_started_diagnostics,
            message="Soniox get transcript started",
        )
        try:
            transcript_payload = client.get_transcript(transcription_id)
        except Exception as error:
            transcript_finished_at = datetime.now(tz=UTC).isoformat()
            transcript_duration = perf_counter() - transcript_timer_started
            transcript_diagnostics = transcript_started_diagnostics.with_updates(
                finished_at=transcript_finished_at,
                duration_seconds=round(transcript_duration, 3),
            )
            emit_provider_event(
                context,
                PipelineEvent.PROVIDER_REQUEST_FAILED,
                stage_name=self.name,
                ordinal=self.ordinal,
                operation=ProviderOperation.SONIOX_GET_TRANSCRIPT,
                diagnostics=transcript_diagnostics,
                message="Soniox get transcript failed",
                extra=provider_error_extra(error),
            )
            raise
        transcript_finished_at = datetime.now(tz=UTC).isoformat()
        transcript_duration = perf_counter() - transcript_timer_started
        transcript_diagnostics = transcript_started_diagnostics.with_updates(
            response_id=transcription_id,
            finished_at=transcript_finished_at,
            duration_seconds=round(transcript_duration, 3),
        )
        transcript_payload["_style_kb_diagnostics"] = transcript_diagnostics.event_data(
            operation=ProviderOperation.SONIOX_GET_TRANSCRIPT.value,
        )
        emit_provider_event(
            context,
            PipelineEvent.PROVIDER_REQUEST_COMPLETED,
            stage_name=self.name,
            ordinal=self.ordinal,
            operation=ProviderOperation.SONIOX_GET_TRANSCRIPT,
            diagnostics=transcript_diagnostics,
            message="Soniox get transcript completed",
        )
        tokens = _normalize_tokens(context.job.video_id, transcript_payload)
        if not tokens:
            raise ProviderError("Soniox transcript is empty", error_code="empty_transcript")
        diarization = _build_speaker_diarization(context.job.video_id, tokens, context)
        tokens = _apply_speaker_roles(tokens, diarization)
        write_json_atomic(context.paths.stt_transcript_raw, transcript_payload)
        write_model(context.paths.stt_speaker_diarization, diarization)
        write_models_jsonl(context.paths.stt_speech_tokens, tokens)
        append_stage_summary(
            context,
            self.name,
            "soniox-fetch-summary",
            {
                "soniox_transcription_id": transcription_id,
                "poll_interval_seconds": 5.0,
                "timeout_seconds": 3600.0,
                "wait_started_at": wait_started_at,
                "wait_finished_at": wait_finished_at,
                "wait_duration_seconds": round(wait_duration, 3),
                "transcript_started_at": transcript_started_at,
                "transcript_finished_at": transcript_finished_at,
                "transcript_duration_seconds": round(transcript_duration, 3),
                "final_status": status_payload.get("status"),
                "tokens_count": len(tokens),
                "detected_speakers": diarization.detected_speakers,
                "unassigned_tokens_count": diarization.unassigned_tokens_count,
                "transcript_raw_path": str(context.paths.stt_transcript_raw),
            },
        )
        return StageResult(
            output_files=self.output_files(context),
            metrics={
                "tokens_count": len(tokens),
                "detected_speakers": diarization.detected_speakers,
                "unassigned_tokens_count": diarization.unassigned_tokens_count,
                "wait_duration_seconds": round(wait_duration, 3),
            },
        )


def _build_speaker_diarization(video_id: str, tokens: list[SpeechToken], context: StageContext) -> SpeakerDiarization:
    speaker_tokens: dict[str, list[SpeechToken]] = defaultdict(list)
    for token in tokens:
        if not token.speaker:
            continue
        speaker_tokens[token.speaker].append(token)

    ranked_speakers = sorted(
        speaker_tokens,
        key=lambda speaker: (_speech_seconds(speaker_tokens[speaker]), len(_joined_text(speaker_tokens[speaker]).split())),
        reverse=True,
    )
    host_speaker = ranked_speakers[0] if ranked_speakers else None
    profiles: list[SpeakerProfile] = []
    for speaker in ranked_speakers:
        group = speaker_tokens[speaker]
        role = SpeakerRole.HOST if speaker == host_speaker else SpeakerRole.OFFSCREEN_QUESTIONER
        profiles.append(
            SpeakerProfile(
                speaker=speaker,
                role=role,
                tokens_count=len(group),
                words_count=len(_joined_text(group).split()),
                speech_seconds=round(_speech_seconds(group), 3),
                first_start=min(token.start for token in group),
                last_end=max(token.end for token in group),
            )
        )
    return SpeakerDiarization(
        video_id=video_id,
        provider=context.config.stt.provider,
        model=context.config.stt.model,
        enabled=context.config.stt.speaker_diarization,
        detected_speakers=len(profiles),
        unassigned_tokens_count=sum(1 for token in tokens if not token.speaker),
        role_strategy=context.config.stt.speaker_role_strategy,
        speakers=profiles,
    )


def _apply_speaker_roles(tokens: list[SpeechToken], diarization: SpeakerDiarization) -> list[SpeechToken]:
    roles = {profile.speaker: profile.role for profile in diarization.speakers}
    return [
        token.model_copy(update={"speaker_role": roles.get(token.speaker) if token.speaker else None})
        for token in tokens
    ]


def _speech_seconds(tokens: list[SpeechToken]) -> float:
    return sum(max(0.0, token.end - token.start) for token in tokens if token.text.strip())


def _joined_text(tokens: list[SpeechToken]) -> str:
    return "".join(token.text for token in tokens).strip()


def _normalize_tokens(video_id: str, transcript_payload: dict) -> list[SpeechToken]:
    raw_tokens = transcript_payload.get("tokens") or []
    tokens: list[SpeechToken] = []
    for index, raw_token in enumerate(raw_tokens):
        text = str(raw_token.get("text") or raw_token.get("token") or raw_token.get("value") or "")
        if text == "":
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
                speaker=_coerce_optional(raw_token, ["speaker", "speaker_id", "speakerId", "speaker_label", "speakerLabel"]),
                language=_coerce_optional(raw_token, ["language", "lang"]),
            )
        )
    return tokens


def _coerce_ms(payload: dict, keys: list[str]) -> int:
    for key in keys:
        if key not in payload or payload[key] is None:
            continue
        value = payload[key]
        if _key_is_milliseconds(key):
            return int(round(float(value)))
        return int(round(float(value) * 1000))
    raise ProviderError("Soniox token is missing timestamp fields", error_code="soniox_token_timestamp_missing")


def _coerce_optional(payload: dict, keys: list[str]) -> str | None:
    for key in keys:
        if payload.get(key) is not None:
            return str(payload[key])
    return None


def _key_is_milliseconds(key: str) -> bool:
    normalized = key.lower()
    return normalized.endswith("_ms") or normalized.endswith("timems")

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from soniox import SonioxClient as SdkSonioxClient
from soniox.types import CreateTranscriptionConfig, StructuredContext, StructuredContextGeneralItem

from style_kb.errors import MissingApiKeyError, ProviderError


class SonioxClient:
    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise MissingApiKeyError(
                "SONIOX_API_KEY is required before stage 04_soniox_upload_audio",
                error_code="missing_soniox_api_key",
            )
        self.client = SdkSonioxClient(api_key=api_key)

    def upload_file(self, audio_path: Path, *, client_reference_id: str) -> dict[str, Any]:
        try:
            uploaded = self.client.files.upload(audio_path)
        except Exception as error:  # pragma: no cover - SDK/runtime surface depends on installed package
            raise ProviderError(str(error), error_code="soniox_upload_failed", details=str(error)) from error
        payload = _sdk_to_plain(uploaded)
        if isinstance(payload, dict):
            payload["client_reference_id"] = client_reference_id
            return payload
        raise ProviderError("unexpected Soniox upload payload shape", error_code="soniox_upload_failed")

    def create_transcription(
        self,
        *,
        file_id: str,
        model: str,
        language_hints: list[str],
        language_hints_strict: bool,
        enable_language_identification: bool,
        enable_speaker_diarization: bool,
        context_domain: str,
        context_terms: list[str],
        client_reference_id: str,
    ) -> dict[str, Any]:
        config = CreateTranscriptionConfig(
            model=model,
            language_hints=language_hints,
            language_hints_strict=language_hints_strict,
            enable_language_identification=enable_language_identification,
            enable_speaker_diarization=enable_speaker_diarization,
            context=StructuredContext(
                general=[StructuredContextGeneralItem(key="domain", value=context_domain)],
                text=context_domain,
                terms=context_terms,
            ),
            client_reference_id=client_reference_id,
        )
        try:
            transcription = self.client.stt.create(config=config, file_id=file_id)
        except Exception as error:  # pragma: no cover - SDK/runtime surface depends on installed package
            raise ProviderError(
                str(error),
                error_code="soniox_create_transcription_failed",
                details=str(error),
            ) from error
        payload = _sdk_to_plain(transcription)
        if isinstance(payload, dict):
            return payload
        raise ProviderError(
            "unexpected Soniox transcription payload shape",
            error_code="soniox_create_transcription_failed",
        )

    def get_transcription(self, transcription_id: str) -> dict[str, Any]:
        try:
            transcription = self.client.stt.get(transcription_id)
        except Exception as error:  # pragma: no cover - SDK/runtime surface depends on installed package
            raise ProviderError(
                str(error),
                error_code="soniox_get_transcription_failed",
                details=str(error),
            ) from error
        if transcription is None:
            raise ProviderError(
                f"Soniox transcription not found: {transcription_id}",
                error_code="soniox_get_transcription_failed",
            )
        payload = _sdk_to_plain(transcription)
        if isinstance(payload, dict):
            return payload
        raise ProviderError(
            "unexpected Soniox transcription payload shape",
            error_code="soniox_get_transcription_failed",
        )

    def wait_for_transcription(
        self,
        transcription_id: str,
        *,
        interval_sec: float = 5.0,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        try:
            transcription = self.client.stt.wait(
                transcription_id,
                interval_sec=interval_sec,
                timeout_sec=timeout_sec,
            )
        except TimeoutError as error:
            raise ProviderError(
                "Soniox transcription polling timed out",
                error_code="soniox_transcription_timeout",
                details=str(error),
            ) from error
        except Exception as error:  # pragma: no cover - SDK/runtime surface depends on installed package
            raise ProviderError(
                str(error),
                error_code="soniox_get_transcription_failed",
                details=str(error),
            ) from error
        payload = _sdk_to_plain(transcription)
        if isinstance(payload, dict):
            return payload
        raise ProviderError(
            "unexpected Soniox transcription payload shape",
            error_code="soniox_get_transcription_failed",
        )

    def get_transcript(self, transcription_id: str) -> dict[str, Any]:
        try:
            transcript = self.client.stt.get_transcript(transcription_id)
        except Exception as error:  # pragma: no cover - SDK/runtime surface depends on installed package
            raise ProviderError(
                str(error),
                error_code="soniox_get_transcript_failed",
                details=str(error),
            ) from error
        payload = _sdk_to_plain(transcript)
        if isinstance(payload, dict):
            return payload
        raise ProviderError(
            "unexpected Soniox transcript payload shape",
            error_code="soniox_get_transcript_failed",
        )


def _sdk_to_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_sdk_to_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _sdk_to_plain(item) for key, item in value.items()}
    if hasattr(value, "__dict__"):
        return {key: _sdk_to_plain(item) for key, item in vars(value).items()}
    return value

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from openai import OpenAI

from style_kb.clients._retry import RetryPolicy, call_with_retry
from style_kb.errors import MissingApiKeyError, ProviderError
from style_kb.utils.files import read_json, read_jsonl, write_json_atomic, write_jsonl_atomic, write_text_atomic

_BATCH_ENDPOINT_RESPONSES = "/v1/responses"
_TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled"}


@dataclass(slots=True)
class OpenAIBatchRequest:
    custom_id: str
    body: dict[str, Any]


@dataclass(slots=True)
class OpenAIBatchResult:
    batch_id: str
    output_lines: dict[str, dict[str, Any]]
    error_lines: dict[str, dict[str, Any]]
    input_file: Path
    manifest_file: Path
    output_file: Path
    error_file: Path


class OpenAIBatchClient:
    def __init__(
        self,
        api_key: str | None,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if not api_key:
            raise MissingApiKeyError("OPENAI_API_KEY is required for OpenAI Batch API", error_code="missing_openai_api_key")
        self.client = OpenAI(api_key=api_key, max_retries=0)
        self.retry_policy = retry_policy or RetryPolicy()

    def run_responses_batch(
        self,
        *,
        requests: list[OpenAIBatchRequest],
        request_metadata: dict[str, Any],
        input_path: Path,
        manifest_path: Path,
        output_path: Path,
        error_path: Path,
        completion_window: str,
        poll_interval_seconds: float,
        poll_timeout_seconds: float | None,
        on_progress: Callable[[str], None] | None = None,
    ) -> OpenAIBatchResult:
        if not requests:
            raise ProviderError("OpenAI batch request list is empty", error_code="openai_batch_empty")
        if completion_window != "24h":
            raise ProviderError(
                f"unsupported OpenAI batch completion_window={completion_window!r}",
                error_code="openai_batch_invalid_completion_window",
            )

        request_fingerprint = _requests_fingerprint(requests, request_metadata)
        manifest = _load_matching_manifest(manifest_path, request_fingerprint)
        if manifest is not None and output_path.exists():
            return OpenAIBatchResult(
                batch_id=str(manifest["batch_id"]),
                output_lines=_jsonl_by_custom_id(output_path),
                error_lines=_jsonl_by_custom_id(error_path) if error_path.exists() else {},
                input_file=input_path,
                manifest_file=manifest_path,
                output_file=output_path,
                error_file=error_path,
            )

        if manifest is None:
            for stale_path in (output_path, error_path):
                if stale_path.exists():
                    stale_path.unlink()
            _write_batch_input(input_path, requests)
            uploaded_file = call_with_retry(
                lambda: self._upload_batch_input(input_path),
                policy=self.retry_policy,
            )
            input_file_id = _object_id(uploaded_file)
            if input_file_id is None:
                raise ProviderError("OpenAI file upload did not return an id", error_code="openai_batch_file_id_missing")
            batch = call_with_retry(
                lambda: self.client.batches.create(
                    input_file_id=input_file_id,
                    endpoint=_BATCH_ENDPOINT_RESPONSES,
                    completion_window=completion_window,
                    metadata=_api_metadata(request_metadata),
                ),
                policy=self.retry_policy,
            )
            manifest = _manifest_payload(
                request_fingerprint=request_fingerprint,
                request_metadata=request_metadata,
                input_file_id=input_file_id,
                batch=batch,
            )
            write_json_atomic(manifest_path, manifest)
            if on_progress is not None:
                on_progress(f"submitted OpenAI batch {manifest['batch_id']} requests={len(requests)}")

        batch_id = str(manifest["batch_id"])
        batch = self._wait_for_batch(
            batch_id,
            request_fingerprint=request_fingerprint,
            request_metadata=request_metadata,
            manifest_path=manifest_path,
            poll_interval_seconds=poll_interval_seconds,
            poll_timeout_seconds=poll_timeout_seconds,
            on_progress=on_progress,
        )

        output_file_id = _string_or_none(_field(batch, "output_file_id"))
        error_file_id = _string_or_none(_field(batch, "error_file_id"))
        if output_file_id is not None:
            _download_file_text(self.client, output_file_id, output_path)
        if error_file_id is not None:
            _download_file_text(self.client, error_file_id, error_path)

        status = _string_or_none(_field(batch, "status"))
        if status != "completed":
            details = f"batch_id={batch_id} status={status or '-'}"
            if error_file_id is not None and error_path.exists():
                details = f"{details} error_file={error_path}"
            raise ProviderError("OpenAI batch did not complete successfully", error_code="openai_batch_not_completed", details=details)
        if not output_path.exists():
            raise ProviderError("OpenAI completed batch has no output file", error_code="openai_batch_output_missing")

        return OpenAIBatchResult(
            batch_id=batch_id,
            output_lines=_jsonl_by_custom_id(output_path),
            error_lines=_jsonl_by_custom_id(error_path) if error_path.exists() else {},
            input_file=input_path,
            manifest_file=manifest_path,
            output_file=output_path,
            error_file=error_path,
        )

    def _upload_batch_input(self, input_path: Path) -> Any:
        with input_path.open("rb") as handle:
            return self.client.files.create(file=handle, purpose="batch")

    def _wait_for_batch(
        self,
        batch_id: str,
        *,
        request_fingerprint: str,
        request_metadata: dict[str, Any],
        manifest_path: Path,
        poll_interval_seconds: float,
        poll_timeout_seconds: float | None,
        on_progress: Callable[[str], None] | None,
    ) -> Any:
        deadline = time.monotonic() + poll_timeout_seconds if poll_timeout_seconds is not None else None
        last_status: str | None = None
        while True:
            batch = call_with_retry(lambda: self.client.batches.retrieve(batch_id), policy=self.retry_policy)
            manifest = _manifest_payload(
                request_fingerprint=request_fingerprint,
                request_metadata=request_metadata,
                input_file_id=_string_or_none(_field(batch, "input_file_id")),
                batch=batch,
            )
            write_json_atomic(manifest_path, manifest)
            status = _string_or_none(_field(batch, "status"))
            if on_progress is not None and status != last_status:
                counts = _field(batch, "request_counts")
                on_progress(f"OpenAI batch {batch_id} status={status or '-'} request_counts={counts or {}}")
            last_status = status
            if status in _TERMINAL_STATUSES:
                return batch
            if deadline is not None and time.monotonic() >= deadline:
                raise ProviderError(
                    "OpenAI batch did not finish before poll_timeout_seconds",
                    error_code="openai_batch_poll_timeout",
                    details=f"batch_id={batch_id} status={status or '-'}",
                )
            time.sleep(poll_interval_seconds)


def _write_batch_input(path: Path, requests: list[OpenAIBatchRequest]) -> None:
    rows = [
        {
            "custom_id": request.custom_id,
            "method": "POST",
            "url": _BATCH_ENDPOINT_RESPONSES,
            "body": request.body,
        }
        for request in requests
    ]
    write_jsonl_atomic(path, rows)


def _load_matching_manifest(path: Path, request_fingerprint: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        manifest = read_json(path)
    except Exception:
        return None
    if not isinstance(manifest, dict):
        return None
    if manifest.get("request_fingerprint") != request_fingerprint:
        return None
    if not manifest.get("batch_id"):
        return None
    return manifest


def _manifest_payload(
    *,
    request_fingerprint: str,
    request_metadata: dict[str, Any],
    input_file_id: str | None,
    batch: Any,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "request_fingerprint": request_fingerprint,
        "request": request_metadata,
        "batch_id": _object_id(batch),
        "input_file_id": input_file_id,
        "batch": _model_dump(batch),
    }


def _requests_fingerprint(requests: list[OpenAIBatchRequest], request_metadata: dict[str, Any]) -> str:
    payload = {
        "request": request_metadata,
        "requests": [{"custom_id": request.custom_id, "body": request.body} for request in requests],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _api_metadata(request_metadata: dict[str, Any]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for key in ("stage", "operation", "video_id", "provider", "model"):
        value = request_metadata.get(key)
        if value is not None:
            metadata[key] = str(value)[:512]
    return metadata


def _jsonl_by_custom_id(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(path):
        if not isinstance(row, dict):
            continue
        custom_id = _string_or_none(row.get("custom_id"))
        if custom_id is None:
            continue
        rows[custom_id] = row
    return rows


def _download_file_text(client: OpenAI, file_id: str, path: Path) -> None:
    response = call_with_retry(lambda: client.files.content(file_id), policy=RetryPolicy())
    text = response.text
    write_text_atomic(path, text if text.endswith("\n") else f"{text}\n", encoding="utf-8")


def _object_id(value: Any) -> str | None:
    return _string_or_none(getattr(value, "id", None) or _field(value, "id"))


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _model_dump(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        return dumper(mode="json")
    return {}


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

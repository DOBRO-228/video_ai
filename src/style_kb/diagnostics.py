from __future__ import annotations

import json
import threading
import traceback
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from style_kb.error_advice import advice_for_error_code
from style_kb.utils.files import append_text, write_json_atomic
from style_kb.utils.redaction import REDACTED, is_sensitive_key, redact_sensitive_text

SCHEMA_VERSION = 1


class PipelineEvent(StrEnum):
    RUN_STARTED = "run_started"
    RUN_STOPPED = "run_stopped"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    JOB_CREATED = "job_created"
    JOB_STARTED = "job_started"
    JOB_RESUMED = "job_resumed"
    JOB_STOPPED = "job_stopped"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    STAGE_STARTED = "stage_started"
    STAGE_SKIPPED = "stage_skipped"
    STAGE_REUSE_DECISION = "stage_reuse_decision"
    STAGE_COMPLETED = "stage_completed"
    STAGE_FAILED = "stage_failed"
    STAGE_RETRY = "stage_retry"
    STAGE_VALIDATION_FAILED = "stage_validation_failed"
    ARTIFACT_WRITTEN = "artifact_written"
    PROVIDER_REQUEST_STARTED = "provider_request_started"
    PROVIDER_REQUEST_COMPLETED = "provider_request_completed"
    PROVIDER_REQUEST_FAILED = "provider_request_failed"
    CACHE_HIT = "cache_hit"
    CACHE_MISS = "cache_miss"
    PROGRESS = "progress"
    WARNING = "warning"
    SUBPROCESS_STARTED = "subprocess_started"
    SUBPROCESS_COMPLETED = "subprocess_completed"
    SUBPROCESS_FAILED = "subprocess_failed"


def new_run_id() -> str:
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


class PipelineLogger:
    def __init__(self, *, jsonl_path: Path, human_path: Path, run_id: str) -> None:
        self.jsonl_path = jsonl_path
        self.human_path = human_path
        self.run_id = run_id
        self._lock = threading.Lock()
        self._seq = 0

    def emit(
        self,
        event: PipelineEvent | str,
        *,
        job_id: str,
        video_id: str,
        stage: str | None = None,
        ordinal: int | None = None,
        attempt: int | None = None,
        status: StrEnum | str | None = None,
        message: str | None = None,
        details_path: Path | str | None = None,
        request_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
            timestamp = datetime.now(tz=UTC).isoformat()
            payload = _sanitize(
                {
                    "schema_version": SCHEMA_VERSION,
                    "event_id": f"{self.run_id}-{seq:06d}",
                    "run_id": self.run_id,
                    "seq": seq,
                    "timestamp": timestamp,
                    "event": _event_value(event),
                    "job_id": job_id,
                    "video_id": video_id,
                    "stage": stage,
                    "ordinal": ordinal,
                    "attempt": attempt,
                    "status": _enum_value(status),
                    "message": message,
                    "details_path": str(details_path) if details_path is not None else None,
                    "request_id": request_id,
                    "data": data or {},
                }
            )
            append_text(
                self.jsonl_path,
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            append_text(
                self.human_path,
                _human_event(payload),
                encoding="utf-8",
            )


def write_failure_report(
    paths: Any,
    *,
    job: Any,
    stage_status: Any,
    error: BaseException,
    stage_input_files: list[Path],
    stage_output_files: list[Path],
) -> None:
    stage_name = getattr(stage_status, "stage_name", None)
    error_code = getattr(error, "error_code", None) or getattr(stage_status, "error_code", None)
    advice = advice_for_error_code(error_code, job_id=job.job_id, stage_name=stage_name)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job.job_id,
        "video_id": job.video_id,
        "url": job.url,
        "status": "failed",
        "failed_stage": stage_name,
        "failed_stage_ordinal": getattr(stage_status, "ordinal", None),
        "stage_attempt": getattr(stage_status, "attempt", None),
        "error": _exception_payload(error, fallback_code=getattr(stage_status, "error_code", None)),
        "advice": advice.model_dump(mode="json") if advice is not None else None,
        "diagnostic_paths": {
            "stage_log": str(paths.stage_log(stage_name)) if stage_name else None,
            "pipeline_log": str(paths.pipeline_log),
            "pipeline_human_log": str(paths.pipeline_human_log),
            "quality_report": str(paths.quality_report) if paths.quality_report.exists() else None,
        },
        "stage_state": stage_status.model_dump(mode="json") if hasattr(stage_status, "model_dump") else {},
        "input_files": [_file_manifest(path, kind="input") for path in stage_input_files],
        "output_files": [_file_manifest(path, kind="output") for path in stage_output_files],
        "recommended_next_commands": [
            f"style-kb status {job.job_id}",
            f"style-kb resume {job.job_id}",
        ],
    }
    write_json_atomic(paths.failure_report, _sanitize(payload))


def write_partial_quality_report(paths: Any, *, job: Any, failed_stage: str | None) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job.job_id,
        "video_id": job.video_id,
        "failed_stage": failed_stage,
        "available_artifacts": {},
        "parseable_artifacts": {},
        "counts": {},
        "durations": {},
        "warnings": [],
        "errors": [],
    }
    for name, path, parser in _partial_quality_artifacts(paths):
        payload["available_artifacts"][name] = str(path) if path.exists() else None
        if not path.exists():
            payload["parseable_artifacts"][name] = False
            continue
        try:
            parsed = parser(path)
        except Exception as error:
            payload["parseable_artifacts"][name] = False
            payload["errors"].append({"artifact": str(path), "error": f"{type(error).__name__}: {error}"})
            continue
        payload["parseable_artifacts"][name] = True
        try:
            _add_partial_quality_stats(payload, name=name, parsed=parsed)
        except Exception as error:
            payload["errors"].append({"artifact": str(path), "error": f"stats {type(error).__name__}: {error}"})
    write_json_atomic(paths.partial_quality_report, _sanitize(payload))


def sanitize_diagnostic_value(value: Any) -> Any:
    return _sanitize(value)


def _human_event(payload: dict[str, Any]) -> str:
    lines = [
        "",
        f"[{payload['event']}] {payload['timestamp']}",
        f"run_id: {payload['run_id']}",
        f"seq: {payload['seq']}",
        f"job_id: {payload['job_id']}",
        f"video_id: {payload['video_id']}",
    ]
    for key in ["stage", "ordinal", "attempt", "status", "message", "details_path", "request_id"]:
        value = payload.get(key)
        if value is not None:
            label = "details" if key == "details_path" else key
            lines.append(f"{label}: {value}")
    data = payload.get("data") or {}
    if data:
        for key, value in data.items():
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
    lines.append("")
    return "\n".join(lines)


def _exception_payload(error: BaseException, *, fallback_code: str | None = None) -> dict[str, Any]:
    payload = {
        "code": getattr(error, "error_code", None) or fallback_code,
        "type": type(error).__name__,
        "message": str(error),
        "details": getattr(error, "details", None),
        "traceback": traceback.format_exception(error),
    }
    cause = error.__cause__ or error.__context__
    if cause is not None and cause is not error:
        payload["cause"] = {
            "type": type(cause).__name__,
            "message": str(cause),
        }
    return payload


def _file_manifest(path: Path, *, kind: str) -> dict[str, Any]:
    exists = path.exists()
    stat = path.stat() if exists else None
    return {
        "kind": kind,
        "path": str(path),
        "exists": exists,
        "size_bytes": stat.st_size if stat is not None else None,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat() if stat is not None else None,
    }


def _partial_quality_artifacts(paths: Any) -> list[tuple[str, Path, Any]]:
    return [
        ("video_info", paths.metadata_video_info, _read_json_artifact),
        ("audio_ffprobe", paths.downloads_audio_ffprobe, _read_json_artifact),
        ("video_ffprobe", paths.downloads_video_ffprobe, _read_json_artifact),
        ("speech_tokens", paths.stt_speech_tokens, _read_jsonl_artifact),
        ("speech_segments", paths.stt_speech_segments, _read_jsonl_artifact),
        ("scenes", paths.scenes_jsonl, _read_jsonl_artifact),
        ("frame_refs", paths.frame_refs_jsonl, _read_jsonl_artifact),
        ("visual_events", paths.visual_events_jsonl, _read_jsonl_artifact),
        ("timeline_events", paths.timeline_events_jsonl, _read_jsonl_artifact),
        ("chunks", paths.chunks_jsonl, _read_jsonl_artifact),
        ("style_claims", paths.style_claims_jsonl, _read_jsonl_artifact),
    ]


def _read_json_artifact(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl_artifact(path: Path) -> list[Any]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _add_partial_quality_stats(payload: dict[str, Any], *, name: str, parsed: Any) -> None:
    if isinstance(parsed, list):
        payload["counts"][name] = len(parsed)
        if parsed and all(isinstance(row, dict) for row in parsed):
            _add_time_span(payload, name=name, rows=parsed)
        return
    if not isinstance(parsed, dict):
        return
    if name == "video_info" and isinstance(parsed.get("duration"), int | float):
        payload["durations"]["metadata"] = parsed["duration"]
    if name in {"audio_ffprobe", "video_ffprobe"}:
        format_payload = parsed.get("format")
        if not isinstance(format_payload, dict):
            payload["warnings"].append(f"{name} format payload is not an object")
            return
        duration = format_payload.get("duration")
        if duration is not None:
            try:
                payload["durations"][name] = float(duration)
            except ValueError:
                payload["warnings"].append(f"{name} duration is not numeric: {duration!r}")


def _add_time_span(payload: dict[str, Any], *, name: str, rows: list[dict[str, Any]]) -> None:
    starts = [row.get("start") for row in rows if isinstance(row.get("start"), int | float)]
    ends = [row.get("end") for row in rows if isinstance(row.get("end"), int | float)]
    if starts and ends:
        payload["durations"][f"{name}_span"] = round(max(ends) - min(starts), 3)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            string_key = str(key)
            if is_sensitive_key(string_key):
                sanitized[string_key] = REDACTED
            else:
                sanitized[string_key] = _sanitize(item)
        return sanitized
    if isinstance(value, list | tuple):
        return [_sanitize(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_sanitize(item) for item in sorted(value, key=str)]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if value is None or isinstance(value, int | float | bool):
        return value
    if hasattr(value, "model_dump"):
        return _sanitize(value.model_dump(mode="json"))
    return str(value)


def _enum_value(value: StrEnum | str | None) -> str | None:
    if isinstance(value, StrEnum):
        return value.value
    return value


def _event_value(event: PipelineEvent | str) -> str:
    if isinstance(event, PipelineEvent):
        return event.value
    return event

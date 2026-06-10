from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from style_kb.diagnostics import PipelineEvent, PipelineLogger, sanitize_diagnostic_value
from style_kb.errors import ExternalToolError
from style_kb.utils.files import append_text

_STREAM_LOG_LIMIT_BYTES = 8 * 1024
_ERROR_MESSAGE_LIMIT_BYTES = 2 * 1024
_SENSITIVE_ARG_FLAGS = {
    "--add-header",
    "--cookies",
    "--cookies-from-browser",
    "--password",
    "--username",
}


def run_subprocess(
    args: list[str],
    *,
    cwd: Path | None = None,
    error_code: str,
    log_path: Path | None = None,
    env: dict[str, str] | None = None,
    pipeline_logger: PipelineLogger | None = None,
    job_id: str | None = None,
    video_id: str | None = None,
    stage: str | None = None,
    ordinal: int | None = None,
    stdout_limit_bytes: int = _STREAM_LOG_LIMIT_BYTES,
    stderr_limit_bytes: int = _STREAM_LOG_LIMIT_BYTES,
    stdout_artifact: Path | None = None,
    text_log_streams: bool = True,
    diagnostics: dict[str, object] | None = None,
) -> subprocess.CompletedProcess[str]:
    started_at = datetime.now(tz=UTC)
    timer_started = perf_counter()
    redacted_args = redact_command_args(args)
    _emit_subprocess_event(
        pipeline_logger,
        PipelineEvent.SUBPROCESS_STARTED,
        job_id=job_id,
        video_id=video_id,
        stage=stage or _stage_name_from_log_path(log_path),
        ordinal=ordinal,
        log_path=log_path,
        message="subprocess started",
        data={
            "command": redacted_args,
            "cwd": str(cwd) if cwd else None,
            **(diagnostics or {}),
        },
    )
    finished_at = datetime.now(tz=UTC)
    duration_seconds = perf_counter() - timer_started
    try:
        completed_bytes = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            check=False,
        )
        completed = subprocess.CompletedProcess(
            completed_bytes.args,
            completed_bytes.returncode,
            stdout=completed_bytes.stdout.decode("utf-8", errors="replace"),
            stderr=completed_bytes.stderr.decode("utf-8", errors="replace"),
        )
        stdout_bytes = len(completed_bytes.stdout)
        stderr_bytes = len(completed_bytes.stderr)
        runtime_error = None
    except OSError as error:
        completed = subprocess.CompletedProcess(args, None, stdout="", stderr=str(error))
        stdout_bytes = 0
        stderr_bytes = len(str(error).encode("utf-8"))
        runtime_error = error
    finished_at = datetime.now(tz=UTC)
    duration_seconds = perf_counter() - timer_started
    stdout_log, stdout_truncated = _truncate_text(completed.stdout, stdout_limit_bytes)
    stderr_log, stderr_truncated = _truncate_text(completed.stderr, stderr_limit_bytes)
    warning_lines = _warning_lines(completed.stderr)

    if log_path is not None:
        stage_name = _stage_name_from_log_path(log_path)
        run_id = pipeline_logger.run_id if pipeline_logger is not None else "-"
        status = "ok" if completed.returncode == 0 else "failed"
        header_lines = [
            "",
            "[subprocess-attempt]",
            f"run_id: {run_id}",
            f"started_at: {started_at.isoformat()}",
            f"finished_at: {finished_at.isoformat()}",
            f"duration_seconds: {duration_seconds:.3f}",
            f"stage: {stage_name}",
            f"status: {status}",
            f"return_code: {completed.returncode}",
            f"stdout_bytes: {stdout_bytes}",
            f"stderr_bytes: {stderr_bytes}",
            f"stdout_logged_bytes: {len(stdout_log.encode('utf-8'))}",
            f"stderr_logged_bytes: {len(stderr_log.encode('utf-8'))}",
            f"stdout_limit_bytes: {stdout_limit_bytes}",
            f"stderr_limit_bytes: {stderr_limit_bytes}",
            f"stdout_truncated: {str(stdout_truncated).lower()}",
            f"stderr_truncated: {str(stderr_truncated).lower()}",
            f"warning_count: {len(warning_lines)}",
            "command: " + " ".join(redacted_args),
        ]
        if completed.returncode != 0:
            header_lines.append(f"failure_code: {error_code}")
        if cwd is not None:
            header_lines.append(f"cwd: {cwd}")
        if stdout_artifact is not None:
            header_lines.append(f"stdout_artifact: {stdout_artifact}")
        for key, value in (diagnostics or {}).items():
            header_lines.append(f"{key}: {value}")
        if text_log_streams:
            stream_lines = ["", "[stdout]", stdout_log, "", "[stderr]", stderr_log, ""]
        else:
            stream_lines = ["", "[streams]", "stdout/stderr omitted from text log; see byte counts and pipeline.jsonl", ""]
        append_text(log_path, "\n".join(header_lines + stream_lines), encoding="utf-8")

    event_data = {
        "command": redacted_args,
        "cwd": str(cwd) if cwd else None,
        "status": "ok" if completed.returncode == 0 else "failed",
        "return_code": completed.returncode,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "warning_count": len(warning_lines),
        "warnings": warning_lines[:5],
        "stdout_artifact": str(stdout_artifact) if stdout_artifact is not None else None,
        **(diagnostics or {}),
    }
    if completed.returncode != 0:
        event_data["failure_code"] = error_code
    _emit_subprocess_event(
        pipeline_logger,
        PipelineEvent.SUBPROCESS_COMPLETED if completed.returncode == 0 else PipelineEvent.SUBPROCESS_FAILED,
        job_id=job_id,
        video_id=video_id,
        stage=stage or _stage_name_from_log_path(log_path),
        ordinal=ordinal,
        log_path=log_path,
        message="subprocess completed" if completed.returncode == 0 else "subprocess failed",
        data=event_data,
    )
    if warning_lines:
        _emit_subprocess_event(
            pipeline_logger,
            PipelineEvent.WARNING,
            job_id=job_id,
            video_id=video_id,
            stage=stage or _stage_name_from_log_path(log_path),
            ordinal=ordinal,
            log_path=log_path,
            message="subprocess warnings",
            data=event_data,
        )

    if completed.returncode != 0:
        raw_message = completed.stderr.strip() or completed.stdout.strip() or "subprocess failed"
        message, _ = _truncate_text(raw_message, _ERROR_MESSAGE_LIMIT_BYTES)
        if log_path is not None:
            message = f"{message}\nsubprocess log: {log_path}"
        raise ExternalToolError(
            message,
            error_code=error_code,
            details=message,
            stage_name=_stage_name_from_log_path(log_path) if log_path is not None else None,
        ) from runtime_error

    return completed


def redact_command_args(args: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            redacted.append("[redacted]")
            redact_next = False
            continue
        if arg in _SENSITIVE_ARG_FLAGS:
            redacted.append(arg)
            redact_next = True
            continue
        if any(arg.startswith(f"{flag}=") for flag in _SENSITIVE_ARG_FLAGS):
            flag, _ = arg.split("=", 1)
            redacted.append(f"{flag}=[redacted]")
            continue
        redacted.append(str(sanitize_diagnostic_value(arg)))
    return redacted


def _stage_name_from_log_path(log_path: Path) -> str:
    if log_path is None:
        return "unknown"
    return log_path.stem.split(".", 1)[0]


def _truncate_text(text: str, limit_bytes: int) -> tuple[str, bool]:
    if limit_bytes < 0:
        return str(sanitize_diagnostic_value(text)), False
    payload = text.encode("utf-8")
    if len(payload) <= limit_bytes:
        return str(sanitize_diagnostic_value(text)), False
    truncated = payload[:limit_bytes].decode("utf-8", errors="replace")
    return str(sanitize_diagnostic_value(truncated)) + "\n[truncated]", True


def _warning_lines(stderr: str) -> list[str]:
    warnings = []
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith(("WARNING:", "ERROR:")):
            warnings.append(str(sanitize_diagnostic_value(stripped)))
    return warnings


def _emit_subprocess_event(
    pipeline_logger: PipelineLogger | None,
    event: PipelineEvent,
    *,
    job_id: str | None,
    video_id: str | None,
    stage: str | None,
    ordinal: int | None,
    log_path: Path | None,
    message: str,
    data: dict[str, object],
) -> None:
    if pipeline_logger is None or job_id is None or video_id is None:
        return
    pipeline_logger.emit(
        event,
        job_id=job_id,
        video_id=video_id,
        stage=stage,
        ordinal=ordinal,
        message=message,
        details_path=log_path,
        data=data,
    )

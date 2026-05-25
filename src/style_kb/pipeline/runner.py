from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from style_kb.config import default_config_path, load_default_config
from style_kb.errors import JobLockError, StageExecutionError, StyleKbError
from style_kb.models import Job, StageStatus
from style_kb.pipeline.base import StageContext
from style_kb.pipeline.catalog import STAGES
from style_kb.pipeline.paths import JobPaths
from style_kb.state.db import initialize_database
from style_kb.state.repository import StateRepository
from style_kb.utils.env import load_dotenv
from style_kb.utils.files import append_text
from style_kb.utils.youtube import extract_video_id


class PipelineRunner:
    def __init__(
        self,
        working_dir: Path | None = None,
        *,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.working_dir = working_dir or Path.cwd()
        load_dotenv(self.working_dir / ".env")
        self.config = load_default_config()
        self.output_root = (self.working_dir / self.config.project.output_dir).resolve()
        self.paths_root = JobPaths(self.output_root, "__bootstrap__")
        initialize_database(self.paths_root.database_path)
        self.repository = StateRepository(self.paths_root.database_path)
        self.progress_callback = progress_callback

    def ingest(self, url: str) -> Job:
        video_id = extract_video_id(url)
        return self._run_for_job(video_id=video_id, url=url, requested_job_id=video_id)

    def resume(self, job_id: str) -> Job:
        job = self.repository.get_job(job_id)
        if job is None:
            raise StyleKbError(f"job not found: {job_id}")
        return self._run_existing_job(job)

    def status(self, job_id: str) -> tuple[Job, list[StageStatus]]:
        job = self.repository.get_job(job_id)
        if job is None:
            raise StyleKbError(f"job not found: {job_id}")
        return job, self.repository.list_stages(job_id)

    def _run_for_job(self, *, video_id: str, url: str, requested_job_id: str) -> Job:
        paths = JobPaths(self.output_root, requested_job_id)
        paths.ensure_directories()
        job = self.repository.create_or_get_job(
            job_id=requested_job_id,
            video_id=video_id,
            url=url,
            job_dir=str(paths.job_dir),
            config_path=str(default_config_path()),
        )
        self.repository.ensure_stages(job.job_id, [(stage.ordinal, stage.name) for stage in STAGES])

        if job.status == "completed" and self._job_has_final_outputs(job):
            return job
        return self._run_existing_job(job)

    def _run_existing_job(self, job: Job) -> Job:
        self._acquire_job_lock(job)
        job = self.repository.update_job(
            job.job_id,
            status="running",
            started_at=job.started_at or datetime.now(tz=UTC),
            finished_at=None,
        )
        self.repository.clear_job_error(job.job_id)
        paths = JobPaths(self.output_root, job.job_id)
        paths.ensure_directories()

        try:
            for stage_class in STAGES:
                stage = stage_class()
                job = self.repository.update_job(job.job_id, current_stage=stage.name)
                context = StageContext(
                    config=self.config,
                    repository=self.repository,
                    job=job,
                    paths=paths,
                    progress_callback=self.progress_callback,
                )
                stage_row = self.repository.get_stage(job.job_id, stage.name)
                should_skip = stage.validate_outputs(context) and stage.outputs_are_current(context)
                if should_skip:
                    finished_stage = self.repository.mark_stage_finished(
                        job_id=job.job_id,
                        stage_name=stage.name,
                        status="completed",
                        output_files=[str(path) for path in stage.output_files(context) if path.exists()],
                        remote_refs=stage_row.remote_refs if stage_row else {},
                        metrics=stage_row.metrics if stage_row else {},
                    )
                    skipped_stage = finished_stage.model_copy(update={"status": "skipped"})
                    self._write_stage_reuse_log(paths=paths, stage_status=skipped_stage)
                    self._emit_stage_progress(skipped_stage)
                    continue

                self.repository.mark_stage_running(
                    job_id=job.job_id,
                    stage_name=stage.name,
                    input_files=[str(path) for path in stage.input_files(context)],
                )
                result = stage.run(context)
                finished_stage = self.repository.mark_stage_finished(
                    job_id=job.job_id,
                    stage_name=stage.name,
                    status="completed",
                    output_files=[str(path) for path in result.output_files],
                    remote_refs=result.remote_refs,
                    metrics=result.metrics,
                )
                self._write_stage_outcome_log(paths=paths, stage_status=finished_stage)
                self._emit_stage_progress(finished_stage)
                job = self.repository.get_job(job.job_id)
                if job is None:
                    raise StyleKbError("job disappeared during execution")
        except StageExecutionError as error:
            stage_name = job.current_stage or "unknown"
            error = error.with_stage(stage_name)
            failed_stage = self.repository.mark_stage_failed(
                job_id=job.job_id,
                stage_name=stage_name,
                error_code=error.error_code,
                error_message=str(error),
            )
            self._write_stage_outcome_log(paths=paths, stage_status=failed_stage, error=error)
            self.repository.update_job(
                job.job_id,
                status="failed",
                error_code=error.error_code,
                error_message=str(error),
                finished_at=None,
            )
            self._release_job_lock(job.job_id)
            raise
        except Exception as error:
            stage_name = self.repository.get_job(job.job_id).current_stage if self.repository.get_job(job.job_id) else "unknown"
            wrapped_error = StageExecutionError(
                f"unexpected error: {error}",
                error_code="unexpected_error",
                details=str(error),
                stage_name=stage_name or "unknown",
            )
            failed_stage = self.repository.mark_stage_failed(
                job_id=job.job_id,
                stage_name=stage_name or "unknown",
                error_code=wrapped_error.error_code,
                error_message=str(wrapped_error),
            )
            self._write_stage_outcome_log(paths=paths, stage_status=failed_stage, error=wrapped_error)
            self.repository.update_job(
                job.job_id,
                status="failed",
                error_code=wrapped_error.error_code,
                error_message=str(wrapped_error),
                finished_at=None,
            )
            self._release_job_lock(job.job_id)
            raise wrapped_error from error

        self.repository.update_job(
            job.job_id,
            status="completed",
            current_stage=STAGES[-1].name,
            finished_at=datetime.now(tz=UTC),
            error_code=None,
            error_message=None,
        )
        self._release_job_lock(job.job_id)
        final_job = self.repository.get_job(job.job_id)
        if final_job is None:
            raise StyleKbError(f"job not found after completion: {job.job_id}")
        return final_job

    def _acquire_job_lock(self, job: Job) -> None:
        live_pid = job.lock_pid if job.lock_pid and _is_pid_alive(job.lock_pid) else None
        if job.status == "running" and live_pid is not None and live_pid != os.getpid():
            raise JobLockError(f"job is already running: {job.job_id} (pid {live_pid})")
        self.repository.set_job_lock(job.job_id, pid=os.getpid(), acquired_at=datetime.now(tz=UTC))

    def _release_job_lock(self, job_id: str) -> None:
        self.repository.set_job_lock(job_id, pid=None, acquired_at=None)

    def _job_has_final_outputs(self, job: Job) -> bool:
        paths = JobPaths(self.output_root, job.job_id)
        return all(
            path.exists()
            for path in [
                paths.timeline_events_jsonl,
                paths.chunks_jsonl,
                paths.quality_report,
                paths.cleanup_report,
                paths.obsidian_index,
            ]
        )

    def _write_stage_outcome_log(
        self,
        *,
        paths: JobPaths,
        stage_status: StageStatus,
        error: StageExecutionError | None = None,
    ) -> None:
        lines = [
            "",
            "[stage-summary]",
            f"job_id: {stage_status.job_id}",
            f"stage: {stage_status.stage_name}",
            f"status: {stage_status.status}",
            f"attempt: {stage_status.attempt}",
            f"started_at: {stage_status.started_at.isoformat() if stage_status.started_at else '-'}",
            f"finished_at: {stage_status.finished_at.isoformat() if stage_status.finished_at else '-'}",
            f"error_code: {stage_status.error_code or '-'}",
            f"error_message: {stage_status.error_message or '-'}",
            "input_files:",
        ]
        if stage_status.input_files:
            lines.extend(f"  - {path}" for path in stage_status.input_files)
        else:
            lines.append("  -")
        lines.append("output_files:")
        if stage_status.output_files:
            lines.extend(f"  - {path}" for path in stage_status.output_files)
        else:
            lines.append("  -")
        lines.append("remote_refs:")
        lines.append(_pretty_json(stage_status.remote_refs))
        lines.append("metrics:")
        lines.append(_pretty_json(stage_status.metrics))
        if error is not None and error.details:
            lines.append("details:")
            lines.append(error.details)
        lines.append("")
        append_text(paths.stage_log(stage_status.stage_name), "\n".join(lines), encoding="utf-8")

    def _write_stage_reuse_log(self, *, paths: JobPaths, stage_status: StageStatus) -> None:
        reused_at = datetime.now(tz=UTC).isoformat()
        lines = [
            "",
            "[stage-reuse]",
            f"job_id: {stage_status.job_id}",
            f"stage: {stage_status.stage_name}",
            f"status: {stage_status.status}",
            f"attempt: {stage_status.attempt}",
            f"reused_at: {reused_at}",
            "output_files:",
        ]
        if stage_status.output_files:
            lines.extend(f"  - {path}" for path in stage_status.output_files)
        else:
            lines.append("  -")
        lines.append("metrics:")
        lines.append(_pretty_json(stage_status.metrics))
        lines.append("")
        append_text(paths.stage_log(stage_status.stage_name), "\n".join(lines), encoding="utf-8")

    def _emit_stage_progress(self, stage_status: StageStatus) -> None:
        if self.progress_callback is None:
            return
        self.progress_callback(
            f"[{stage_status.ordinal:02d} {stage_status.stage_name}] {stage_status.status}"
        )


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pretty_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)

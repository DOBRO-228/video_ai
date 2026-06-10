from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from style_kb.config import default_config_path, load_default_config
from style_kb.config.models import AppConfig
from style_kb.diagnostics import (
    PipelineEvent,
    PipelineLogger,
    new_run_id,
    write_failure_report,
    write_partial_quality_report,
)
from style_kb.diagnostics_env import run_environment_snapshot
from style_kb.error_advice import advice_for_error_code
from style_kb.errors import JobLockError, StageExecutionError, StyleKbError
from style_kb.models import Job, JobState, StageState, StageStatus
from style_kb.pipeline.base import StageContext
from style_kb.pipeline.catalog import STAGES
from style_kb.pipeline.paths import JobPaths
from style_kb.state.db import initialize_database
from style_kb.state.repository import StateRepository
from style_kb.utils.env import load_dotenv
from style_kb.utils.files import append_text, read_json, write_json_atomic
from style_kb.utils.json import pretty_json
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
        self.run_id: str | None = None
        self._stage_run_start_logs: set[tuple[str, str]] = set()

    def ingest(self, url: str) -> Job:
        self._start_run()
        video_id = extract_video_id(url)
        return self._run_for_job(video_id=video_id, url=url, requested_job_id=video_id)

    def resume(self, job_id: str) -> Job:
        self._start_run()
        job = self.repository.get_job(job_id)
        if job is None:
            raise StyleKbError(f"job not found: {job_id}")
        try:
            self.repository.ensure_stages(job.job_id, [(stage.ordinal, stage.name) for stage in STAGES])
        except Exception as error:
            paths = JobPaths(self.output_root, job.job_id)
            paths.ensure_directories()
            pipeline_logger = self._pipeline_logger(paths)
            self._emit_run_started(pipeline_logger, job)
            self._emit_run_failed(
                pipeline_logger,
                job=job,
                status=job.status,
                error=error,
                message="failed before job execution",
            )
            self._write_partial_quality_report_safely(paths=paths, job=job, failed_stage="pipeline_setup")
            raise
        return self._run_existing_job(job, job_event=PipelineEvent.JOB_RESUMED)

    def status(self, job_id: str) -> tuple[Job, list[StageStatus]]:
        job = self.repository.get_job(job_id)
        if job is None:
            raise StyleKbError(f"job not found: {job_id}")
        return job, self.repository.list_stages(job_id)

    def _run_for_job(self, *, video_id: str, url: str, requested_job_id: str) -> Job:
        paths = JobPaths(self.output_root, requested_job_id)
        paths.ensure_directories()
        existing_job = self.repository.get_job(requested_job_id)
        job = self.repository.create_or_get_job(
            job_id=requested_job_id,
            video_id=video_id,
            url=url,
            job_dir=str(paths.job_dir),
            config_path=str(default_config_path()),
        )
        try:
            self.repository.ensure_stages(job.job_id, [(stage.ordinal, stage.name) for stage in STAGES])
        except Exception as error:
            pipeline_logger = self._pipeline_logger(paths)
            self._emit_run_started(pipeline_logger, job)
            if existing_job is None:
                self._emit_job_created(pipeline_logger, job)
            self._emit_run_failed(
                pipeline_logger,
                job=job,
                status=job.status,
                error=error,
                message="failed before job execution",
            )
            self._write_partial_quality_report_safely(paths=paths, job=job, failed_stage="pipeline_setup")
            raise

        if job.status == JobState.COMPLETED and self._job_has_final_outputs(job):
            pipeline_logger = self._pipeline_logger(paths)
            self._emit_run_started(pipeline_logger, job)
            self._emit_completed_job_reuse_decisions(pipeline_logger, job=job, paths=paths)
            self._resolve_failure_report_safely(paths=paths, job=job)
            pipeline_logger.emit(
                PipelineEvent.JOB_COMPLETED,
                job_id=job.job_id,
                video_id=job.video_id,
                status=job.status,
                message="job already completed; final outputs are current",
                data={"job_dir": job.job_dir},
            )
            pipeline_logger.emit(
                PipelineEvent.RUN_COMPLETED,
                job_id=job.job_id,
                video_id=job.video_id,
                status=job.status,
                message="run completed without executing stages",
            )
            return job
        return self._run_existing_job(
            job,
            job_event=PipelineEvent.JOB_STARTED,
            job_created=existing_job is None,
        )

    def _run_existing_job(self, job: Job, *, job_event: PipelineEvent, job_created: bool = False) -> Job:
        paths = JobPaths(self.output_root, job.job_id)
        paths.ensure_directories()
        pipeline_logger = self._pipeline_logger(paths)
        self._emit_run_started(pipeline_logger, job)
        if job_created:
            self._emit_job_created(pipeline_logger, job)
        pipeline_logger.emit(
            job_event,
            job_id=job.job_id,
            video_id=job.video_id,
            status=job.status,
            message="job run requested",
            data={"job_dir": job.job_dir},
        )
        try:
            self._acquire_job_lock(job)
        except JobLockError as error:
            pipeline_logger.emit(
                PipelineEvent.RUN_FAILED,
                job_id=job.job_id,
                video_id=job.video_id,
                status=job.status,
                message=str(error),
                data={"error_type": type(error).__name__},
            )
            raise
        except Exception as error:
            setup_error = self._write_setup_failure_report(paths=paths, pipeline_logger=pipeline_logger, job=job, error=error)
            raise setup_error from error
        try:
            job = self.repository.update_job(
                job.job_id,
                status=JobState.RUNNING,
                started_at=job.started_at or datetime.now(tz=UTC),
                finished_at=None,
            )
            self.repository.clear_job_error(job.job_id)
        except Exception as error:
            setup_error = self._write_setup_failure_report(paths=paths, pipeline_logger=pipeline_logger, job=job, error=error)
            self._release_job_lock_safely(job.job_id)
            raise setup_error from error

        try:
            for stage_class in STAGES:
                stage = stage_class()
                job = self.repository.update_job(job.job_id, current_stage=stage.name)
                context = StageContext(
                    config=self.config,
                    repository=self.repository,
                    job=job,
                    paths=paths,
                    progress_callback=self._progress_callback(
                        pipeline_logger,
                        job=job,
                        stage=stage.name,
                        ordinal=stage.ordinal,
                    ),
                    pipeline_logger=pipeline_logger,
                    run_id=self._current_run_id(),
                )
                stage_row = self.repository.get_stage(job.job_id, stage.name)
                self._write_stage_run_start(paths=paths, stage_name=stage.name)
                self._emit_stage_reuse_decision(pipeline_logger, job=job, paths=paths, stage=stage, context=context)
                should_skip = stage.validate_outputs(context) and stage.outputs_are_current(context)
                if should_skip:
                    finished_stage = self.repository.mark_stage_finished(
                        job_id=job.job_id,
                        stage_name=stage.name,
                        status=StageState.COMPLETED,
                        output_files=[str(path) for path in stage.output_files(context) if path.exists()],
                        remote_refs=stage_row.remote_refs if stage_row else {},
                        metrics=stage_row.metrics if stage_row else {},
                    )
                    skipped_stage = finished_stage.model_copy(update={"status": StageState.SKIPPED})
                    pipeline_logger.emit(
                        PipelineEvent.STAGE_SKIPPED,
                        job_id=job.job_id,
                        video_id=job.video_id,
                        stage=stage.name,
                        ordinal=stage.ordinal,
                        attempt=skipped_stage.attempt,
                        status=StageState.SKIPPED,
                        message="stage outputs are current",
                        details_path=paths.stage_log(stage.name),
                        data=self._stage_event_data(skipped_stage),
                    )
                    self._write_stage_reuse_log(paths=paths, stage_status=skipped_stage)
                    self._emit_stage_progress(skipped_stage, pipeline_logger=pipeline_logger, job=job)
                    continue

                running_stage = self.repository.mark_stage_running(
                    job_id=job.job_id,
                    stage_name=stage.name,
                    input_files=[str(path) for path in stage.input_files(context)],
                )
                pipeline_logger.emit(
                    PipelineEvent.STAGE_STARTED,
                    job_id=job.job_id,
                    video_id=job.video_id,
                    stage=stage.name,
                    ordinal=stage.ordinal,
                    attempt=running_stage.attempt,
                    status=running_stage.status,
                    message="stage started",
                    details_path=paths.stage_log(stage.name),
                    data={"input_files": running_stage.input_files},
                )
                result = stage.run(context)
                finished_stage = self.repository.mark_stage_finished(
                    job_id=job.job_id,
                    stage_name=stage.name,
                    status=StageState.COMPLETED,
                    output_files=[str(path) for path in result.output_files],
                    remote_refs=result.remote_refs,
                    metrics=result.metrics,
                )
                self._write_stage_outcome_log(paths=paths, stage_status=finished_stage)
                pipeline_logger.emit(
                    PipelineEvent.STAGE_COMPLETED,
                    job_id=job.job_id,
                    video_id=job.video_id,
                    stage=stage.name,
                    ordinal=stage.ordinal,
                    attempt=finished_stage.attempt,
                    status=finished_stage.status,
                    message="stage completed",
                    details_path=paths.stage_log(stage.name),
                    data=self._stage_event_data(finished_stage),
                )
                self._emit_stage_progress(finished_stage, pipeline_logger=pipeline_logger, job=job)
                job = self.repository.get_job(job.job_id)
                if job is None:
                    raise StyleKbError("job disappeared during execution")
        except StageExecutionError as error:
            stage_name = job.current_stage or "unknown"
            error = error.with_stage(stage_name)
            failed_stage = self._safe_mark_stage_failed(
                job_id=job.job_id,
                stage_name=stage_name,
                error_code=error.error_code,
                error_message=str(error),
            )
            stage_input_files, stage_output_files = self._failure_stage_files(
                job=job,
                paths=paths,
                stage_name=stage_name,
                stage_status=failed_stage,
            )
            write_failure_report(
                paths,
                job=job,
                stage_status=failed_stage,
                error=error,
                stage_input_files=stage_input_files,
                stage_output_files=stage_output_files,
            )
            self._write_partial_quality_report_safely(paths=paths, job=job, failed_stage=stage_name)
            self._write_stage_outcome_log(paths=paths, stage_status=failed_stage, error=error)
            pipeline_logger.emit(
                PipelineEvent.STAGE_FAILED,
                job_id=job.job_id,
                video_id=job.video_id,
                stage=stage_name,
                ordinal=failed_stage.ordinal,
                attempt=failed_stage.attempt,
                status=failed_stage.status,
                message=str(error),
                details_path=paths.stage_log(stage_name),
                data={
                    **self._stage_event_data(failed_stage),
                    "error_code": error.error_code,
                    "error_message": str(error),
                    "advice": self._error_advice_data(error.error_code, job=job, stage_name=stage_name),
                },
            )
            self.repository.update_job(
                job.job_id,
                status=JobState.FAILED,
                error_code=error.error_code,
                error_message=str(error),
                finished_at=None,
            )
            pipeline_logger.emit(
                PipelineEvent.JOB_FAILED,
                job_id=job.job_id,
                video_id=job.video_id,
                status=JobState.FAILED,
                message=str(error),
                data={
                    "error_code": error.error_code,
                    "error_message": str(error),
                    "advice": self._error_advice_data(error.error_code, job=job, stage_name=stage_name),
                },
            )
            pipeline_logger.emit(
                PipelineEvent.RUN_FAILED,
                job_id=job.job_id,
                video_id=job.video_id,
                status=JobState.FAILED,
                message=str(error),
                data={
                    "error_code": error.error_code,
                    "error_message": str(error),
                    "advice": self._error_advice_data(error.error_code, job=job, stage_name=stage_name),
                },
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
            failed_stage = self._safe_mark_stage_failed(
                job_id=job.job_id,
                stage_name=stage_name or "unknown",
                error_code=wrapped_error.error_code,
                error_message=str(wrapped_error),
            )
            stage_input_files, stage_output_files = self._failure_stage_files(
                job=job,
                paths=paths,
                stage_name=stage_name or "unknown",
                stage_status=failed_stage,
            )
            write_failure_report(
                paths,
                job=job,
                stage_status=failed_stage,
                error=error,
                stage_input_files=stage_input_files,
                stage_output_files=stage_output_files,
            )
            self._write_partial_quality_report_safely(paths=paths, job=job, failed_stage=stage_name or "unknown")
            self._write_stage_outcome_log(paths=paths, stage_status=failed_stage, error=wrapped_error)
            pipeline_logger.emit(
                PipelineEvent.STAGE_FAILED,
                job_id=job.job_id,
                video_id=job.video_id,
                stage=stage_name or "unknown",
                ordinal=failed_stage.ordinal,
                attempt=failed_stage.attempt,
                status=failed_stage.status,
                message=str(wrapped_error),
                details_path=paths.stage_log(stage_name or "unknown"),
                data={
                    **self._stage_event_data(failed_stage),
                    "error_code": wrapped_error.error_code,
                    "error_message": str(wrapped_error),
                    "error_type": type(error).__name__,
                    "advice": self._error_advice_data(wrapped_error.error_code, job=job, stage_name=stage_name or "unknown"),
                },
            )
            self.repository.update_job(
                job.job_id,
                status=JobState.FAILED,
                error_code=wrapped_error.error_code,
                error_message=str(wrapped_error),
                finished_at=None,
            )
            pipeline_logger.emit(
                PipelineEvent.JOB_FAILED,
                job_id=job.job_id,
                video_id=job.video_id,
                status=JobState.FAILED,
                message=str(wrapped_error),
                data={
                    "error_code": wrapped_error.error_code,
                    "error_message": str(wrapped_error),
                    "error_type": type(error).__name__,
                    "advice": self._error_advice_data(wrapped_error.error_code, job=job, stage_name=stage_name or "unknown"),
                },
            )
            pipeline_logger.emit(
                PipelineEvent.RUN_FAILED,
                job_id=job.job_id,
                video_id=job.video_id,
                status=JobState.FAILED,
                message=str(wrapped_error),
                data={
                    "error_code": wrapped_error.error_code,
                    "error_message": str(wrapped_error),
                    "error_type": type(error).__name__,
                    "advice": self._error_advice_data(wrapped_error.error_code, job=job, stage_name=stage_name or "unknown"),
                },
            )
            self._release_job_lock(job.job_id)
            raise wrapped_error from error

        completed_job = self.repository.update_job(
            job.job_id,
            status=JobState.COMPLETED,
            current_stage=STAGES[-1].name,
            finished_at=datetime.now(tz=UTC),
            error_code=None,
            error_message=None,
        )
        self._release_job_lock(job.job_id)
        final_job = self.repository.get_job(job.job_id)
        if final_job is None:
            raise StyleKbError(f"job not found after completion: {job.job_id}")
        self._resolve_failure_report_safely(paths=paths, job=final_job)
        pipeline_logger.emit(
            PipelineEvent.JOB_COMPLETED,
            job_id=final_job.job_id,
            video_id=final_job.video_id,
            status=completed_job.status,
            message="job completed",
            data={"job_dir": final_job.job_dir},
        )
        pipeline_logger.emit(
            PipelineEvent.RUN_COMPLETED,
            job_id=final_job.job_id,
            video_id=final_job.video_id,
            status=completed_job.status,
            message="run completed",
        )
        return final_job

    def _pipeline_logger(self, paths: JobPaths) -> PipelineLogger:
        return PipelineLogger(
            jsonl_path=paths.pipeline_events_jsonl,
            human_path=paths.pipeline_human_log,
            run_id=self._current_run_id(),
        )

    def _start_run(self) -> None:
        self.run_id = new_run_id()
        self._stage_run_start_logs.clear()

    def _current_run_id(self) -> str:
        if self.run_id is None:
            self._start_run()
        return self.run_id or new_run_id()

    @staticmethod
    def _emit_job_created(pipeline_logger: PipelineLogger, job: Job) -> None:
        pipeline_logger.emit(
            PipelineEvent.JOB_CREATED,
            job_id=job.job_id,
            video_id=job.video_id,
            status=job.status,
            message="job record created",
            data={"job_dir": job.job_dir},
        )

    def _emit_run_started(self, pipeline_logger: PipelineLogger, job: Job) -> None:
        pipeline_logger.emit(
            PipelineEvent.RUN_STARTED,
            job_id=job.job_id,
            video_id=job.video_id,
            status="started",
            message="run started",
            data={
                "job_dir": job.job_dir,
                "previous_job_status": job.status,
                "run_status": "started",
                **run_environment_snapshot(config=self.config, config_path=default_config_path()),
            },
        )

    def _emit_completed_job_reuse_decisions(self, pipeline_logger: PipelineLogger, *, job: Job, paths: JobPaths) -> None:
        context = StageContext(
            config=self.config,
            repository=self.repository,
            job=job,
            paths=paths,
        )
        for stage_class in STAGES:
            stage = stage_class()
            self._write_stage_run_start(paths=paths, stage_name=stage.name)
            data = self._stage_reuse_decision_data(stage, context)
            if _stage_outputs_may_be_cleaned(stage.name, self.config) and not data["can_skip"]:
                data = {
                    **data,
                    "can_skip": True,
                    "reasons": [
                        "stage outputs may be cleaned by config; final job outputs are current",
                        *list(data["reasons"]),
                    ],
                }
            stage_row = self.repository.get_stage(job.job_id, stage.name)
            if stage_row is not None:
                self._write_stage_reuse_log(
                    paths=paths,
                    stage_status=stage_row.model_copy(
                        update={
                            "status": StageState.SKIPPED,
                            "input_files": _stage_input_file_strings(stage, context, fallback=stage_row.input_files),
                            "output_files": _stage_output_file_strings(stage, context, fallback=stage_row.output_files),
                        }
                    ),
                )
            self._emit_stage_reuse_decision_data(
                pipeline_logger,
                job=job,
                paths=paths,
                stage=stage,
                data=data,
            )

    def _emit_stage_reuse_decision(
        self,
        pipeline_logger: PipelineLogger,
        *,
        job: Job,
        paths: JobPaths,
        stage,
        context: StageContext,
    ) -> None:
        self._emit_stage_reuse_decision_data(
            pipeline_logger,
            job=job,
            paths=paths,
            stage=stage,
            data=self._stage_reuse_decision_data(stage, context),
        )

    @staticmethod
    def _emit_stage_reuse_decision_data(
        pipeline_logger: PipelineLogger,
        *,
        job: Job,
        paths: JobPaths,
        stage,
        data: dict[str, object],
    ) -> None:
        can_skip = bool(data.get("can_skip"))
        pipeline_logger.emit(
            PipelineEvent.STAGE_REUSE_DECISION,
            job_id=job.job_id,
            video_id=job.video_id,
            stage=stage.name,
            ordinal=stage.ordinal,
            status=StageState.SKIPPED if can_skip else StageState.RUNNING,
            message="stage reuse decision",
            details_path=paths.stage_log(stage.name),
            data=data,
        )

    @staticmethod
    def _stage_reuse_decision_data(stage, context: StageContext) -> dict[str, object]:
        try:
            return stage.diagnose_outputs(context).model_dump(mode="json")
        except Exception as error:
            return {
                "stage_name": stage.name,
                "valid_outputs": False,
                "outputs_current": False,
                "can_skip": False,
                "reasons": [f"diagnose_outputs raised {type(error).__name__}: {error}"],
                "missing_input_files": [],
                "missing_output_files": [],
                "stale_output_files": [],
            }

    @staticmethod
    def _error_advice_data(error_code: str | None, *, job: Job, stage_name: str | None) -> dict[str, object] | None:
        advice = advice_for_error_code(error_code, job_id=job.job_id, stage_name=stage_name)
        return advice.model_dump(mode="json") if advice is not None else None

    @staticmethod
    def _emit_run_failed(
        pipeline_logger: PipelineLogger,
        *,
        job: Job,
        status: JobState | StageState | str,
        error: BaseException,
        message: str,
    ) -> None:
        pipeline_logger.emit(
            PipelineEvent.RUN_FAILED,
            job_id=job.job_id,
            video_id=job.video_id,
            status=status,
            message=f"{message}: {error}",
            data={"error_type": type(error).__name__, "error_message": str(error)},
        )

    def _progress_callback(
        self,
        pipeline_logger: PipelineLogger,
        *,
        job: Job,
        stage: str | None = None,
        ordinal: int | None = None,
    ) -> Callable[[str], None]:
        def _callback(message: str) -> None:
            pipeline_logger.emit(
                PipelineEvent.PROGRESS,
                job_id=job.job_id,
                video_id=job.video_id,
                stage=stage,
                ordinal=ordinal,
                message=message,
                data={"line": message},
            )
            if self.progress_callback is not None:
                self.progress_callback(message)

        return _callback

    @staticmethod
    def _stage_event_data(stage_status: StageStatus) -> dict[str, object]:
        return {
            "input_files": stage_status.input_files,
            "output_files": stage_status.output_files,
            "remote_refs": stage_status.remote_refs,
            "metrics": stage_status.metrics,
        }

    def _failure_stage_files(
        self,
        *,
        job: Job,
        paths: JobPaths,
        stage_name: str,
        stage_status: StageStatus,
    ) -> tuple[list[Path], list[Path]]:
        context = StageContext(
            config=self.config,
            repository=self.repository,
            job=job,
            paths=paths,
        )
        stage = _stage_by_name(stage_name)
        if stage is None:
            return [Path(path) for path in stage_status.input_files], [Path(path) for path in stage_status.output_files]
        try:
            input_files = list(stage.input_files(context))
        except Exception:
            input_files = [Path(path) for path in stage_status.input_files]
        try:
            output_files = list(stage.output_files(context))
        except Exception:
            output_files = [Path(path) for path in stage_status.output_files]
        return input_files, output_files

    def _safe_mark_stage_failed(
        self,
        *,
        job_id: str,
        stage_name: str,
        error_code: str,
        error_message: str,
    ) -> StageStatus:
        try:
            return self.repository.mark_stage_failed(
                job_id=job_id,
                stage_name=stage_name,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception as state_error:
            return self._fallback_failed_stage_status(
                job_id=job_id,
                stage_name=stage_name,
                error_code=error_code,
                error_message=error_message,
                state_error=state_error,
            )

    def _fallback_failed_stage_status(
        self,
        *,
        job_id: str,
        stage_name: str,
        error_code: str,
        error_message: str,
        state_error: BaseException | None = None,
    ) -> StageStatus:
        stage_row = None
        try:
            stage_row = self.repository.get_stage(job_id, stage_name)
        except Exception:
            stage_row = None
        metrics = dict(stage_row.metrics) if stage_row is not None else {}
        if state_error is not None:
            metrics["stage_state_update_error"] = f"{type(state_error).__name__}: {state_error}"
        if stage_row is not None:
            return stage_row.model_copy(
                update={
                    "status": StageState.FAILED,
                    "error_code": error_code,
                    "error_message": error_message,
                    "metrics": metrics,
                }
            )
        return StageStatus(
            job_id=job_id,
            stage_name=stage_name,
            ordinal=_stage_ordinal(stage_name),
            status=StageState.FAILED,
            attempt=0,
            error_code=error_code,
            error_message=error_message,
            metrics=metrics,
        )

    def _write_setup_failure_report(
        self,
        *,
        paths: JobPaths,
        pipeline_logger: PipelineLogger,
        job: Job,
        error: BaseException,
    ) -> StageExecutionError:
        setup_error = StageExecutionError(
            f"unexpected setup error: {error}; failure_report: {paths.failure_report}",
            error_code="pipeline_setup_failed",
            details=str(error),
            stage_name="pipeline_setup",
        )
        failed_stage = self._fallback_failed_stage_status(
            job_id=job.job_id,
            stage_name="pipeline_setup",
            error_code=setup_error.error_code,
            error_message=str(setup_error),
        )
        write_failure_report(
            paths,
            job=job,
            stage_status=failed_stage,
            error=error,
            stage_input_files=[],
            stage_output_files=[],
        )
        self._write_partial_quality_report_safely(paths=paths, job=job, failed_stage="pipeline_setup")
        pipeline_logger.emit(
            PipelineEvent.RUN_FAILED,
            job_id=job.job_id,
            video_id=job.video_id,
            stage="pipeline_setup",
            ordinal=failed_stage.ordinal,
            attempt=failed_stage.attempt,
            status=JobState.FAILED,
            message=str(setup_error),
            data={
                "error_code": setup_error.error_code,
                "error_message": str(setup_error),
                "error_type": type(error).__name__,
                "advice": self._error_advice_data(setup_error.error_code, job=job, stage_name="pipeline_setup"),
            },
        )
        return setup_error

    @staticmethod
    def _write_partial_quality_report_safely(*, paths: JobPaths, job: Job, failed_stage: str | None) -> None:
        try:
            write_partial_quality_report(paths, job=job, failed_stage=failed_stage)
        except Exception:
            return

    def _resolve_failure_report_safely(self, *, paths: JobPaths, job: Job) -> None:
        if not paths.failure_report.exists():
            return
        try:
            payload = read_json(paths.failure_report)
            resolved_at = datetime.now(tz=UTC).isoformat()
            history_payload = {
                **(payload if isinstance(payload, dict) else {"payload": payload}),
                "status": "resolved",
                "resolution": {
                    "resolved_at": resolved_at,
                    "resolved_by_run_id": self._current_run_id(),
                    "resolved_job_status": job.status.value,
                    "job_finished_at": job.finished_at.isoformat() if job.finished_at else None,
                },
            }
            history_path = paths.failure_history_report(self._current_run_id())
            write_json_atomic(history_path, history_payload)
            paths.failure_report.unlink()
            append_text(
                paths.pipeline_human_log,
                "\n".join(
                    [
                        "",
                        "[failure-report-resolved]",
                        f"run_id: {self._current_run_id()}",
                        f"job_id: {job.job_id}",
                        f"resolved_at: {resolved_at}",
                        f"history_path: {history_path}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
        except Exception:
            return

    def _acquire_job_lock(self, job: Job) -> None:
        live_pid = job.lock_pid if job.lock_pid and _is_pid_alive(job.lock_pid) else None
        if job.status == JobState.RUNNING and live_pid is not None and live_pid != os.getpid():
            raise JobLockError(f"job is already running: {job.job_id} (pid {live_pid})")
        self.repository.set_job_lock(job.job_id, pid=os.getpid(), acquired_at=datetime.now(tz=UTC))

    def _release_job_lock(self, job_id: str) -> None:
        self.repository.set_job_lock(job_id, pid=None, acquired_at=None)

    def _release_job_lock_safely(self, job_id: str) -> None:
        try:
            self._release_job_lock(job_id)
        except Exception:
            return

    def _job_has_final_outputs(self, job: Job) -> bool:
        paths = JobPaths(self.output_root, job.job_id)
        if not all(
            path.exists()
            for path in [
                paths.timeline_events_jsonl,
                paths.chunks_jsonl,
                paths.style_claims_jsonl,
                paths.quality_report,
                paths.cleanup_report,
                paths.obsidian_index,
            ]
        ):
            return False

        context = StageContext(
            config=self.config,
            repository=self.repository,
            job=job,
            paths=paths,
            progress_callback=self.progress_callback,
        )
        for stage_class in STAGES:
            stage = stage_class()
            if _stage_outputs_may_be_cleaned(stage.name, self.config):
                continue
            try:
                outputs_current = stage.validate_outputs(context) and stage.outputs_are_current(context)
            except Exception:
                return False
            if not outputs_current:
                return False
        return True

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
            f"run_id: {self._current_run_id()}",
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
        lines.extend(_file_manifest_lines(stage_status.input_files))
        lines.append("output_files:")
        lines.extend(_file_manifest_lines(stage_status.output_files))
        lines.append("remote_refs:")
        lines.append(pretty_json(stage_status.remote_refs))
        lines.append("metrics:")
        lines.append(pretty_json(stage_status.metrics))
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
            f"run_id: {self._current_run_id()}",
            f"job_id: {stage_status.job_id}",
            f"stage: {stage_status.stage_name}",
            f"status: {stage_status.status}",
            f"attempt: {stage_status.attempt}",
            f"reused_at: {reused_at}",
            "input_files:",
        ]
        lines.extend(_file_manifest_lines(stage_status.input_files))
        lines.extend(
            [
                "output_files:",
            ]
        )
        lines.extend(_file_manifest_lines(stage_status.output_files))
        lines.extend(
            [
                "metrics:",
                pretty_json(stage_status.metrics),
                "",
            ]
        )
        append_text(paths.stage_log(stage_status.stage_name), "\n".join(lines), encoding="utf-8")

    def _write_stage_run_start(self, *, paths: JobPaths, stage_name: str) -> None:
        run_id = self._current_run_id()
        key = (run_id, stage_name)
        if key in self._stage_run_start_logs:
            return
        lines = [
            "",
            "[run-start]",
            f"run_id: {run_id}",
            f"started_at: {datetime.now(tz=UTC).isoformat()}",
            "",
        ]
        append_text(paths.stage_log(stage_name), "\n".join(lines), encoding="utf-8")
        self._stage_run_start_logs.add(key)

    def _emit_stage_progress(
        self,
        stage_status: StageStatus,
        *,
        pipeline_logger: PipelineLogger,
        job: Job,
    ) -> None:
        self._progress_callback(
            pipeline_logger,
            job=job,
            stage=stage_status.stage_name,
            ordinal=stage_status.ordinal,
        )(
            f"[{stage_status.ordinal:02d} {stage_status.stage_name}] {stage_status.status}"
        )


def _stage_input_file_strings(stage, context: StageContext, *, fallback: list[str]) -> list[str]:
    try:
        return [str(path) for path in stage.input_files(context)]
    except Exception:
        return fallback


def _stage_output_file_strings(stage, context: StageContext, *, fallback: list[str]) -> list[str]:
    try:
        return [str(path) for path in stage.output_files(context)]
    except Exception:
        return fallback


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stage_outputs_may_be_cleaned(stage_name: str, config: AppConfig) -> bool:
    if stage_name in {"02_download_audio", "03_download_video_proxy"} and not config.project.keep_media:
        return True
    return stage_name == "09_extract_keyframes" and not config.project.keep_frames


def _stage_by_name(stage_name: str):
    for stage_class in STAGES:
        if stage_class.name == stage_name:
            return stage_class()
    return None


def _stage_ordinal(stage_name: str) -> int:
    stage = _stage_by_name(stage_name)
    return stage.ordinal if stage is not None else 0


def _file_manifest_lines(paths: list[str]) -> list[str]:
    if not paths:
        return ["  - []"]
    lines: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        exists = path.exists()
        size_bytes: int | str = "-"
        mtime = "-"
        if exists:
            try:
                stat = path.stat()
                size_bytes = stat.st_size
                mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
            except OSError as error:
                mtime = f"stat_error:{type(error).__name__}"
        lines.extend(
            [
                f"  - path: {path}",
                f"    exists: {str(exists).lower()}",
                f"    size_bytes: {size_bytes}",
                f"    mtime: {mtime}",
            ]
        )
    return lines

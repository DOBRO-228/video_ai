from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from style_kb.error_advice import advice_for_error_code
from style_kb.errors import StyleKbError
from style_kb.models import Job, JobState, StageState, StageStatus
from style_kb.pipeline.paths import JobPaths
from style_kb.pipeline.runner import PipelineRunner
from style_kb.utils.files import read_json

app = typer.Typer(add_completion=False, no_args_is_help=True)

StopAfterStageArg = Annotated[
    int | None,
    typer.Argument(
        min=1,
        help="Optional stage number to stop immediately after. Example: 9 stops before visual description.",
    ),
]


@app.command()
def ingest(url: str, stop_after_stage: StopAfterStageArg = None) -> None:
    """Ingest exactly one YouTube video URL."""
    runner = PipelineRunner(Path.cwd(), progress_callback=typer.echo)
    _run_command(lambda: runner.ingest(url, stop_after_stage=stop_after_stage), action="ingest")


@app.command()
def status(job_id: str) -> None:
    """Print job and stage status."""
    runner = PipelineRunner(Path.cwd())
    try:
        job, stages = runner.status(job_id)
    except StyleKbError as error:
        typer.secho(str(error), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from error

    typer.echo(f"job_id: {job.job_id}")
    typer.echo(f"status: {job.status}")
    typer.echo(f"video_id: {job.video_id}")
    typer.echo(f"url: {job.url}")
    typer.echo(f"current_stage: {job.current_stage or '-'}")
    typer.echo(f"title: {job.title or '-'}")
    typer.echo(f"channel: {job.channel or '-'}")
    typer.echo(f"job_dir: {job.job_dir}")
    if job.error_code or job.error_message:
        typer.echo(f"error: {job.error_code or '-'} {job.error_message or ''}".strip())
    paths = JobPaths(runner.output_root, job.job_id)
    artifact_paths = paths.artifact_summary()
    typer.echo("artifacts:")
    for key, value in artifact_paths.items():
        typer.echo(f"  {key}: {value}")
    _print_diagnostics_block(job, stages, paths)
    typer.echo("")
    typer.echo("stages:")
    for stage in stages:
        _print_stage_status(stage, job=job, paths=paths)


@app.command()
def resume(job_id: str, stop_after_stage: StopAfterStageArg = None) -> None:
    """Resume an existing job."""
    runner = PipelineRunner(Path.cwd(), progress_callback=typer.echo)
    _run_command(lambda: runner.resume(job_id, stop_after_stage=stop_after_stage), action="resume")


def _run_command(func, *, action: str) -> None:
    try:
        job = func()
    except StyleKbError as error:
        typer.secho(f"{action} failed: {error}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from error
    typer.echo(f"job_id: {job.job_id}")
    typer.echo(f"status: {job.status}")
    typer.echo(f"job_dir: {job.job_dir}")
    for key, value in _artifact_paths(job.job_id).items():
        typer.echo(f"{key}: {value}")


def _artifact_paths(job_id: str) -> dict[str, str]:
    runner = PipelineRunner(Path.cwd())
    paths = JobPaths(runner.output_root, job_id)
    return paths.artifact_summary()


def _print_diagnostics_block(job: Job, stages: list[StageStatus], paths: JobPaths) -> None:
    failed_stage = next((stage for stage in stages if stage.status == StageState.FAILED), None)
    has_failure_history = bool(_latest_failure_history(paths))
    if job.status != JobState.FAILED and failed_stage is None and not paths.failure_report.exists() and not has_failure_history:
        return
    typer.echo("diagnostics:")
    if paths.failure_report.exists():
        typer.echo(f"  current_failure_report: {paths.failure_report}")
    else:
        typer.echo("  current_failure_report: none")
    latest_history = _latest_failure_history(paths)
    if latest_history is not None:
        payload = _read_json_safely(latest_history)
        resolution = payload.get("resolution") if isinstance(payload, dict) else None
        typer.echo(f"  last_failure_status: {payload.get('status') if isinstance(payload, dict) else 'resolved'}")
        if isinstance(resolution, dict):
            typer.echo(f"  last_failure_resolved_at: {resolution.get('resolved_at') or '-'}")
            typer.echo(f"  last_failure_resolved_by_run_id: {resolution.get('resolved_by_run_id') or '-'}")
        typer.echo(f"  last_failure_report: {latest_history}")
    typer.echo(f"  pipeline_log: {paths.pipeline_log}")
    if failed_stage is not None:
        typer.echo(f"  failed_stage_log: {paths.stage_log(failed_stage.stage_name)}")
    advice = advice_for_error_code(
        job.error_code or (failed_stage.error_code if failed_stage is not None else None),
        job_id=job.job_id,
        stage_name=failed_stage.stage_name if failed_stage is not None else job.current_stage,
    )
    if advice is not None:
        typer.echo(f"  advice: {advice.summary}")
        for action in advice.actions:
            typer.echo(f"  action: {action}")
        for inspect_path in advice.inspect[:4]:
            typer.echo(f"  inspect: {inspect_path}")
    typer.echo(f"  resume: style-kb resume {job.job_id}")


def _latest_failure_history(paths: JobPaths) -> Path | None:
    if not paths.failure_history_dir.exists():
        return None
    candidates = sorted(paths.failure_history_dir.glob("failure_report_resolved_*.json"), key=lambda path: path.stat().st_mtime)
    return candidates[-1] if candidates else None


def _read_json_safely(path: Path) -> dict:
    try:
        payload = read_json(path)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _print_stage_status(stage: StageStatus, *, job: Job, paths: JobPaths) -> None:
    log_path = paths.stage_log(stage.stage_name)
    parts = [
        f"  {stage.ordinal:02d} {stage.stage_name:<28} {stage.status:<9}",
        f"attempts={stage.attempt}",
    ]
    if stage.error_code:
        parts.append(f"error={stage.error_code}")
    if log_path.exists():
        parts.append(f"log={log_path}")
    typer.echo(" ".join(parts))
    if stage.status == StageState.FAILED or stage.stage_name == job.current_stage:
        _print_stage_diagnostics(stage, job=job, paths=paths)


def _print_stage_diagnostics(stage: StageStatus, *, job: Job, paths: JobPaths) -> None:
    duration = _duration_seconds(stage)
    values = {
        "error_code": stage.error_code,
        "error_message": stage.error_message,
        "started_at": stage.started_at.isoformat() if stage.started_at else None,
        "finished_at": stage.finished_at.isoformat() if stage.finished_at else None,
        "duration_seconds": f"{duration:.3f}" if duration is not None else None,
        "log_path": str(paths.stage_log(stage.stage_name)),
        "failure_report": (
            str(paths.failure_report)
            if paths.failure_report.exists() and (job.status == JobState.FAILED or stage.status == StageState.FAILED)
            else None
        ),
        "input_files": str(len(stage.input_files)),
        "output_files": str(len(stage.output_files)),
        "metrics": _compact_mapping(stage.metrics),
    }
    for key, value in values.items():
        if value is not None and value != "":
            typer.echo(f"      {key}: {value}")


def _duration_seconds(stage: StageStatus) -> float | None:
    if stage.started_at is None or stage.finished_at is None:
        return None
    return max(0.0, (stage.finished_at - stage.started_at).total_seconds())


def _compact_mapping(value: dict[str, object], *, limit: int = 6) -> str:
    if not value:
        return ""
    items = list(value.items())
    rendered = [f"{key}={_compact_value(item)}" for key, item in items[:limit]]
    if len(items) > limit:
        rendered.append(f"+{len(items) - limit} more")
    return ", ".join(rendered)


def _compact_value(value: object, *, max_chars: int = 80) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, int | float | bool) or value is None:
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


if __name__ == "__main__":
    app()

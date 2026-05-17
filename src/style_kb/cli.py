from __future__ import annotations

from pathlib import Path

import typer

from style_kb.errors import StyleKbError
from style_kb.pipeline.paths import JobPaths
from style_kb.pipeline.runner import PipelineRunner

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command()
def ingest(url: str) -> None:
    """Ingest exactly one YouTube video URL."""
    runner = PipelineRunner(Path.cwd(), progress_callback=typer.echo)
    _run_command(lambda: runner.ingest(url), action="ingest")


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
    artifact_paths = _artifact_paths(job.job_id)
    typer.echo("artifacts:")
    for key, value in artifact_paths.items():
        typer.echo(f"  {key}: {value}")
    typer.echo("")
    typer.echo("stages:")
    for stage in stages:
        typer.echo(
            f"  {stage.ordinal:02d} {stage.stage_name:<28} {stage.status:<9} attempts={stage.attempt}"
        )


@app.command()
def resume(job_id: str) -> None:
    """Resume an existing job."""
    runner = PipelineRunner(Path.cwd(), progress_callback=typer.echo)
    _run_command(lambda: runner.resume(job_id), action="resume")


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


if __name__ == "__main__":
    app()

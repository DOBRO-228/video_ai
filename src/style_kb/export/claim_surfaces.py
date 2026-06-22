from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from style_kb.config.models import AppConfig
from style_kb.export.jsonl import patch_style_claims_manifest_metadata, write_style_claims_export
from style_kb.export.obsidian import render_obsidian_export
from style_kb.models import QualityReport, StageState
from style_kb.pipeline.paths import JobPaths
from style_kb.state.repository import StateRepository
from style_kb.stages.common import (
    effective_style_claims_path_for_paths,
    load_chunks,
    load_frame_refs,
    load_style_claims,
    load_timeline_events,
    load_video_info,
)
from style_kb.stages.stage_16_quality_report import (
    Stage16QualityReport,
    build_quality_report,
    quality_report_stage_metrics,
)
from style_kb.utils.files import read_json, read_jsonl
from style_kb.utils.pydantic_io import write_model


@dataclass(slots=True)
class ClaimSurfaceRefreshResult:
    output_files: list[Path] = field(default_factory=list)
    jsonl_refreshed: bool = False
    jsonl_skipped: bool = False
    jsonl_error: str | None = None
    manifest_refreshed: bool = False
    obsidian_refreshed: bool = False
    obsidian_skipped: bool = False
    obsidian_error: str | None = None
    stale_obsidian_notes_removed: int = 0
    quality_report_refreshed: bool = False
    quality_report_skipped: bool = False
    quality_report_error: str | None = None

    @property
    def has_errors(self) -> bool:
        return self.jsonl_error is not None or self.obsidian_error is not None or self.quality_report_error is not None

    def stale_payload(self) -> dict[str, Any]:
        stale: dict[str, Any] = {}
        if self.jsonl_error:
            stale["exports/jsonl"] = self.jsonl_error
        if self.obsidian_error:
            stale["exports/obsidian"] = self.obsidian_error
        if self.quality_report_error:
            stale["reports/quality_report.json"] = self.quality_report_error
        return stale


def refresh_existing_claim_surfaces(
    *,
    paths: JobPaths,
    config: AppConfig,
    repository: StateRepository,
    refresh_jsonl: bool = True,
    refresh_obsidian: bool = True,
    refresh_quality_report: bool = True,
) -> ClaimSurfaceRefreshResult:
    result = ClaimSurfaceRefreshResult()
    effective_claims_path = effective_style_claims_path_for_paths(paths)
    if refresh_jsonl:
        _refresh_existing_jsonl_surface(paths=paths, effective_claims_path=effective_claims_path, result=result)
    if refresh_obsidian:
        _refresh_existing_obsidian_surface(paths=paths, config=config, result=result)
    if refresh_quality_report:
        _refresh_existing_quality_report_surface(paths=paths, config=config, repository=repository, result=result)
    return result


def _refresh_existing_jsonl_surface(
    *,
    paths: JobPaths,
    effective_claims_path: Path,
    result: ClaimSurfaceRefreshResult,
) -> None:
    export_path = paths.export_jsonl("style_claims.jsonl")
    manifest_path = paths.export_jsonl("manifest.json")
    if not export_path.exists():
        result.jsonl_skipped = True
        return
    if not manifest_path.exists():
        result.jsonl_error = f"missing JSONL manifest: {manifest_path}"
        return
    try:
        manifest = read_json(manifest_path)
        if not isinstance(manifest, dict):
            raise ValueError(f"manifest must be a JSON object: {manifest_path}")
        patched_manifest = {
            **manifest,
            **_claim_manifest_patch(paths=paths, effective_claims_path=effective_claims_path),
        }
        current_rows = _read_dict_rows(effective_claims_path)
        if _read_dict_rows(export_path) != current_rows:
            write_style_claims_export(paths.export_jsonl_dir, current_rows)
            result.jsonl_refreshed = True
            result.output_files.append(export_path)
        if manifest != patched_manifest:
            patch_style_claims_manifest_metadata(
                manifest_path=manifest_path,
                style_claims_path=effective_claims_path,
                job_dir=paths.job_dir,
            )
            result.manifest_refreshed = True
            result.output_files.append(manifest_path)
    except Exception as error:
        result.jsonl_error = f"{type(error).__name__}: {error}"


def _claim_manifest_patch(*, paths: JobPaths, effective_claims_path: Path) -> dict[str, Any]:
    from style_kb.export.jsonl import style_claims_manifest_metadata

    return style_claims_manifest_metadata(effective_claims_path, job_dir=paths.job_dir)


def _refresh_existing_obsidian_surface(
    *,
    paths: JobPaths,
    config: AppConfig,
    result: ClaimSurfaceRefreshResult,
) -> None:
    if not _obsidian_surface_exists(paths):
        result.obsidian_skipped = True
        return
    try:
        before_mtimes = _markdown_mtimes(paths.export_obsidian_dir)
        outputs, removed = render_obsidian_claim_surface(paths=paths, config=config, write_if_changed=True)
        after_mtimes = _markdown_mtimes(paths.export_obsidian_dir)
        result.obsidian_refreshed = removed > 0 or before_mtimes != after_mtimes
        result.stale_obsidian_notes_removed = removed
        result.output_files.extend(outputs)
    except Exception as error:
        result.obsidian_error = f"{type(error).__name__}: {error}"


def _refresh_existing_quality_report_surface(
    *,
    paths: JobPaths,
    config: AppConfig,
    repository: StateRepository,
    result: ClaimSurfaceRefreshResult,
) -> None:
    try:
        job = repository.get_job(paths.job_id)
        if job is None:
            raise ValueError(f"job not found: {paths.job_id}")
        if job.lock_pid and job.lock_pid != os.getpid() and _is_pid_alive(job.lock_pid):
            raise ValueError(f"job is locked by running pipeline process pid={job.lock_pid}")
        report = build_quality_report(config=config, job=job, paths=paths)
        if not _quality_report_needs_refresh(paths=paths, report=report, repository=repository):
            result.quality_report_skipped = True
            return
        report_written = _quality_report_file_needs_refresh(paths=paths, report=report)
        if report_written:
            write_model(paths.quality_report, report)
            result.output_files.append(paths.quality_report)
        _mark_quality_report_stage_finished(paths=paths, report=report, repository=repository)
        result.quality_report_refreshed = True
    except Exception as error:
        result.quality_report_error = f"{type(error).__name__}: {error}"


def _quality_report_needs_refresh(*, paths: JobPaths, report: QualityReport, repository: StateRepository) -> bool:
    if _quality_report_file_needs_refresh(paths=paths, report=report):
        return True
    stage = repository.get_stage(paths.job_id, Stage16QualityReport.name)
    if stage is None or stage.finished_at is None:
        return True
    if stage.metrics != quality_report_stage_metrics(report):
        return True
    report_mtime = paths.quality_report.stat().st_mtime
    return report_mtime > stage.finished_at.timestamp()


def _quality_report_file_needs_refresh(*, paths: JobPaths, report: QualityReport) -> bool:
    if not paths.quality_report.exists():
        return True
    try:
        existing = QualityReport.model_validate(read_json(paths.quality_report))
    except Exception:
        return True
    if existing.model_dump(mode="json") != report.model_dump(mode="json"):
        return True
    effective_claims_path = effective_style_claims_path_for_paths(paths)
    return effective_claims_path.exists() and paths.quality_report.stat().st_mtime < effective_claims_path.stat().st_mtime


def _mark_quality_report_stage_finished(
    *,
    paths: JobPaths,
    report: QualityReport,
    repository: StateRepository,
) -> None:
    repository.mark_stage_finished(
        job_id=paths.job_id,
        stage_name=Stage16QualityReport.name,
        status=StageState.COMPLETED,
        output_files=[str(paths.quality_report)],
        remote_refs={},
        metrics=quality_report_stage_metrics(report),
    )


def render_obsidian_claim_surface(
    *,
    paths: JobPaths,
    config: AppConfig,
    write_if_changed: bool = False,
) -> tuple[list[Path], int]:
    video = load_video_info(paths.metadata_video_info)
    timeline_events = load_timeline_events(paths.timeline_events_jsonl)
    chunks = load_chunks(paths.chunks_jsonl)
    style_claims = load_style_claims(effective_style_claims_path_for_paths(paths))
    frame_refs = load_frame_refs(paths.frame_refs_jsonl) if config.pipeline.visual_enabled else []
    frame_map: dict[str, list[str]] = defaultdict(list)
    if config.pipeline.visual_enabled and config.project.keep_frames:
        for frame in frame_refs:
            frame_path = paths.job_dir / frame.path
            relative_link = frame_path.relative_to(paths.job_dir).as_posix()
            frame_map[frame.scene_id].append(f"../../../{relative_link}")

    removed = remove_stale_obsidian_chunk_notes(paths=paths, expected_chunk_ids={chunk.chunk_id for chunk in chunks})
    outputs = render_obsidian_export(
        templates_dir=Path(__file__).resolve().parents[1] / "templates",
        index_title=config.project.name,
        video=video,
        timeline_events=timeline_events,
        chunks=chunks,
        style_claims=style_claims,
        obsidian_index_path=paths.obsidian_index,
        video_note_path=paths.obsidian_video_note(video.video_id),
        chunk_note_paths={chunk.chunk_id: paths.obsidian_chunk_note(chunk.chunk_id) for chunk in chunks},
        event_frame_links=frame_map,
        write_if_changed=write_if_changed,
    )
    return outputs, removed


def remove_stale_obsidian_chunk_notes(*, paths: JobPaths, expected_chunk_ids: set[str]) -> int:
    chunks_dir = paths.export_obsidian_dir / "chunks"
    if not chunks_dir.exists():
        return 0
    removed = 0
    expected_names = {f"{chunk_id}.md" for chunk_id in expected_chunk_ids}
    for note_path in chunks_dir.glob("*.md"):
        if note_path.name not in expected_names and is_stage_owned_obsidian_chunk_note(paths, note_path):
            note_path.unlink()
            removed += 1
    return removed


def is_stage_owned_obsidian_chunk_note(paths: JobPaths, note_path: Path) -> bool:
    pattern = rf"{re.escape(paths.job_id)}_c_\d{{6}}_\d{{6}}\.md"
    return re.fullmatch(pattern, note_path.name) is not None


def _obsidian_surface_exists(paths: JobPaths) -> bool:
    if paths.obsidian_index.exists():
        return True
    if (paths.export_obsidian_dir / "videos").exists() and any((paths.export_obsidian_dir / "videos").glob("*.md")):
        return True
    if (paths.export_obsidian_dir / "chunks").exists() and any((paths.export_obsidian_dir / "chunks").glob("*.md")):
        return True
    return False


def _markdown_mtimes(root: Path) -> dict[str, int]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.stat().st_mtime_ns
        for path in root.rglob("*.md")
        if path.is_file()
    }


def _read_dict_rows(path: Path) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    return [row for row in rows if isinstance(row, dict)]


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

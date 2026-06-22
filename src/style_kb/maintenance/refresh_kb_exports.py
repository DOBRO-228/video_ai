from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from style_kb.config import load_default_config
from style_kb.export.claim_surfaces import refresh_existing_claim_surfaces
from style_kb.models import JobState
from style_kb.pipeline.paths import JobPaths
from style_kb.state.repository import StateRepository


def refresh_kb_exports(*, working_dir: Path | None = None) -> dict[str, Any]:
    root = working_dir or Path.cwd()
    config = load_default_config()
    output_root = (root / config.project.output_dir).resolve()
    db_path = output_root / "jobs.sqlite3"
    summary: dict[str, Any] = {
        "output_root": str(output_root),
        "database_path": str(db_path),
        "jobs": [],
        "counts": {
            "jobs_seen": 0,
            "jobs_refreshed": 0,
            "jobs_skipped": 0,
            "jsonl_refreshed": 0,
            "manifest_refreshed": 0,
            "obsidian_refreshed": 0,
            "quality_report_refreshed": 0,
            "stale_obsidian_notes_removed": 0,
            "errors": 0,
        },
    }
    if not db_path.exists():
        summary["error"] = f"database not found: {db_path}"
        return summary

    repository = StateRepository(db_path)
    for job in repository.list_jobs():
        summary["counts"]["jobs_seen"] += 1
        paths = JobPaths(output_root, job.job_id)
        job_entry: dict[str, Any] = {
            "job_id": job.job_id,
            "status": job.status.value,
            "jsonl_refreshed": False,
            "manifest_refreshed": False,
            "obsidian_refreshed": False,
            "quality_report_refreshed": False,
            "stale_obsidian_notes_removed": 0,
            "skipped": None,
            "errors": {},
        }
        if job.status != JobState.COMPLETED:
            job_entry["skipped"] = "job is not completed"
            summary["counts"]["jobs_skipped"] += 1
            summary["jobs"].append(job_entry)
            continue
        if job.lock_pid and _is_pid_alive(job.lock_pid):
            job_entry["skipped"] = f"job is locked by pid {job.lock_pid}"
            summary["counts"]["jobs_skipped"] += 1
            summary["jobs"].append(job_entry)
            continue

        result = refresh_existing_claim_surfaces(paths=paths, config=config, repository=repository)
        job_entry.update(
            {
                "jsonl_refreshed": result.jsonl_refreshed,
                "jsonl_skipped": result.jsonl_skipped,
                "manifest_refreshed": result.manifest_refreshed,
                "obsidian_refreshed": result.obsidian_refreshed,
                "obsidian_skipped": result.obsidian_skipped,
                "quality_report_refreshed": result.quality_report_refreshed,
                "quality_report_skipped": result.quality_report_skipped,
                "stale_obsidian_notes_removed": result.stale_obsidian_notes_removed,
                "errors": result.stale_payload(),
            }
        )
        if (
            result.jsonl_refreshed
            or result.manifest_refreshed
            or result.obsidian_refreshed
            or result.quality_report_refreshed
            or result.stale_obsidian_notes_removed
        ):
            summary["counts"]["jobs_refreshed"] += 1
        if result.jsonl_refreshed:
            summary["counts"]["jsonl_refreshed"] += 1
        if result.manifest_refreshed:
            summary["counts"]["manifest_refreshed"] += 1
        if result.obsidian_refreshed:
            summary["counts"]["obsidian_refreshed"] += 1
        if result.quality_report_refreshed:
            summary["counts"]["quality_report_refreshed"] += 1
        summary["counts"]["stale_obsidian_notes_removed"] += result.stale_obsidian_notes_removed
        if result.has_errors:
            summary["counts"]["errors"] += 1
        summary["jobs"].append(job_entry)
    return summary


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


def main() -> None:
    print(json.dumps(refresh_kb_exports(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

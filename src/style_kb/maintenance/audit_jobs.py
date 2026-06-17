from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from style_kb.config import load_default_config
from style_kb.pipeline.paths import JobPaths
from style_kb.stages.common import effective_style_claims_path_for_paths, jsonl_rows_equal
from style_kb.stages.stage_10_describe_visuals import (
    presentation_noise_markers,
    technical_visual_markers,
)
from style_kb.utils.files import read_json, write_json_atomic

_MONITORED_EVENTS = {
    "stage_failed",
    "job_failed",
    "run_failed",
    "provider_request_failed",
    "stage_validation_failed",
    "warning",
    "subprocess_failed",
}
_VISUAL_FIELDS = ("visual_summary", "observations", "interpretations", "items", "style_topics", "notes")
_CHUNK_FIELDS = ("visual_text", "presenter_brief", "topics", "entities")


def audit_jobs(*, working_dir: Path | None = None, write_snapshot: bool = True) -> dict[str, Any]:
    root = working_dir or Path.cwd()
    config = load_default_config()
    output_root = (root / config.project.output_dir).resolve()
    db_path = output_root / "jobs.sqlite3"
    snapshot = _empty_snapshot(output_root=output_root, db_path=db_path)
    if not db_path.exists():
        snapshot["errors"].append(f"database not found: {db_path}")
        _finalize_counters(snapshot)
        return _write_snapshot(output_root, snapshot) if write_snapshot else snapshot

    jobs = _read_jobs(db_path)
    stages = _read_stages(db_path)
    snapshot["jobs"] = [_job_payload(job) for job in jobs]
    snapshot["stage_attempts"] = [_stage_payload(stage) for stage in stages]
    _audit_stage_state(snapshot, stages)

    for job in jobs:
        paths = JobPaths(output_root, str(job["job_id"]))
        _audit_job_artifacts(snapshot, job=job, stages=stages, paths=paths)
        _audit_pipeline_events(snapshot, paths=paths)

    _finalize_counters(snapshot)
    if write_snapshot:
        return _write_snapshot(output_root, snapshot)
    return snapshot


def _empty_snapshot(*, output_root: Path, db_path: Path) -> dict[str, Any]:
    now = datetime.now(tz=UTC).isoformat()
    return {
        "schema_version": 1,
        "created_at": now,
        "output_root": str(output_root),
        "database_path": str(db_path),
        "jobs": [],
        "stage_attempts": [],
        "counts": {
            "events_by_event": Counter(),
            "events_by_stage": Counter(),
            "events_by_job_id": Counter(),
            "events_by_error_code": Counter(),
            "stage_status": Counter(),
            "job_status": Counter(),
        },
        "affected_ratios": {},
        "quality_warnings": [],
        "quality_warning_templates": Counter(),
        "provider_failures": [],
        "provider_failures_by_status_code": Counter(),
        "subprocess_success_error_code_leaks": [],
        "stale_failure_reports": [],
        "failure_history": [],
        "drift_suspicions": [],
        "claim_export_drifts": [],
        "quality_report_drifts": [],
        "obsidian_drifts": [],
        "dashboard_overlay_jobs": [],
        "top_markers": {
            "technical_visual": Counter(),
            "presentation_visual": Counter(),
            "presentation_chunks": Counter(),
        },
        "errors": [],
    }


def _read_jobs(db_path: Path) -> list[sqlite3.Row]:
    with closing(_connect_readonly(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM jobs ORDER BY created_at ASC").fetchall()


def _read_stages(db_path: Path) -> list[sqlite3.Row]:
    with closing(_connect_readonly(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM stages ORDER BY job_id ASC, ordinal ASC").fetchall()


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_sqlite_readonly_uri(db_path), uri=True, timeout=5.0)


def _sqlite_readonly_uri(db_path: Path) -> str:
    return f"file:{quote(str(db_path.resolve()), safe='/:')}?mode=ro"


def _job_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "video_id": row["video_id"],
        "status": row["status"],
        "current_stage": row["current_stage"],
        "title": row["title"],
        "channel": row["channel"],
        "job_dir": row["job_dir"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
    }


def _stage_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "stage_name": row["stage_name"],
        "ordinal": row["ordinal"],
        "status": row["status"],
        "attempt": row["attempt"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "metrics": _json_or_empty(row["metrics"]),
    }


def _audit_stage_state(snapshot: dict[str, Any], stages: list[sqlite3.Row]) -> None:
    for stage in stages:
        snapshot["counts"]["stage_status"][stage["status"]] += 1


def _audit_job_artifacts(
    snapshot: dict[str, Any],
    *,
    job: sqlite3.Row,
    stages: list[sqlite3.Row],
    paths: JobPaths,
) -> None:
    snapshot["counts"]["job_status"][job["status"]] += 1
    if paths.failure_report.exists():
        failure_payload = _read_json_or_empty(paths.failure_report)
        entry = {
            "job_id": job["job_id"],
            "status": failure_payload.get("status"),
            "failed_stage": failure_payload.get("failed_stage"),
            "error_code": (failure_payload.get("error") or {}).get("code") if isinstance(failure_payload.get("error"), dict) else None,
            "path": str(paths.failure_report),
        }
        if job["status"] == "completed" and failure_payload.get("status") != "resolved":
            snapshot["stale_failure_reports"].append(entry)
        else:
            snapshot["failure_history"].append(entry)

    if paths.failure_history_dir.exists():
        for path in sorted(paths.failure_history_dir.glob("failure_report_resolved_*.json")):
            payload = _read_json_or_empty(path)
            resolution = payload.get("resolution") if isinstance(payload.get("resolution"), dict) else {}
            snapshot["failure_history"].append(
                {
                    "job_id": job["job_id"],
                    "status": payload.get("status"),
                    "failed_stage": payload.get("failed_stage"),
                    "resolved_at": resolution.get("resolved_at"),
                    "resolved_by_run_id": resolution.get("resolved_by_run_id"),
                    "path": str(path),
                }
            )

    quality_report = _read_json_or_none(paths.quality_report)
    if isinstance(quality_report, dict):
        _audit_quality_report(snapshot, job=job, stages=stages, paths=paths, quality_report=quality_report)
    if job["status"] == "completed":
        _audit_claim_export_drift(snapshot, job=job, paths=paths)
        _audit_obsidian_drift(snapshot, job=job, paths=paths)
    _audit_visual_markers(snapshot, paths=paths)
    _audit_chunk_markers(snapshot, paths=paths)


def _audit_quality_report(
    snapshot: dict[str, Any],
    *,
    job: sqlite3.Row,
    stages: list[sqlite3.Row],
    paths: JobPaths,
    quality_report: dict[str, Any],
) -> None:
    warnings = quality_report.get("warnings") if isinstance(quality_report.get("warnings"), list) else []
    for warning in warnings:
        text = str(warning)
        ratio = _affected_ratio(text)
        snapshot["quality_warnings"].append(
            {
                "job_id": job["job_id"],
                "warning": text,
                "template": _warning_template(text),
                "affected_ratio": ratio,
            }
        )
        snapshot["quality_warning_templates"][_warning_template(text)] += 1
    stage = next((candidate for candidate in stages if candidate["job_id"] == job["job_id"] and candidate["stage_name"] == "16_quality_report"), None)
    if stage is not None:
        metrics = _json_or_empty(stage["metrics"])
        finished_at = _parse_iso(stage["finished_at"])
        report_mtime = datetime.fromtimestamp(paths.quality_report.stat().st_mtime, tz=UTC)
        if finished_at is not None and report_mtime > finished_at:
            snapshot["drift_suspicions"].append(
                {
                    "job_id": job["job_id"],
                    "kind": "quality_report_newer_than_db_stage",
                    "quality_report_mtime": report_mtime.isoformat(),
                    "db_stage_finished_at": finished_at.isoformat(),
                    "path": str(paths.quality_report),
                }
            )
        warnings_count = metrics.get("warnings_count")
        if warnings_count is not None and int(warnings_count) != len(warnings):
            snapshot["drift_suspicions"].append(
                {
                    "job_id": job["job_id"],
                    "kind": "quality_warning_count_mismatch",
                    "artifact_warnings_count": len(warnings),
                    "db_warnings_count": warnings_count,
                    "path": str(paths.quality_report),
                }
            )
    if quality_report.get("job_id") != job["job_id"]:
        snapshot["drift_suspicions"].append(
            {
                "job_id": job["job_id"],
                "kind": "quality_report_job_id_mismatch",
                "artifact_job_id": quality_report.get("job_id"),
                "db_job_id": job["job_id"],
                "path": str(paths.quality_report),
            }
        )
    if quality_report.get("video_id") != job["video_id"]:
        snapshot["drift_suspicions"].append(
            {
                "job_id": job["job_id"],
                "kind": "quality_report_video_id_mismatch",
                "artifact_video_id": quality_report.get("video_id"),
                "db_video_id": job["video_id"],
                "path": str(paths.quality_report),
            }
        )
    effective_claims_path = effective_style_claims_path_for_paths(paths)
    effective_claims_count = len(_read_jsonl(effective_claims_path))
    report_claims_count = (quality_report.get("stage_counts") or {}).get("style_claims")
    if report_claims_count != effective_claims_count:
        entry = {
            "job_id": job["job_id"],
            "kind": "quality_style_claims_count_mismatch",
            "quality_report_count": report_claims_count,
            "effective_claims_count": effective_claims_count,
            "path": str(paths.quality_report),
        }
        snapshot["quality_report_drifts"].append(entry)
        snapshot["drift_suspicions"].append(entry)
    if paths.quality_report.exists() and effective_claims_path.exists():
        report_mtime = paths.quality_report.stat().st_mtime
        claims_mtime = effective_claims_path.stat().st_mtime
        if report_mtime < claims_mtime:
            entry = {
                "job_id": job["job_id"],
                "kind": "quality_report_older_than_effective_claims",
                "quality_report_mtime": datetime.fromtimestamp(report_mtime, tz=UTC).isoformat(),
                "effective_claims_mtime": datetime.fromtimestamp(claims_mtime, tz=UTC).isoformat(),
                "path": str(paths.quality_report),
            }
            snapshot["quality_report_drifts"].append(entry)
            snapshot["drift_suspicions"].append(entry)
    artifact_status = quality_report.get("status")
    if artifact_status is not None and artifact_status != job["status"]:
        snapshot["drift_suspicions"].append(
            {
                "job_id": job["job_id"],
                "kind": "quality_report_status_mismatch",
                "artifact_status": artifact_status,
                "db_status": job["status"],
                "path": str(paths.quality_report),
            }
        )


def _audit_visual_markers(snapshot: dict[str, Any], *, paths: JobPaths) -> None:
    for row in _read_jsonl(paths.visual_events_jsonl):
        for field in _VISUAL_FIELDS:
            snapshot["top_markers"]["technical_visual"].update(technical_visual_markers(row.get(field)))
            snapshot["top_markers"]["presentation_visual"].update(presentation_noise_markers(row.get(field)))
        presenter_context = row.get("presenter_context")
        if isinstance(presenter_context, dict):
            snapshot["top_markers"]["presentation_visual"].update(
                presentation_noise_markers(presenter_context.get("scene_deltas"))
            )


def _audit_chunk_markers(snapshot: dict[str, Any], *, paths: JobPaths) -> None:
    for row in _read_jsonl(paths.chunks_jsonl):
        for field in _CHUNK_FIELDS:
            snapshot["top_markers"]["presentation_chunks"].update(presentation_noise_markers(row.get(field)))
        snapshot["top_markers"]["presentation_chunks"].update(
            presentation_noise_markers(_chunk_combined_visual_component(row))
        )


def _audit_claim_export_drift(snapshot: dict[str, Any], *, job: sqlite3.Row, paths: JobPaths) -> None:
    effective_claims_path = effective_style_claims_path_for_paths(paths)
    if paths.style_claims_current_jsonl.exists():
        snapshot["dashboard_overlay_jobs"].append(
            {
                "job_id": job["job_id"],
                "effective_claims_path": str(effective_claims_path),
            }
        )
    export_path = paths.export_jsonl("style_claims.jsonl")
    if not effective_claims_path.exists() or not export_path.exists():
        return
    if paths.style_claims_current_jsonl.exists() and paths.style_claims_current_jsonl.stat().st_mtime > export_path.stat().st_mtime:
        entry = {
            "job_id": job["job_id"],
            "kind": "style_claims_current_newer_than_jsonl_export",
            "effective_claims_path": str(effective_claims_path),
            "export_path": str(export_path),
        }
        snapshot["claim_export_drifts"].append(entry)
        snapshot["drift_suspicions"].append(entry)
    if not jsonl_rows_equal(effective_claims_path, export_path):
        entry = {
            "job_id": job["job_id"],
            "kind": "jsonl_export_differs_from_effective_claims",
            "effective_claims_count": len(_read_jsonl(effective_claims_path)),
            "export_claims_count": len(_read_jsonl(export_path)),
            "effective_claims_path": str(effective_claims_path),
            "export_path": str(export_path),
        }
        snapshot["claim_export_drifts"].append(entry)
        snapshot["drift_suspicions"].append(entry)


def _audit_obsidian_drift(snapshot: dict[str, Any], *, job: sqlite3.Row, paths: JobPaths) -> None:
    effective_claims_path = effective_style_claims_path_for_paths(paths)
    if not effective_claims_path.exists():
        return
    claims_mtime = effective_claims_path.stat().st_mtime
    video_note = paths.obsidian_video_note(str(job["video_id"]))
    if video_note.exists() and video_note.stat().st_mtime < claims_mtime:
        entry = {
            "job_id": job["job_id"],
            "kind": "obsidian_video_note_older_than_effective_claims",
            "path": str(video_note),
        }
        snapshot["obsidian_drifts"].append(entry)
        snapshot["drift_suspicions"].append(entry)
    chunks = _read_jsonl(paths.chunks_jsonl)
    expected_names = {f"{row.get('chunk_id')}.md" for row in chunks if row.get("chunk_id")}
    chunks_dir = paths.export_obsidian_dir / "chunks"
    existing_names = {path.name for path in chunks_dir.glob("*.md")} if chunks_dir.exists() else set()
    missing = sorted(expected_names - existing_names)
    extra = sorted(existing_names - expected_names)
    if missing or extra:
        entry = {
            "job_id": job["job_id"],
            "kind": "obsidian_chunk_note_set_mismatch",
            "missing": missing,
            "extra": extra,
            "chunks_dir": str(chunks_dir),
        }
        snapshot["obsidian_drifts"].append(entry)
        snapshot["drift_suspicions"].append(entry)


def _chunk_combined_visual_component(row: dict[str, Any]) -> str:
    return "\n".join(
        str(part).strip()
        for part in [row.get("presenter_brief"), row.get("visual_text")]
        if str(part or "").strip()
    )


def _audit_pipeline_events(snapshot: dict[str, Any], *, paths: JobPaths) -> None:
    for event in _read_jsonl(paths.pipeline_events_jsonl):
        event_name = str(event.get("event") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event_name == "subprocess_completed" and data.get("return_code") == 0 and "error_code" in data:
            snapshot["subprocess_success_error_code_leaks"].append(
                {
                    "job_id": event.get("job_id"),
                    "stage": event.get("stage"),
                    "event_id": event.get("event_id"),
                    "error_code": data.get("error_code"),
                    "return_code": data.get("return_code"),
                }
            )
        if event_name not in _MONITORED_EVENTS:
            continue
        snapshot["counts"]["events_by_event"][event_name] += 1
        snapshot["counts"]["events_by_stage"][event.get("stage") or "-"] += 1
        snapshot["counts"]["events_by_job_id"][event.get("job_id") or "-"] += 1
        error_code = _event_error_code(event)
        if error_code:
            snapshot["counts"]["events_by_error_code"][error_code] += 1
        if event_name == "provider_request_failed":
            status_code = data.get("status_code")
            snapshot["provider_failures"].append(
                {
                    "job_id": event.get("job_id"),
                    "stage": event.get("stage"),
                    "operation": data.get("operation"),
                    "provider": data.get("provider"),
                    "model": data.get("model"),
                    "status_code": status_code,
                    "error_code": error_code,
                    "request_id": event.get("request_id") or data.get("request_id"),
                    "event_id": event.get("event_id"),
                }
            )
            snapshot["provider_failures_by_status_code"][str(status_code or "-")] += 1


def _event_error_code(event: dict[str, Any]) -> str | None:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    for key in ("error_code", "failure_code"):
        value = data.get(key)
        if value:
            return str(value)
    return None


def _finalize_counters(snapshot: dict[str, Any]) -> None:
    for section in ("counts", "top_markers"):
        for key, value in list(snapshot[section].items()):
            if isinstance(value, Counter):
                snapshot[section][key] = _counter_payload(value)
            elif isinstance(value, dict):
                snapshot[section][key] = {
                    nested_key: _counter_payload(nested_value) if isinstance(nested_value, Counter) else nested_value
                    for nested_key, nested_value in value.items()
                }
    snapshot["quality_warning_templates"] = _counter_payload(snapshot["quality_warning_templates"])
    snapshot["provider_failures_by_status_code"] = _counter_payload(snapshot["provider_failures_by_status_code"])
    snapshot["affected_ratios"] = _quality_affected_ratio_summary(snapshot["quality_warnings"])


def _counter_payload(counter: Counter) -> list[dict[str, Any]]:
    total = sum(counter.values())
    return [
        {
            "value": value,
            "count": count,
            "affected_ratio": round(count / total, 4) if total else 0,
        }
        for value, count in counter.most_common()
    ]


def _quality_affected_ratio_summary(warnings: list[dict[str, Any]]) -> dict[str, Any]:
    ratios_by_template: dict[str, list[float]] = defaultdict(list)
    for warning in warnings:
        ratio = warning.get("affected_ratio")
        if isinstance(ratio, int | float):
            ratios_by_template[str(warning.get("template") or warning.get("warning"))].append(float(ratio))
    return {
        template: {
            "count": len(ratios),
            "max": max(ratios),
            "avg": round(sum(ratios) / len(ratios), 4),
        }
        for template, ratios in sorted(ratios_by_template.items())
    }


def _write_snapshot(output_root: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    diagnostics_dir = output_root / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
    snapshot_path = diagnostics_dir / f"audit_{timestamp}.json"
    latest_path = diagnostics_dir / "latest.json"
    snapshot["snapshot_path"] = str(snapshot_path)
    snapshot["latest_path"] = str(latest_path)
    write_json_atomic(snapshot_path, snapshot)
    write_json_atomic(latest_path, snapshot)
    return snapshot


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
    except Exception:
        return []
    return rows


def _read_json_or_none(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return read_json(path)
    except Exception:
        return None


def _read_json_or_empty(path: Path) -> dict[str, Any]:
    payload = _read_json_or_none(path)
    return payload if isinstance(payload, dict) else {}


def _json_or_empty(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _warning_template(value: str) -> str:
    return re.sub(r"\b\d+/\d+\b", "{affected}/{total}", value)


def _affected_ratio(value: str) -> float | None:
    match = re.search(r"\b(\d+)/(\d+)\b", value)
    if match is None:
        return None
    affected = int(match.group(1))
    total = int(match.group(2))
    return round(affected / total, 4) if total else None


def main() -> None:
    snapshot = audit_jobs()
    print(json.dumps(_console_summary(snapshot), ensure_ascii=False, indent=2, sort_keys=True))


def _console_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    subprocess_leaks = snapshot.get("subprocess_success_error_code_leaks") or []
    return {
        "snapshot_path": snapshot.get("snapshot_path"),
        "jobs": snapshot.get("counts", {}).get("job_status"),
        "events": snapshot.get("counts", {}).get("events_by_event"),
        "provider_failures_by_status_code": snapshot.get("provider_failures_by_status_code"),
        "quality_warning_templates": snapshot.get("quality_warning_templates"),
        "stale_failure_reports": snapshot.get("stale_failure_reports"),
        "claim_export_drifts": snapshot.get("claim_export_drifts"),
        "quality_report_drifts": snapshot.get("quality_report_drifts"),
        "obsidian_drifts": snapshot.get("obsidian_drifts"),
        "drift_suspicions": snapshot.get("drift_suspicions"),
        "subprocess_success_error_code_leaks_count": len(subprocess_leaks),
        "subprocess_success_error_code_leaks_sample": subprocess_leaks[:10],
    }


if __name__ == "__main__":
    main()

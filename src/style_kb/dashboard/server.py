from __future__ import annotations

import json
import hashlib
import mimetypes
import re
import sqlite3
from collections import Counter, deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from style_kb.config import load_default_config
from style_kb.dashboard.assets import APP_JS, INDEX_HTML, STYLES_CSS
from style_kb.pipeline.paths import JobPaths


HOST = "127.0.0.1"
PORT = 8765

_PRESENTATION_NOISE_PATTERNS = (
    r"\b(?:появлен\w*|появил\w*|появля\w*)\b",
    r"\b(?:справа|слева|сверху|снизу)\b",
    r"\b(?:прав\w+|лев\w+|верхн\w+|нижн\w+)\s+(?:сторон\w+|част\w+|угл\w+)\b",
    r"\b(?:дополнительн\w+|альтернативн\w+)\s+образ\w*\b",
    r"\bдемонстрируем\w*\s+как\s+отдельн\w+\s+элемент\w*\b",
)
_TECHNICAL_VISUAL_PATTERNS = (
    r"\bобразовательн\w*\s+формат\w*",
    r"\bформат\w*\s+(?:видео|контент\w*|подач\w*)",
    r"\bвизуальн\w*\s+подач\w*",
    r"\bподач\w*\s+на\s+экран\w*",
    r"\b(?:крупн\w*|средн\w*|общ\w*)\s+план\w*\b",
    r"\b(?:ракурс\w*|кадр\w*|камер\w*|съемк\w*|съёмк\w*|монтаж\w*)\b",
    r"\b(?:заставк\w*|оверле\w*|overlay|overlays|on\s+screen|screen)\b",
    r"\b(?:экран\w*|текстов\w*\s+вставк\w*|надпис\w*|слайд\w*|slide|slides)\b",
    r"\b(?:visual\s+aids?|instructional\s+aids?|presentation|presentational|formal\s+presentation)\b",
    r"\b(?:фон\w*|background|интерьер\w*|книжн\w*\s+полк\w*|полк\w*|стол\w*|камин\w*)\b",
    r"\b(?:микрофон\w*|петличк\w*|lapel\s+mic|microphone)\b",
)


def main() -> None:
    working_dir = Path.cwd()
    config = load_default_config()
    output_root = (working_dir / config.project.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    handler = type(
        "StyleKbDashboardHandler",
        (DashboardHandler,),
        {"output_root": output_root},
    )
    server = _bind_server(handler)
    host, port = server.server_address
    print(f"style-kb dashboard: http://{host}:{port}")
    print(f"output root: {output_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping dashboard")
    finally:
        server.server_close()


def _bind_server(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    for port in range(PORT, PORT + 50):
        try:
            return ThreadingHTTPServer((HOST, port), handler)
        except OSError:
            continue
    raise RuntimeError(f"no available localhost port in {PORT}-{PORT + 49}")


class DashboardHandler(BaseHTTPRequestHandler):
    output_root: Path

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in {"/", "/index.html"}:
                self._send_text(INDEX_HTML, content_type="text/html; charset=utf-8")
                return
            if path == "/assets/styles.css":
                self._send_text(STYLES_CSS, content_type="text/css; charset=utf-8")
                return
            if path == "/assets/app.js":
                self._send_text(APP_JS, content_type="text/javascript; charset=utf-8")
                return
            if path == "/api/summary":
                self._send_json(summary_payload(self.output_root))
                return
            if path.startswith("/api/jobs/"):
                job_id = unquote(path.removeprefix("/api/jobs/")).strip("/")
                self._send_json(job_payload(self.output_root, job_id))
                return
            if path.startswith("/media/"):
                self._send_media(path.removeprefix("/media/"))
                return
            self._send_json({"error": "not_found", "path": path}, status=404)
        except DashboardError as error:
            self._send_json({"error": error.code, "message": error.message}, status=error.status)
        except Exception as error:
            self._send_json({"error": "internal_error", "message": str(error)}, status=500)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, payload: Any, *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, *, content_type: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_media(self, raw_path: str) -> None:
        parts = raw_path.split("/", 1)
        if len(parts) != 2:
            raise DashboardError("bad_media_path", "media path must include job id and relative path", status=400)
        job_id = unquote(parts[0])
        relative_path = unquote(parts[1])
        job_dir = (self.output_root / "jobs" / job_id).resolve()
        target = (job_dir / relative_path).resolve()
        if not target.is_relative_to(job_dir):
            raise DashboardError("forbidden_media_path", "media path escapes job directory", status=403)
        if not target.exists() or not target.is_file():
            raise DashboardError("media_not_found", f"media not found: {relative_path}", status=404)

        body = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "max-age=60")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DashboardError(Exception):
    def __init__(self, code: str, message: str, *, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def summary_payload(output_root: Path) -> dict[str, Any]:
    jobs = list_jobs(output_root)
    return {
        "output_root": str(output_root),
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "jobs": jobs,
    }


def job_payload(output_root: Path, job_id: str) -> dict[str, Any]:
    if not job_id or "/" in job_id or "\\" in job_id:
        raise DashboardError("bad_job_id", "job id contains unsupported characters", status=400)
    paths = JobPaths(output_root, job_id)
    if not paths.job_dir.exists():
        raise DashboardError("job_not_found", f"job not found: {job_id}", status=404)

    job = get_job(output_root, job_id) or synthesize_job(paths)
    stages = list_stages(output_root, job_id)
    quality_report = read_json(paths.quality_report)
    failure_report = read_json(paths.failure_report)
    partial_quality_report = read_json(paths.partial_quality_report)
    cleanup_report = read_json(paths.cleanup_report)
    video_info = read_json(paths.metadata_video_info)
    presenter_profile = read_json(paths.visual_presenter_profile)
    speaker_diarization = read_json(paths.stt_speaker_diarization)
    timeline_events = read_jsonl(paths.timeline_events_jsonl)
    chunks = read_jsonl(paths.chunks_jsonl)
    style_claims = read_jsonl(paths.style_claims_jsonl)
    visual_events = read_jsonl(paths.visual_events_jsonl)
    frame_refs = read_jsonl(paths.frame_refs_jsonl)
    pipeline_events = tail_jsonl(paths.pipeline_events_jsonl, max_items=1200)
    selected_claims = read_json(paths.claims_dir / "style_claims_selected.json")

    payload = {
        "job": job,
        "stages": stages,
        "artifacts": artifact_statuses(paths),
        "video_info": video_info,
        "quality_report": quality_report,
        "failure_report": failure_report,
        "partial_quality_report": partial_quality_report,
        "cleanup_report": cleanup_report,
        "presenter_profile": presenter_profile,
        "speaker_diarization": speaker_diarization,
        "timeline_events": timeline_events,
        "chunks": chunks,
        "style_claims": style_claims,
        "style_claims_selected": selected_claims,
        "visual_events": visual_events,
        "frame_refs": frame_refs,
        "pipeline_events": pipeline_events,
    }
    payload["quality_issues"] = build_quality_issues(payload)
    payload["derived"] = derive_summary(payload)
    return payload


def list_jobs(output_root: Path) -> list[dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = {}
    for job in db_jobs(output_root):
        jobs[job["job_id"]] = enrich_job_summary(output_root, job)

    jobs_root = output_root / "jobs"
    if jobs_root.exists():
        for job_dir in jobs_root.iterdir():
            if not job_dir.is_dir():
                continue
            if job_dir.name not in jobs:
                jobs[job_dir.name] = synthesize_job(JobPaths(output_root, job_dir.name))

    return sorted(
        jobs.values(),
        key=lambda item: item.get("created_at") or item.get("updated_at") or "",
        reverse=True,
    )


def db_jobs(output_root: Path) -> list[dict[str, Any]]:
    db_path = output_root / "jobs.sqlite3"
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def get_job(output_root: Path, job_id: str) -> dict[str, Any] | None:
    db_path = output_root / "jobs.sqlite3"
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    return enrich_job_summary(output_root, dict(row))


def list_stages(output_root: Path, job_id: str) -> list[dict[str, Any]]:
    db_path = output_root / "jobs.sqlite3"
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM stages WHERE job_id = ? ORDER BY ordinal ASC",
            (job_id,),
        ).fetchall()
    return [decode_stage(dict(row)) for row in rows]


def decode_stage(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("input_files", "output_files", "remote_refs", "metrics"):
        row[key] = decode_json_value(row.get(key), [] if key.endswith("_files") else {})
    row["duration_seconds"] = duration_seconds(row.get("started_at"), row.get("finished_at"))
    return row


def enrich_job_summary(output_root: Path, job: dict[str, Any]) -> dict[str, Any]:
    paths = JobPaths(output_root, job["job_id"])
    video_info = read_json(paths.metadata_video_info)
    quality = read_json(paths.quality_report)
    if video_info:
        job["title"] = job.get("title") or video_info.get("title")
        job["channel"] = job.get("channel") or video_info.get("channel")
        job["duration"] = video_info.get("duration")
        job["thumbnail_url"] = video_info.get("thumbnail_url")
    if quality:
        job["stage_counts"] = quality.get("stage_counts", {})
        job["warnings_count"] = len(quality.get("warnings", []))
        job["errors_count"] = len(quality.get("errors", []))
    job["artifact_mtime"] = iso_mtime(paths.job_dir)
    return job


def synthesize_job(paths: JobPaths) -> dict[str, Any]:
    video_info = read_json(paths.metadata_video_info)
    quality = read_json(paths.quality_report)
    failure = read_json(paths.failure_report)
    created_at = iso_mtime(paths.job_dir)
    return {
        "job_id": paths.job_id,
        "video_id": (video_info or quality or failure or {}).get("video_id", paths.job_id),
        "url": (video_info or failure or {}).get("url", ""),
        "status": (failure or {}).get("status") or ("completed" if quality else "unknown"),
        "current_stage": (failure or {}).get("failed_stage"),
        "title": (video_info or {}).get("title"),
        "channel": (video_info or {}).get("channel"),
        "duration": (video_info or {}).get("duration"),
        "thumbnail_url": (video_info or {}).get("thumbnail_url"),
        "job_dir": str(paths.job_dir),
        "config_path": "",
        "created_at": created_at,
        "updated_at": created_at,
        "started_at": None,
        "finished_at": None,
        "lock_pid": None,
        "lock_acquired_at": None,
        "error_code": (failure.get("error") or {}).get("code") if failure else None,
        "error_message": (failure.get("error") or {}).get("message") if failure else None,
        "stage_counts": (quality or {}).get("stage_counts", {}),
        "warnings_count": len((quality or {}).get("warnings", [])),
        "errors_count": len((quality or {}).get("errors", [])),
        "artifact_mtime": created_at,
    }


def artifact_statuses(paths: JobPaths) -> list[dict[str, Any]]:
    artifact_paths = {
        "video_info": paths.metadata_video_info,
        "speaker_diarization": paths.stt_speaker_diarization,
        "speech_segments": paths.stt_speech_segments,
        "scenes": paths.scenes_jsonl,
        "frame_refs": paths.frame_refs_jsonl,
        "visual_events": paths.visual_events_jsonl,
        "presenter_profile": paths.visual_presenter_profile,
        "timeline_events": paths.timeline_events_jsonl,
        "chunks": paths.chunks_jsonl,
        "chunk_plan": paths.chunk_plan,
        "chunk_plan_warnings": paths.chunk_plan_warnings,
        "style_claims": paths.style_claims_jsonl,
        "style_claims_selected": paths.claims_dir / "style_claims_selected.json",
        "style_claims_errors": paths.style_claims_errors,
        "quality_report": paths.quality_report,
        "partial_quality_report": paths.partial_quality_report,
        "failure_report": paths.failure_report,
        "cleanup_report": paths.cleanup_report,
        "pipeline_events": paths.pipeline_events_jsonl,
        "pipeline_log": paths.pipeline_human_log,
        "obsidian_index": paths.obsidian_index,
    }
    statuses = []
    for key, path in artifact_paths.items():
        statuses.append(file_status(key, path, base_dir=paths.job_dir))
    return statuses


def file_status(key: str, path: Path, *, base_dir: Path) -> dict[str, Any]:
    exists = path.exists()
    relative_path = None
    try:
        relative_path = str(path.relative_to(base_dir))
    except ValueError:
        relative_path = str(path)
    return {
        "key": key,
        "path": str(path),
        "relative_path": relative_path,
        "exists": exists,
        "size_bytes": path.stat().st_size if exists else None,
        "mtime": iso_mtime(path) if exists else None,
    }


def build_quality_issues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    quality = payload.get("quality_report") or {}
    issues: list[dict[str, Any]] = []
    for index, warning in enumerate(quality.get("warnings") or []):
        locations = locations_for_warning(warning, payload)
        issues.append(
            {
                "issue_id": f"warning_{index:03d}",
                "severity": "warning",
                "title": warning,
                "summary": issue_summary(warning, locations),
                "source": "quality_report.warnings",
                "locations_count": len(locations),
                "locations": locations,
            }
        )

    for index, error in enumerate(quality.get("errors") or []):
        issues.append(
            {
                "issue_id": f"quality_error_{index:03d}",
                "severity": "error",
                "title": str(error),
                "summary": "Quality report error",
                "source": "quality_report.errors",
                "locations_count": 1,
                "locations": [job_location(payload, field="quality_report.errors", marker=str(error))],
            }
        )

    failure = payload.get("failure_report")
    job_status = ((payload.get("job") or {}).get("status") or "").lower()
    if failure and job_status == "failed":
        issues.append(failure_issue(failure))
    return issues


def locations_for_warning(warning: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if warning.startswith("presentation-style visual wording remains"):
        return visual_marker_locations(
            payload.get("visual_events") or [],
            fields=("visual_summary", "observations", "interpretations", "items", "style_topics", "notes", "presenter_context.scene_deltas"),
            marker_patterns=_PRESENTATION_NOISE_PATTERNS,
            issue_kind="visual_presentation_noise",
        )
    if warning.startswith("presentation-style visual wording reached KB chunk fields"):
        return chunk_marker_locations(
            payload.get("chunks") or [],
            fields=("visual_text", "presenter_brief", "topics", "entities", "combined_text"),
            marker_patterns=_PRESENTATION_NOISE_PATTERNS,
            issue_kind="chunk_presentation_noise",
        )
    if warning.startswith("technical presentation labels remain"):
        return visual_marker_locations(
            payload.get("visual_events") or [],
            fields=("visual_summary", "observations", "interpretations", "items", "style_topics", "notes"),
            marker_patterns=_TECHNICAL_VISUAL_PATTERNS,
            issue_kind="technical_visual_label",
        )
    if warning == "style claims are empty despite non-empty chunks":
        return [
            chunk_location(chunk, field="style_claims", marker="no extracted style claims")
            for chunk in payload.get("chunks") or []
        ]
    if warning == "more than half of chunks have no extracted style claims":
        chunks_with_claims = {claim.get("chunk_id") for claim in payload.get("style_claims") or []}
        return [
            chunk_location(chunk, field="style_claims", marker="chunk has no extracted style claims")
            for chunk in payload.get("chunks") or []
            if chunk.get("chunk_id") not in chunks_with_claims
        ]
    if warning == "scene detection produced a single scene":
        return [
            {
                "location_id": "scene_detection:single_scene",
                "kind": "artifact",
                "object_id": "scenes.jsonl",
                "label": "scenes.jsonl",
                "field": "scenes",
                "marker": warning,
                "preview": warning,
            }
        ]
    return [job_location(payload, field="quality_report.warnings", marker=warning)]


def visual_marker_locations(
    visual_events: list[dict[str, Any]],
    *,
    fields: tuple[str, ...],
    marker_patterns: tuple[str, ...],
    issue_kind: str,
) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in visual_events:
        for field in fields:
            for marker in markers_for_value(nested_value(event, field), marker_patterns):
                key = f"{event.get('visual_event_id')}:{field}:{marker}"
                if key in seen:
                    continue
                seen.add(key)
                locations.append(visual_location(event, field=field, marker=marker, issue_kind=issue_kind))
    return locations


def chunk_marker_locations(
    chunks: list[dict[str, Any]],
    *,
    fields: tuple[str, ...],
    marker_patterns: tuple[str, ...],
    issue_kind: str,
) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        for field in fields:
            for marker in markers_for_value(nested_value(chunk, field), marker_patterns):
                key = f"{chunk.get('chunk_id')}:{field}:{marker}"
                if key in seen:
                    continue
                seen.add(key)
                locations.append(chunk_location(chunk, field=field, marker=marker, issue_kind=issue_kind))
    return locations


def markers_for_value(value: Any, patterns: tuple[str, ...]) -> list[str]:
    values = value if isinstance(value, list) else [value]
    markers: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if not text:
            continue
        normalized = normalize_quality_text(text)
        if any(re.search(pattern, normalized, flags=re.UNICODE) for pattern in patterns) and text not in seen:
            seen.add(text)
            markers.append(text)
    return markers


def nested_value(item: dict[str, Any], field: str) -> Any:
    value: Any = item
    for part in field.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def visual_location(event: dict[str, Any], *, field: str, marker: str, issue_kind: str) -> dict[str, Any]:
    visual_event_id = event.get("visual_event_id") or event.get("scene_id") or "visual_event"
    scene_id = event.get("scene_id")
    return {
        "location_id": f"{issue_kind}:{visual_event_id}:{field}:{short_hash(marker)}",
        "kind": "visual_event",
        "object_id": visual_event_id,
        "related_kind": "visual",
        "related_id": visual_event_id,
        "label": f"{format_range(event)} · {scene_id or visual_event_id}",
        "field": field,
        "marker": marker,
        "preview": marker,
        "start": event.get("start"),
        "end": event.get("end"),
        "timestamp_url": event.get("timestamp_url"),
        "scene_id": scene_id,
        "visual_event_id": visual_event_id,
    }


def chunk_location(chunk: dict[str, Any], *, field: str, marker: str, issue_kind: str = "chunk_issue") -> dict[str, Any]:
    chunk_id = chunk.get("chunk_id") or "chunk"
    return {
        "location_id": f"{issue_kind}:{chunk_id}:{field}:{short_hash(marker)}",
        "kind": "chunk",
        "object_id": chunk_id,
        "related_kind": "chunk",
        "related_id": chunk_id,
        "label": f"{format_range(chunk)} · {chunk.get('chunk_title') or chunk_id}",
        "field": field,
        "marker": marker,
        "preview": marker,
        "start": chunk.get("start"),
        "end": chunk.get("end"),
        "timestamp_url": chunk.get("timestamp_url"),
        "chunk_id": chunk_id,
        "timeline_event_ids": chunk.get("timeline_event_ids") or [],
    }


def job_location(payload: dict[str, Any], *, field: str, marker: str) -> dict[str, Any]:
    job = payload.get("job") or {}
    return {
        "location_id": f"job:{job.get('job_id', 'job')}:{field}:{short_hash(marker)}",
        "kind": "job",
        "object_id": job.get("job_id"),
        "label": job.get("job_id") or "job",
        "field": field,
        "marker": marker,
        "preview": marker,
    }


def failure_issue(failure: dict[str, Any]) -> dict[str, Any]:
    error = failure.get("error") or {}
    stage = failure.get("failed_stage") or (failure.get("stage_state") or {}).get("stage_name") or "pipeline"
    marker = error.get("message") or error.get("code") or "job failed"
    locations = [
        {
            "location_id": f"failure:{stage}",
            "kind": "stage",
            "object_id": stage,
            "related_kind": "stage",
            "related_id": stage,
            "label": stage,
            "field": "failure_report",
            "marker": marker,
            "preview": str(error.get("details") or marker),
            "error_code": error.get("code"),
            "stage": stage,
        }
    ]
    return {
        "issue_id": "failure_report",
        "severity": "error",
        "title": marker,
        "summary": f"Failed stage: {stage}",
        "source": "reports/failure_report.json",
        "locations_count": len(locations),
        "locations": locations,
    }


def issue_summary(warning: str, locations: list[dict[str, Any]]) -> str:
    if not locations:
        return "No concrete locations found in current artifacts"
    fields = Counter(str(location.get("field") or "-") for location in locations)
    field_summary = ", ".join(f"{field}: {count}" for field, count in fields.most_common(4))
    return f"{len(locations)} locations; {field_summary}"


def normalize_quality_text(value: str) -> str:
    text = value.casefold().replace("ё", "е")
    text = re.sub(r"[-‐‑‒–—]+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text, flags=re.UNICODE).strip()


def format_range(item: dict[str, Any]) -> str:
    start = item.get("start")
    end = item.get("end")
    if isinstance(start, int | float) and isinstance(end, int | float):
        return f"{format_seconds(start)}-{format_seconds(end)}"
    return "time unknown"


def format_seconds(value: float) -> str:
    total = max(0, int(value))
    return f"{total // 60}:{total % 60:02d}"


def short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def derive_summary(payload: dict[str, Any]) -> dict[str, Any]:
    quality = payload.get("quality_report") or {}
    counts = dict(quality.get("stage_counts") or {})
    count_defaults = {
        "timeline_events": len(payload.get("timeline_events") or []),
        "chunks": len(payload.get("chunks") or []),
        "style_claims": len(payload.get("style_claims") or []),
        "visual_events": len(payload.get("visual_events") or []),
        "frame_refs": len(payload.get("frame_refs") or []),
        "speakers": len(((payload.get("speaker_diarization") or {}).get("speakers")) or []),
    }
    for key, value in count_defaults.items():
        counts.setdefault(key, value)

    claim_types = Counter(claim.get("claim_type") or "unknown" for claim in payload.get("style_claims") or [])
    confidence = Counter(claim.get("confidence") or "unknown" for claim in payload.get("style_claims") or [])
    stage_statuses = Counter(stage.get("status") or "unknown" for stage in payload.get("stages") or [])
    pipeline_events = Counter(event.get("event") or "unknown" for event in payload.get("pipeline_events") or [])
    topics = Counter()
    for collection_name in ("style_claims", "chunks", "timeline_events", "visual_events"):
        for item in payload.get(collection_name) or []:
            for topic in item.get("topics") or item.get("style_topics") or []:
                topics[str(topic)] += 1

    return {
        "counts": counts,
        "claim_type_counts": dict(claim_types),
        "claim_confidence_counts": dict(confidence),
        "stage_status_counts": dict(stage_statuses),
        "pipeline_event_counts": dict(pipeline_events),
        "top_topics": [{"topic": topic, "count": count} for topic, count in topics.most_common(30)],
        "job_duration_seconds": duration_seconds(
            (payload.get("job") or {}).get("started_at"),
            (payload.get("job") or {}).get("finished_at"),
        ),
    }


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def tail_jsonl(path: Path, *, max_items: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: deque[dict[str, Any]] = deque(maxlen=max_items)
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return list(items)


def decode_json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def iso_mtime(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def duration_seconds(start: str | None, finish: str | None) -> float | None:
    if not start or not finish:
        return None
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        finish_dt = datetime.fromisoformat(finish.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (finish_dt - start_dt).total_seconds())

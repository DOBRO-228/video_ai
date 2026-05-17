from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from style_kb.models import Job, StageStatus


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_load(value: str) -> Any:
    return json.loads(value)


class StateRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON;")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def create_or_get_job(
        self,
        *,
        job_id: str,
        video_id: str,
        url: str,
        job_dir: str,
        config_path: str,
    ) -> Job:
        existing = self.get_job(job_id)
        if existing is not None:
            return existing

        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, video_id, url, status, current_stage, title, channel, job_dir,
                    config_path, created_at, updated_at, started_at, finished_at,
                    lock_pid, lock_acquired_at, error_code, error_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    video_id,
                    url,
                    "pending",
                    None,
                    None,
                    None,
                    job_dir,
                    config_path,
                    _to_iso(now),
                    _to_iso(now),
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Job | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def list_jobs(self) -> list[Job]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [self._row_to_job(row) for row in rows]

    def update_job(self, job_id: str, **fields: Any) -> Job:
        if not fields:
            job = self.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            return job

        assignments = []
        values = []
        for key, value in fields.items():
            if isinstance(value, datetime):
                value = _to_iso(value)
            assignments.append(f"{key} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.append(_to_iso(utc_now()))
        values.append(job_id)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id = ?",
                tuple(values),
            )
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def set_job_lock(self, job_id: str, *, pid: int | None, acquired_at: datetime | None) -> Job:
        return self.update_job(job_id, lock_pid=pid, lock_acquired_at=acquired_at)

    def clear_job_error(self, job_id: str) -> Job:
        return self.update_job(job_id, error_code=None, error_message=None)

    def ensure_stages(self, job_id: str, stage_specs: list[tuple[int, str]]) -> None:
        with self.connect() as connection:
            for ordinal, stage_name in stage_specs:
                connection.execute(
                    """
                    INSERT INTO stages (
                        job_id, stage_name, ordinal, status, attempt, started_at, finished_at,
                        input_files, output_files, remote_refs, error_code, error_message, metrics
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id, stage_name) DO NOTHING
                    """,
                    (
                        job_id,
                        stage_name,
                        ordinal,
                        "pending",
                        0,
                        None,
                        None,
                        "[]",
                        "[]",
                        "{}",
                        None,
                        None,
                        "{}",
                    ),
                )

    def get_stage(self, job_id: str, stage_name: str) -> StageStatus | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM stages WHERE job_id = ? AND stage_name = ?",
                (job_id, stage_name),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_stage(row)

    def list_stages(self, job_id: str) -> list[StageStatus]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM stages WHERE job_id = ? ORDER BY ordinal ASC",
                (job_id,),
            ).fetchall()
        return [self._row_to_stage(row) for row in rows]

    def mark_stage_running(
        self,
        *,
        job_id: str,
        stage_name: str,
        input_files: list[str],
    ) -> StageStatus:
        current = self.get_stage(job_id, stage_name)
        if current is None:
            raise KeyError((job_id, stage_name))
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE stages
                SET status = ?, attempt = ?, started_at = ?, finished_at = ?, input_files = ?,
                    output_files = ?, remote_refs = ?, error_code = ?, error_message = ?, metrics = ?
                WHERE job_id = ? AND stage_name = ?
                """,
                (
                    "running",
                    current.attempt + 1,
                    _to_iso(utc_now()),
                    None,
                    _json_dump(input_files),
                    "[]",
                    "{}",
                    None,
                    None,
                    "{}",
                    job_id,
                    stage_name,
                ),
            )
        stage = self.get_stage(job_id, stage_name)
        if stage is None:
            raise KeyError((job_id, stage_name))
        return stage

    def mark_stage_finished(
        self,
        *,
        job_id: str,
        stage_name: str,
        status: str,
        output_files: list[str],
        remote_refs: dict[str, Any],
        metrics: dict[str, Any],
    ) -> StageStatus:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE stages
                SET status = ?, finished_at = ?, output_files = ?, remote_refs = ?, metrics = ?,
                    error_code = ?, error_message = ?
                WHERE job_id = ? AND stage_name = ?
                """,
                (
                    status,
                    _to_iso(utc_now()),
                    _json_dump(output_files),
                    _json_dump(remote_refs),
                    _json_dump(metrics),
                    None,
                    None,
                    job_id,
                    stage_name,
                ),
            )
        stage = self.get_stage(job_id, stage_name)
        if stage is None:
            raise KeyError((job_id, stage_name))
        return stage

    def mark_stage_failed(
        self,
        *,
        job_id: str,
        stage_name: str,
        error_code: str,
        error_message: str,
    ) -> StageStatus:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE stages
                SET status = ?, finished_at = ?, error_code = ?, error_message = ?
                WHERE job_id = ? AND stage_name = ?
                """,
                ("failed", _to_iso(utc_now()), error_code, error_message, job_id, stage_name),
            )
        stage = self.get_stage(job_id, stage_name)
        if stage is None:
            raise KeyError((job_id, stage_name))
        return stage

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job.model_validate(
            {
                "job_id": row["job_id"],
                "video_id": row["video_id"],
                "url": row["url"],
                "status": row["status"],
                "current_stage": row["current_stage"],
                "title": row["title"],
                "channel": row["channel"],
                "job_dir": row["job_dir"],
                "config_path": row["config_path"],
                "created_at": _from_iso(row["created_at"]),
                "updated_at": _from_iso(row["updated_at"]),
                "started_at": _from_iso(row["started_at"]),
                "finished_at": _from_iso(row["finished_at"]),
                "lock_pid": row["lock_pid"],
                "lock_acquired_at": _from_iso(row["lock_acquired_at"]),
                "error_code": row["error_code"],
                "error_message": row["error_message"],
            }
        )

    @staticmethod
    def _row_to_stage(row: sqlite3.Row) -> StageStatus:
        return StageStatus.model_validate(
            {
                "job_id": row["job_id"],
                "stage_name": row["stage_name"],
                "ordinal": row["ordinal"],
                "status": row["status"],
                "attempt": row["attempt"],
                "started_at": _from_iso(row["started_at"]),
                "finished_at": _from_iso(row["finished_at"]),
                "input_files": _json_load(row["input_files"]),
                "output_files": _json_load(row["output_files"]),
                "remote_refs": _json_load(row["remote_refs"]),
                "error_code": row["error_code"],
                "error_message": row["error_message"],
                "metrics": _json_load(row["metrics"]),
            }
        )


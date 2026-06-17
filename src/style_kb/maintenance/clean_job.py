from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from style_kb.config import load_default_config


@dataclass(frozen=True)
class CleanJobResult:
    job_id: str
    output_root: Path
    job_dir: Path
    deleted_job_dir: bool
    deleted_job_rows: int
    deleted_stage_rows: int

    @property
    def found_anything(self) -> bool:
        return self.deleted_job_dir or self.deleted_job_rows > 0 or self.deleted_stage_rows > 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete one local style-kb job from artifacts and SQLite state.")
    parser.add_argument("job_id", help="Job ID to delete, usually the YouTube video id.")
    args = parser.parse_args()

    working_dir = Path.cwd()
    config = load_default_config()
    output_root = (working_dir / config.project.output_dir).resolve()

    try:
        result = clean_job(output_root, args.job_id)
    except CleanJobError as error:
        raise SystemExit(str(error)) from error

    print(f"job_id: {result.job_id}")
    print(f"output_root: {result.output_root}")
    print(f"job_dir: {result.job_dir}")
    print(f"deleted_job_dir: {str(result.deleted_job_dir).lower()}")
    print(f"deleted_job_rows: {result.deleted_job_rows}")
    print(f"deleted_stage_rows: {result.deleted_stage_rows}")


class CleanJobError(Exception):
    pass


def clean_job(output_root: Path, job_id: str) -> CleanJobResult:
    safe_job_id = validate_job_id(job_id)
    output_root = output_root.resolve()
    jobs_root = (output_root / "jobs").resolve()
    job_dir = (jobs_root / safe_job_id).resolve()
    if not job_dir.is_relative_to(jobs_root):
        raise CleanJobError(f"job path escapes jobs root: {job_id}")

    db_path = output_root / "jobs.sqlite3"
    ensure_job_not_running(db_path, safe_job_id)

    deleted_job_dir = False
    if job_dir.exists():
        if not job_dir.is_dir():
            raise CleanJobError(f"job path exists but is not a directory: {job_dir}")
        shutil.rmtree(job_dir)
        deleted_job_dir = True

    deleted_stage_rows, deleted_job_rows = delete_job_rows(db_path, safe_job_id)
    result = CleanJobResult(
        job_id=safe_job_id,
        output_root=output_root,
        job_dir=job_dir,
        deleted_job_dir=deleted_job_dir,
        deleted_job_rows=deleted_job_rows,
        deleted_stage_rows=deleted_stage_rows,
    )
    if not result.found_anything:
        raise CleanJobError(f"job not found in artifacts or SQLite: {safe_job_id}")
    return result


def validate_job_id(job_id: str) -> str:
    safe_job_id = job_id.strip()
    if not safe_job_id:
        raise CleanJobError("job_id is required")
    if safe_job_id in {".", "..", "__bootstrap__"}:
        raise CleanJobError(f"unsupported job_id: {safe_job_id}")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", safe_job_id):
        raise CleanJobError("job_id may contain only letters, digits, underscore, and dash")
    return safe_job_id


def ensure_job_not_running(db_path: Path, job_id: str) -> None:
    if not db_path.exists():
        return
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT status, lock_pid FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        return
    lock_pid = row["lock_pid"]
    if row["status"] == "running" and lock_pid and is_pid_alive(int(lock_pid)):
        raise CleanJobError(f"refusing to clean running job: {job_id} (pid {lock_pid})")


def delete_job_rows(db_path: Path, job_id: str) -> tuple[int, int]:
    if not db_path.exists():
        return 0, 0

    with closing(sqlite3.connect(db_path)) as connection:
        with connection:
            connection.execute("PRAGMA foreign_keys=ON;")
            stage_cursor = connection.execute("DELETE FROM stages WHERE job_id = ?", (job_id,))
            job_cursor = connection.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            deleted_stage_rows = stage_cursor.rowcount if stage_cursor.rowcount is not None else 0
            deleted_job_rows = job_cursor.rowcount if job_cursor.rowcount is not None else 0
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    return deleted_stage_rows, deleted_job_rows


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if __name__ == "__main__":
    main()

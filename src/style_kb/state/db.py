from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def initialize_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as connection:
        with connection:
            connection.execute("PRAGMA journal_mode=WAL;")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_stage TEXT,
                    title TEXT,
                    channel TEXT,
                    job_dir TEXT NOT NULL,
                    config_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    lock_pid INTEGER,
                    lock_acquired_at TEXT,
                    error_code TEXT,
                    error_message TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stages (
                    job_id TEXT NOT NULL,
                    stage_name TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    input_files TEXT NOT NULL,
                    output_files TEXT NOT NULL,
                    remote_refs TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    metrics TEXT NOT NULL,
                    PRIMARY KEY (job_id, stage_name),
                    FOREIGN KEY (job_id) REFERENCES jobs (job_id) ON DELETE CASCADE
                )
                """
            )

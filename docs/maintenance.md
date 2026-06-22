# Maintenance Commands

Run from the repository root.

## Audit Jobs

Create a read-only diagnostics snapshot:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m style_kb.maintenance.audit_jobs
```

The audit reads:

```text
mens-style-kb/jobs.sqlite3
mens-style-kb/jobs/JOB_ID/**
```

It writes machine-readable snapshots:

```text
mens-style-kb/diagnostics/audit_YYYYMMDD_HHMMSS.json
mens-style-kb/diagnostics/latest.json
```

Use this before and after pipeline changes to compare:

- job/stage status and attempts;
- `stage_failed`, `job_failed`, `run_failed`;
- `provider_request_failed`;
- `stage_validation_failed`;
- subprocess warning/failure noise;
- final `quality_report.json` warnings;
- stale unresolved `failure_report.json`;
- resolved failure history;
- lightweight DB/artifact drift suspicion;
- dashboard overlay jobs;
- JSONL style-claim export drift from effective claims;
- stale quality-report claim counts;
- Obsidian video/chunk-note drift.
- overlay and export/Obsidian drift for jobs that are not completed but still carry
  `claims/style_claims_current.jsonl` or non-empty manual edit records.

Audit must stay read-only for job artifacts and SQLite state. It may write only diagnostics snapshots.

## Refresh KB Exports

Reconcile already-completed jobs whose dashboard claim overlay has not reached derived KB
surfaces:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m style_kb.maintenance.refresh_kb_exports
```

The command iterates jobs from `mens-style-kb/jobs.sqlite3`, skips jobs that are not
completed or have a live lock pid, and refreshes only existing surfaces:

- `exports/jsonl/style_claims.jsonl`;
- claim-owned fields in `exports/jsonl/manifest.json`;
- human-facing Obsidian Markdown under `exports/obsidian/`.
- `reports/quality_report.json` and the SQLite state for stage 16.

It never runs stage 13, never renumbers `claim_id`, and never changes
`claims/style_claims.jsonl`, `claims/style_claims_raw.json`, or dashboard overlay files.
The quality report is recomputed from the same stage 16 logic used by the pipeline and
is written only for unlocked completed jobs.

## Delete One Job

```bash
make clean-job JOB_ID=1_zCcipRRik
```

This deletes all local artifacts for the job:

```text
mens-style-kb/jobs/JOB_ID
```

It also removes the job from SQLite:

```text
mens-style-kb/jobs.sqlite3
```

Rows are deleted from both `stages` and `jobs`. The command refuses to clean a job whose SQLite state is `running` with a live lock pid.

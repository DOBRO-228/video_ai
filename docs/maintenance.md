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
- lightweight DB/artifact drift suspicion.

Audit must stay read-only for job artifacts and SQLite state. It may write only diagnostics snapshots.

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

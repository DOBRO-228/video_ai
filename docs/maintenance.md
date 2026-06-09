# Maintenance Commands

Run from the repository root.

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

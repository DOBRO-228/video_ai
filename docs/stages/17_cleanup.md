# 17_cleanup

## Purpose

Remove large intermediate artifacts after successful processing according to config.

## How It Works

The stage reads cleanup settings from config. When `project.keep_media=false`, it deletes downloaded audio/video files and ffprobe JSON files. When `project.keep_frames=false`, it deletes frame refs and JPG frames. It writes a cleanup report listing removed files.

When `pipeline.visual_enabled=false`, visual media/frame artifacts are not created by the current run. Cleanup still only removes files that exist and match the configured cleanup settings.

## Inputs

- `reports/quality_report.json`
- `project.keep_media`
- `project.keep_frames`

## Outputs

- `reports/cleanup.json`

## Skip Validation

The stage can be skipped when `cleanup.json` exists and its `job_id` matches the current job.

## Important Notes

- Cleanup should run only after quality report succeeds.
- Default config keeps frames and removes media.
- Do not delete canonical JSON/JSONL exports or Obsidian notes.
- Failed jobs should retain artifacts needed for debugging and resume.

## Related Code

- `src/style_kb/stages/stage_17_cleanup.py`
- `src/style_kb/config/default.yaml`

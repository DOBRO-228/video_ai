# 01_metadata

## Purpose

Create the canonical video metadata artifacts for the job. This stage establishes the title, channel, duration, canonical URL, and timestamp URL used by downstream provenance.

## How It Works

The stage calls the `yt-dlp` wrapper `fetch_metadata()` with the job URL and optional `download.cookies_from_browser` from config. It writes the raw `yt-dlp` payload, then normalizes the subset needed by the pipeline into `VideoInfo`.

It also updates the SQLite job row with `title` and `channel`.

## Inputs

- `context.job.url`
- `context.job.video_id`
- `src/style_kb/config/default.yaml`

## Outputs

- `metadata/raw_ytdlp.json`
- `metadata/video_info.json`

## Skip Validation

The stage can be skipped only when `video_info.json` parses as `VideoInfo` and its `video_id` matches the current job.

## Important Notes

- This is the first external YouTube call.
- Keep raw metadata for debugging provider/download issues.
- Do not add user-facing metadata options to the CLI.
- Downstream timestamp URLs depend on this stage preserving the job `video_id`.

## Related Code

- `src/style_kb/stages/stage_01_metadata.py`
- `src/style_kb/clients/ytdlp.py`
- `src/style_kb/models.py::VideoInfo`


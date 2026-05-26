# 15_quality_report

## Purpose

Summarize pipeline quality, coverage, durations, counts, warnings, and artifact paths.

## How It Works

The stage loads all canonical upstream artifacts, verifies that timeline and chunks are non-empty, reads media durations, computes coverage ratios, and writes a `QualityReport`. It also emits presenter-aware warnings when recurring presenter handling appears unstable.

## Inputs

- `metadata/video_info.json`
- `downloads/audio.ffprobe.json`
- `downloads/video_proxy.ffprobe.json`
- `stt/speaker_diarization.json`
- `stt/speech_tokens.jsonl`
- `stt/speech_segments.jsonl`
- `scenes/scenes.jsonl`
- `frames/frame_refs.jsonl`
- `visual/visual_events.jsonl`
- `visual/presenter_profile.json`
- `timeline/timeline_events.jsonl`
- `chunks/chunks.jsonl`

## Outputs

- `reports/quality_report.json`

## Skip Validation

The stage can be skipped when `quality_report.json` exists and parses as `QualityReport`.

## Important Notes

- This is the last stage that should inspect all canonical data before cleanup.
- Failed jobs retain media/frames for resume and debugging because cleanup runs only after this stage.
- Warnings should be diagnostic; hard failures should be reserved for impossible consistency states.
- Speaker count is not fixed. Quality report warns only when diarization is enabled but no speaker labels are detected.

## Related Code

- `src/style_kb/stages/stage_15_quality_report.py`
- `src/style_kb/models.py::QualityReport`

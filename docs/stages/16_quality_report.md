# 16_quality_report

## Purpose

Summarize pipeline quality, coverage, durations, counts, warnings, and artifact paths.

## How It Works

The stage loads all canonical upstream artifacts, verifies that timeline and chunks are non-empty, reads media durations, computes coverage ratios, and writes a `QualityReport`. It also emits presenter-aware warnings when recurring presenter handling appears unstable, including non-blocking recurring presenter baseline leakage, technical visual leakage warnings, soft presentation-noise warnings for final visual/chunk fields, and claim-aware warnings when extracted style knowledge is missing.

Planned stage 08/09 hardening may add scene/keyframe quality metrics and warnings here. Those warnings should stay diagnostic and actionable: too many short scenes, too many long scenes, coverage gaps, excessive frames per scene, duplicate-frame reduction, or content-aware extraction fallback counts.

## Inputs

- `metadata/video_info.json`
- `timeline/media_durations.json`
- `stt/speaker_diarization.json`
- `stt/speech_tokens.jsonl`
- `stt/speech_segments.jsonl`
- `scenes/scenes.jsonl`
- planned: `scenes/scene_detection_report.json`
- `frames/frame_refs.jsonl`
- `frames/frame_extraction_report.json`
- `visual/visual_events.jsonl`
- `visual/presenter_profile.json`
- `timeline/timeline_events.jsonl`
- `chunks/chunks.jsonl`
- `claims/style_claims.jsonl`

## Outputs

- `reports/quality_report.json`

## Skip Validation

The stage can be skipped when `quality_report.json` exists and parses as `QualityReport`.

## Important Notes

- This is the last stage that should inspect all canonical data before cleanup.
- Duration checks use `timeline/media_durations.json` so the report can be regenerated after cleanup removes downloaded media diagnostics.
- Failed jobs retain media/frames for resume and debugging because cleanup runs only after this stage.
- Warnings should be diagnostic; hard failures should be reserved for impossible consistency states.
- Recurring presenter baseline leakage is a quality warning/metric, not a hard failure.
- Technical presentation leakage that remains after visual retry is a quality warning/metric, not a hard failure.
- Presentation-noise metrics are broader than strict technical leakage. They flag wording such as visual placement, appearance/change language, and presentation-style phrasing that may still be useful to inspect but must not fail or rewrite the job.
- Scene/keyframe warnings should help tune stage 08/09 cost and granularity without hard-failing jobs unless an invariant is impossible.
- If stage 09 content-aware selection is enabled, report fallback/duplicate counters separately from final frame counts.
- `quality_report.json` includes a `metrics` object for non-fatal quality counters in addition to human-readable warnings.
- Speaker count is not fixed. Quality report warns only when diarization is enabled but no speaker labels are detected.
- Empty style claims are a warning, not a hard failure, because some chunks may contain only service or promotional content.

## Related Code

- `src/style_kb/stages/stage_16_quality_report.py`
- `src/style_kb/models.py::QualityReport`

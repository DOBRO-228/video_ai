# 08_detect_scenes

## Purpose

Detect visual scene boundaries in the proxy video and produce the `Scene` objects that drive keyframes, visual analysis, timeline events, and chunk visual evidence.

## How It Works

The current implementation opens the proxy video with PySceneDetect and uses `AdaptiveDetector`. The configured `scene_detection.min_scene_len_seconds` is converted to frames using ffprobe FPS. Detected scenes are normalized into `Scene` objects.

If PySceneDetect returns no scenes, the stage emits one full-video scene from `0..video_duration`.

The hardening contract from `PLAN_08_09_stages.md` is stricter than the current implementation. When changing this stage, move toward the target behavior below rather than preserving old shortcuts.

## Inputs

Current consumed inputs:

- `downloads/video_proxy.mp4`
- `downloads/video_proxy.ffprobe.json`
- `scene_detection.min_scene_len_seconds`

Configured but not honored by current behavior:

- planned: `scene_detection.detector` (current code always uses `AdaptiveDetector`)
- planned: `scene_detection.fallback_scene_seconds`

## Outputs

- `scenes/scenes.jsonl`
- planned: `scenes/scene_detection_report.json`

## Skip Validation

The stage can be skipped when `scenes.jsonl` exists and parses to at least one `Scene`.

Planned skip validation must additionally require `scene_detection_report.json` and reject stale, corrupt, or config-mismatched outputs. The report should bind the output to stage-relevant config, proxy media identity, ffprobe duration/FPS, detector, scene counts, fallback state, and validation warnings.

## Important Notes

- Current no-cut fallback is one full-video scene. Planned fallback behavior is bounded splitting by `fallback_scene_seconds`; a no-cut 10-minute video should become about 20 scenes with the default `30s` fallback.
- Planned detector behavior should honor `scene_detection.detector`. Initially support `"adaptive"` explicitly and fail fast for unsupported detector names instead of silently ignoring config.
- Planned scene validation should stay lightweight but strict enough to catch corrupt/stale artifacts: non-empty scenes, matching `video_id`, contiguous indexes, `start < end`, sane duration, sorted ranges, no obvious overlaps, and coverage close to proxy duration.
- Fallback splitting improves timeline/chunk granularity but can increase stage 10 provider requests. Pair it with stage 09 duplicate filtering and, when needed, conservative stage 10 duplicate visual-analysis reuse.
- Scene timestamps drive keyframes, visual events, timeline events, and chunk grouping.
- Do not introduce embeddings, vector stores, CLIP/open-clip/torch, or heavy ML dependencies for this MVP work.

## Planned Report Metrics

`scene_detection_report.json` should include at least:

- `schema_version`, `video_id`, `stage`;
- relevant input paths, mtimes, proxy size, proxy mtime, proxy duration/FPS;
- stage-relevant config and config fingerprint;
- detector name and actual detector class;
- `scenes_count`, `fallback_used`, `fallback_scene_seconds`, `fallback_scene_count`;
- min/max/average scene duration;
- short/long scene counters;
- coverage gap/overlap warnings.

## Related Code

- `src/style_kb/stages/stage_08_detect_scenes.py`
- `src/style_kb/pipeline/paths.py`
- `src/style_kb/config/models.py`
- `src/style_kb/models.py::Scene`

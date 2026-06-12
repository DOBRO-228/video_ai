# 08_detect_scenes

## Purpose

Detect visual scene boundaries in the proxy video and produce the `Scene` objects that drive keyframes, visual analysis, timeline events, and chunk visual evidence.

## How It Works

The current implementation opens the proxy video with PySceneDetect and uses `AdaptiveDetector`. The configured `scene_detection.min_scene_len_seconds` is converted to frames using ffprobe FPS. Only `scene_detection.detector: adaptive` is supported; other detector values fail fast.

Detected scenes are normalized into frame-boundary ranges. If PySceneDetect returns no scenes, the stage emits bounded fallback ranges using `scene_detection.fallback_scene_seconds`.

When `scene_detection.palette_boundary_refinement.enabled` is true, Stage 08 then runs a deterministic palette-boundary refinement pass on PySceneDetect and fallback ranges. It samples each sufficiently long range with ffmpeg rawvideo at `palette_boundary_refinement.sample_step_seconds`, computes HSV H-S histograms plus saturation/colorfulness diagnostics, and can move an existing boundary earlier when the late tail of the left scene already matches the stable palette profile at the start of the next scene. The search is bounded by `palette_boundary_refinement.max_boundary_shift_seconds` so a boundary is not moved far back into the previous scene. The refinement uses ffmpeg pixels, not `cv2.VideoCapture`, so its evidence is aligned with Stage 09 extraction.

This pass is designed for scenes like `qxXRWoSYf7I_scene_000011`, where the video changes from a color presenter scene to a monochrome REC-style scene before PySceneDetect's boundary and the following scene is already the same monochrome REC context. In that case Stage 08 keeps the scene count unchanged and moves the shared boundary so the gray tail belongs to the next scene. Monochrome content is not treated as low quality.

The hardening contract from `PLAN_08_09_stages.md` is stricter than the current implementation. When changing this stage, move toward the target behavior below rather than preserving old shortcuts.

## Inputs

Current consumed inputs:

- `downloads/video_proxy.mp4`
- `downloads/video_proxy.ffprobe.json`
- `scene_detection.min_scene_len_seconds`
- `scene_detection.detector`
- `scene_detection.fallback_scene_seconds`
- `scene_detection.palette_boundary_refinement.*`

## Outputs

- `scenes/scenes.jsonl`
- `scenes/scene_detection_report.json`

## Skip Validation

The stage can be skipped only when:

- `scenes/scenes.jsonl` exists and parses to at least one `Scene`;
- `scenes/scene_detection_report.json` exists and parses;
- report `video_id` matches the job;
- report detector settings match current `scene_detection.detector` and `min_scene_len_seconds`;
- report palette-refinement settings match current `scene_detection.palette_boundary_refinement`;
- scenes are sorted, indexed contiguously, have valid frame/time ranges, and are contiguous by frame boundaries;
- report `scenes_count` and `palette_boundary_refinement.output_scenes_count` match the parsed scene count.

This intentionally invalidates older jobs that only have `scenes.jsonl`. Boundary changes must force Stage 09 and downstream visual evidence to rebuild.

## Important Notes

- Fallback behavior is bounded splitting by `fallback_scene_seconds`; a no-cut 10-minute video should become about 20 scenes with the default `30s` fallback.
- Palette refinement also runs on fallback ranges. If PySceneDetect finds no cuts, sustained palette changes may be the only useful boundary signal.
- Detector behavior supports `"adaptive"` explicitly and fails fast for unsupported detector names instead of silently ignoring config.
- Planned scene validation should stay lightweight but strict enough to catch corrupt/stale artifacts: non-empty scenes, matching `video_id`, contiguous indexes, `start < end`, sane duration, sorted ranges, no obvious overlaps, and coverage close to proxy duration.
- Fallback splitting improves timeline/chunk granularity but can increase stage 10 provider requests. Pair it with stage 09 duplicate filtering and, when needed, conservative stage 10 duplicate visual-analysis reuse.
- Scene timestamps drive keyframes, visual events, timeline events, and chunk grouping.
- Scene ids are index-based. Palette boundary adjustment preserves the number of scenes but can change start/end timestamps, so downstream stages must read current reports/refs rather than directory glob leftovers.
- Do not introduce embeddings, vector stores, CLIP/open-clip/torch, or heavy ML dependencies for this MVP work.

## Planned Report Metrics

`scene_detection_report.json` should include at least:

- `schema_version`, `video_id`, `stage`;
- relevant input paths, mtimes, proxy size, proxy mtime, proxy duration/FPS;
- detector name and actual detector class;
- palette refinement config, sampling metrics, accepted boundary adjustments, and rejected candidate counters;
- `scenes_count`, `fallback_used`, `fallback_scene_seconds`, `fallback_scene_count`;
- min/max/average scene duration;
- coverage gap/overlap warnings.

## Related Code

- `src/style_kb/stages/stage_08_detect_scenes.py`
- `src/style_kb/pipeline/paths.py`
- `src/style_kb/config/models.py`
- `src/style_kb/models.py::Scene`

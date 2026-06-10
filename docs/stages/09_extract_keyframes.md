# 09_extract_keyframes

## Purpose

Extract representative frame images for each detected scene. These frames are the visual evidence sent to stage 10, so this stage is a direct cost and quality gate for vision requests.

## How It Works

The current implementation samples base timestamps at 15%, 50%, and 85% of each scene duration, limited by `scene_detection.images_per_scene`. It also samples extra frames every `scene_detection.extra_sample_every_seconds`. Timestamps are clamped inside scene bounds and deduplicated.

Each frame is extracted from the proxy video with ffmpeg and recorded as a `FrameRef`. The stage also writes `frames/frame_extraction_report.json` and `logs/09_extract_keyframes.jsonl`.

The hardening contract from `PLAN_08_09_stages.md` is stricter than the current implementation. When changing this stage, move toward the target behavior below.

## Inputs

- `downloads/video_proxy.mp4`
- `scenes/scenes.jsonl`
- `scene_detection.images_per_scene`
- `scene_detection.extra_sample_every_seconds`
- planned: `scene_detection.min_frames_per_scene`
- planned: `scene_detection.max_frames_per_scene`
- planned: `scene_detection.keyframe_boundary_padding_seconds`
- planned: `scene_detection.frame_selection`
- planned: `scene_detection.sharpness_candidates`
- planned: `scene_detection.sharpness_window_seconds`
- planned: `scene_detection.phash_min_distance`
- planned: `scene_detection.cross_scene_dedup`

## Outputs

- `frames/frame_refs.jsonl`
- `frames/scene_*.jpg`
- `frames/frame_extraction_report.json`
- `logs/09_extract_keyframes.jsonl`

## Skip Validation

The current stage can be skipped only when:

- `frames/frame_refs.jsonl` exists and contains at least one `FrameRef`;
- `frames/frame_extraction_report.json` exists;
- `logs/09_extract_keyframes.jsonl` exists;
- `frame_extraction_report.frames_count == len(frame_refs)`;
- every referenced frame file exists.

Current skip validation is report-aware, but only partially. It does not yet compare config fingerprints, media identity, scene fingerprints/ranges, frame-selection backend, or planned timestamp shape.

Planned skip validation should reject stale, partial, corrupt, or config/media/scene-mismatched outputs using durable report metadata, not expensive pixel-dependent recomputation.

## Important Notes

- During extraction, missing or empty frame files fail the stage. Current skip validation checks referenced frame existence, not file size.
- Frame paths are stored relative to the job directory for portable Obsidian/export references.
- Frame timestamps are evidence paths for visual provenance.
- Planned timestamp planning should be separated from extraction so it can be unit tested without ffmpeg.
- Planned frame counts should adapt to scene duration: very short scenes use one middle frame; medium scenes use two or three frames; long scenes may include extra samples but must never exceed `max_frames_per_scene`.
- Planned boundary padding should avoid selecting transition frames exactly at scene cuts. If the scene is shorter than `2 * padding`, use the midpoint.
- Planned stale frame handling may remove only stage-owned files matching `frames/scene_*.jpg` that are no longer part of the current plan.
- Normal diagnostics should be aggregated in `logs/09_extract_keyframes.jsonl` and `frames/frame_extraction_report.json`; do not create one log file per frame by default.
- `frame_selection=legacy` should remain the deterministic ffmpeg rollback path.
- `frame_selection=content_aware` should be opt-in until A/B checks pass. It may use OpenCV/numpy to score narrow timestamp windows by sharpness/brightness/contrast and write the selected decoded frame without invoking ffmpeg again.
- Duplicate filtering should use pHash/dHash-style perceptual distance, preserve at least `min_frames_per_scene`, avoid removing the first keyframe of a scene only because a neighboring scene is similar, and log skipped duplicates.
- Do not add CLIP/open-clip/torch, embeddings, vector stores, or heavy ML dependencies for MVP frame selection.

## Planned Report Contract

`frame_extraction_report.json` should include:

- `schema_version`, `video_id`, `stage`;
- input paths and media identity for proxy video and `scenes.jsonl`;
- stage-relevant config and config fingerprint;
- frame selection backend;
- target timestamps and selected timestamps;
- frame role, path, file size, score, skip/fallback reason where applicable;
- scenes count, frames count, frames per scene average/max;
- duplicate candidates skipped;
- extraction elapsed seconds and backend operation counts.

For `content_aware`, skip validation should validate recorded structure and metadata without re-decoding video or recomputing sharpness/pHash selection.

## Related Code

- `src/style_kb/stages/stage_09_extract_keyframes.py`
- `src/style_kb/clients/media.py`
- planned: `src/style_kb/clients/frame_selection.py`
- `src/style_kb/models.py::FrameRef`

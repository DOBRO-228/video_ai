# 09_extract_keyframes

## Purpose

Extract representative frame images for each detected scene. These frames are the visual evidence sent to stage 10, so this stage is a direct cost and quality gate for vision requests.

## How It Works

The current implementation uses adaptive timestamp planning. Scenes up to 4 seconds use one middle keyframe, scenes up to 8 seconds use two keyframes, and longer scenes use at least `scene_detection.images_per_scene` keyframes. Long scenes increase the target count from `ceil(scene.duration / scene_detection.extra_sample_every_seconds)`, capped by `scene_detection.max_frames_per_scene`. Timestamps are distributed inside the scene, clamped inside scene bounds, and deduplicated.

Each candidate frame is extracted from the proxy video with ffmpeg. Intra-scene duplicate filtering then decides which extracted frames are included in `FrameRef` records for stage 10. Dropped duplicate files remain on disk and are recorded in `frames/frame_extraction_report.json` for dashboard review. The stage also writes `logs/09_extract_keyframes.jsonl`.

Default duplicate filtering is intentionally moderately aggressive: `phash_max_distance=8` and `ssim_confirm=0.85`. This treats frames as duplicates when their perceptual hashes are close and SSIM confirms high structural similarity. On `qxXRWoSYf7I`, adaptive planning with `images_per_scene=4`, `extra_sample_every_seconds=4`, and `max_frames_per_scene=8` plans 322 candidate frames across 75 scenes before dedup, reducing the stage 10 frame evidence upper bound while preserving at least one frame per scene.

The hardening contract from `PLAN_08_09_stages.md` is stricter than the current implementation. When changing this stage, move toward the target behavior below.

## Inputs

- `downloads/video_proxy.mp4`
- `scenes/scenes.jsonl`
- `scene_detection.images_per_scene`
- `scene_detection.extra_sample_every_seconds`
- `scene_detection.max_frames_per_scene`
- `scene_detection.intra_scene_dedup`
- `scene_detection.phash_max_distance`
- `scene_detection.ssim_confirm`
- `scene_detection.min_frames_per_scene`
- planned: `scene_detection.keyframe_boundary_padding_seconds`
- planned: `scene_detection.frame_selection`
- planned: `scene_detection.sharpness_candidates`
- planned: `scene_detection.sharpness_window_seconds`
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
- `frame_extraction_report.frames_count` matches the number of report frames with `included_in_frame_refs=true`;
- `frame_extraction_report.frames_extracted_total == len(frame_extraction_report.frames)`;
- report `dedup` settings match current dedup config;
- report `frame_selection` matches current adaptive timestamp config;
- every report frame file exists and is non-empty, including dropped duplicates.

Current skip validation is report-aware for adaptive timestamp planning and dedup config, but not a full config/media fingerprint. It does not yet compare media identity, scene fingerprints/ranges, or planned timestamp shape beyond the recorded frame-selection config.

Planned skip validation should reject stale, partial, corrupt, or config/media/scene-mismatched outputs using durable report metadata, not expensive pixel-dependent recomputation.

## Important Notes

- During extraction, missing or empty frame files fail the stage. Current skip validation checks referenced frame existence, not file size.
- Frame paths are stored relative to the job directory for portable Obsidian/export references.
- Frame timestamps are evidence paths for visual provenance.
- Planned timestamp planning should be separated from extraction so it can be unit tested without ffmpeg.
- Adaptive sampling bounds candidate frames by scene duration; duplicate filtering then reduces near-identical frames before stage 10.
- `frame_refs.jsonl` is the canonical visual evidence input for stage 10 and contains only frames that passed dedup.
- Dropped duplicate frames are diagnostic/review data only. They must not be treated as KB evidence in timeline, chunk, claim, or export paths.
- Loosening duplicate filtering means increasing `phash_max_distance` and/or lowering `ssim_confirm`; this drops more near-duplicate frames before stage 10.
- Frame counts adapt to scene duration: very short scenes use one middle frame; medium scenes use two frames; long scenes include extra samples but never exceed `max_frames_per_scene`.
- Planned boundary padding should avoid selecting transition frames exactly at scene cuts. If the scene is shorter than `2 * padding`, use the midpoint.
- Planned stale frame handling may remove only stage-owned files matching `frames/scene_*.jpg` that are no longer part of the current plan.
- Normal diagnostics should be aggregated in `logs/09_extract_keyframes.jsonl` and `frames/frame_extraction_report.json`; do not create one log file per frame by default.
- `frame_selection=legacy` should remain the deterministic ffmpeg rollback path.
- `frame_selection=content_aware` should be opt-in until A/B checks pass. It may use OpenCV/numpy to score narrow timestamp windows by sharpness/brightness/contrast and write the selected decoded frame without invoking ffmpeg again.
- Duplicate filtering should use pHash/dHash-style perceptual distance, preserve at least `min_frames_per_scene`, avoid removing the first keyframe of a scene only because a neighboring scene is similar, and log skipped duplicates.
- Do not add CLIP/open-clip/torch, embeddings, vector stores, or heavy ML dependencies for MVP frame selection.

## Planned Report Contract

`frame_extraction_report.json` includes:

- `video_id`;
- `scenes_count`;
- `frames_count`: number of frames included in `frame_refs.jsonl`;
- `frames_extracted_total`: number of physically extracted frames;
- `frames_dropped`: number of extracted frames excluded by dedup;
- `dedup`: `enabled`, `phash_max_distance`, `ssim_confirm`, `min_frames_per_scene`;
- `frame_selection`: adaptive duration strategy and configured frame-count bounds;
- `frames`: one row per extracted frame, with `included_in_frame_refs`.

Dropped duplicate rows in `frames` include a nested `dedup` object:

- `matched_frame`;
- `matched_timestamp`;
- `phash_distance`;
- `ssim`;
- `skip_reason`.

All frame paths in the report are relative to the job directory.

For `content_aware`, skip validation should validate recorded structure and metadata without re-decoding video or recomputing sharpness/pHash selection.

## Related Code

- `src/style_kb/stages/stage_09_extract_keyframes.py`
- `src/style_kb/clients/media.py`
- planned: `src/style_kb/clients/frame_selection.py`
- `src/style_kb/models.py::FrameRef`

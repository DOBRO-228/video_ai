# 09_extract_keyframes

## Purpose

Extract representative frame images for each detected scene. These frames are the visual evidence sent to stage 10, so this stage is a direct cost and quality gate for vision requests.

## How It Works

When `pipeline.visual_enabled=false` (the default audio-only mode), the pipeline skips this stage by config. Audio-only jobs do not create `frames/frame_refs.jsonl`, frame JPGs, or a frame extraction report.

The current implementation uses adaptive timestamp planning. Scenes up to 4 seconds use one middle keyframe, scenes up to 8 seconds use two keyframes, and longer scenes use at least `scene_detection.images_per_scene` keyframes. Long scenes increase the target count from `ceil(scene.duration / scene_detection.extra_sample_every_seconds)`, capped by `scene_detection.max_frames_per_scene`. Timestamps are distributed inside the scene, clamped inside scene bounds, and deduplicated.

When `scene_detection.keyframe_edge_inset_seconds > 0` and the scene is long enough, planned timestamps are clamped to `[scene.start + inset, scene.end - inset]`. This keeps representative frames away from hard scene boundaries. If frame quality probing is enabled, probe timestamps are clamped to the same inset bounds so the `±probe_window_seconds` window cannot select a boundary frame that the planned timestamp avoided.

Each candidate frame is extracted from the proxy video with ffmpeg. When `scene_detection.frame_quality.enabled` is true, each planned timestamp is first expanded into a short ffmpeg probe window; the selected probe is promoted to the canonical `frames/scene_*.jpg` path. Probe timestamps are nominal absolute positions from the probe grid (`window_start + index * probe_step_seconds`), not measured emitted PTS, before they are written to `FrameRef`, timestamp URLs, or reports. `planned_nearest_kept` means the selected emitted frame was nearest to the planned timestamp and may differ by roughly half a probe step; the original planned timestamp remains in the report as `planned_timestamp`.

Intra-scene duplicate filtering then decides which selected frames are included in `FrameRef` records for stage 10. Dropped duplicate or quality-rejected files remain on disk when they are canonical selected frames and are recorded in `frames/frame_extraction_report.json` for dashboard review. The stage also writes `logs/09_extract_keyframes.jsonl`.

Default duplicate filtering is intentionally less conservative: `phash_max_distance=10` and `ssim_confirm=0.82`. This treats frames as duplicates when their perceptual hashes are close and SSIM still confirms structural similarity. On `qxXRWoSYf7I`, adaptive planning with `images_per_scene=4`, `extra_sample_every_seconds=4`, and `max_frames_per_scene=8` plans 322 candidate frames across 75 scenes before dedup. Offline replay against the extracted frames estimates roughly 207 kept frames and 115 dedup drops with these thresholds, reducing the stage 10 frame evidence upper bound while preserving at least one frame per scene.

The hardening contract from `PLAN_08_09_stages.md` is stricter than the current implementation. When changing this stage, move toward the target behavior below.

## Inputs

- `downloads/video_proxy.mp4`
- `scenes/scenes.jsonl`
- `scene_detection.images_per_scene`
- `scene_detection.extra_sample_every_seconds`
- `scene_detection.max_frames_per_scene`
- `scene_detection.keyframe_edge_inset_seconds`
- `scene_detection.intra_scene_dedup`
- `scene_detection.phash_max_distance`
- `scene_detection.ssim_confirm`
- `scene_detection.min_frames_per_scene`
- `scene_detection.frame_quality.*`
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
- report `frame_quality` matches current quality config and extraction backend;
- if frame quality is enabled, `selection_summary` and `quality_class_summary` sum to `selected_frames_count`;
- every report frame file exists and is non-empty, including dropped duplicates.

Current skip validation is report-aware for adaptive timestamp planning and dedup config, but not a full config/media fingerprint. It does not yet compare media identity, scene fingerprints/ranges, or planned timestamp shape beyond the recorded frame-selection config.

Planned skip validation should reject stale, partial, corrupt, or config/media/scene-mismatched outputs using durable report metadata, not expensive pixel-dependent recomputation.

## Important Notes

- During extraction, missing or empty frame files fail the stage. Current skip validation checks referenced frame existence, not file size.
- Frame paths are stored relative to the job directory for portable Obsidian/export references.
- Frame timestamps are evidence paths for visual provenance.
- Planned timestamp planning should be separated from extraction so it can be unit tested without ffmpeg. Unit tests are not currently part of the requested implementation, so A/B and dashboard review are the verification path.
- Adaptive sampling bounds candidate frames by scene duration; duplicate filtering then reduces near-identical frames before stage 10.
- `frame_refs.jsonl` is the canonical visual evidence input for stage 10 and contains only frames that passed dedup.
- Audio-only jobs must not require or synthesize empty frame refs.
- Dropped duplicate and quality-rejected frames are diagnostic/review data only. They must not be treated as KB evidence in timeline, chunk, claim, or export paths.
- Loosening duplicate filtering means increasing `phash_max_distance` and/or lowering `ssim_confirm`; this drops more near-duplicate frames before stage 10.
- Frame counts adapt to scene duration: very short scenes use one middle frame; medium scenes use two frames; long scenes include extra samples but never exceed `max_frames_per_scene`.
- `keyframe_edge_inset_seconds` is a guard against detector boundaries that are a little late or early. It is applied to both planned timestamps and quality probe timestamp windows when the scene is long enough.
- Planned boundary padding should avoid selecting transition frames exactly at scene cuts. If the scene is shorter than `2 * padding`, use the midpoint.
- Planned stale frame handling may remove only stage-owned files matching `frames/scene_*.jpg` that are no longer part of the current plan.
- Normal diagnostics should be aggregated in `logs/09_extract_keyframes.jsonl` and `frames/frame_extraction_report.json`; do not create one log file per frame by default.
- `frame_selection=legacy` should remain the deterministic ffmpeg rollback path.
- Frame quality selection is opt-in until A/B checks pass. It uses ffmpeg window extraction for probe pixels and OpenCV/numpy metrics for scoring.
- Current quality thresholds are calibrated only enough for Phase A relative ranking. Do not enable `scene_detection.frame_quality.drop_low_quality` until A/B calibration on real jobs confirms the hard-drop criteria.
- Low saturation is diagnostic by itself. It does not make a frame low-quality unless paired with weak contrast/edge evidence as `gray_transition_like`, so intentional monochrome voiceover/recording-overlay frames can remain valid visual evidence.
- Duplicate filtering should use pHash/dHash-style perceptual distance, preserve at least `min_frames_per_scene`, avoid removing the first keyframe of a scene only because a neighboring scene is similar, and log skipped duplicates.
- Do not add CLIP/open-clip/torch, embeddings, vector stores, or heavy ML dependencies for MVP frame selection.

## Planned Report Contract

`frame_extraction_report.json` includes:

- `video_id`;
- `scenes_count`;
- `frames_count`: number of frames included in `frame_refs.jsonl`;
- `frames_extracted_total`: number of physically extracted frames;
- `frames_dropped`: number of selected frames excluded from `frame_refs.jsonl` by dedup or quality drop;
- `dedup`: `enabled`, `phash_max_distance`, `ssim_confirm`, `min_frames_per_scene`;
- `frame_selection`: adaptive duration strategy, configured frame-count bounds, and `keyframe_edge_inset_seconds`;
- `frame_quality`: quality-selection config plus the `ffmpeg_window` extraction backend;
- `quality_summary`: slot/window/probe extraction counters;
- `selection_summary`: `planned_nearest_kept` vs `replaced_by_higher_quality_probe`;
- `quality_class_summary`: `good`, `low_quality_kept`, `low_quality_dropped`;
- `frames`: one row per extracted frame, with `included_in_frame_refs`.

Dropped duplicate rows in `frames` include a nested `dedup` object:

- `matched_frame`;
- `matched_timestamp`;
- `phash_distance`;
- `ssim`;
- `skip_reason`.

Quality-rejected rows include `quality_drop`. Quality-enabled rows include:

- `planned_timestamp`;
- selected absolute `timestamp`;
- `timestamp_delta_seconds`;
- `selection_decision`;
- `quality_class`;
- selected-frame `quality` metrics;
- compact `quality_probes` metrics.

Quality metrics include sharpness/contrast/edge density, local blur ratios, saturation/colorfulness fields, and diagnostic flags such as `low_saturation` or `gray_transition_like`.

All frame paths in the report are relative to the job directory.

For `content_aware`, skip validation should validate recorded structure and metadata without re-decoding video or recomputing sharpness/pHash selection.

## Related Code

- `src/style_kb/stages/stage_09_extract_keyframes.py`
- `src/style_kb/clients/media.py`
- planned: `src/style_kb/clients/frame_selection.py`
- `src/style_kb/models.py::FrameRef`

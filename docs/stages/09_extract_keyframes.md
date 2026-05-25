# 09_extract_keyframes

## Purpose

Extract representative frame images for each detected scene.

## How It Works

For each scene, the stage samples base timestamps at 15%, 50%, and 85% of scene duration, limited by `scene_detection.images_per_scene`. It also samples extra frames every `scene_detection.extra_sample_every_seconds`. Timestamps are clamped inside scene bounds and deduplicated.

Each frame is extracted from the proxy video and recorded as a `FrameRef`.

## Inputs

- `downloads/video_proxy.mp4`
- `scenes/scenes.jsonl`
- `scene_detection.images_per_scene`
- `scene_detection.extra_sample_every_seconds`

## Outputs

- `frames/frame_refs.jsonl`
- `frames/scene_*.jpg`

## Skip Validation

The stage can be skipped when `frame_refs.jsonl` exists, contains at least one frame ref, and every referenced frame file exists.

## Important Notes

- Missing or empty frame files fail the stage.
- Frame paths are stored relative to the job directory for portable Obsidian/export references.
- Frame timestamps are evidence paths for visual provenance.

## Related Code

- `src/style_kb/stages/stage_09_extract_keyframes.py`
- `src/style_kb/clients/media.py`
- `src/style_kb/models.py::FrameRef`


# 08_detect_scenes

## Purpose

Detect visual scene boundaries in the proxy video.

## How It Works

The stage opens the proxy video with PySceneDetect and uses `AdaptiveDetector`. The configured minimum scene length is converted to frames using ffprobe FPS. Detected scenes are normalized into `Scene` objects.

If PySceneDetect returns no scenes, the stage emits one full-video scene from `0..video_duration`.

## Inputs

- `downloads/video_proxy.mp4`
- `downloads/video_proxy.ffprobe.json`
- `scene_detection.detector`
- `scene_detection.min_scene_len_seconds`

## Outputs

- `scenes/scenes.jsonl`

## Skip Validation

The stage can be skipped when `scenes.jsonl` exists and parses to at least one `Scene`.

## Important Notes

- The full-video scene behavior is the only explicit no-cut special case.
- `fallback_scene_seconds` exists in config for forward compatibility but is not used by this stage.
- Scene timestamps drive keyframes, visual events, timeline events, and chunk grouping.

## Related Code

- `src/style_kb/stages/stage_08_detect_scenes.py`
- `src/style_kb/models.py::Scene`


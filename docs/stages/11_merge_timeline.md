# 11_merge_timeline

## Purpose

Build the central timeline. With visuals enabled, this is one `TimelineEvent` per scene containing clipped speech, visual summary, on-screen text, and source grounding. With `pipeline.visual_enabled=false`, this is an audio-only timeline with one `TimelineEvent` per speech segment.

## How It Works

The stage stores durable media duration metadata, loads speech segments and speech tokens, and then builds timeline events.

When `pipeline.visual_enabled=true`, the stage checks audio/video duration mismatch, loads scenes and visual events, then builds one timeline event per scene. Speech is clipped to scene boundaries using token midpoint timestamps instead of blindly attaching whole overlapping segments.

Visual fields and `presenter_context` are copied from the scene's `VisualEvent`. Speech is also grouped into `speech_turns` so downstream consumers can distinguish `host` from `offscreen_questioner`.

When `pipeline.visual_enabled=false`, stages 03/08/09/10 are skipped. Stage 11 uses `metadata/video_info.json` as the video-duration reference, compares audio ffprobe duration to metadata duration, and builds audio-only timeline events from speech segments. Visual fields, on-screen text, items, topics, and presenter context are empty/defaulted. Source refs use audio/YouTube grounding only.

## Inputs

- `metadata/video_info.json`
- `stt/speech_tokens.jsonl`
- `stt/speech_segments.jsonl`
- `downloads/audio.ffprobe.json`
- when `pipeline.visual_enabled=true`: `visual/visual_events.jsonl`
- when `pipeline.visual_enabled=true`: `scenes/scenes.jsonl`
- when `pipeline.visual_enabled=true`: `downloads/video_proxy.ffprobe.json`

## Outputs

- `timeline/timeline_events.jsonl`
- `timeline/media_durations.json`

## Skip Validation

The stage rebuilds expected timeline events and compares them with the existing output. It skips only when the existing output matches the deterministic rebuild and `timeline/media_durations.json` still matches current ffprobe data when ffprobe files are present.

After `17_cleanup` removes media and ffprobe files, validation uses `timeline/media_durations.json` as the durable source for the original audio/video duration check.

## Important Notes

- Fail when the active duration reference mismatch exceeds 1 second: audio vs proxy video with visuals enabled, audio vs metadata in audio-only mode.
- Missing visual event for any scene is a hard error only when `pipeline.visual_enabled=true`.
- Timeline events are the main bridge between raw modalities and chunks.
- Keep `speech_text` plain for compatibility; use `speech_turns` for speaker-aware dialogue.
- Do not synthesize fake scene/frame/visual records for audio-only jobs.

## Related Code

- `src/style_kb/stages/stage_11_merge_timeline.py`
- `src/style_kb/models.py::TimelineEvent`

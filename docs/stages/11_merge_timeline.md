# 11_merge_timeline

## Purpose

Build the central multimodal timeline: one `TimelineEvent` per scene containing clipped speech, visual summary, on-screen text, and source grounding.

## How It Works

The stage checks audio/video duration mismatch, stores the checked durations in durable timeline metadata, loads scenes, visual events, speech segments, and speech tokens, then builds one timeline event per scene. Speech is clipped to scene boundaries using token midpoint timestamps instead of blindly attaching whole overlapping segments.

Visual fields and `presenter_context` are copied from the scene's `VisualEvent`. Speech is also grouped into `speech_turns` so downstream consumers can distinguish `host` from `offscreen_questioner`.

## Inputs

- `metadata/video_info.json`
- `stt/speech_tokens.jsonl`
- `stt/speech_segments.jsonl`
- `visual/visual_events.jsonl`
- `scenes/scenes.jsonl`
- `downloads/audio.ffprobe.json`
- `downloads/video_proxy.ffprobe.json`

## Outputs

- `timeline/timeline_events.jsonl`
- `timeline/media_durations.json`

## Skip Validation

The stage rebuilds expected timeline events and compares them with the existing output. It skips only when the existing output matches the deterministic rebuild and `timeline/media_durations.json` still matches current ffprobe data when ffprobe files are present.

After `17_cleanup` removes media and ffprobe files, validation uses `timeline/media_durations.json` as the durable source for the original audio/video duration check.

## Important Notes

- Fail when audio/video duration mismatch exceeds 1 second.
- Missing visual event for any scene is a hard error.
- Timeline events are the main bridge between raw modalities and chunks.
- Keep `speech_text` plain for compatibility; use `speech_turns` for speaker-aware dialogue.

## Related Code

- `src/style_kb/stages/stage_11_merge_timeline.py`
- `src/style_kb/models.py::TimelineEvent`

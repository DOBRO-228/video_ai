# 13_export_jsonl

## Purpose

Publish a stable machine-readable JSONL export bundle.

## How It Works

The stage copies canonical internal artifacts into `exports/jsonl/` using the export helper. It does not transform business semantics; it makes a stable bundle for future import into databases, vector indexes, RAG systems, or web UI.

## Inputs

- `metadata/video_info.json`
- `stt/speaker_diarization.json`
- `stt/speech_tokens.jsonl`
- `stt/speech_segments.jsonl`
- `scenes/scenes.jsonl`
- `frames/frame_refs.jsonl`
- `visual/visual_events.jsonl`
- `timeline/timeline_events.jsonl`
- `chunks/chunks.jsonl`

## Outputs

- `exports/jsonl/video_info.jsonl`
- `exports/jsonl/speaker_diarization.jsonl`
- `exports/jsonl/speech_tokens.jsonl`
- `exports/jsonl/speech_segments.jsonl`
- `exports/jsonl/scenes.jsonl`
- `exports/jsonl/frame_refs.jsonl`
- `exports/jsonl/visual_events.jsonl`
- `exports/jsonl/timeline_events.jsonl`
- `exports/jsonl/chunks.jsonl`

## Skip Validation

The stage can be skipped when every expected exported JSONL file exists, including `speaker_diarization.jsonl`.

## Important Notes

- Export compatibility matters more than presentation here.
- Do not omit provenance fields from export outputs.
- Keep canonical internal artifact schemas aligned with exported files.

## Related Code

- `src/style_kb/stages/stage_13_export_jsonl.py`
- `src/style_kb/export/jsonl.py`

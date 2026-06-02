# 14_export_jsonl

## Purpose

Publish a stable machine-readable JSONL export bundle.

## How It Works

The stage copies canonical internal artifacts into `exports/jsonl/` using the export helper. It does not transform business semantics; it makes a stable machine-readable bundle and writes `manifest.json` so future importers know which files are knowledge and which files are audit/debug context.

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
- `chunks/chunk_plan.json`
- `claims/style_claims.jsonl`

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
- `exports/jsonl/chunk_plan.jsonl`
- `exports/jsonl/style_claims.jsonl`
- `exports/jsonl/manifest.json`

## Skip Validation

The stage can be skipped when every expected exported JSONL file exists, including `speaker_diarization.jsonl`, `chunk_plan.jsonl`, and `style_claims.jsonl`, and `manifest.json` exists.

## Important Notes

- Export compatibility matters more than presentation here.
- Do not omit provenance fields from export outputs.
- Keep canonical internal artifact schemas aligned with exported files.
- JSONL is the machine-readable export source of truth. Obsidian Markdown is a presentation export.
- Future KB/RAG importers must use `manifest.json`; the initial knowledge allowlist is `chunks.jsonl` and `style_claims.jsonl`.
- Low-level files such as `speech_tokens.jsonl`, `scenes.jsonl`, `frame_refs.jsonl`, `visual_events.jsonl`, `timeline_events.jsonl`, and `chunk_plan.jsonl` are audit context, not default RAG input.

## Related Code

- `src/style_kb/stages/stage_14_export_jsonl.py`
- `src/style_kb/export/jsonl.py`

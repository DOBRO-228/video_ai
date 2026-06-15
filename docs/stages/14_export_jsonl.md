# 14_export_jsonl

## Purpose

Publish a stable machine-readable JSONL export bundle.

## How It Works

The stage copies canonical internal artifacts into `exports/jsonl/` using the export helper. It does not transform business semantics; it makes a stable machine-readable bundle and writes `manifest.json` so future importers know which files are knowledge and which files are audit/debug context.

For style claims, the stage exports `claims/style_claims_current.jsonl` when the dashboard has materialized manual edits/deletes. If that overlay does not exist, it falls back to the original LLM artifact at `claims/style_claims.jsonl`.

When `pipeline.visual_enabled=false`, the bundle omits `scenes.jsonl`, `frame_refs.jsonl`, and `visual_events.jsonl`, removes stale copies of those files from previous visual runs, and writes `manifest.visual_enabled=false`.

## Inputs

- `metadata/video_info.json`
- `stt/speaker_diarization.json`
- `stt/speech_tokens.jsonl`
- `stt/speech_segments.jsonl`
- when `pipeline.visual_enabled=true`: `scenes/scenes.jsonl`
- when `pipeline.visual_enabled=true`: `frames/frame_refs.jsonl`
- when `pipeline.visual_enabled=true`: `visual/visual_events.jsonl`
- `timeline/timeline_events.jsonl`
- `chunks/chunks.jsonl`
- `chunks/chunk_plan.json`
- `claims/style_claims_current.jsonl` when present; otherwise `claims/style_claims.jsonl`

## Outputs

- `exports/jsonl/video_info.jsonl`
- `exports/jsonl/speaker_diarization.jsonl`
- `exports/jsonl/speech_tokens.jsonl`
- `exports/jsonl/speech_segments.jsonl`
- when `pipeline.visual_enabled=true`: `exports/jsonl/scenes.jsonl`
- when `pipeline.visual_enabled=true`: `exports/jsonl/frame_refs.jsonl`
- when `pipeline.visual_enabled=true`: `exports/jsonl/visual_events.jsonl`
- `exports/jsonl/timeline_events.jsonl`
- `exports/jsonl/chunks.jsonl`
- `exports/jsonl/chunk_plan.jsonl`
- `exports/jsonl/style_claims.jsonl`
- `exports/jsonl/manifest.json`

## Skip Validation

The stage can be skipped when every expected exported JSONL file exists, including `speaker_diarization.jsonl`, `chunk_plan.jsonl`, and `style_claims.jsonl`, and `manifest.json` exists. The manifest must match the current `pipeline.visual_enabled` value and must include or omit the visual export filenames accordingly. The effective style-claims input participates in freshness checks, so newer dashboard edits/deletes invalidate the export. In audio-only mode, stale visual export files make the stage rebuild so they can be removed.

## Important Notes

- Export compatibility matters more than presentation here.
- Do not omit provenance fields from export outputs.
- Keep canonical internal artifact schemas aligned with exported files.
- JSONL is the machine-readable export source of truth. Obsidian Markdown is a presentation export.
- Future KB/RAG importers must use `manifest.json`; the initial knowledge allowlist is `chunks.jsonl` and `style_claims.jsonl`.
- `exports/jsonl/style_claims.jsonl` must contain the effective Claims after dashboard manual edits/deletes, not necessarily the original LLM Claims.
- Low-level files such as `speech_tokens.jsonl`, optional visual audit files, `timeline_events.jsonl`, and `chunk_plan.jsonl` are audit context, not default RAG input.

## Related Code

- `src/style_kb/stages/stage_14_export_jsonl.py`
- `src/style_kb/export/jsonl.py`

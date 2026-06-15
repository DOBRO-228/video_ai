# 15_export_obsidian

## Purpose

Render human-readable Obsidian Markdown notes from video, timeline, chunks, style claims, and frame refs.

## How It Works

The stage loads video metadata, timeline events, chunks, effective style claims, and, when visuals are enabled, frame refs. Effective style claims come from `claims/style_claims_current.jsonl` when dashboard manual edits/deletes exist, otherwise from `claims/style_claims.jsonl`. If `pipeline.visual_enabled=true` and `project.keep_frames=true`, it builds relative frame links for scene notes. It then renders index, video, and chunk Markdown files through Jinja2 templates.

When `pipeline.visual_enabled=false`, the stage renders notes without frame links and does not require `frames/frame_refs.jsonl`.

When `speech_turns` are available, video notes render speaker-aware dialogue instead of a single unlabeled speech block.
Style claims are rendered in the video note and in their corresponding chunk notes.
Markdown is a human navigation view. JSONL remains the machine-readable source for importers.

## Inputs

- `metadata/video_info.json`
- `timeline/timeline_events.jsonl`
- `chunks/chunks.jsonl`
- `claims/style_claims_current.jsonl` when present; otherwise `claims/style_claims.jsonl`
- when `pipeline.visual_enabled=true`: `frames/frame_refs.jsonl`
- `pipeline.visual_enabled`
- `project.keep_frames`

## Outputs

- `exports/obsidian/index.md`
- `exports/obsidian/videos/{video_id}.md`
- `exports/obsidian/chunks/{chunk_id}.md`

## Skip Validation

The stage can be skipped when the index note, video note, and every expected chunk note exist.

## Important Notes

- Obsidian notes are presentation exports, not canonical data.
- Do not import Obsidian notes into the same KB/RAG index as JSONL chunks; use the JSONL manifest allowlist for machine ingestion.
- Canonical LLM claim data remains `claims/style_claims.jsonl`; dashboard manual edits/deletes are read from the materialized `claims/style_claims_current.jsonl` overlay when present. Markdown should not become the source of truth.
- Keep presenter blocks conditional so recurring background presenter descriptions do not spam notes.
- Keep speaker role labels human-readable in Russian: `Ведущий` and `Закадровый вопрос`.
- Frame links should remain relative to the job export layout when visual artifacts exist.

## Related Code

- `src/style_kb/stages/stage_15_export_obsidian.py`
- `src/style_kb/export/obsidian.py`
- `src/style_kb/templates/`

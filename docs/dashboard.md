# Local Dashboard

Local UI for inspecting `style-kb` run artifacts from the configured output root.

Run from the repository root:

```bash
make dashboard
```

The dashboard reads `src/style_kb/config/default.yaml`, opens `./mens-style-kb` by default, and serves a localhost URL. It does not modify pipeline state, the public `style-kb ingest/status/resume` CLI contract, or the original LLM-generated `claims/style_claims.jsonl` artifact. Ingest/resume may be run with an optional stage number, such as `style-kb ingest URL 9`, to stop after stage 09 and inspect intermediate artifacts.

Claim files:

```text
claims/style_claims.jsonl
claims/style_claims_current.jsonl
claims/style_claims_manual_edits.jsonl
```

- `style_claims.jsonl`: original LLM-generated Claims. The dashboard does not rewrite this file.
- `style_claims_current.jsonl`: current Claims after applying all manual edits. The dashboard rebuilds this file atomically after every saved edit.
- `style_claims_manual_edits.jsonl`: append-only audit log. Each edit record includes the original LLM Claim, the previous effective Claim, the updated Claim, changed fields, timestamp, and actor.

The dashboard applies the audit log as an overlay when displaying Claims, then materializes the same result into `style_claims_current.jsonl`.

Views:

- `Обзор`: job status, stage table, quality report coverage, warnings, artifact inventory, presenter profile.
- `Timeline`: speech/visual/OCR events with keyframes and YouTube timestamp links.
- `Claims`: extracted style claims with type/confidence filters, evidence, related frames, manual edit badges, editable claim fields, and edit history.
- `Chunks`: chunk boundaries, combined text, topics, entities, and related claims.
- `Visuals`: scene-level visual descriptions, OCR, items, topics, and frames. If stage 09 has produced `frame_refs.jsonl` but stage 10 has not materialized visual events yet, this view falls back to frame-only scene cards.
- `Logs`: SQLite stage state and recent `logs/pipeline.jsonl` events.

Frame duplicate review:

- Canonical frame strips use `frames/frame_refs.jsonl` and show only frames that were sent to visual analysis.
- Dropped duplicate frames are read from `frames/frame_extraction_report.json` and shown only in a separate review strip on Visuals cards.
- Dropped duplicate frames are diagnostic data and are not shown as claim/chunk/timeline evidence.

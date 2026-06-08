# Local Dashboard

Read-only UI for inspecting `style-kb` run artifacts from the configured output root.

Run from the repository root:

```bash
PYTHONPATH=src python -m style_kb.dashboard
```

The dashboard reads `src/style_kb/config/default.yaml`, opens `./mens-style-kb` by default, and serves a localhost URL. It does not modify pipeline state, job artifacts, or the public `style-kb ingest/status/resume` CLI contract.

Views:

- `Обзор`: job status, stage table, quality report coverage, warnings, artifact inventory, presenter profile.
- `Timeline`: speech/visual/OCR events with keyframes and YouTube timestamp links.
- `Claims`: extracted style claims with type/confidence filters, evidence, and related frames.
- `Chunks`: chunk boundaries, combined text, topics, entities, and related claims.
- `Visuals`: scene-level visual descriptions, OCR, items, topics, and frames.
- `Logs`: SQLite stage state and recent `logs/pipeline.jsonl` events.


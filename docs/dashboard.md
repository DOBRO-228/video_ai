# Local Dashboard

Local UI for inspecting `style-kb` run artifacts from the configured output root.

Run from the repository root:

```bash
make dashboard
```

The dashboard reads `src/style_kb/config/default.yaml`, opens `./mens-style-kb` by default, and serves a localhost URL. It does not change the public `style-kb ingest/status/resume` CLI contract or the original LLM-generated `claims/style_claims.jsonl` artifact. Manual dashboard state is stored as separate dashboard-managed artifacts. Claim edits briefly acquire the same SQLite job lock used by the pipeline and update only the SQLite state for stage 16 after refreshing the quality report. Ingest/resume may be run with an optional stage number, such as `style-kb ingest URL 9`, to stop after stage 09 and inspect intermediate artifacts. Ingest/resume may also use `--batch` to enable eligible OpenAI Batch API requests for that run.

Dashboard claim edits are terminal post-pipeline edits. They are allowed only after the job
is completed and no live pipeline lock is present. Run all required `resume` commands before
editing claims in the dashboard.

Job review metadata:

```text
reports/human_review.json
```

- `human_reviewed`: boolean flag set only by the dashboard checkbox.
- `reviewed_at`: timestamp of the latest positive manual review mark.
- `updated_at`: timestamp of the latest dashboard change to the flag.

Jobs marked `human_reviewed=true` are highlighted green in the dashboard job list.

Claim files:

```text
claims/style_claims.jsonl
claims/style_claims_current.jsonl
claims/style_claims_manual_edits.jsonl
```

- `style_claims.jsonl`: original LLM-generated Claims. The dashboard does not rewrite this file.
- `style_claims_current.jsonl`: current Claims after applying all manual edits and deletes. The dashboard rebuilds this file atomically after every saved edit or delete.
- `style_claims_manual_edits.jsonl`: append-only audit log. Each update record includes the original LLM Claim, the previous effective Claim, the updated Claim, changed fields, timestamp, and actor. Delete records use `action: delete`, keep the original and previous effective Claim, and remove the Claim only from the current dashboard/materialized overlay.

The dashboard applies the audit log as an overlay when displaying Claims, then materializes the same result into `style_claims_current.jsonl`. Deleting a Claim does not rewrite `style_claims.jsonl`.

After every saved claim edit/delete, the dashboard refreshes existing claim-derived
surfaces:

- `exports/jsonl/style_claims.jsonl` and claim-owned fields in `exports/jsonl/manifest.json`
  (`style_claims_source`, `style_claims_sha256`, `style_claims_count`);
- the human-facing Obsidian export under `exports/obsidian/`, rendered through the same
  stage 15 path;
- `reports/quality_report.json`, recomputed through the same builder as stage 16, plus
  the SQLite state for stage 16.

If any derived refresh fails, the overlay remains the source of truth and the failed
derived surface is reported as stale.

Views:

- `Обзор`: job status, stage table, quality report coverage, warnings, artifact inventory, presenter profile.
- `Timeline`: speech/visual/OCR events with keyframes and YouTube timestamp links.
- `Claims`: extracted style claims with type/confidence filters, evidence, related frames, manual edit badges, editable claim fields, delete controls, and edit history.
- `Chunks`: chunk boundaries, combined text, topics, entities, and related claims.
- `Visuals`: scene-level visual descriptions, OCR, items, topics, and frames. If stage 09 has produced `frame_refs.jsonl` but stage 10 has not materialized visual events yet, this view falls back to frame-only scene cards.
- `Logs`: SQLite stage state and recent `logs/pipeline.jsonl` events.

Frame duplicate review:

- Canonical frame strips use `frames/frame_refs.jsonl` and show only frames that were sent to visual analysis.
- Dropped duplicate and quality-rejected frames are read from `frames/frame_extraction_report.json` and shown only in a separate review strip on Visuals cards.
- Frame quality diagnostics expose selected-vs-planned timestamps, selection decisions, quality classes, and probe metrics in the raw job payload/report; rejected probes are not mixed into normal evidence strips.
- Dropped duplicate frames are diagnostic data and are not shown as claim/chunk/timeline evidence.

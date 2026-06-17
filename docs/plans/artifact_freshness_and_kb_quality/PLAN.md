# PLAN: artifact freshness and KB quality hardening

Status: implemented for artifact freshness/backfill/domain-guard diagnostics; multimodal validation remains a follow-up controlled run
Date: 2026-06-15
Implemented: 2026-06-17
Scope: dashboard claim overlays, downstream export freshness, quality-report freshness, Obsidian stale notes, audit noise, domain-term leakage, validation of the current audio-only baseline, and chunk-text redundancy diagnostics.

## Goal

Make completed jobs internally consistent after ingest/resume and after terminal dashboard
manual edits.

The immediate product goal is not to add new features. It is to make the existing KB artifacts trustworthy:

- the effective style claims used by humans and importers must be the same everywhere;
- stale presentation files must not survive after chunk or claim changes;
- quality/audit reports must distinguish real failures from stale or diagnostic noise;
- obvious STT domain-term mistakes must not become high-confidence knowledge claims;
- audio-only jobs must stay cleanly audio-only;
- multimodal behavior must be measured separately because the current job set did not exercise visual stages.

## Current Baseline

The inspected local baseline contains 10 completed jobs under `mens-style-kb/jobs`.

Important observations:

- `pipeline.visual_enabled=false` in `src/style_kb/config/default.yaml`, so stages 03, 08, 09, and 10 were skipped for every job.
- Final artifacts are audio-only: 92 chunks, all with `modality=["audio"]`, 0 scenes, 0 frame refs, 0 visual events.
- Claims are well grounded: 407 original `StyleClaim` records have `evidence`, `source_refs`, `timeline_event_ids`, and YouTube timestamp URLs.
- Quality reports show high coverage and no warnings, but some reports are stale after dashboard edits.
- Historical retry noise is concentrated in stage 07 speech segmentation and some stage 12 chunk planning.
- At least one STT/domain error reached final claims: `следки` became `слитки`, producing `Слитки — это невидимые носки.`

Concrete drift cases to preserve as regression examples:

| Job | Problem | Expected result |
| --- | --- | --- |
| `auxaGqHkFXE` | `claims/style_claims_current.jsonl` has 28 claims, but `exports/jsonl/style_claims.jsonl` still has 30. | Exported KB claims match the effective current claims exactly. |
| `K4jQPK2GmOg` | `style_claims_current.jsonl` has 45 claims and JSONL export has 45, but `quality_report.json` still counts 51. | Quality report counts effective current claims or is reported as stale until regenerated. |
| `qxXRWoSYf7I` | `style_claims_current.jsonl` has 55 claims and JSONL export has 55, but `quality_report.json` still counts 60. | Quality report counts effective current claims or is reported as stale until regenerated. |
| `KFDLiQE3V98` | 9 canonical chunks, but 13 Obsidian chunk notes remain. | Obsidian chunk notes exactly match current canonical chunk ids. |
| all audio-only jobs | `audit_jobs` scans full `combined_text` and reports presentation markers from normal speech. | Audit scans only visual chunk components for presentation-noise markers. |
| `0LwmYihRD9c` | Final claim uses `слитки` instead of `следки`. | Known domain-term mistakes are corrected, retried, or blocked before final KB claims. |

## KB-quality observations (content redundancy)

The drift cases above are consistency bugs: the same claims must appear identically on
every surface. Separately, the inspected jobs show a content-quality problem where a job is
internally consistent but redundant. This is not addressed by the freshness work and is
recorded here as a quality anchor.

### Chunk-text redundancy

The retrieval body `chunks.jsonl.combined_text` concatenates speech + `presenter_brief` +
`visual_text`, while `topics`, `entities`, and `visual_text` repeat the same garment
phrase lists. With visuals enabled this duplicates large phrase lists verbatim into
`combined_text`, inflating the RAG payload with repeated tokens. This is independent of
claim duplication and is likely the cheaper of the two to fix.

### Per-job-only KB

Export and Obsidian are strictly per job; only `chunks.jsonl` and `style_claims.jsonl` are
`role: knowledge` / `kb_import` in the manifest. There is no global, deduplicated KB across
videos, so recurring themes (fit, height adjustments, high-rise trousers, and seasonality
recur across many of the inspected jobs) coexist as independent records. Cross-job
consolidation is intentionally deferred (see the Deferred section) but is the larger
product lever for KB quality.

## Non-Goals

- Do not add CLI flags or runtime controls.
- Do not move product settings out of `src/style_kb/config/default.yaml`.
- Do not rewrite the original LLM artifact `claims/style_claims.jsonl` when applying dashboard edits.
- Do not make Obsidian Markdown the machine-readable source of truth.
- Do not introduce embeddings, vector stores, CLIP/open-clip/torch, or heavy ML dependencies.
- Do not solve all stage 07 semantic segmentation brittleness in this plan; measure and reduce the most obvious retry noise only after freshness bugs are fixed.
- Do not treat the current audio-only job set as proof that visual stages are production-ready.
- Do not perform cross-job / global claim consolidation in this phase; keep the KB per job and defer global consolidation to a later design.

## Design Principles

### 1. Effective claims are a first-class input

Any stage or dashboard path that consumes claims must use the effective claims source:

1. `claims/style_claims_current.jsonl` when it exists;
2. otherwise `claims/style_claims.jsonl`.

This effective input must participate in freshness checks, validation, export manifests, quality counts, and audit drift detection.

### 2. Dashboard edits must not silently split the KB

After a dashboard update/delete:

- `style_claims_current.jsonl` is the current claim overlay;
- the machine KB/RAG surface, when present, must be refreshed atomically and must match
  the current overlay: `exports/jsonl/style_claims.jsonl` plus its export manifest;
- the human-facing Obsidian surface, when present, must be re-rendered via the stage 15
  path and must match the current overlay;
- the quality report is not refreshed immediately and must be explicitly reported as stale instead of being shown as current.

Do not mutate SQLite stage state from the dashboard unless a separate design explicitly approves that. The dashboard already owns dashboard-managed artifacts; pipeline stage state should remain owned by ingest/resume.

### 3. Stage skip validation must validate semantics, not only existence

The stage docs already require `validate_outputs(context)` plus `outputs_are_current(context)`. For durable reports and exports, existence is not enough. Validation must check the relevant source identity, counts, and config/report metadata.

### 4. Raw transcript remains raw; KB-facing fields get guarded

Do not rewrite Soniox tokens or original transcript text opportunistically. Instead:

- prevent known STT/domain mistakes from becoming high-confidence claims;
- normalize user-facing claim fields where the correction is safe and domain-specific;
- preserve diagnostics that make the original mistake inspectable.

### 5. Dashboard edits are terminal post-pipeline edits

Dashboard editing always happens after the full pipeline has completed, including any
required `resume` runs. The overlay (`style_claims_current.jsonl`) and the manual-edits
log are therefore always built on the finalized `claim_id`s of `style_claims.jsonl`.
This is a workflow invariant, not just an implementation convenience.

Consequences:

- `style_claims.jsonl` is the stage-13-owned baseline; the dashboard never mutates it, only
  layers an overlay on top, so `claim_id`s stay stable for the overlay to reference;
- the original extraction stays recoverable from `style_claims_raw.json`. Grounded
  provenance fields are never mutated.
- once an overlay exists, a later `resume` that would re-run stage 13 is not an ordinary
  supported path. It must be refused; intentional fresh extraction requires manual
  discard/re-review of the dashboard overlay first, because `_renumber_claims` can orphan
  manual edits.

### 6. Dashboard/backfill operate only on completed, unlocked jobs

The normal ordering is: `ingest` / all necessary `resume` runs first, dashboard edits
afterward. Code should still guard that invariant:

- dashboard edit endpoints must refuse jobs that are not completed;
- dashboard edit endpoints must refuse jobs with an active pipeline/job lock or running
  `resume`;
- the export backfill must skip locked/running jobs and report them as skipped;
- `resume` must refuse to execute stage 13 when a dashboard overlay already exists;
- the existing claim-edit lock still serializes concurrent dashboard edits for the same
  completed job.

This is intentionally a check-then-act guard for the local single-user tool, not a full
shared job-lock acquisition by the dashboard/backfill. It sharply reduces accidental
races, but a `resume` process started immediately after the check can still race. Fully
closing that window would require dashboard/backfill to acquire the same pipeline job
lock, which is deferred unless real concurrent usage appears.

## Implementation Plan

### 1. Add focused regression tests and fixture helpers

Create a minimal test structure if it does not exist:

```text
tests/
tests/fixtures/
tests/test_export_freshness.py
tests/test_obsidian_freshness.py
tests/test_quality_report_freshness.py
tests/test_resume_overlay_guard.py
tests/test_audit_jobs.py
tests/test_dashboard_claim_edits.py
tests/test_style_claim_domain_terms.py
```

Use tiny synthetic job directories rather than copying large real artifacts. Fixtures should include only the fields required by the affected stages/models.

Test cases:

- effective claims overlay has fewer claims than original and export must match overlay;
- stage 14 validation fails when exported `style_claims.jsonl` differs from effective claims even if files and manifest exist;
- stage 15 validation fails when extra stale chunk notes exist;
- stage 15 `run()` removes stale chunk notes and writes exactly one note per current chunk;
- stage 16 validation fails when report claim count differs from effective claims count;
- resume refuses to re-run stage 13 when `style_claims_current.jsonl` or
  `style_claims_manual_edits.jsonl` already exists;
- dashboard claim edits are refused for incomplete or locked jobs;
- `audit_jobs` reports current/export/quality drift and does not report presentation chunk markers from audio transcript text;
- domain-term guard rejects or repairs `слитки` as a style term when the intended term is `следки`.

Verification command:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m pytest
```

If pytest is not installed in the local environment, add a lightweight project-approved test dependency or document the current blocker before implementation.

### 2. Centralize effective-claims helpers

Files:

- `src/style_kb/stages/common.py`
- possibly `src/style_kb/utils/files.py` or a new small utility module

Add shared helpers:

- `effective_style_claims_path(context)`: keep existing behavior.
- `read_effective_style_claim_rows(paths_or_context)`: returns raw dict rows for byte/content comparison and dashboard use.
- `effective_style_claims_fingerprint(path)`: stable SHA-256 over normalized JSONL rows.
- `jsonl_rows_equal(left_path, right_path)`: order-sensitive equality for exported claim rows.

Rules:

- Compare parsed JSON rows, not raw bytes, when the writer may change key order.
- Preserve canonical row order from the effective claims file.
- Keep helpers independent of provider calls and pipeline side effects.

### 2a. Add a shared claim-surface refresh service

Files:

- new `src/style_kb/export/claim_surfaces.py` or similarly small export helper module
- `src/style_kb/export/jsonl.py`
- `src/style_kb/export/obsidian.py`
- `src/style_kb/stages/stage_14_export_jsonl.py`
- `src/style_kb/stages/stage_15_export_obsidian.py`
- `src/style_kb/dashboard/server.py`
- new `src/style_kb/maintenance/refresh_kb_exports.py`

Purpose:

- one implementation refreshes claim-derived surfaces from effective claims;
- stage 14, stage 15, dashboard edits, and the backfill call this shared path instead of
  duplicating writer/render logic;
- the shared path updates `exports/jsonl/style_claims.jsonl` and the JSONL export
  `manifest.json` together, including the effective-claims source/count/fingerprint;
- when called from dashboard edits or backfill, the shared path patches only claim-owned
  manifest fields (`style_claims_source`, `style_claims_sha256`, `style_claims_count`, and
  any future claim-specific timestamp/fingerprint fields) and preserves all unrelated
  manifest metadata such as chunks/timeline/visual entries;
- full manifest regeneration remains stage 14's responsibility;
- the shared path reuses the exact stage 15 Obsidian renderer for human-facing Markdown;
- callers can choose which existing surfaces to refresh, but they must not create a
  surface the pipeline never produced unless that stage is actively running.

Rules:

- Content-compare before writing so idempotent dashboard/backfill runs do not rewrite
  already-current files.
- If dashboard/backfill cannot parse an existing manifest, they should report the JSONL
  surface as stale/error instead of reconstructing unrelated manifest fields they do not
  own.
- Keep the machine KB/RAG surface (`exports/jsonl/*`) and human-facing Obsidian surface
  separate in naming and docs.
- Never mutate `claims/style_claims.jsonl`, `claims/style_claims_raw.json`, or `claim_id`s.

### 3. Harden stage 14 JSONL export freshness

Files:

- `src/style_kb/stages/stage_14_export_jsonl.py`
- `src/style_kb/export/jsonl.py`
- `docs/stages/14_export_jsonl.md`

Changes:

- In `validate_outputs`, verify `exports/jsonl/style_claims.jsonl` exactly matches the current effective style-claims input.
- Extend `manifest.json` with claim-source metadata:
  - `style_claims_source`: relative path to effective input;
  - `style_claims_sha256`: fingerprint of effective input;
  - `style_claims_count`.
- Validate those manifest fields before skip.
- Use the shared claim-surface refresh service so stage 14, dashboard edits, and backfill
  write identical JSONL export rows and manifest metadata.
- Keep existing visual-enabled manifest behavior.
- Keep stale visual export removal in audio-only mode.

Acceptance checks:

- `auxaGqHkFXE` style claims export mismatch is detected before rebuild.
- After stage 14 rebuild, exported `style_claims.jsonl` equals `claims/style_claims_current.jsonl`.
- After stage 14 rebuild, `exports/jsonl/manifest.json` points at the effective claims
  source and carries the matching count/fingerprint.
- Audio-only bundles still omit visual export files and keep `manifest.visual_enabled=false`.

### 4. Make dashboard claim overlay refresh deterministic

Files:

- `src/style_kb/dashboard/server.py`
- shared claim-surface refresh helper from section 2a
- `src/style_kb/export/jsonl.py`
- `src/style_kb/export/obsidian.py`
- `docs/dashboard.md`

Decision: dashboard edits are allowed only after the full job is complete, including all
required `resume` runs. Every claim edit refreshes the existing machine KB/RAG surface
(JSONL export plus manifest) and the existing human-facing Obsidian surface in the same
request, so the dashboard never leaves a derived surface showing a claim the user already
deleted or edited. The quality report is the one derived artifact the dashboard does not
refresh, because its counts are a pipeline computation rather than a render; it stays
marked stale until a deliberate pipeline rebuild. This replaces the earlier stale-only
Obsidian approach.

Changes:

- Refuse dashboard edits unless the job is completed and has no active pipeline/job lock.
- Keep `claims/style_claims.jsonl` immutable.
- Ensure every update/delete writes `style_claims_current.jsonl` and the
  `style_claims_manual_edits.jsonl` log atomically.
- On every claim edit, refresh claim-derived surfaces atomically from the effective claims,
  each gated on the surface already existing for the job (do not materialize a surface the
  pipeline has not produced yet):
  - `exports/jsonl/style_claims.jsonl` and `exports/jsonl/manifest.json` via the shared
    claim-surface refresh service, matching the overlay exactly and recording the effective
    claims source/count/fingerprint;
  - the Obsidian vault by re-rendering the job with the same logic stage 15 uses
    (`render_obsidian_export`) from effective claims, so claim-bearing chunk notes, the
    video note, and the index all reflect the edit.
- Scope the Obsidian refresh to stage-15-owned files only
  (`exports/obsidian/index.md`, `exports/obsidian/videos/{video_id}.md`,
  `exports/obsidian/chunks/{chunk_id}.md`). Do not touch unrelated files.
- Make the overlay write the source of truth: if either export refresh fails, the overlay
  write must still hold, and the failed surface is reported as stale in the response
  instead of silently leaving partial output.
- Keep returning a `derived_artifacts_stale` section for `reports/quality_report.json` when
  effective claims are newer, and surface it in summary/detail payloads so the UI does not
  imply the quality report reflects the current overlay.

Do not refresh `quality_report.json` from the dashboard in this phase. It is a pipeline
report and should be regenerated only by an explicit pipeline rebuild/stage 16.

Accepted tradeoff: after the job is complete, the dashboard becomes the post-pipeline
writer of claim-derived surfaces. This is acceptable because the render is local and
deterministic, but it requires reusing the shared JSONL/manifest writer and exact stage 15
render path (no divergent dashboard-only renderer) with tight file scoping.

Acceptance checks:

- Dashboard edits are refused for incomplete jobs and jobs with an active pipeline/job
  lock.
- Editing/deleting a claim updates `style_claims_current.jsonl` and the manual-edits log.
- An existing JSONL export and manifest update in the same request and match the overlay
  source/count/fingerprint exactly.
- An existing Obsidian export updates in the same request: a deleted claim no longer appears
  in any chunk note, and the rendered note set matches the effective claims.
- The quality report is still reported stale (not refreshed) until an explicit pipeline
  rebuild.
- A failed export refresh surfaces in the API response and is marked stale rather than
  silently left inconsistent with the overlay.
- Dashboard docs state which artifacts are refreshed immediately (JSONL/RAG + manifest +
  Obsidian), which require pipeline rebuild (quality report), and that edits are terminal
  post-pipeline operations.

### 4a. Refuse resume stage-13 rebuilds when dashboard overlays exist

Files:

- `src/style_kb/pipeline/runner.py`
- `src/style_kb/stages/stage_13_extract_style_claims.py` if stage-local validation is cleaner
- `docs/stages/13_extract_style_claims.md`

Decision: dashboard overlays are built on finalized `claim_id`s. A later stage 13 rebuild
can renumber those ids and orphan `style_claims_current.jsonl` /
`style_claims_manual_edits.jsonl`. The runner must therefore reject a `resume` path that
would actually execute stage 13 for a job with an overlay.

Changes:

- Detect dashboard overlay presence before stage 13 is run:
  - `claims/style_claims_current.jsonl` exists; or
  - `claims/style_claims_manual_edits.jsonl` exists and is non-empty.
- If stage 13 is current and will be skipped, allow downstream resume behavior to proceed.
- If stage 13 would be executed and an overlay exists, fail fast before mutating any stage
  13 output.
- The error message must explain that rebuilding stage 13 can change `claim_id`s and that
  fresh extraction requires manual overlay discard/re-review by moving/removing the overlay
  artifacts first.
- Do not add a CLI flag for discard or force-rebuild behavior.

Acceptance checks:

- An overlay job whose stage 13 would be re-run fails before changing
  `claims/style_claims.jsonl`, `claims/style_claims_current.jsonl`,
  `claims/style_claims_manual_edits.jsonl`, or `claims/style_claims_raw.json`.
- The failure message names the overlay files and the reason (`claim_id` stability).
- A job without an overlay can still re-run stage 13 when normal freshness validation
  requires it.
- A job with an overlay can still resume downstream stages when stage 13 remains current
  and is skipped.

### 5. Harden stage 15 Obsidian freshness and cleanup

Files:

- `src/style_kb/stages/stage_15_export_obsidian.py`
- `src/style_kb/export/obsidian.py` if needed
- `docs/stages/15_export_obsidian.md`

Changes:

- In `validate_outputs`, require exact note identity:
  - expected video note exists;
  - expected index exists;
  - every current chunk has one note;
  - no extra `exports/obsidian/chunks/*.md` files exist for stale chunk ids.
- Require notes to be current relative to:
  - metadata;
  - timeline events;
  - chunks;
  - effective claims;
  - frame refs when visuals are enabled.
- In `run()`, remove only stage-owned stale chunk notes for chunk ids no longer present in `chunks/chunks.jsonl`.
- Do not delete unrelated user files outside the stage-owned `exports/obsidian/chunks/{chunk_id}.md` pattern.
- Consider adding `exports/obsidian/manifest.json` only if mtime/content checks become too brittle. Do not add it unless needed.

Acceptance checks:

- `KFDLiQE3V98` stale notes are detected.
- After stage 15 rebuild, note count equals canonical chunk count and stale chunk note files are gone.
- Notes render effective current claims when `style_claims_current.jsonl` exists.

### 6. Harden stage 16 quality-report freshness

Files:

- `src/style_kb/stages/stage_16_quality_report.py`
- `docs/stages/16_quality_report.md`

Changes:

- In `validate_outputs`, verify:
  - report parses as `QualityReport`;
  - report `job_id` and `video_id` match current job;
  - report stage counts match current canonical artifact counts;
  - `style_claims` count uses effective claims, not original claims;
  - report is current relative to `timeline/media_durations.json`, chunks, and effective claims.
- Keep warnings diagnostic; do not hard-fail completed jobs merely because style claims were manually reduced.
- Include a metric such as `effective_style_claims_count` if useful, but keep `QualityReport.metrics` integer-only.

Acceptance checks:

- `K4jQPK2GmOg` report count `51` vs current `45` is detected.
- `qxXRWoSYf7I` report count `60` vs current `55` is detected.
- After stage 16 rebuild, counts match effective claims.

### 7. Improve maintenance audit drift detection

Files:

- `src/style_kb/maintenance/audit_jobs.py`
- `docs/maintenance.md`

Changes:

- Stop scanning full `combined_text` as chunk presentation evidence. Use the same visual-component logic as stage 16: `presenter_brief + visual_text`.
- Add drift checks:
  - `style_claims_current.jsonl` newer than JSONL export;
  - JSONL export content differs from effective claims;
  - quality report claim count differs from effective claims count;
  - quality report older than effective claims;
  - Obsidian video note older than effective claims;
  - Obsidian chunk notes missing or extra relative to canonical chunks.
- Add concise payload fields:
  - `claim_export_drifts`;
  - `quality_report_drifts`;
  - `obsidian_drifts`;
  - `dashboard_overlay_jobs`.
- Keep resolved failure-history behavior unchanged.

Acceptance checks:

- Before fixes, audit identifies the known drifts listed in this plan.
- After rebuild, audit no longer reports those drifts.
- Audio-only speech text no longer appears in `top_markers.presentation_chunks`.

### 8. Add a domain-term guard for style claims

Files:

- `src/style_kb/stages/stage_13_extract_style_claims.py`
- `src/style_kb/prompts/style_claims_ru.txt`
- `src/style_kb/prompts/style_claims_retry_advisor_ru.txt`
- `docs/stages/13_extract_style_claims.md`
- optional new module: `src/style_kb/clients/domain_terms.py` or `src/style_kb/stages/domain_terms.py`

Initial domain-term table:

| Bad / risky form | Canonical form | Notes |
| --- | --- | --- |
| `слитки` as socks/accessory term | `следки` | Known STT error for no-show socks. |
| `слагсы` | `слаксы` or `slacks` | Normalize user-facing style terminology. |
| `серсакер` | `seersucker` plus Russian alias where useful | Dashboard edits already corrected this manually in one job. |
| `тенсил` | `тенсел` / `Tencel` | Keep brand/common spelling consistent. |

Implementation approach:

1. Add prompt glossary so the model avoids obvious term drift.
2. Add deterministic validation for known bad terms in user-facing claim fields:
   - `subject`;
   - `claim`;
   - `rationale`;
   - `conditions`;
   - `applies_to`;
   - `avoid`;
   - `prefer`;
   - `topics`.
3. If a bad term appears in an accepted provider response:
   - retry with advisor instructions when retry budget remains;
   - otherwise fail stage 13 rather than accepting a high-confidence false term.
4. Apply deterministic cleanup only for exact, low-risk replacements after validation proves context:
   - for example, `слитки` -> `следки` only when the field also concerns socks/no-show socks.

Do not rewrite raw speech tokens, raw transcript, or original provider attempt files.

Acceptance checks:

- A fixture with `Слитки — это невидимые носки` cannot be accepted as final claims.
- Corrected output uses `следки` in user-facing claim fields.
- Cleanup/retry metrics show the correction path.

### 9. Measure stage 07 retry noise before changing behavior

Files:

- `src/style_kb/stages/stage_07_build_speech_segments.py`
- `src/style_kb/prompts/speech_segments_semantic_ru.txt`
- `src/style_kb/prompts/speech_segments_retry_advisor_ru.txt`
- `docs/stages/07_build_speech_segments.md`

Initial action:

- Add or extend a read-only diagnostics summary for stage 07 retry causes:
  - words exceeded;
  - duration exceeded;
  - mixed speakers;
  - semantic density;
  - provider/network errors.
- Do not weaken deterministic validation.
- Do not add deterministic boundary repair until the measured baseline justifies it.

Possible prompt-only improvements:

- Emphasize not returning near-limit 145-150 word ranges when sentence count is high.
- Emphasize preserving successful local splits across retries.
- Include exact failed range ids and "do not rejoin" instructions in a compact way.

Acceptance checks:

- Re-running representative jobs reduces stage 07 failed attempts without increasing max segment duration/word count.
- No segment crosses speaker boundaries.
- No accepted segment violates semantic-density validation.

### 10. Validate multimodal behavior separately

This plan fixes artifacts discovered from audio-only jobs. It does not prove stages 08-10 are good.

Validation task after freshness fixes:

- Temporarily enable `pipeline.visual_enabled=true` in a local test branch or controlled run.
- Ingest at least one short representative job and one visually rich job.
- Measure:
  - scene count;
  - frame count;
  - frames per scene;
  - duplicate frames skipped;
  - stage 10 provider requests;
  - materialized visual events;
  - presentation leakage metrics;
  - KB chunks with visual evidence.

Do not add a CLI switch for this. Use config changes only in a controlled validation branch and revert or document them before merging if the default must remain audio-only.

### 11. Measure and reduce chunk-text redundancy

Files:

- `src/style_kb/stages/stage_12_build_chunks.py`
- `src/style_kb/export/jsonl.py` (only if a compact/derived field is added)
- `docs/stages/12_build_chunks.md`

Changes:

- Add a read-only diagnostic that measures redundancy in `combined_text`: the fraction of
  `combined_text` tokens contributed by phrase lists already present in `visual_text` /
  `topics` / `entities`, per chunk and per job.
- Only after measuring, decide whether to deduplicate phrase lists inside `combined_text`
  (speech + `presenter_brief` + a deduplicated visual phrase set). Keep `speech_text`,
  `visual_text`, and `presenter_brief` stored separately and unchanged.
- Do not change chunk boundaries or chunk ids.

Acceptance checks:

- A per-job redundancy metric exists before any text change.
- If dedup is applied, `combined_text` token count drops while `speech_text` /
  `visual_text` / `presenter_brief` and chunk ids stay stable.

### 12. One-time backfill: refresh existing JSONL/RAG, manifest, and Obsidian surfaces from effective claims

Most existing jobs already carry manual claim edits in `style_claims_current.jsonl` that
never reached the materialized exports (for example `auxaGqHkFXE`: overlay has 28 claims but
both `exports/jsonl/style_claims.jsonl` and the Obsidian vault still show 30, including the
two deleted claims 000020/000021). A one-time maintenance script reconciles these jobs
without re-running the pipeline. It depends on the export/render code from sections 4 and 5
and should run after they land.

Files:

- new `src/style_kb/maintenance/refresh_kb_exports.py`
- reuse `src/style_kb/stages/common.py` (`effective_style_claims_path`),
  the shared claim-surface refresh helper from section 2a,
  `src/style_kb/export/jsonl.py`, and `src/style_kb/export/obsidian.py`
- `docs/maintenance.md`

Behavior:

- Iterate every job under the output root, the same way `audit_jobs` does, with no product
  CLI flags (mirror the `audit_jobs` invocation style).
- Skip jobs that are not completed or that have an active pipeline/job lock; report them as
  skipped instead of racing `resume`.
- Compute effective claims per job: `style_claims_current.jsonl` when present, else
  `style_claims.jsonl`.
- Export-only, never claim-regenerating: the script must not run stage 13 and must not
  renumber `claim_id`s. It only re-materializes claim-derived exports from the effective
  claims already on disk. `claims/style_claims.jsonl` and `claims/style_claims_raw.json` are
  never touched.
- For each job, gated on the export already existing (do not create exports the pipeline
  never produced):
  - refresh `exports/jsonl/style_claims.jsonl` and `exports/jsonl/manifest.json` to exactly
    match effective claims, using the same shared writer as the dashboard and stage 14;
  - re-render the Obsidian vault from effective claims via the stage 15 render path,
    including the stale chunk-note cleanup from section 5.
- Do not refresh `quality_report.json`; report it as stale (explicit pipeline
  rebuild/stage 16 owns it).
- Idempotent: a second run over already-reconciled jobs rewrites nothing.
- Print a per-job summary: JSONL/manifest refreshed, Obsidian refreshed, stale notes
  removed, skipped (no export yet, incomplete job, or active lock), and quality reports left
  stale.

Invocation:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m style_kb.maintenance.refresh_kb_exports
```

Acceptance checks:

- After the run, every job with an overlay has `exports/jsonl/style_claims.jsonl` equal to
  its effective claims, a matching `exports/jsonl/manifest.json`, and an Obsidian vault
  with no deleted/edited-away claim text.
- `auxaGqHkFXE` JSONL export count is 28 and its Obsidian notes no longer contain claims
  000020/000021.
- `KFDLiQE3V98` has exactly 9 Obsidian chunk notes after the run.
- A second run is a no-op (no files rewritten).
- The script never changes `claims/style_claims.jsonl`, `claims/style_claims_raw.json`, or
  any `claim_id`.

## Deferred to a later phase: cross-job / global KB consolidation

The within-job work above keeps each job clean. The larger quality lever is a global,
deduplicated KB across all videos, which does not exist today (export is per job). This is
a genuine new feature and conflicts with this plan's "no new features" goal, so it is
deferred. When designed, it should:

- consolidate recurring claims across jobs into canonical KB entries with per-video
  provenance (each canonical claim links back to its source claims and timestamps);
- reuse the curate LLM approach, not embeddings, to stay within the dependency non-goal;
- surface cross-video contradictions (for example, different recommended hem widths from
  different presenters) instead of silently picking one;
- treat per-job exports as the source records and the global KB as a derived,
  regeneratable view.

## Documentation Updates Required

Update docs in the same implementation change as code:

- `docs/dashboard.md`
  - Clarify that dashboard edits are terminal post-pipeline operations allowed only for
    completed, unlocked jobs.
  - Clarify which artifacts dashboard edits refresh immediately.
  - Document stale derived artifact indicators.
- `docs/stages/14_export_jsonl.md`
  - Document effective-claims fingerprint/content validation and manifest refresh rules.
- `docs/stages/15_export_obsidian.md`
  - Document stale chunk note cleanup and exact expected note set.
- `docs/stages/16_quality_report.md`
  - Document effective-claims count and report freshness validation.
- `docs/maintenance.md`
  - Document new audit drift categories.
  - Document the `refresh_kb_exports` backfill: what it reconciles (JSONL/RAG +
    manifest + Obsidian), that it is export-only, skips incomplete/locked jobs, never
    touches claims or `claim_id`s, and leaves quality reports for explicit pipeline
    rebuilds.
- `docs/stages/13_extract_style_claims.md`
  - Document domain-term guard behavior and retry/fail policy.
  - Document the resume guard that refuses stage 13 rebuilds when dashboard overlays exist.
- `docs/stages/12_build_chunks.md`
  - Document the `combined_text` redundancy metric and any dedup behavior.

## Rollout Order

1. Add regression tests and tiny fixtures for the known drift cases.
2. Implement shared effective-claims helpers and the shared claim-surface refresh service.
3. Add the resume overlay guard before any work that may rebuild stage 13 on existing jobs.
4. Fix stage 14 and dashboard JSONL/manifest + Obsidian export refresh.
5. Fix stage 15 stale note validation/cleanup.
6. Fix stage 16 report validation.
7. Fix audit false positives and add drift checks.
8. Add domain-term guard for stage 13.
9. Run audit before/after on existing local jobs.
10. Reconcile existing completed/unlocked jobs: run the KB export backfill (section 12) to
   fix claim-edit drift in JSONL/RAG + manifest + Obsidian without resume; use
   `style-kb resume JOB_ID` only before dashboard edits and only where a true pipeline
   rebuild (chunks/timeline/quality) is needed.
11. Run a separate visual-enabled validation pass.
12. Measure chunk-text redundancy; apply `combined_text` dedup only if the metric justifies it.
13. Re-run audit; compare redundancy metrics before/after.

## Verification Checklist

Run from repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m pytest
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m style_kb.maintenance.refresh_kb_exports
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m style_kb.maintenance.audit_jobs
```

Manual artifact checks:

- For every job with `claims/style_claims_current.jsonl`, `exports/jsonl/style_claims.jsonl` matches it exactly after dashboard edit or stage 14 rebuild.
- For every job with `claims/style_claims_current.jsonl`, `exports/jsonl/manifest.json`
  records the effective claim source/count/fingerprint after dashboard edit, stage 14
  rebuild, or backfill.
- Dashboard/backfill manifest updates preserve unrelated manifest fields.
- `style-kb resume JOB_ID` refuses to re-run stage 13 when overlay files exist and leaves
  all claim artifacts unchanged.
- Dashboard edits are refused for incomplete jobs and jobs with an active pipeline/job lock.
- After running `refresh_kb_exports`, every completed/unlocked overlay job's RAG/manifest
  and Obsidian exports match effective claims, a second run rewrites nothing, skipped
  locked/running jobs are reported, and `claims/style_claims.jsonl` / `claim_id`s are
  unchanged.
- `KFDLiQE3V98` has 9 canonical chunks and 9 Obsidian chunk notes after stage 15 rebuild.
- `K4jQPK2GmOg` quality report style-claim count is 45 after stage 16 rebuild.
- `qxXRWoSYf7I` quality report style-claim count is 55 after stage 16 rebuild.
- `auxaGqHkFXE` JSONL export style-claim count is 28 after dashboard refresh or stage 14 rebuild, and its Obsidian notes no longer show the two deleted claims (000020, 000021) after a dashboard edit or stage 15 rebuild.
- No final claim contains `слитки` as the no-show sock term.
- Audio-only jobs still have no visual export files and no placeholder visual events.
- A per-job `combined_text` redundancy metric is recorded; if dedup is applied, token count drops with stable chunk ids and unchanged `speech_text` / `visual_text` / `presenter_brief`.

## Risks

- Comparing parsed JSON rows instead of bytes may hide formatting differences, but that is acceptable for semantic export freshness.
- Dashboard immediate export refresh (JSONL or Obsidian) can fail if an existing export file is corrupt or unwritable; failures should surface in the API response instead of silently leaving stale output, and the overlay write must remain the source of truth.
- Dashboard edits are designed as terminal post-pipeline operations, not concurrent
  `resume` operations. The implementation enforces this with check-then-act guards
  (completed/locked checks), not by acquiring the pipeline job lock from dashboard/backfill.
  This is proportionate for a local single-user tool but leaves a small TOCTOU window if a
  `resume` starts immediately after the check.
- Refreshing Obsidian from the dashboard makes it a post-pipeline writer of stage 15
  outputs. It must reuse the exact stage 15 render path (not a divergent dashboard-only
  renderer); otherwise dashboard and resume can produce different Markdown.
- Removing stale Obsidian notes must be scoped tightly to stage-owned chunk-note filenames.
- Domain-term cleanup can overcorrect if implemented as a broad regex. Keep corrections contextual and small.
- Quality report validation may become expensive if it fully rematerializes all counts on every resume. Start with count/fingerprint checks and only deepen if needed.
- Over-aggressive `combined_text` dedup can drop retrieval signal. Measure first and keep the separate text fields intact.
- The `refresh_kb_exports` backfill must stay export-only. If it ever re-runs stage 13 or
  renumbers claims it would orphan overlays. Keep it reading effective claims and writing
  only `exports/jsonl/style_claims.jsonl`, `exports/jsonl/manifest.json`, and the Obsidian
  vault, gated on those surfaces already existing.
- Re-running stage 13 renumbers `claim_id`s (`_renumber_claims`). Because dashboard edits
  are terminal post-pipeline edits, a later `resume` that would re-run stage 13 on a job
  with an overlay is outside the normal workflow and must be refused by the resume guard.
  For the 5 existing overlay jobs (`auxaGqHkFXE`, `K4jQPK2GmOg`, `qxXRWoSYf7I`,
  `sNZTDcdoZWY`, `WWopy43NzKU`), the guard prevents accidental rebuild; intentional fresh
  extraction requires manual overlay discard/re-review first. The export backfill
  (section 12) avoids this because it never re-runs stage 13.

## Done Definition

The work is done when:

- known drift cases are covered by tests;
- `resume` refuses to re-run stage 13 when dashboard overlay files exist;
- stage 14/15/16 skip validation rejects stale outputs;
- dashboard claim edits are allowed only for completed/unlocked jobs and cannot leave stale
  JSONL/RAG, JSONL manifest, or human-facing Obsidian exports;
- the `refresh_kb_exports` backfill has reconciled existing completed/unlocked overlay jobs
  so their RAG/manifest and Obsidian exports match effective claims, without touching
  claims or `claim_id`s;
- audit reports real current drift and stops reporting audio transcript text as presentation chunk noise;
- known bad domain term `слитки` cannot reach final claims as a high-confidence no-show sock definition;
- docs reflect the implemented behavior;
- existing audio-only jobs can be audited without false failure status, and non-overlay
  jobs can still be resumed normally;
- chunk-text redundancy is measured and not worsened.

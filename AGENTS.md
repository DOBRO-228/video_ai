# style-kb Agent Guide

This file is for AI agents working on this repository.

## Project Contract

`style-kb` is a CLI-first Python application that ingests exactly one YouTube video URL into a local multimodal knowledge base.

The public ingest command is fixed:

```bash
style-kb ingest "https://www.youtube.com/watch?v=VIDEO_ID"
```

Do not add CLI options for config, output paths, providers, models, quality, formats, cleanup, chunking, language, or partial execution. Runtime product settings come from `src/style_kb/config/default.yaml`.

Allowed service commands are:

```bash
style-kb status JOB_ID
style-kb resume JOB_ID
```

## Before Editing

Read the relevant stage document in `docs/stages/` before changing stage code. The ordered pipeline is defined in `src/style_kb/pipeline/catalog.py`.

Important files:

- `src/style_kb/config/default.yaml`: single source of truth for product settings.
- `src/style_kb/models.py`: Pydantic data contracts for artifacts.
- `src/style_kb/pipeline/base.py`: stage interface and freshness checks.
- `src/style_kb/pipeline/runner.py`: orchestration, SQLite state, locks, progress output.
- `src/style_kb/pipeline/paths.py`: canonical artifact paths.
- `docs/stages/`: agent-facing stage behavior notes.
- `src/style_kb/maintenance/audit_jobs.py`: read-only diagnostics/audit snapshot tool.

## Stage Rules

Each stage must stay:

- Idempotent.
- Resumable.
- Explicit about input and output files.
- Validated before skip.
- Compatible with SQLite job/stage state.
- Safe for atomic file writes.

If a stage writes JSON, JSONL, or Markdown, use existing atomic write helpers. Do not write partially complete final artifacts.

Stage skip must be invalidated when stage-relevant config, prompt/schema, upstream artifact identity, or durable report metadata no longer matches the current run. Do not rely only on file existence.

When adding or changing stage behavior, update the corresponding `docs/stages/*.md` file in the same change.

## Diagnostics and Reports

Use read-only diagnostics for before/after comparisons. The audit module reads `mens-style-kb/jobs.sqlite3` and job artifacts, then writes snapshots under:

```text
mens-style-kb/diagnostics/audit_YYYYMMDD_HHMMSS.json
mens-style-kb/diagnostics/latest.json
```

Audit output should distinguish:

- current final quality warnings;
- historical resolved failures;
- unresolved `failure_report.json`;
- provider/subprocess failures;
- `stage_validation_failed` retry noise;
- DB/artifact drift suspicion.

Completed jobs must not look failed because of a stale `reports/failure_report.json`. Preserve resolved failure history under `reports/failure_history/` instead of silently deleting it.

Subprocess diagnostics must use `failure_code` only for real non-zero return codes. Successful subprocess events/logs should use `status: ok` and must not carry an `error_code`.

## Data Rules

Preserve provenance on useful outputs:

- `video_id`
- title/channel/url where relevant
- start/end timestamps
- YouTube timestamp URL
- modality/source refs

`timeline_event` is the central object: speech plus visual evidence plus on-screen text plus timestamp/source grounding.

`presentation_context` on `VisualEvent` is diagnostic/quarantine data. It must not be used as style evidence in timeline/chunk/export/claim paths.

KB-facing visual and chunk fields must not contain video-format or presentation wording such as screen/slide/overlay/frame/camera/shot/background labels, positional wording, or appearance/framing wording. If useful clothing evidence is mixed with presentation framing, keep only the clothing evidence in KB fields and preserve the original framing in diagnostics/quarantine.

Speech diarization uses raw Soniox speaker ids plus repository-level roles:

- `host`: the speaker with the largest speech duration.
- `offscreen_questioner`: other detected speakers, usually questions from off camera.
- `unknown`: only for data that cannot be assigned.

Speech segmentation is LLM-assisted but deterministically validated. Retry-advisor outputs are repair instructions only; they are not accepted segmentation artifacts and cannot bypass validation. Do not add brittle deterministic boundary repair unless a new measured baseline justifies it.

## Visual Pipeline Rules

Stage 08 and 09 are the cost gate for stage 10. Changes there must be measured by scene count, frame count, frames per scene, extraction elapsed time, duplicate frames skipped, stage 10 provider request count, and materialized visual event count.

The rules below are the target contract for 08/09 hardening work. Some are not fully implemented yet; check `docs/stages/08_detect_scenes.md` and `docs/stages/09_extract_keyframes.md` for current behavior before relying on them as existing runtime behavior.

For stage 08/09 work:

- honor `scene_detection.*` config from `default.yaml`;
- keep scene/keyframe reports durable and config-aware;
- validate reports before skip;
- use bounded fallback scenes based on `fallback_scene_seconds` when no cuts are detected;
- cap frames per scene once `max_frames_per_scene` exists;
- keep OpenCV/content-aware frame selection opt-in until A/B checks pass;
- do not add CLIP/open-clip/torch, embeddings, vector stores, or heavy ML dependencies for MVP frame selection.

## Environment

API keys are loaded from `.env` into environment variables:

- `SONIOX_API_KEY`
- `GEMINI_API_KEY`
- `OPENAI_API_KEY`

Do not move API keys into YAML config. Do not print full API keys in logs or console.

## Non-Goals

Do not add Postgres, pgvector, embeddings, RAG server, web UI, user accounts, playlist/channel ingestion, scheduled jobs, manual review UI, or CLI-based provider/model selection for MVP work.

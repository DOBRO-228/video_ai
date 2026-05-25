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

## Stage Rules

Each stage must stay:

- Idempotent.
- Resumable.
- Explicit about input and output files.
- Validated before skip.
- Compatible with SQLite job/stage state.
- Safe for atomic file writes.

If a stage writes JSON, JSONL, or Markdown, use existing atomic write helpers. Do not write partially complete final artifacts.

## Data Rules

Preserve provenance on useful outputs:

- `video_id`
- title/channel/url where relevant
- start/end timestamps
- YouTube timestamp URL
- modality/source refs

`timeline_event` is the central object: speech plus visual evidence plus on-screen text plus timestamp/source grounding.

## Environment

API keys are loaded from `.env` into environment variables:

- `SONIOX_API_KEY`
- `OPENAI_API_KEY`

Do not move API keys into YAML config. Do not print full API keys in logs or console.

## Non-Goals

Do not add Postgres, pgvector, embeddings, RAG server, web UI, user accounts, playlist/channel ingestion, scheduled jobs, manual review UI, or CLI-based provider/model selection for MVP work.

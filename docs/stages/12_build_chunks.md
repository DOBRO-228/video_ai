# 12_build_chunks

## Purpose

Create deterministic multimodal chunks from timeline events for future RAG/agent ingestion.

## How It Works

The stage groups consecutive timeline events according to configured target/max words, overlap seconds, and max scenes per chunk. It concatenates speech text, presenter brief, visual summaries, and on-screen text without LLM summarization.

Presenter baseline enters chunk text only through `presenter_brief`, and only when timeline event relevance is `brief` or `primary_example`.

## Inputs

- `timeline/timeline_events.jsonl`
- `chunking.*` config values

## Outputs

- `chunks/chunks.jsonl`

## Skip Validation

The stage can be skipped when `chunks.jsonl` exists and parses to at least one `Chunk`.

## Important Notes

- Do not add semantic LLM chunking in MVP.
- Chunks must preserve source refs and YouTube timestamp URLs.
- Keep chunk text deterministic so exports and validation remain reproducible.

## Related Code

- `src/style_kb/stages/stage_12_build_chunks.py`
- `src/style_kb/models.py::Chunk`


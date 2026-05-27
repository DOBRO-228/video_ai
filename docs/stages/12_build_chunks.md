# 12_build_chunks

## Purpose

Create deterministic multimodal chunks from timeline events for future RAG/agent ingestion.

## How It Works

The stage groups consecutive timeline events according to configured target/max words, overlap seconds, and max scenes per chunk. It concatenates speech/dialogue text, presenter brief, visual summaries, and on-screen text without LLM summarization.

Presenter baseline enters chunk text only through `presenter_brief`, and only when timeline event relevance is `brief` or `primary_example`.

When timeline events contain `speech_turns`, chunks also include `dialogue_text` with labels such as `Ведущий:` and `Закадровый вопрос:`.

Scene overlap is discrete: a next chunk overlaps the previous chunk only with the previous last timeline event, and only when that event alone is long enough to cover configured `overlap_seconds`. This avoids excessive sliding-window duplication on videos with many short scenes.

## Inputs

- `timeline/timeline_events.jsonl`
- `chunking.*` config values

## Outputs

- `chunks/chunks.jsonl`

## Skip Validation

The stage can be skipped when `chunks.jsonl` exists and matches the deterministic rebuild from current timeline events and chunking config.

## Important Notes

- Do not add semantic LLM chunking in MVP.
- Chunks must preserve source refs and YouTube timestamp URLs.
- Keep chunk text deterministic so exports and validation remain reproducible.
- `speech_text` remains plain transcript; `dialogue_text` is speaker-aware.

## Related Code

- `src/style_kb/stages/stage_12_build_chunks.py`
- `src/style_kb/models.py::Chunk`

# 12_build_chunks

## Purpose

Create RAG-oriented multimodal chunks from semantic speech segments plus timeline visual evidence.

## How It Works

The stage uses an OpenAI structured-output chunk planner. The model receives compact multimodal planning input:

- semantic `SpeechSegment` objects as the primary units;
- overlapping `TimelineEvent` ids;
- compact visual hints such as visual summary, on-screen text, topics, items, and presenter relevance.

The model returns only a chunk plan: contiguous `speech_segment_ids`, a short title, boundary reason, topics, and notes. Python then validates the plan and materializes final `Chunk` objects deterministically.

The LLM is not allowed to generate final chunk text. Final `speech_text`, `dialogue_text`, `visual_text`, `combined_text`, `timeline_event_ids`, timestamps, and `source_refs` are built by code.

Planner windows are QA-aware: a window boundary is shifted when it would split an `offscreen_questioner` segment from a nearby `host` answer. After all window plans are returned, Python also performs a deterministic safety merge for any remaining adjacent QA split, as long as hard chunk limits are still respected. If the adjacent chunks cannot be merged without violating `max_speech_segments_per_chunk` or `max_words`, the split is kept as a warning instead of failing the stage.

Each planner window has a raw cache keyed by prompt/config/window/input fingerprint. Cache-hit windows do not call OpenAI. Cache-miss windows are sent in parallel up to `chunking.planner_parallel_requests`. Stale raw cache/attempt files for window indexes beyond the current window count are removed at the start of planning.

## Inputs

- `metadata/video_info.json`
- `stt/speech_segments.jsonl`
- `timeline/timeline_events.jsonl`
- `src/style_kb/prompts/chunk_plan_ru.txt`
- `chunking.*` config values

## Outputs

- `chunks/chunks.jsonl`
- `chunks/chunk_plan.json`
- `chunks/chunk_plan_errors.json` when planner validation fails
- `chunks/chunk_plan_warnings.json` when QA splits are preserved because hard chunk limits prevent merging
- `chunks/raw/chunk_plan_window_XXX_attempt_YY.json` as diagnostic raw attempts, not canonical stage outputs
- `chunks/raw/chunk_plan_window_XXX_cache.json` as per-window planner cache, not canonical stage output

## Skip Validation

The stage can be skipped only when `chunk_plan.json` matches current config/prompt, covers every speech segment exactly once, respects ordering and hard limits, passes question-answer boundary validation, has valid planner window metadata, has current warning artifacts when unmergeable QA splits exist, and `chunks.jsonl` equals deterministic materialization from that plan.

## Important Notes

- No embeddings are used in MVP chunking.
- There is no fallback LLM planner. Python still applies deterministic QA repair and allowed QA-limit warnings after planner output; if the repaired plan remains invalid after configured retries, the stage fails.
- Planner retries are reserved for malformed/recoverable planner outputs and Python validation feedback. Unknown provider errors fail immediately after the OpenAI client-level retry policy is exhausted.
- Parallel window execution aborts on the first fatal window failure, cancels queued windows, logs the abort, and waits only for already in-flight windows to finish.
- Planner metadata limits such as title length, boundary reason length, notes length, topic length, and topic count come from `chunking.*` config values.
- Skip validation materializes expected chunks once and compares that deterministic result with `chunks.jsonl`.
- Chunk time ranges are based on speech segment ranges, not full visual scene boundaries.
- Visual evidence is attached by overlap, or by nearby distance within `visual_attach_seconds` when needed.
- Offscreen questions should stay with nearby host answers when they form one QA exchange.
- A QA split that cannot be merged without exceeding hard chunk limits is allowed only with a warning in `chunk_plan_warnings.json`, chunk plan notes, stage log, and console progress.
- `title`, `channel`, and canonical video URL come from `metadata/video_info.json`, not from a best-effort timeline fallback.
- Chunks preserve source refs and YouTube timestamp URLs.
- `chunks/raw/*` files are diagnostic/cache artifacts. They are retained for reuse and post-mortem analysis, but must not be imported into a future KB or read as style knowledge by agents.

## Related Code

- `src/style_kb/stages/stage_12_build_chunks.py`
- `src/style_kb/clients/openai_chunk_planner.py`
- `src/style_kb/models.py::Chunk`
- `src/style_kb/models.py::ChunkPlan`

# 13_extract_style_claims

## Purpose

Extract canonical structured style knowledge from deterministic chunks. This stage turns transcript and visual context into `StyleClaim` records that can be indexed or exported independently of presentation notes.

## How It Works

The stage sends one chunk at a time to OpenAI structured output using `style_claims_ru.txt`. Each response may contain `0..style_claims.max_claims_per_chunk` claims. Raw responses are cached per chunk under `claims/raw/` with a request fingerprint that includes the chunk payload, prompt hash, model, retry count, and claim settings.

Provider output is validated before it can become a cache hit or a final artifact. If a chunk response contains service metadata as style knowledge, technical identifiers in user-facing fields, invalid enums, missing required text, malformed JSON, or too many claims, the stage writes the attempt raw output to `claims/raw/{chunk_id}_attempt_{attempt}.json`, logs the validation error, records it in `claims/style_claims_errors.json`, emits progress, and retries up to `style_claims.max_retries`.

After all chunk responses are loaded, the stage performs deterministic exact deduplication by `(claim_type, subject, claim)`. Duplicate claims keep the earliest primary chunk and merge timeline/source/evidence/topic provenance.

## Inputs

- `timeline/timeline_events.jsonl`
- `chunks/chunks.jsonl`
- `src/style_kb/prompts/style_claims_ru.txt`
- `style_claims.*` config values
- `OPENAI_API_KEY`

## Outputs

- `claims/style_claims.jsonl`
- `claims/style_claims_raw.json`
- `claims/raw/{chunk_id}.json`
- `claims/raw/{chunk_id}_attempt_{attempt}.json` for regenerated API attempts
- `claims/style_claims_errors.json` while invalid attempts or a final retry failure exist

## Skip Validation

The stage can be skipped when `style_claims.jsonl` parses as `StyleClaim`, `style_claims_raw.json` matches the current provider/model/prompt/max-claims/max-retries config, claim ids are deterministic, each claim is grounded to known chunks/timeline events, no exact duplicate claims remain, and no user-facing claim field leaks service metadata or technical ids.

## Important Notes

- Empty claim output is allowed for non-educational chunks, but quality report warns when the whole video has no claims or most chunks have none.
- Do not silently convert invalid provider output to zero claims. Invalid chunk responses must retry/regenerate with logs and diagnostic artifacts, then fail the stage if all attempts remain invalid.
- Canonical per-chunk raw cache files use schema version 2. Older canonical raw cache files are deleted automatically at the start of stage 13; attempt files are left intact and new retries use the next available attempt number.
- Python attaches `chunk_id`, `timeline_event_ids`, `timestamp_url`, and `source_refs` to accepted claims. The model must not output those as claim content.
- Claims must remain normalized Russian knowledge objects with timestamp/source grounding.
- Do not add CLI controls for claim extraction.

## Related Code

- `src/style_kb/stages/stage_13_extract_style_claims.py`
- `src/style_kb/clients/openai_claims.py`
- `src/style_kb/prompts/style_claims_ru.txt`
- `src/style_kb/models.py::StyleClaim`

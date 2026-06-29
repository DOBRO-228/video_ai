# 13_extract_style_claims

## Purpose

Extract canonical structured style knowledge from deterministic chunks. This stage turns transcript and visual context into `StyleClaim` records that can be indexed or exported independently of presentation notes.

## How It Works

The stage sends chunks to OpenAI structured output using `style_claims_ru.txt`. Each response may contain `0..style_claims.max_claims_per_chunk` claims. Raw responses are cached per chunk under `claims/raw/` with a request fingerprint that includes the chunk payload, prompt hash, model, retry count, prompt-cache settings, and claim settings.

The extraction prompt requires the canonical `claim` text to preserve the source's modal force.
Hard bans, obligations, exclusive conditions, and absolute statements must not be softened in
the main claim text, even when `avoid`, `prefer`, or `evidence` also carry that information.
For example, source wording such as `нельзя`, `запрещено`, `обязательно`, `должен`,
`только`, or `никогда` should not become weaker `не рекомендуется`, `лучше`, `стоит`, or
`можно` phrasing unless the source itself is that soft.

By default, cache-miss chunks are requested synchronously. When the current CLI run uses `--batch`, first-attempt cache-miss extraction requests are submitted through the OpenAI Batch API with endpoint `/v1/responses`. The batch input, manifest, output, and error files are durable artifacts under `claims/raw/`. If a batch response is missing, has an API error, or fails deterministic claim validation, that raw response is preserved as an attempt artifact and the chunk falls back to the existing synchronous retry-advisor/retry flow. Batch is a transport/cost mode only; it is not part of semantic skip validation.

Provider output is validated before it can become a cache hit or a final artifact. If a chunk response contains service metadata as style knowledge, technical identifiers in user-facing fields, invalid enums, missing required text, malformed JSON, or too many claims, the stage keeps the attempt raw output at `claims/raw/{chunk_id}_attempt_{attempt}.json`, logs the validation error, records it in `claims/style_claims_errors.json`, emits progress, and retries up to `style_claims.max_retries`. A first successful attempt is written to the canonical per-chunk raw cache only; an identical successful attempt file is removed to avoid duplicate raw artifacts.

When a per-chunk extraction attempt fails validation and another retry remains, the stage sends the candidate response, chunk payload, constraints, and structured validation errors to the retry-advisor model configured in `style_claims.retry_advisor_model`. The advisor returns only repair instructions, not replacement claims. Those instructions are added to the next retry prompt for the main extraction model. Final acceptance remains deterministic.

After all chunk responses are loaded, the stage performs deterministic cleanup of obvious presentation artifacts in user-facing fields, including evidence source prefixes such as `В chunk:`/`On-screen:` and Latin homoglyphs inside Cyrillic words. It does not enforce a closed `applies_to` taxonomy; the summary records observed `applies_to` counts so a stable vocabulary can be chosen later from real jobs.

The cleanup/validation path also guards a small domain-term table. Known low-risk terms are
normalized in user-facing claim fields, for example `слагсы` -> `слаксы`, `серсакер` ->
`seersucker`, and `тенсил` -> `тенсел`. The known STT error `слитки` is allowed to be
repaired to `следки` only in a no-show-sock context; otherwise the provider response fails
validation instead of becoming a high-confidence style claim.

The stage then performs deterministic exact deduplication by `(claim_type, subject, claim)`. Duplicate claims keep the earliest primary chunk and merge timeline/source/evidence/topic provenance.

When `style_claims.curate.enabled` is true, a single post-deduplication curation request is sent using `style_claims_curate_ru.txt` with the configured `style_claims.curate.reasoning_effort` value. The curator is decisions-only: it may mark safe semantic duplicates, revise confidence, and flag split/rewrite/`applies_to` notes for audit. It must not generate new claims or rewrite canonical claim text. Python applies only safe decisions: valid same-`claim_type` semantic merges and confidence revisions. Split suggestions, rewrite suggestions, confidence reasons, and `applies_to` notes remain audit-only.

## Inputs

- `timeline/timeline_events.jsonl`
- `chunks/chunks.jsonl`
- `src/style_kb/prompts/style_claims_ru.txt`
- `src/style_kb/prompts/style_claims_retry_advisor_ru.txt`
- `src/style_kb/prompts/style_claims_curate_ru.txt` when curation is enabled
- `style_claims.*` config values
- `openai.prompt_cache.*` and `openai.batch.*` config values
- current run `--batch` flag
- `OPENAI_API_KEY`

## Outputs

- `claims/style_claims.jsonl`
- `claims/style_claims_raw.json`
- `claims/style_claims_curate_raw.json` when curation is enabled
- `claims/raw/{chunk_id}.json`
- `claims/raw/curate.json` when curation is enabled
- `claims/raw/{chunk_id}_attempt_{attempt}.json` for invalid or regenerated API attempts
- `claims/raw/{chunk_id}_retry_advice_attempt_{attempt}.json` for retry-advisor calls after failed attempts
- `claims/raw/batch_extract_input.jsonl` when `--batch` submits extraction requests
- `claims/raw/batch_extract_manifest.json` when `--batch` submits or resumes a batch
- `claims/raw/batch_extract_output.jsonl` when a batch output file is downloaded
- `claims/raw/batch_extract_errors.jsonl` when the provider returns a batch error file
- `claims/style_claims_errors.json` while invalid attempts or a final retry failure exist

## Skip Validation

The stage can be skipped when `style_claims.jsonl` parses as `StyleClaim`, `style_claims_raw.json` matches the current provider/model/prompt hash/max-claims/max-retries and curation config/reasoning effort/prompt hash, required curation audit artifacts exist when enabled, claim ids are deterministic, each claim is grounded to known chunks/timeline events, no exact duplicate claims remain, and no user-facing claim field leaks service metadata, technical ids, evidence meta-prefixes, or unnormalized guarded domain terms.

## Important Notes

- Empty claim output is allowed for non-educational chunks, but quality report warns when the whole video has no claims or most chunks have none.
- Do not silently convert invalid provider output to zero claims. Invalid chunk responses must retry/regenerate with logs and diagnostic artifacts, then fail the stage if all attempts remain invalid.
- Canonical per-chunk raw cache files use schema version 3. Older canonical raw cache files are deleted automatically at the start of stage 13; non-duplicate attempt files are left intact and new retries use the next available attempt number.
- `claims/raw/*` files are diagnostic/cache artifacts. They are retained for reuse and post-mortem analysis, but must not be imported into a future KB or read as style knowledge by agents.
- `claims/style_claims_raw.json` records counts and small relative-path samples, not a full absolute-path inventory.
- `claims/style_claims_curate_raw.json` is an audit artifact only. It records decisions, confidence reasons, split/rewrite suggestions, and ignored invalid decisions; it must not be imported as style knowledge.
- Python attaches `chunk_id`, `timeline_event_ids`, `timestamp_url`, and `source_refs` to accepted claims. The model must not output those as claim content.
- Retry-advisor outputs are diagnostic prompt guidance only. They are not claims and must not be imported into the KB.
- Claims must remain normalized Russian knowledge objects with timestamp/source grounding. Normalization may simplify wording, but it must not weaken the source's modal force in the main `claim` field.
- Dashboard overlays are terminal post-pipeline edits. If `claims/style_claims_current.jsonl`
  or a non-empty `claims/style_claims_manual_edits.jsonl` exists, `resume` must not re-run
  this stage. The runner performs an early preflight before executing upstream stages
  when the current run would reach stage 13. The refusal preserves the previous job
  status and avoids mutating stage 13 outputs because `_renumber_claims` can orphan
  dashboard edits.
- Curation intentionally does not auto-split or auto-rewrite canonical claims in the MVP. Those suggestions are collected for later analysis across multiple jobs.
- `--batch` is the only CLI execution switch for claim extraction. Do not add CLI controls for provider, model, prompt, chunking, quality, output path, or partial extraction behavior.

## Related Code

- `src/style_kb/stages/stage_13_extract_style_claims.py`
- `src/style_kb/clients/openai_claims.py`
- `src/style_kb/clients/openai_claims_curate.py`
- `src/style_kb/prompts/style_claims_ru.txt`
- `src/style_kb/models.py::StyleClaim`

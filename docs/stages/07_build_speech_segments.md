# 07_build_speech_segments

## Purpose

Convert token-level transcript output into meaning-oriented speech segments suitable for timeline construction and RAG chunks.

## How It Works

The stage builds deterministic transcript units from Soniox tokens, then sends the full transcript plus unit metadata to the OpenAI segmenter. Unit boundaries carry boundary metadata: `boundary_reason`, `can_end_segment`, and `must_end_segment`. The model returns segment ranges by unit index, but it may only end a segment at a segmentable unit boundary. The stage validates contiguity, duration, word limits, token coverage, speaker consistency, required boundaries, segmentable end boundaries, and semantic boundary quality, then writes `SpeechSegment` objects.

OpenAI requests use the configured prompt-cache settings plus `speech_segmentation.reasoning_effort` and `speech_segmentation.retry_advisor_reasoning_effort`. The stage passes a stable `prompt_cache_key` for the main segmentation prompt and retry-advisor prompt; provider-reported cached token counts remain diagnostic only and do not affect validation or skip behavior.

Units are planning atoms, not all legal segment ends. Speaker changes and transcript end are required boundaries. Sentence endings are safe segment boundaries. Pauses, strong clauses, and soft duration/word planning boundaries can be internal boundaries when ending there would split a word, leave non-terminal punctuation, or start the next segment with a continuation. Soft duration/word boundaries are only emitted at semantically safe boundaries; the old behavior of cutting exactly at `_UNIT_MAX_SECONDS` or `_UNIT_MAX_WORDS` regardless of word/phrase shape is intentionally avoided.

When a segmentation attempt fails validation, the stage records structured errors in the stage log and sends the failed ranges plus errors to the retry-advisor model configured in `speech_segmentation.retry_advisor_model`. The advisor must return only repair instructions, not replacement ranges. Those instructions are appended to the next retry prompt for the main segmenter model. Final acceptance is still deterministic: advisor output cannot bypass stage validation.

Retry prompts include the previous compact candidate plan with segment index, unit range, duration, word count, and sentence count. This lets the main model apply local boundary repairs while still returning the full segmentation JSON.

The base prompt is organized as a compact contract rather than a running list of historical fixes: hard constraints, decision policy, boundary-quality rules, and retry protocol. It requires a silent preflight before returning JSON: every proposed range must be checked against summed unit word counts, duration, speaker boundaries, `can_end_segment`, `must_end_segment`, and semantic-density thresholds. If a range exceeds `max_segment_words`, `max_segment_seconds`, or semantic-density thresholds, the model is instructed to add an internal valid boundary rather than preserve a single thematically broad segment. Retry prompts treat previously failed exact ranges as forbidden to repeat unchanged.

Semantic-density validation is explicit in both the request constraints and retry prompts. A segment fails semantic-boundary validation when it simultaneously exceeds the preferred duration threshold and reaches the preferred word and sentence thresholds. Raw attempt validation reports semantic-density failures even when the same attempt also has hard-limit or speaker-boundary errors, so retry guidance sees all currently invalid ranges instead of discovering dense ranges only after hard-limit issues are fixed. Retry feedback includes the failed unit range when available, and retry prompts forbid replacing a failed dense range with a neighboring or overlapping dense range that still violates the same thresholds.

Units are forced to break on speaker changes. The LLM is not allowed to combine different speakers into a single segment.

After LLM segmentation, consecutive short segments from the same speaker are merged when the merged segment still respects max duration and max word constraints.

Stage metrics include `semantic_retry_events_count`, `retry_advisor_used_count`, and `semantic_retry_resolved_count` for monitoring retry noise without adding deterministic boundary shifting.

The semantic prompt is hardened through general rules rather than accumulated per-case patches:

- dense ranges around 40-48 seconds with 90+ words and 6+ sentences should be split before validation;
- hard-limit failures must not be returned again as the same exact range on retry;
- semantic-density failures must not be returned again as the same exact range or shifted into an overlapping dense range on retry;
- previously valid local splits must not be regressed by later retries rejoining the range;
- boundaries must not split a recognized word or word form across units;
- a segment must not end at a unit where `can_end_segment=false`;
- a segment must not cross a unit where `must_end_segment=true`;
- a colon that introduces a quoted question/example should stay with the quoted content until that mini-construction ends;
- retry-advisor output must be a direct repair instruction for the next main-model attempt, not a replacement plan and not general advice.

## Inputs

- `stt/speech_tokens.jsonl`
- `src/style_kb/prompts/speech_segments_semantic_ru.txt`
- `src/style_kb/prompts/speech_segments_retry_advisor_ru.txt`
- `src/style_kb/config/default.yaml`, including `speech_segmentation.*` config values
- `openai.prompt_cache.*` config values
- `OPENAI_API_KEY`

## Outputs

- `stt/speech_segments.jsonl`
- `stt/speech_segments_raw.json` for the accepted OpenAI segmentation response plus `_style_kb_request` metadata
- `stt/speech_segments_raw_attempt_XX.json` for each OpenAI segmentation attempt plus `_style_kb_request` metadata
- `stt/speech_segments_retry_advice_attempt_XX.json` for retry-advisor calls after failed attempts

## Skip Validation

The stage can be skipped when raw segmentation output exists, the accepted response matches the configured main segmentation reasoning effort, all speech segments parse, cover every token exactly once, align with the current transcript-unit boundaries, respect configured limits, preserve speaker turns, do not cross required boundaries, end only at segmentable unit boundaries, and do not have semantic boundary violations.

## Important Notes

- This stage uses an LLM for semantic segmentation, not a simple pause-only algorithm.
- Source grounding must remain token-based: segment start/end come from the first and last covered token.
- Short speaker-labeled turns may be shorter than `min_segment_seconds`; speaker changes must not be merged just to satisfy a duration floor.
- Soft unit thresholds are planning hints, not permission to create word-splitting or phrase-splitting segment boundaries.
- `SpeechSegment.speaker` and `SpeechSegment.speaker_role` should represent exactly one speaker turn, not a majority vote across multiple speakers.
- Do not summarize or rewrite transcript text here.
- `stt/speech_segments_raw.json` is retained for audit/debugging the accepted segmentation output and request metadata. It is not KB input.
- Retry attempts write `stt/speech_segments_raw_attempt_XX.json` before semantic-boundary validation, so rejected LLM responses remain inspectable after a later attempt succeeds.
- Retry-advisor attempts are diagnostic prompt guidance only. They do not produce KB artifacts and must not be treated as accepted segmentation.
- Do not weaken deterministic validation to reduce retry noise. Improve prompt examples and retry instructions first; add deterministic boundary repair only after a measured pattern justifies the risk.

## Related Code

- `src/style_kb/stages/stage_07_build_speech_segments.py`
- `src/style_kb/clients/openai_segmenter.py`
- `src/style_kb/models.py::SpeechSegment`

# 07_build_speech_segments

## Purpose

Convert token-level transcript output into meaning-oriented speech segments suitable for timeline construction and RAG chunks.

## How It Works

The stage builds deterministic transcript units from Soniox tokens, then sends the full transcript plus unit metadata to the OpenAI segmenter. The model returns segment ranges by unit index. The stage validates contiguity, duration, word limits, token coverage, speaker consistency, and semantic boundary quality, then writes `SpeechSegment` objects.

When a segmentation attempt fails validation, the stage records structured errors in the stage log and sends the failed ranges plus errors to the retry-advisor model configured in `speech_segmentation.retry_advisor_model`. The advisor must return only repair instructions, not replacement ranges. Those instructions are appended to the next retry prompt for the main segmenter model. Final acceptance is still deterministic: advisor output cannot bypass stage validation.

Retry prompts include the previous compact candidate plan with segment index, unit range, duration, word count, and sentence count. This lets the main model apply local boundary repairs while still returning the full segmentation JSON.

Semantic-density validation is explicit in both the request constraints and retry prompts. A segment fails semantic-boundary validation when it simultaneously exceeds the preferred duration threshold and reaches the preferred word and sentence thresholds.

Units are forced to break on speaker changes. The LLM is not allowed to combine different speakers into a single segment.

After LLM segmentation, consecutive short segments from the same speaker are merged when the merged segment still respects max duration and max word constraints.

## Inputs

- `stt/speech_tokens.jsonl`
- `src/style_kb/prompts/speech_segments_semantic_ru.txt`
- `src/style_kb/prompts/speech_segments_retry_advisor_ru.txt`
- `speech_segmentation.*` config values
- `OPENAI_API_KEY`

## Outputs

- `stt/speech_segments.jsonl`
- `stt/speech_segments_raw.json` for the accepted OpenAI segmentation response
- `stt/speech_segments_raw_attempt_XX.json` for each OpenAI segmentation attempt
- `stt/speech_segments_retry_advice_attempt_XX.json` for retry-advisor calls after failed attempts

## Skip Validation

The stage can be skipped when raw segmentation output exists, all speech segments parse, cover every token exactly once, respect configured limits, preserve speaker turns, and do not have semantic boundary violations.

## Important Notes

- This stage uses an LLM for semantic segmentation, not a simple pause-only algorithm.
- Source grounding must remain token-based: segment start/end come from the first and last covered token.
- Short speaker-labeled turns may be shorter than `min_segment_seconds`; speaker changes must not be merged just to satisfy a duration floor.
- `SpeechSegment.speaker` and `SpeechSegment.speaker_role` should represent exactly one speaker turn, not a majority vote across multiple speakers.
- Do not summarize or rewrite transcript text here.
- `stt/speech_segments_raw.json` is retained for audit/debugging the accepted segmentation output. It is not KB input.
- Retry attempts write `stt/speech_segments_raw_attempt_XX.json` before semantic-boundary validation, so rejected LLM responses remain inspectable after a later attempt succeeds.
- Retry-advisor attempts are diagnostic prompt guidance only. They do not produce KB artifacts and must not be treated as accepted segmentation.

## Related Code

- `src/style_kb/stages/stage_07_build_speech_segments.py`
- `src/style_kb/clients/openai_segmenter.py`
- `src/style_kb/models.py::SpeechSegment`

# 07_build_speech_segments

## Purpose

Convert token-level transcript output into meaning-oriented speech segments suitable for timeline construction and RAG chunks.

## How It Works

The stage builds deterministic transcript units from Soniox tokens, then sends the full transcript plus unit metadata to the OpenAI segmenter. The model returns segment ranges by unit index. The stage validates contiguity, duration, word limits, token coverage, speaker consistency, and semantic boundary quality, then writes `SpeechSegment` objects.

Units are forced to break on speaker changes. The LLM is not allowed to combine different speakers into a single segment.

## Inputs

- `stt/speech_tokens.jsonl`
- `src/style_kb/prompts/speech_segments_semantic_ru.txt`
- `speech_segmentation.*` config values
- `OPENAI_API_KEY`

## Outputs

- `stt/speech_segments.jsonl`
- `stt/speech_segments_raw.json`

## Skip Validation

The stage can be skipped when raw segmentation output exists, all speech segments parse, cover every token exactly once, respect configured limits, preserve speaker turns, and do not have semantic boundary violations.

## Important Notes

- This stage uses an LLM for semantic segmentation, not a simple pause-only algorithm.
- Source grounding must remain token-based: segment start/end come from the first and last covered token.
- Short speaker-labeled turns may be shorter than `min_segment_seconds`; speaker changes must not be merged just to satisfy a duration floor.
- `SpeechSegment.speaker` and `SpeechSegment.speaker_role` should represent exactly one speaker turn, not a majority vote across multiple speakers.
- Do not summarize or rewrite transcript text here.

## Related Code

- `src/style_kb/stages/stage_07_build_speech_segments.py`
- `src/style_kb/clients/openai_segmenter.py`
- `src/style_kb/models.py::SpeechSegment`

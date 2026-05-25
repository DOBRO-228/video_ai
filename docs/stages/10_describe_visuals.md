# 10_describe_visuals

## Purpose

Describe visual content for every scene and produce structured `VisualEvent` objects.

## How It Works

The stage loads scenes, frame refs, and nearby speech segments. It first builds or reuses a presenter profile from representative frames when `vision.presenter_bootstrap_enabled=true`. The scene prompt is then composed from `visual_menswear_ru.txt` plus the serialized presenter profile.

Scene analysis runs through `OpenAIVisionClient.describe_scene()` with structured JSON output. Requests are parallelized with `vision.batch_size`. Raw scene responses are cached under `visual/raw/` and invalidated when prompt inputs or `presenter_profile.json` change.

## Inputs

- `frames/frame_refs.jsonl`
- `scenes/scenes.jsonl`
- `stt/speech_segments.jsonl`
- `src/style_kb/prompts/visual_menswear_ru.txt`
- `src/style_kb/prompts/presenter_profile_ru.txt`
- `OPENAI_API_KEY`

## Outputs

- `visual/presenter_profile.json`
- `visual/raw/presenter_profile.raw.json`
- `visual/raw/{scene_id}.json`
- `visual/visual_events.jsonl`

## Skip Validation

The stage can be skipped when `visual_events.jsonl` and `presenter_profile.json` exist, parse successfully, and visual event count matches scene count.

## Important Notes

- Do not add face-recognition dependencies for MVP. Presenter recurrence is detected by OpenAI vision through bootstrap profile plus scene-level classification.
- `presenter_context` is required on every `VisualEvent`.
- Recurring presenter baseline should not pollute scene-specific summary/items/colors/topics when relevance is only `background`.
- Keep structured output schemas in sync with Pydantic models.

## Related Code

- `src/style_kb/stages/stage_10_describe_visuals.py`
- `src/style_kb/clients/openai_vision.py`
- `src/style_kb/prompts/visual_menswear_ru.txt`
- `src/style_kb/prompts/presenter_profile_ru.txt`
- `src/style_kb/models.py::VisualEvent`


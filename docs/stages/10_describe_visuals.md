# 10_describe_visuals

## Purpose

Describe visual content for every scene and produce structured `VisualEvent` objects.

## How It Works

The stage loads scenes, frame refs, and nearby speech segments. It first builds or reuses a presenter profile from representative frames when `vision.presenter_bootstrap_enabled=true`. The scene prompt is then composed from `visual_menswear_ru.txt` plus the serialized presenter profile.

Scene analysis runs through `OpenAIVisionClient.describe_scene()` with structured JSON output. Requests are parallelized with `vision.batch_size`. Each request receives only the selected still frames for that scene plus a structured transcript context JSON: `previous_context`, `current_scene_context`, and `next_context`. `current_scene_context` contains speech segments that overlap the scene; adjacent context is boundary orientation only and must not be treated as visual evidence. Raw scene responses are cached under `visual/raw/` and invalidated when prompt inputs or `presenter_profile.json` change.

Scene responses are content-validated before materialization. If a recurring presenter's baseline markers leak into scene-specific fields such as `visual_summary`, `observations`, `items`, `style_topics`, or `notes`, the scene response is retried once with validation feedback. Technical presentation labels such as screen/slides/overlays/camera/shot/background labels are treated the same way, including in free-text scene fields. Cached raw scene responses with the same leakage are treated as invalid cache entries and regenerated. If the retry still leaks polluted content, the stage fails.

Before writing `VisualEvent`, the stage sanitizes visual lists so final artifacts keep only style-relevant labels. Presentation and video-format labels such as close/medium/general shots, educational format, visual presentation, camera angle, frame, screen text, slides, overlays, and background labels are removed from `items` and `style_topics`. Standalone color words are also dropped when they appear as labels without a style object. Colors are not a separate structured field; important color information should remain attached to concrete visual descriptions such as `visual_summary`, `observations`, or style-relevant topics.

## Inputs

- `frames/frame_refs.jsonl`
- `scenes/scenes.jsonl`
- `stt/speech_segments.jsonl`
- `src/style_kb/prompts/visual_menswear_ru.txt`
- `src/style_kb/prompts/presenter_profile_ru.txt`
- `OPENAI_API_KEY`

## Outputs

- `visual/presenter_profile.json`
- `visual/visual_events.jsonl`
- `visual/raw/presenter_profile.raw.json` as diagnostic/cache provider output
- `visual/raw/{scene_id}.json` as diagnostic/cache provider output

## Skip Validation

The stage can be skipped when `visual_events.jsonl` and `presenter_profile.json` exist, parse successfully, visual event count matches scene count, and final `VisualEvent` fields do not contain blocked baseline or technical presentation leakage.

## Important Notes

- Do not add face-recognition dependencies for MVP. Presenter recurrence is detected by OpenAI vision through bootstrap profile plus scene-level classification.
- `presenter_context` is required on every `VisualEvent`.
- Recurring presenter baseline should not pollute scene-specific summary/items/topics for any presenter relevance, including `primary_example`.
- `items` and `style_topics` must stay useful for menswear knowledge retrieval; technical presentation labels must not reach final timeline or chunk topics/entities.
- `visual/raw/*` files are diagnostic/cache artifacts. They are retained for reuse and post-mortem analysis, but must not be imported into a future KB or read as style knowledge by agents.
- Baseline leakage retries and failures must be logged in `logs/10_describe_visuals.log` with scene id, fields, and matched markers.
- Keep structured output schemas in sync with Pydantic models.

## Related Code

- `src/style_kb/stages/stage_10_describe_visuals.py`
- `src/style_kb/clients/openai_vision.py`
- `src/style_kb/prompts/visual_menswear_ru.txt`
- `src/style_kb/prompts/presenter_profile_ru.txt`
- `src/style_kb/models.py::VisualEvent`

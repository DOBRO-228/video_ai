# 10_describe_visuals

## Purpose

Describe visual content for every scene and produce structured `VisualEvent` objects.

## How It Works

The stage loads scenes, frame refs, and nearby speech segments. It first builds or reuses a presenter profile from representative frames when `vision.presenter_bootstrap_enabled=true`. The scene prompt is then composed from `visual_menswear_ru.txt` plus the serialized presenter profile.

Scene analysis runs through the configured vision provider client with structured JSON output. The current default is `vision.provider=openai`, `vision.model=gpt-5-nano`, and `vision.detail=high`. Gemini remains implemented and can be enabled in `default.yaml` when billing is available. There is no automatic provider fallback. A provider/API error fails the stage and the job should be resumed after fixing the environment or provider issue.

Requests are parallelized with `vision.batch_size`. Each request receives only the selected still frames for that scene plus a structured transcript context JSON: `previous_context`, `current_scene_context`, and `next_context`. `current_scene_context` contains speech segments that overlap the scene; adjacent context is boundary orientation only and must not be treated as visual evidence. Raw scene responses are cached under `visual/raw/` and invalidated when prompt inputs, `presenter_profile.json`, provider, model, media/detail settings, thinking level, or output schema change.

Scene responses are content-validated before materialization. Technical presentation labels such as screen/slides/overlays/camera/shot/background labels are retried once with validation feedback, including in free-text scene fields. If the retry still leaks polluted technical content, the raw response may be accepted for diagnostics, but KB-facing `VisualEvent` fields are deterministically scrubbed by deleting contaminated field values and moving them into `presentation_context`.

Each API scene attempt is saved under `visual/raw/{scene_id}_attempt_XX.json`. The accepted response is written to canonical `visual/raw/{scene_id}.json` for cache reuse. Cached raw scene responses with technical leakage are regenerated unless their canonical raw payload was explicitly marked as accepted with warning after retry.

Recurring presenter baseline leakage is tracked as a non-blocking quality metric. If a recurring presenter's baseline markers appear in scene-specific fields such as `visual_summary`, `observations`, `items`, `style_topics`, or `notes`, the stage records counts in metrics and `logs/10_describe_visuals.log`, but does not retry, clean, or fail the scene.

Before writing `VisualEvent`, the stage sanitizes KB-facing fields so final artifacts keep only style-relevant labels. Presentation and video-format labels such as close/medium/general shots, educational format, visual presentation, camera angle, frame, screen text, slides, overlays, background labels, positional wording, and appearance/framing wording are removed from `visual_summary`, `observations`, `interpretations`, `items`, `style_topics`, `notes`, and presenter scene deltas. Removed values are preserved in `presentation_context`, which is diagnostic/quarantine data and must not be used as style evidence. Standalone color words are also dropped when they appear as labels without a style object. Colors are not a separate structured field; important color information should remain attached to concrete visual descriptions such as `visual_summary`, `observations`, or style-relevant topics.

## Inputs

- `frames/frame_refs.jsonl`
- `scenes/scenes.jsonl`
- `stt/speech_segments.jsonl`
- `src/style_kb/prompts/visual_menswear_ru.txt`
- `src/style_kb/prompts/presenter_profile_ru.txt`
- `GEMINI_API_KEY` when `vision.provider=gemini`
- `OPENAI_API_KEY` when `vision.provider=openai`

## Outputs

- `visual/presenter_profile.json`
- `visual/visual_events.jsonl`
- `visual/raw/presenter_profile.raw.json` as diagnostic/cache provider output
- `visual/raw/{scene_id}.json` as accepted diagnostic/cache provider output
- `visual/raw/{scene_id}_attempt_XX.json` as diagnostic provider output for each scene API attempt

## Skip Validation

The stage can be skipped when `visual_events.jsonl` and `presenter_profile.json` exist, parse successfully, and visual event count matches scene count. Technical presentation leakage and baseline leakage do not block skip; they are reported through stage metrics and the quality report.

## Important Notes

- Do not add face-recognition dependencies for MVP. Presenter recurrence is detected by the configured vision provider through bootstrap profile plus scene-level classification.
- `presenter_context` is required on every `VisualEvent`.
- `presentation_context` is a diagnostic sink for non-KB video framing/background notes and must not flow into timeline, chunks, exports, or style claims as style evidence.
- Recurring presenter baseline should not pollute scene-specific summary/items/topics for any presenter relevance, including `primary_example`, but this is a quality signal rather than a stage-failing condition.
- `items` and `style_topics` must stay useful for menswear knowledge retrieval; technical presentation labels must not reach final timeline or chunk topics/entities.
- `visual/raw/*` files are diagnostic/cache artifacts. They are retained for reuse and post-mortem analysis, but must not be imported into a future KB or read as style knowledge by agents.
- Baseline leakage summary metrics must be logged in `logs/10_describe_visuals.log`; technical presentation leakage retries and accepted warnings must be logged with scene id, fields, matched markers, and raw attempt paths.
- Keep structured output schemas in sync with Pydantic models.

## Related Code

- `src/style_kb/stages/stage_10_describe_visuals.py`
- `src/style_kb/clients/openai_vision.py`
- `src/style_kb/clients/gemini_vision.py`
- `src/style_kb/prompts/visual_menswear_ru.txt`
- `src/style_kb/prompts/presenter_profile_ru.txt`
- `src/style_kb/models.py::VisualEvent`

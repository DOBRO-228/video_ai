# 10_describe_visuals

## Purpose

Describe visual content for every scene and produce structured `VisualEvent` objects.

## How It Works

When `pipeline.visual_enabled=false` (the default audio-only mode), the pipeline skips this stage by config. No presenter profile, raw vision cache, or `visual/visual_events.jsonl` is required, and downstream stages must not treat stale visual artifacts as current evidence.

The stage loads scenes, frame refs, and nearby speech segments. It first builds or reuses a presenter profile from representative frames when `vision.presenter_bootstrap_enabled=true`. The scene prompt is then composed from `visual_menswear_ru.txt` plus the serialized presenter profile.

Scene analysis runs through the configured vision provider client with structured JSON output. The current default is `vision.provider=gemini`, `vision.model=gemini-3-flash-preview`, `vision.media_resolution=high`, `vision.thinking_level=medium`, and `vision.thinking_budget=null`. OpenAI remains implemented and can be re-enabled in `default.yaml` as a rollback path. There is no automatic provider fallback. A provider/API error fails the stage and the job should be resumed after fixing the environment or provider issue.

Requests are parallelized with `vision.batch_size`, with a provider-specific cap of 2 concurrent scene requests for Gemini. Each request receives only the selected still frames for that scene plus a structured transcript context JSON: `previous_context`, `current_scene_context`, and `next_context`. `current_scene_context` contains speech segments that overlap the scene; adjacent context is boundary orientation only and must not be treated as visual evidence. Raw scene responses are cached under `visual/raw/` and invalidated when prompt inputs, `presenter_profile.json`, provider, model, media/detail settings, thinking level/budget, or output schema change.

Gemini provider requests use exponential backoff for retryable provider failures such as 429 and 5xx responses. Retries are logged to the stage log and pipeline log, and the next retry delay is printed to console progress output.

Token usage is parsed by provider and model. OpenAI vision responses use the OpenAI Responses API `usage` fields. The active Gemini default, `gemini-3-flash-preview`, uses Gemini `usage_metadata`; when Gemini omits `total_token_count`, stage metrics and console progress use the sum of known reported counts such as candidate and thinking tokens. If Gemini omits `prompt_token_count`, `input_tokens` remains `0` rather than being guessed.

Stage 08/09 changes can alter scene boundaries, frame counts, selected timestamps, and visual signatures. Such changes should intentionally invalidate stale stage 10 raw/final artifacts when request shape or prompt evidence changes.

Scene responses are content-validated before materialization. Technical presentation labels such as screen/slides/overlays/camera/shot/background labels are retried once with validation feedback, including in free-text scene fields. If the retry still leaks polluted technical content, the raw response may be accepted for diagnostics, but KB-facing `VisualEvent` fields are deterministically scrubbed by deleting contaminated field values and moving them into `presentation_context`.

Each API scene attempt is saved under `visual/raw/{scene_id}_attempt_XX.json`. The accepted response is written to canonical `visual/raw/{scene_id}.json` for cache reuse. Cached raw scene responses with technical leakage are regenerated unless their canonical raw payload was explicitly marked as accepted with warning after retry.

Recurring presenter baseline leakage is tracked as a non-blocking quality metric. If a recurring presenter's baseline markers appear in scene-specific fields such as `visual_summary`, `observations`, `items`, `style_topics`, or `notes`, the stage records counts in metrics and `logs/10_describe_visuals.log`, but does not retry, clean, or fail the scene.

Before writing `VisualEvent`, the stage sanitizes KB-facing fields so final artifacts keep only style-relevant labels. Presentation and video-format labels such as close/medium/general shots, educational format, visual presentation, camera angle, frame, screen text, slides, overlays, background labels, positional wording, and appearance/framing wording are removed from `visual_summary`, `observations`, `interpretations`, `items`, `style_topics`, `notes`, and presenter scene deltas. Removed values are preserved in `presentation_context`, which is diagnostic/quarantine data and must not be used as style evidence. Standalone color words are also dropped when they appear as labels without a style object. Colors are not a separate structured field; important color information should remain attached to concrete visual descriptions such as `visual_summary`, `observations`, or style-relevant topics.

The scene prompt should keep mixed phrases split:

- KB-facing fields receive only visible menswear/style evidence;
- `presentation_context` receives location/framing/display details such as `в кадре`, `на экране`, `вставка`, `фон`, `стол`, `полки`, `слева/справа`, `крупный план`, `overlay`, and similar wording;
- if useful clothing evidence cannot be separated from framing without adding unsupported meaning, leave the KB-facing field empty and preserve the original phrase in `presentation_context`.

## Inputs

- `frames/frame_refs.jsonl`
- `scenes/scenes.jsonl`
- `frames/frame_extraction_report.json` when stage 09 visual signatures/selection metadata are used
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
- Audio-only jobs should not create placeholder visual events; absence of this stage's artifacts is expected when `pipeline.visual_enabled=false`.
- Baseline leakage summary metrics must be logged in `logs/10_describe_visuals.log`; technical presentation leakage retries and accepted warnings must be logged with scene id, fields, matched markers, and raw attempt paths.
- Planned duplicate visual-analysis reuse for fallback/static scene windows must be conservative. Reuse only when visual signatures/frame sets are near-identical and transcript context is not materially different. Materialize separate `VisualEvent` objects for each scene, preserving each scene's own `scene_id`, time range, timestamp URL, and source refs.
- If duplicate visual-analysis reuse is added, metrics must distinguish provider requests from materialized visual events.
- Keep structured output schemas in sync with Pydantic models.

## Related Code

- `src/style_kb/stages/stage_10_describe_visuals.py`
- `src/style_kb/clients/openai_vision.py`
- `src/style_kb/clients/gemini_vision.py`
- `src/style_kb/prompts/visual_menswear_ru.txt`
- `src/style_kb/prompts/presenter_profile_ru.txt`
- `src/style_kb/models.py::VisualEvent`

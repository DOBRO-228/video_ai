# PLAN: stage 10 visual evidence hardening

Status: proposal to implement
Date: 2026-06-12
Scope: stage 10 visual prompt, cached-response sanitation, retry/quality signals, and regression validation.

## Goal

Improve `10_describe_visuals` without impoverishing the knowledge base.

The main product goal is better menswear evidence extraction from real, clearly visible examples. Noise removal is secondary and must not silently reduce useful evidence.

The immediate baseline is job `qxXRWoSYf7I`:

- 75 visual events total;
- 32 scenes currently contain `items`, so 43 scenes have no item evidence;
- `presenter_context.relevance="primary_example"` appears only once;
- at least one large real inserted outfit is missed entirely (`scene_000007`);
- several final KB-facing fields contain English or presentation wording;
- one cartoon scene produces valid-looking clothing words in `items`.

Target outcome:

- large, clearly visible real menswear examples are extracted as KB evidence;
- useful `items` and evidence-bearing scenes do not regress without explicit review;
- presenter baseline clothing stays out of scene-specific fields;
- presentation/UI/video wording stays only in `presentation_context`;
- cartoons, UI thumbnails, social/mobile grids, and catalog chrome do not become KB evidence;
- OCR/product text is context only and must not be promoted into visual claims;
- final KB-facing strings are Russian, except accepted style terms and literal `on_screen_text`;
- quality reporting exposes both quarantined noise and evidence-retention changes.

## Regression Cases

Use these `qxXRWoSYf7I` scenes as concrete examples when designing tests and manual checks.

| Scene | Current problem | Expected behavior |
| --- | --- | --- |
| `scene_000004` | `visual_summary` starts with presentation wording: `Наложение изображения...` | Keep only clothing evidence: long dark coat, patterned shirt, dark trousers/shoes. Move overlay wording to `presentation_context`. |
| `scene_000007` | Large inserted real outfit is ignored; only on-screen text remains | Extract visible outfit evidence from the large inserted photo: light jacket, white/open-collar shirt, light high-rise trousers, pocket square/belt if confidently visible. |
| `scene_000011` | KB fields are in English and include `An image... displayed` | Retry/prompt should produce Russian KB fields and omit `image/displayed/insert` wording. |
| `scene_000041` | Cartoon green character produces `items=["рубашка","галстук","кепка"]` | Record-level guard should quarantine KB evidence because the scene describes an animated/non-real figure, even though individual item strings are valid clothing words. |
| `scene_000055` | Website/catalog text leaks into evidence: fabric names, composition, prices, product details | Keep only visibly identifiable clothing from large product photos. Price, composition, manufacturer, product titles stay only in `on_screen_text`. Do not denylist fabric names by themselves. |
| `scene_000068` | Presenter gestures and pointing become scene deltas / observations; `primary_example` is over-assigned | Keep clothing evidence from inserted real man; move presenter gestures/pointing to `presentation_context`, or drop if not useful. |
| `scene_000070` | Mobile/social thumbnails become generic clothing evidence | Ignore small mobile thumbnails/grid previews unless an item is large enough for confident identification. |
| `scene_000072` | KB fields are in English and include `overlay` wording | Retry/prompt should produce Russian KB fields; overlay/set wording goes to `presentation_context`. |

## Non-Goals

- Do not add new CLI flags.
- Do not change provider/model selection via CLI.
- Do not add embeddings, vector stores, CLIP/open-clip/torch, or new heavy ML dependencies.
- Do not make `presentation_context` available as KB evidence.
- Do not rewrite stages 08/09 in this task.
- Do not add a manual review UI.

## Design Principles

### 1. Prompt and retry own semantic decisions

Some failures are semantic, not lexical:

- whether `сирсакер` is visually recognized fabric or just OCR/product text;
- whether an image is a cartoon/non-real example;
- whether an English response should be regenerated in Russian;
- whether a large inserted photo is useful visual evidence.

Regex cannot reliably decide these. Prompt/retry should be the primary mechanism for recall, OCR handling, language, and non-real examples.

### 2. Deterministic sanitation is only a backstop

Use deterministic cleanup for cases that are safely lexical:

- currency and price text;
- percentages and composition strings;
- explicit product-page chrome;
- presentation/framing words like `image`, `displayed`, `overlay`, `вставка`, `наложение`, `кадр`, `экран`;
- presenter gesture/pointing wording in KB-facing fields.

Do not denylist clothing words or fabric names by themselves.

### 3. Guards that need context operate at record level

Per-string regex is insufficient for:

- cartoon evidence: `рубашка`, `галстук`, `кепка` are valid item strings, but the record says the wearer is animated;
- language: an English response is a property of the record, not one isolated string;
- text-derived claims: exact fabric/product facts may be coming from OCR even if the item string itself looks valid.

Cross-field guards should inspect the full payload: `visual_summary`, `observations`, `items`, `style_topics`, `notes`, `on_screen_text`, `presentation_context`, and `presenter_context`.

### 4. Preserve recall explicitly

Every cleanup change must be paired with evidence-retention checks:

- total `items` before vs after;
- scenes with any KB evidence before vs after;
- scenes with useful clothing evidence that were preserved;
- manual review of known positive scenes, especially `scene_000007`.

## Implementation Plan

### 1. Update stage contract documentation

File: `docs/stages/10_describe_visuals.md`

Document the runtime contract:

- large real inserted examples should produce KB evidence when clothing is clearly visible;
- small mobile/social/catalog thumbnails should not produce evidence unless the clothing is large enough to identify confidently;
- cartoons, mascots, animations, memes, icons, and non-real figures do not produce menswear evidence;
- OCR/product-page text is context only and cannot become visual evidence by itself;
- exact price, composition, manufacturer, product title, and brand text stay in `on_screen_text`;
- fabric names are not globally banned; they are allowed only when the model visually identifies the fabric rather than copying OCR;
- final KB-facing fields should be Russian;
- `presentation_context` remains diagnostic and must not flow into KB evidence.

### 2. Strengthen the prompt around positive extraction

File: `src/style_kb/prompts/visual_menswear_ru.txt`

Make recall a first-class instruction:

- If a large inserted photo shows a real person or physical garment clearly enough, extract visible menswear evidence even if the recurring presenter is also present.
- Describe only the clothing evidence. Do not include words such as photo, insert, image, screen, overlay, side, frame, or displayed.
- When the only visible new content is readable text, keep it in `on_screen_text` and leave KB fields empty.
- If a scene contains both a large useful example and on-screen text, extract the visible clothing and keep OCR/product text separate.

Add a positive few-shot for `scene_000007`-style content:

- Input situation: recurring presenter plus large inserted photo of a real man in a light jacket, open-collar shirt, light high-rise trousers.
- Good output: `items`/`observations` contain only visible clothing; `presentation_context` contains insertion/framing details; no baseline presenter clothing is repeated.

### 3. Strengthen the prompt around semantic exclusions

File: `src/style_kb/prompts/visual_menswear_ru.txt`

Add explicit negative rules:

- Return all KB-facing strings in Russian except accepted style terms and literal `on_screen_text`.
- Do not treat cartoons, animations, mascots, icons, memes, or illustrations as menswear evidence.
- Do not infer clothing from small mobile/social grids, thumbnail sheets, blurred catalog previews, or UI screenshots.
- Do not promote OCR/product text to visual claims. Price, composition, country, manufacturer, product title, product model, and brand remain only in `on_screen_text`.
- Do not infer exact fabric from OCR text. If fabric is visually recognizable, describe the visible texture/fabric cautiously; otherwise keep the fabric name only in `on_screen_text`.
- Presenter gestures, pointing, pose changes, and instructional body language are not style evidence unless a clothing detail on the presenter changed.

Add negative few-shots:

- cartoon character with shirt/tie/cap -> empty KB-facing evidence, diagnostic context only;
- mobile phone grid with tiny outfits -> empty KB-facing evidence, readable text only;
- catalog page with product names/prices/fabric composition -> visible outfit evidence only, OCR facts only in `on_screen_text`;
- English response with `An image... displayed` -> retry target is Russian clothing-only wording.

### 4. Expand deterministic lexical backstop

File: `src/style_kb/stages/stage_10_describe_visuals.py`

Extend marker patterns only for safely lexical leakage:

- presentation/framing: `image`, `images`, `insert`, `inset`, `displayed`, `shown`, `photo`, `picture`, `overlay`, `screen`, `on-screen`, `вставк`, `наложен`, `изображен`, `изображени`, `фото`, `картинк`, `кадр`, `экран`;
- UI/page chrome: `мобильный экран`, `смартфон`, `миниатюр`, `thumbnail`, `grid`, `сетка`, `каталог`, `страница`, `сайт`, `website`, `product page`;
- presenter action noise: `ведущий демонстрирует`, `ведущий указывает`, `жестикулир`, `указывает`, `pointing`, `gesturing`;
- hard text-derived facts: currency symbols, `руб`, `р.`, `%`, composition patterns, explicit manufacturer/product metadata.

Do not add a denylist for fabric names such as `сирсакер`, `seersucker`, `тропическая шерсть`, `твид`, `фланель`, or `шерсть`. Fabric names should be handled by prompt/retry and retention review.

### 5. Add cross-field guards

File: `src/style_kb/stages/stage_10_describe_visuals.py`

Implement record-level guards that inspect the whole parsed payload before materializing `VisualEvent`.

#### Cartoon / non-real guard

Trigger when the payload says the evidence source is animated/cartoon/non-real and there is no separate large real person, mannequin, or physical garment example.

Signals may come from:

- `visual_summary`;
- `observations`;
- `notes`;
- `presentation_context`;
- `presenter_context.present=false` as supporting evidence only, not as a standalone reason.

Behavior:

- clear KB-facing `items`, `observations`, `interpretations`, `style_topics`, `visual_summary`, and `notes`;
- preserve the original diagnostic wording in `presentation_context`;
- add quarantine reason `non_real_visual_example`.

Do not apply this guard to physical mannequins, flat-lay garments, product photos, or large real photos just because `presenter_context.present=false`.

#### UI / thumbnail guard

Trigger when clothing evidence is based only on small UI thumbnails, mobile/social grids, or catalog preview tiles.

Behavior:

- quarantine generic clothing evidence derived from tiny UI elements;
- keep readable text in `on_screen_text`;
- add reason `ui_thumbnail_evidence`.

Do not apply when the page contains large product photos where garments are clearly visible.

#### OCR/product-text guard

Trigger when KB-facing fields contain hard product/OCR facts:

- price/currency;
- percentages/composition;
- product title fragments;
- manufacturer names;
- `Подробнее`, `Купить`, and similar page actions.

Behavior:

- remove these hard facts from KB-facing fields;
- preserve them in `on_screen_text` if not already present, or `presentation_context` if not literal text;
- add reason `ocr_product_text_promoted`.

Fabric names alone are not a trigger.

#### Presenter-action guard

Trigger when `observations`, `scene_deltas`, or `narrative_brief` contain only gestures, pointing, framing, or instructional body language.

Behavior:

- remove the action from KB-facing fields;
- preserve diagnostic wording in `presentation_context` if useful;
- if `presenter_context.relevance` is `primary_example` only due to action/framing, downgrade based on remaining evidence.

### 6. Add language retry signal

Use language detection as a retry/quality signal, not a scrubber.

Proposed heuristic for KB-facing fields:

- ignore `on_screen_text`;
- allow a whitelist of accepted style terms such as `smart casual`, `oversize`, `total black`, `business casual`;
- flag strings with length above a small threshold and latin-letter share above 50%;
- flag a scene when enough KB-facing text is flagged.

Behavior:

- prefer one retry with explicit Russian-language feedback;
- if still non-Russian, keep the payload but report quarantine/quality warnings rather than deleting potentially useful evidence.

Reason: `non_russian_kb_text`.

### 7. Use one quarantine metric family

Avoid a large set of mostly-zero metrics.

Add one compact metric family:

- `kb_evidence_quarantined_count`
- `kb_evidence_quarantined_scenes_count`
- `kb_evidence_quarantine_reasons`: map of reason -> count

Initial reasons:

- `presentation_framing`
- `non_real_visual_example`
- `ui_thumbnail_evidence`
- `ocr_product_text_promoted`
- `presenter_action_only`
- `non_russian_kb_text`

Keep existing `technical_leakage_*` and `baseline_leakage_*` metrics if they are already used, but do not add six separate new families until a reason is noisy enough to deserve promotion.

### 8. Add evidence-retention audit

This is the safety rail against over-cleaning.

For an artifact-level before/after check on `qxXRWoSYf7I`, compute:

- `items_total_before`
- `items_total_after`
- `items_delta`
- `evidence_scenes_before`: scenes with any non-empty `visual_summary`, `observations`, `items`, or `style_topics`
- `evidence_scenes_after`
- `evidence_scenes_delta`
- `quarantined_items_count`
- `quarantined_items_by_reason`

Acceptance guard:

- `items_total_after` and `evidence_scenes_after` must not drop materially without a reviewed reason;
- positive scenes such as `scene_000007`, `scene_000011`, `scene_000021`, `scene_000055`, `scene_000062`, and `scene_000068` must keep useful visible clothing evidence;
- removed evidence must be explainable by quarantine reasons.

This retention audit can be a test helper or diagnostic snapshot; it does not need to become a public CLI command.

### 9. Add focused tests

Find existing test layout first.

Suggested test groups:

1. Lexical backstop catches presentation wording:
   - `An image of a man in a dark suit is displayed`;
   - `Вставка отображает мужчину в белой рубашке`;
   - `Наложение изображения мужчины`;
   - `overlay shows two men`.

2. Lexical backstop catches hard OCR/product facts:
   - `45 000 р.`;
   - `95% cotton, 5% elastane`;
   - `Подробнее`, `Купить`;
   - manufacturer/product-page fragments.

3. Lexical backstop does not remove valid clothing/fabric words by themselves:
   - `светло-серый пиджак`;
   - `брюки с высокой талией`;
   - `темно-коричневые лоферы`;
   - `костюм из сирсакера`;
   - `твидовый костюм`;
   - `тропическая шерсть`.

4. Cross-field cartoon guard:
   - payload has `items=["рубашка","галстук","кепка"]`;
   - payload also says `анимированная зеленая фигура`;
   - result clears KB evidence and records `non_real_visual_example`.

5. Cross-field UI thumbnail guard:
   - payload has generic clothing from a mobile grid;
   - result clears generic evidence and records `ui_thumbnail_evidence`.

6. Language guard:
   - English-heavy KB fields are flagged for retry;
   - `on_screen_text` and whitelisted style terms are ignored.

7. Retention audit:
   - useful clothing payloads remain counted after sanitation;
   - quarantined evidence is counted with reasons.

### 10. Cost-aware implementation order

Conceptual priority is prompt/retry because the main gap is recall and semantic interpretation. Execution order should still minimize provider cost.

1. Update docs/stage contract.
2. Implement deterministic lexical backstop and cross-field guards.
3. Run artifact-level smoke checks on cached raw payloads from `qxXRWoSYf7I`; no provider calls.
4. Add retention audit over current final artifacts and cached transformed payloads.
5. Update prompt for positive extraction, OCR handling, non-real examples, and Russian output.
6. Run unit tests.
7. Run one provider rerun for stage 10 after prompt hash changes.
8. Compare before/after on `qxXRWoSYf7I`: retention metrics, quarantine reasons, and manual scene checks.

### 11. Artifact-level smoke test

Use existing raw payloads from `qxXRWoSYf7I` without calling providers.

Procedure:

1. Load selected `visual/raw/{scene_id}.json`.
2. Extract `parsed`.
3. Run the updated sanitizer/build helper.
4. Compare output constraints and retention metrics.

Required scenes:

- `scene_000004`
- `scene_000011`
- `scene_000041`
- `scene_000055`
- `scene_000068`
- `scene_000070`
- `scene_000072`

Note: `scene_000007` currently has no useful evidence in the raw provider response, so deterministic sanitation cannot recover it. It must be validated after prompt update and rerun.

### 12. Manual validation after rerun

Inspect these scenes after the prompt rerun:

- `scene_000004`
- `scene_000007`
- `scene_000011`
- `scene_000041`
- `scene_000055`
- `scene_000068`
- `scene_000070`
- `scene_000072`

Expected manual results:

- `scene_000007` has visible clothing evidence from the large inserted real photo.
- `scene_000041` has no `items`, no menswear `observations`, and no style `interpretations`.
- `scene_000055` has no price/composition/manufacturer/product-title facts in KB-facing fields, while visibly identifiable product-photo clothing remains.
- Fabric names are not removed solely because they are fabric names.
- `scene_000068` has no presenter gesture in `observations`, `scene_deltas`, or `narrative_brief`; clothing evidence remains.
- `scene_000070` has no generic evidence from tiny mobile thumbnails.
- English KB fields are regenerated in Russian or clearly reported as `non_russian_kb_text`.

## Acceptance Criteria

- All new tests pass.
- Stage 10 docs match implemented behavior.
- Prompt changes explicitly prioritize extraction from large real menswear examples.
- `scene_000007` recall improves after prompt rerun.
- Final KB-facing fields do not contain presentation words like `вставка`, `наложение`, `image`, `displayed`, `overlay`, `screen`, `ведущий демонстрирует`, or `указывает`.
- Final KB-facing fields are Russian except whitelisted style terms and literal `on_screen_text`.
- Cartoon/non-real records do not produce menswear evidence.
- Mobile/social thumbnails do not produce generic clothing evidence.
- Catalog/product text does not produce price, composition, manufacturer, brand, or product-title evidence in KB-facing fields.
- Fabric names are not globally denylisted and can remain when visually justified.
- `kb_evidence_quarantined_*` metrics include reason counts.
- Retention audit reports before/after `items` and evidence-scene counts; any material drop is manually reviewed.
- Quality report exposes remaining leakage/quarantine instead of reporting a clean job.

## Files Likely To Change

- `docs/stages/10_describe_visuals.md`
- `src/style_kb/prompts/visual_menswear_ru.txt`
- `src/style_kb/stages/stage_10_describe_visuals.py`
- stage 10 tests, depending on current test layout
- possibly quality/audit report aggregation if stage metrics are not already surfaced

## Risks

- Prompt-only fixes may be inconsistent across providers or model versions; deterministic backstop remains necessary.
- Over-aggressive regexes can remove valid clothing descriptions. Positive tests and retention audit must catch this.
- Cross-field guards can over-quarantine if they treat `present=false` as sufficient proof. It must only be supporting context.
- Sanitizer can remove bad evidence but cannot recover evidence omitted by the provider. `scene_000007` requires prompt change plus rerun.
- Language detection can falsely flag legitimate style terms and brand text; use a whitelist and prefer retry over deletion.

# Pipeline Stage Notes

These files are written for AI agents that need to understand or modify the `style-kb` pipeline.

Read the stage document before editing the corresponding `src/style_kb/stages/stage_*.py` module. The runtime order is defined in `src/style_kb/pipeline/catalog.py`.

## Ordered Stages

| Stage | Document | Code |
|---|---|---|
| 01_metadata | [01_metadata.md](01_metadata.md) | `src/style_kb/stages/stage_01_metadata.py` |
| 02_download_audio | [02_download_audio.md](02_download_audio.md) | `src/style_kb/stages/stage_02_download_audio.py` |
| 03_download_video_proxy | [03_download_video_proxy.md](03_download_video_proxy.md) | `src/style_kb/stages/stage_03_download_video_proxy.py` |
| 04_soniox_upload_audio | [04_soniox_upload_audio.md](04_soniox_upload_audio.md) | `src/style_kb/stages/stage_04_soniox_upload_audio.py` |
| 05_soniox_create_transcription | [05_soniox_create_transcription.md](05_soniox_create_transcription.md) | `src/style_kb/stages/stage_05_soniox_create_transcription.py` |
| 06_soniox_fetch_transcript | [06_soniox_fetch_transcript.md](06_soniox_fetch_transcript.md) | `src/style_kb/stages/stage_06_soniox_fetch_transcript.py` |
| 07_build_speech_segments | [07_build_speech_segments.md](07_build_speech_segments.md) | `src/style_kb/stages/stage_07_build_speech_segments.py` |
| 08_detect_scenes | [08_detect_scenes.md](08_detect_scenes.md) | `src/style_kb/stages/stage_08_detect_scenes.py` |
| 09_extract_keyframes | [09_extract_keyframes.md](09_extract_keyframes.md) | `src/style_kb/stages/stage_09_extract_keyframes.py` |
| 10_describe_visuals | [10_describe_visuals.md](10_describe_visuals.md) | `src/style_kb/stages/stage_10_describe_visuals.py` |
| 11_merge_timeline | [11_merge_timeline.md](11_merge_timeline.md) | `src/style_kb/stages/stage_11_merge_timeline.py` |
| 12_build_chunks | [12_build_chunks.md](12_build_chunks.md) | `src/style_kb/stages/stage_12_build_chunks.py` |
| 13_extract_style_claims | [13_extract_style_claims.md](13_extract_style_claims.md) | `src/style_kb/stages/stage_13_extract_style_claims.py` |
| 14_export_jsonl | [14_export_jsonl.md](14_export_jsonl.md) | `src/style_kb/stages/stage_14_export_jsonl.py` |
| 15_export_obsidian | [15_export_obsidian.md](15_export_obsidian.md) | `src/style_kb/stages/stage_15_export_obsidian.py` |
| 16_quality_report | [16_quality_report.md](16_quality_report.md) | `src/style_kb/stages/stage_16_quality_report.py` |
| 17_cleanup | [17_cleanup.md](17_cleanup.md) | `src/style_kb/stages/stage_17_cleanup.py` |

## Cross-Stage Invariants

- `job_id == video_id` in the MVP.
- Stage skip requires both `validate_outputs(context)` and `outputs_are_current(context)`.
- Stages with durable reports should validate report schema/config/input identity before skip, not only artifact existence.
- JSON/JSONL/Markdown writes should use existing atomic write helpers.
- Output objects that represent knowledge must preserve timestamp/source grounding.
- Diagnostic/quarantine fields such as stage 10 `presentation_context` and raw provider outputs must not become KB style evidence.
- Product configuration comes from `src/style_kb/config/default.yaml`, not CLI flags, except the explicit `--batch` runtime execution switch.
- `pipeline.visual_enabled=false` is the default audio-only mode. In this mode stages 03, 08, 09, and 10 are skipped by config, and downstream stages must not require scene, frame, visual, presenter, or video-proxy artifacts.
- `style-kb ingest URL` and `style-kb resume JOB_ID` accept an optional stage number to stop immediately after that stage completes, for example `style-kb ingest URL 9`.
- `style-kb ingest URL --batch` and `style-kb resume JOB_ID --batch` enable eligible OpenAI Batch API requests for that run.
- `style-kb ingest URL` is for new jobs only. If SQLite already has that job id, or the corresponding job artifact directory already contains entries, ingest must refuse to start and the user should run `style-kb resume JOB_ID`.
- API keys come from `.env`/environment variables, not YAML.
- Avoid fallback behavior unless it is already explicit in a stage document.
- Stage 08/09 changes must be measured by scene count, frame count, frame cap behavior, extraction elapsed time, duplicate frames skipped, stage 10 provider requests, and materialized visual event count.
- Do not add embeddings, vector stores, CLIP/open-clip/torch, or other heavy ML dependencies for MVP visual frame selection.

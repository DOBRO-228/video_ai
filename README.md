# style-kb

CLI application for ingesting one YouTube video URL into a local multimodal knowledge base.

Primary ingest:

```bash
style-kb ingest "https://www.youtube.com/watch?v=VIDEO_ID"
```

`ingest` only starts a new local job. If the job already exists in SQLite or the
job artifact directory already contains entries, the command refuses to start and
prints the existing job id. Continue an existing job with `resume`.

For inspection workflows, pass an optional stage number to stop immediately after that stage completes:

```bash
style-kb ingest "https://www.youtube.com/watch?v=VIDEO_ID" 9
style-kb resume VIDEO_ID 9
```

To use the OpenAI Batch API for eligible claim-extraction requests in the current run, pass `--batch`:

```bash
style-kb ingest "https://www.youtube.com/watch?v=VIDEO_ID" --batch
style-kb resume VIDEO_ID --batch
```

API keys are loaded from `.env` or the environment:

- `SONIOX_API_KEY` for speech transcription.
- `OPENAI_API_KEY` for the default OpenAI vision stage, speech segmentation, chunk planning, and style-claim extraction.
- `GEMINI_API_KEY` for the optional Gemini vision stage.

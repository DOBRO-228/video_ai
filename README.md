# style-kb

CLI application for ingesting one YouTube video URL into a local multimodal knowledge base.

API keys are loaded from `.env` or the environment:

- `SONIOX_API_KEY` for speech transcription.
- `OPENAI_API_KEY` for the default OpenAI vision stage, speech segmentation, chunk planning, and style-claim extraction.
- `GEMINI_API_KEY` for the optional Gemini vision stage.

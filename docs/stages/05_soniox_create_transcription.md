# 05_soniox_create_transcription

## Purpose

Create an asynchronous Soniox transcription job for the uploaded audio.

## How It Works

The stage reads the uploaded file id, then calls `SonioxClient.create_transcription()` with STT settings from config: model, language hints, language identification, diarization, and domain context terms.

## Inputs

- `stt/soniox_upload.json`
- `stt.*` config values
- `SONIOX_API_KEY`

## Outputs

- `stt/soniox_transcription.json`
- SQLite remote ref `soniox_transcription_id`

## Skip Validation

The stage can be skipped when `soniox_transcription.json` exists and contains an `id`.

## Important Notes

- Do not duplicate STT settings as CLI flags.
- This stage only creates the remote async job; it does not fetch final transcript tokens.
- The Soniox context terms are important for Russian/English menswear vocabulary.

## Related Code

- `src/style_kb/stages/stage_05_soniox_create_transcription.py`
- `src/style_kb/clients/soniox.py`
- `src/style_kb/config/default.yaml`


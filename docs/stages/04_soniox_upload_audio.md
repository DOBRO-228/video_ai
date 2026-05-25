# 04_soniox_upload_audio

## Purpose

Upload the downloaded audio file to Soniox and persist the remote file id.

## How It Works

The stage creates `SonioxClient` from `SONIOX_API_KEY`, uploads `downloads/audio.mp3`, and writes the Soniox upload payload. The job id is used as `client_reference_id`.

## Inputs

- `downloads/audio.mp3`
- `SONIOX_API_KEY`

## Outputs

- `stt/soniox_upload.json`
- SQLite remote ref `soniox_file_id`

## Skip Validation

The stage can be skipped when `soniox_upload.json` exists and contains an `id`.

## Important Notes

- The API key is loaded from `.env`/environment, not YAML.
- Missing or invalid Soniox credentials should fail here before downstream transcription creation.
- Do not re-upload when a valid remote file id already exists.

## Related Code

- `src/style_kb/stages/stage_04_soniox_upload_audio.py`
- `src/style_kb/clients/soniox.py`


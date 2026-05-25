# 06_soniox_fetch_transcript

## Purpose

Wait for the Soniox transcription to complete and normalize the transcript into token-level timestamp artifacts.

## How It Works

The stage polls Soniox through `wait_for_transcription()`. When the remote job is terminal and successful, it writes the latest transcription status payload, downloads the transcript, normalizes token text/timestamps/language/speaker fields, and writes `SpeechToken` JSONL.

## Inputs

- `stt/soniox_transcription.json`
- `SONIOX_API_KEY`

## Outputs

- `stt/transcript_raw.json`
- `stt/speech_tokens.jsonl`
- Updated `stt/soniox_transcription.json`

## Skip Validation

The stage can be skipped when raw transcript and token JSONL exist, tokens are non-empty, timestamps are ordered, and normalized tokens match the raw transcript.

## Important Notes

- Empty transcripts fail the stage.
- Token timestamps are the source of truth for later speech clipping in `11_merge_timeline`.
- Keep console progress compact during polling; detailed provider payloads belong in logs/artifacts.

## Related Code

- `src/style_kb/stages/stage_06_soniox_fetch_transcript.py`
- `src/style_kb/clients/soniox.py`
- `src/style_kb/models.py::SpeechToken`


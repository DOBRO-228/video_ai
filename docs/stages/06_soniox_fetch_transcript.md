# 06_soniox_fetch_transcript

## Purpose

Wait for the Soniox transcription to complete and normalize the transcript into token-level timestamp artifacts.

## How It Works

The stage polls Soniox through `wait_for_transcription()`. When the remote job is terminal and successful, it writes the latest transcription status payload, downloads the transcript, normalizes token text/timestamps/language/speaker fields, assigns speaker roles, and writes `SpeechToken` JSONL.

Speaker role mapping is deterministic for the MVP:

- Speaker with the largest speech duration becomes `host`.
- Other detected speakers become `offscreen_questioner`.
- If Soniox returns no speaker labels, tokens keep `speaker_role=null` and quality report emits a warning.
- Tokens without a Soniox speaker label are counted in `unassigned_tokens_count`.

## Inputs

- `stt/soniox_transcription.json`
- `SONIOX_API_KEY`

## Outputs

- `stt/transcript_raw.json`
- `stt/speaker_diarization.json`
- `stt/speech_tokens.jsonl`
- Updated `stt/soniox_transcription.json`

## Skip Validation

The stage can be skipped when raw transcript, speaker diarization, and token JSONL exist, tokens are non-empty, timestamps are ordered, and normalized role-enriched tokens match the raw transcript plus diarization report.

## Important Notes

- Empty transcripts fail the stage.
- Token timestamps are the source of truth for later speech clipping in `11_merge_timeline`.
- `speaker` is the raw Soniox speaker id; `speaker_role` is the repository-level semantic role.
- Keep console progress compact during polling; detailed provider payloads belong in logs/artifacts.

## Related Code

- `src/style_kb/stages/stage_06_soniox_fetch_transcript.py`
- `src/style_kb/clients/soniox.py`
- `src/style_kb/models.py::SpeechToken`

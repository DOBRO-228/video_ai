# 02_download_audio

## Purpose

Download the audio track used for speech-to-text.

## How It Works

The stage calls the `yt-dlp` audio wrapper with audio format and quality from config. It then runs `ffprobe` and stores the media probe payload so later stages can validate duration and detect audio/video mismatches.

## Inputs

- `metadata/video_info.json`
- `download.audio_format`
- `download.audio_quality`
- `download.cookies_from_browser`

## Outputs

- `downloads/audio.mp3`
- `downloads/audio.ffprobe.json`

## Skip Validation

The stage can be skipped when both files exist and `audio.ffprobe.json` reports duration greater than zero.

## Important Notes

- This stage should fail if `yt-dlp` or `ffprobe` fails.
- Cleanup may remove audio and its probe after successful quality reporting when `project.keep_media=false`.
- Do not make audio quality configurable through CLI flags.

## Related Code

- `src/style_kb/stages/stage_02_download_audio.py`
- `src/style_kb/clients/ytdlp.py`
- `src/style_kb/clients/media.py`


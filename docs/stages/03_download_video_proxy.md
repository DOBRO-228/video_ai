# 03_download_video_proxy

## Purpose

Download the visual proxy video used for scene detection and keyframe extraction.

## How It Works

The stage calls the `yt-dlp` video proxy wrapper using configured height and format. By default this is a 720p MP4 proxy. MP4/H.264 is preferred for decoder compatibility, with fallback to any MP4 proxy when H.264 is unavailable. It then runs `ffprobe` and stores the result for duration and FPS consumers.

## Inputs

- `metadata/video_info.json`
- `download.video_height`
- `download.video_format`
- `download.cookies_from_browser`

## Outputs

- `downloads/video_proxy.mp4`
- `downloads/video_proxy.ffprobe.json`

## Skip Validation

The stage can be skipped when the video and ffprobe JSON exist and the probed duration is greater than zero.

## Important Notes

- Downstream visual stages depend on this proxy, not on the original full-quality video.
- Cleanup may remove proxy video and probe output after successful quality reporting when `project.keep_media=false`.
- Avoid changing proxy quality defaults without considering vision provider cost and PySceneDetect runtime.

## Related Code

- `src/style_kb/stages/stage_03_download_video_proxy.py`
- `src/style_kb/clients/ytdlp.py`
- `src/style_kb/clients/media.py`

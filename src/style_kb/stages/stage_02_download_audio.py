from __future__ import annotations

from style_kb.clients.media import duration_seconds, ffprobe_json
from style_kb.clients.ytdlp import download_audio
from style_kb.errors import MediaToolError
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.diagnostics import append_stage_summary, file_size, stream_summary
from style_kb.utils.files import read_json, write_json_atomic


class Stage02DownloadAudio(Stage):
    name = "02_download_audio"
    ordinal = 2

    def input_files(self, context: StageContext) -> list:
        return [context.paths.metadata_video_info]

    def output_files(self, context: StageContext) -> list:
        return [context.paths.downloads_audio, context.paths.downloads_audio_ffprobe]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.downloads_audio.exists() or not context.paths.downloads_audio_ffprobe.exists():
            return False
        payload = read_json(context.paths.downloads_audio_ffprobe)
        return duration_seconds(payload) > 0

    def run(self, context: StageContext) -> StageResult:
        download_audio(
            context.job.url,
            destination=context.paths.downloads_audio,
            audio_format=context.config.download.audio_format,
            audio_quality=context.config.download.audio_quality,
            cookies_from_browser=context.config.download.cookies_from_browser,
            remote_components=context.config.download.remote_components,
            log_path=context.paths.stage_log(self.name),
            pipeline_logger=context.pipeline_logger,
            job_id=context.job.job_id,
            video_id=context.job.video_id,
            stage=self.name,
            ordinal=self.ordinal,
        )
        ffprobe_payload = ffprobe_json(
            context.paths.downloads_audio,
            log_path=context.paths.stage_log(f"{self.name}.ffprobe"),
            pipeline_logger=context.pipeline_logger,
            job_id=context.job.job_id,
            video_id=context.job.video_id,
            stage=self.name,
            ordinal=self.ordinal,
        )
        duration = duration_seconds(ffprobe_payload)
        if duration <= 0:
            raise MediaToolError("downloaded audio has invalid duration", error_code="audio_duration_invalid")
        write_json_atomic(context.paths.downloads_audio_ffprobe, ffprobe_payload)
        size_bytes = file_size(context.paths.downloads_audio)
        append_stage_summary(
            context,
            self.name,
            "media-summary",
            {
                "requested_audio_format": context.config.download.audio_format,
                "requested_audio_quality": context.config.download.audio_quality,
                "yt_dlp_remote_components": context.config.download.remote_components or "disabled",
                "destination": str(context.paths.downloads_audio),
                "file_size_bytes": size_bytes,
                "ffprobe_duration": duration,
                "ffprobe_streams": stream_summary(ffprobe_payload),
                "ffprobe_path": str(context.paths.downloads_audio_ffprobe),
            },
        )
        return StageResult(
            output_files=self.output_files(context),
            metrics={
                "duration": duration,
                "file_size_bytes": size_bytes,
                "format_name": ffprobe_payload.get("format", {}).get("format_name"),
            },
        )

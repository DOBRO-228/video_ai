from __future__ import annotations

from style_kb.clients.ytdlp import fetch_metadata
from style_kb.models import VideoInfo
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.diagnostics import append_stage_summary
from style_kb.utils.files import read_json, write_json_atomic
from style_kb.utils.pydantic_io import write_model
from style_kb.utils.time import build_timestamp_url


class Stage01Metadata(Stage):
    name = "01_metadata"
    ordinal = 1

    def output_files(self, context: StageContext) -> list:
        return [context.paths.metadata_raw_ytdlp, context.paths.metadata_video_info]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.metadata_video_info.exists():
            return False
        payload = read_json(context.paths.metadata_video_info)
        video_info = VideoInfo.model_validate(payload)
        return video_info.video_id == context.job.video_id

    def run(self, context: StageContext) -> StageResult:
        raw_metadata = fetch_metadata(
            context.job.url,
            log_path=context.paths.stage_log(self.name),
            cookies_from_browser=context.config.download.cookies_from_browser,
            stdout_artifact=context.paths.metadata_raw_ytdlp,
            pipeline_logger=context.pipeline_logger,
            job_id=context.job.job_id,
            video_id=context.job.video_id,
            stage=self.name,
            ordinal=self.ordinal,
        )
        write_json_atomic(context.paths.metadata_raw_ytdlp, raw_metadata)

        video_info = VideoInfo(
            job_id=context.job.job_id,
            video_id=context.job.video_id,
            url=context.job.url,
            title=raw_metadata.get("title") or context.job.video_id,
            channel=raw_metadata.get("channel") or raw_metadata.get("uploader") or "unknown",
            duration=float(raw_metadata.get("duration") or 0.0),
            description=raw_metadata.get("description"),
            thumbnail_url=raw_metadata.get("thumbnail"),
            upload_date=raw_metadata.get("upload_date"),
            timestamp_url=build_timestamp_url(context.job.video_id, 0.0),
        )
        write_model(context.paths.metadata_video_info, video_info)
        context.repository.update_job(context.job.job_id, title=video_info.title, channel=video_info.channel)
        append_stage_summary(
            context,
            self.name,
            "metadata-summary",
            {
                "url": context.job.url,
                "video_id": context.job.video_id,
                "cookies_from_browser": context.config.download.cookies_from_browser,
                "yt_dlp_version": raw_metadata.get("_style_kb_ytdlp_version"),
                "title": video_info.title,
                "channel": video_info.channel,
                "duration": video_info.duration,
                "raw_metadata_path": str(context.paths.metadata_raw_ytdlp),
                "video_info_path": str(context.paths.metadata_video_info),
            },
        )
        return StageResult(
            output_files=self.output_files(context),
            metrics={
                "duration": video_info.duration,
                "yt_dlp_version": raw_metadata.get("_style_kb_ytdlp_version"),
                "title_present": bool(video_info.title),
                "channel_present": bool(video_info.channel),
            },
        )

from __future__ import annotations

import json
from pathlib import Path

from style_kb.diagnostics import PipelineLogger
from style_kb.errors import MediaToolError
from style_kb.utils.process import run_subprocess


def ffprobe_json(
    media_path: Path,
    *,
    log_path: Path,
    pipeline_logger: PipelineLogger | None = None,
    job_id: str | None = None,
    video_id: str | None = None,
    stage: str | None = None,
    ordinal: int | None = None,
) -> dict:
    args = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(media_path),
    ]
    completed = run_subprocess(
        args,
        error_code="ffprobe_failed",
        log_path=log_path,
        pipeline_logger=pipeline_logger,
        job_id=job_id,
        video_id=video_id,
        stage=stage,
        ordinal=ordinal,
    )
    return json.loads(completed.stdout)


def duration_seconds(ffprobe_payload: dict) -> float:
    duration = ffprobe_payload.get("format", {}).get("duration")
    if duration is None:
        raise MediaToolError("ffprobe output has no duration", error_code="ffprobe_duration_missing")
    return float(duration)


def fps(ffprobe_payload: dict) -> float:
    video_streams = [stream for stream in ffprobe_payload.get("streams", []) if stream.get("codec_type") == "video"]
    if not video_streams:
        raise MediaToolError("ffprobe output has no video stream", error_code="ffprobe_video_stream_missing")
    rate = video_streams[0].get("avg_frame_rate") or video_streams[0].get("r_frame_rate")
    if not rate or rate == "0/0":
        raise MediaToolError("ffprobe output has invalid frame rate", error_code="ffprobe_fps_missing")
    numerator, denominator = rate.split("/", 1)
    return float(numerator) / float(denominator)


def extract_frame(
    video_path: Path,
    *,
    timestamp: float,
    destination: Path,
    log_path: Path,
    pipeline_logger: PipelineLogger | None = None,
    job_id: str | None = None,
    video_id: str | None = None,
    stage: str | None = None,
    ordinal: int | None = None,
    text_log_streams: bool = True,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.parent / f"{destination.stem}.tmp{destination.suffix}"
    args = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-y",
        str(temp_path),
    ]
    run_subprocess(
        args,
        error_code="ffmpeg_extract_frame_failed",
        log_path=log_path,
        pipeline_logger=pipeline_logger,
        job_id=job_id,
        video_id=video_id,
        stage=stage,
        ordinal=ordinal,
        text_log_streams=text_log_streams,
    )
    if not temp_path.exists() or temp_path.stat().st_size == 0:
        raise MediaToolError("ffmpeg did not create frame image", error_code="frame_missing")
    temp_path.replace(destination)

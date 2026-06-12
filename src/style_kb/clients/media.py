from __future__ import annotations

import json

from dataclasses import dataclass
from pathlib import Path

from style_kb.diagnostics import PipelineLogger
from style_kb.errors import MediaToolError
from style_kb.utils.process import run_subprocess


@dataclass(frozen=True, slots=True)
class ExtractedWindowFrame:
    path: Path
    timestamp: float
    offset_seconds: float


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


def extract_frame_window(
    video_path: Path,
    *,
    window_start: float,
    window_end: float,
    probe_step_seconds: float,
    destination_dir: Path,
    filename_prefix: str,
    single_timestamp: float | None = None,
    log_path: Path | None = None,
    pipeline_logger: PipelineLogger | None = None,
    job_id: str | None = None,
    video_id: str | None = None,
    stage: str | None = None,
    ordinal: int | None = None,
    text_log_streams: bool = True,
) -> list[ExtractedWindowFrame]:
    destination_dir.mkdir(parents=True, exist_ok=True)
    for stale_path in destination_dir.glob(f"{filename_prefix}_*.jpg"):
        stale_path.unlink()

    window_duration = max(0.0, window_end - window_start)
    if window_duration <= 0 or window_duration < probe_step_seconds:
        timestamp = window_start if single_timestamp is None else single_timestamp
        destination = destination_dir / f"{filename_prefix}_001.jpg"
        _extract_window_single_frame(
            video_path,
            timestamp=timestamp,
            destination=destination,
            log_path=log_path,
            pipeline_logger=pipeline_logger,
            job_id=job_id,
            video_id=video_id,
            stage=stage,
            ordinal=ordinal,
            text_log_streams=text_log_streams,
        )
        return [ExtractedWindowFrame(path=destination, timestamp=round(timestamp, 3), offset_seconds=0.0)]

    probe_fps = 1 / probe_step_seconds
    pattern = destination_dir / f"{filename_prefix}_%03d.jpg"
    args = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{window_start:.3f}",
        "-i",
        str(video_path),
        "-t",
        f"{window_duration + 0.001:.3f}",
        "-vf",
        f"fps={probe_fps:.6f}",
        "-q:v",
        "2",
        "-y",
        str(pattern),
    ]
    run_subprocess(
        args,
        error_code="ffmpeg_extract_frame_window_failed",
        log_path=log_path,
        pipeline_logger=pipeline_logger,
        job_id=job_id,
        video_id=video_id,
        stage=stage,
        ordinal=ordinal,
        text_log_streams=text_log_streams,
    )

    frames: list[ExtractedWindowFrame] = []
    for index, path in enumerate(sorted(destination_dir.glob(f"{filename_prefix}_*.jpg"))):
        if not path.exists() or path.stat().st_size == 0:
            continue
        offset_seconds = index * probe_step_seconds
        timestamp = min(window_end, window_start + offset_seconds)
        frames.append(
            ExtractedWindowFrame(
                path=path,
                timestamp=round(timestamp, 3),
                offset_seconds=round(offset_seconds, 3),
            )
        )
    if not frames:
        raise MediaToolError("ffmpeg did not create probe window frames", error_code="frame_window_missing")
    return frames


def _extract_window_single_frame(
    video_path: Path,
    *,
    timestamp: float,
    destination: Path,
    log_path: Path | None,
    pipeline_logger: PipelineLogger | None,
    job_id: str | None,
    video_id: str | None,
    stage: str | None,
    ordinal: int | None,
    text_log_streams: bool,
) -> None:
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
        str(destination),
    ]
    run_subprocess(
        args,
        error_code="ffmpeg_extract_frame_window_failed",
        log_path=log_path,
        pipeline_logger=pipeline_logger,
        job_id=job_id,
        video_id=video_id,
        stage=stage,
        ordinal=ordinal,
        text_log_streams=text_log_streams,
    )
    if not destination.exists() or destination.stat().st_size == 0:
        raise MediaToolError("ffmpeg did not create probe frame image", error_code="frame_window_missing")

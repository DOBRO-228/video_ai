from __future__ import annotations

import functools
import json
import re
import shutil
import tempfile
from pathlib import Path

from style_kb.diagnostics import PipelineLogger
from style_kb.errors import ExternalToolError
from style_kb.utils.process import run_subprocess

MIN_YT_DLP_VERSION = (2025, 11, 12)
SUPPORTED_JS_RUNTIMES = ("deno", "node", "bun", "quickjs")


def _base_args(cookies_from_browser: str | None, remote_components: str | None) -> list[str]:
    args = ["yt-dlp", "--no-playlist", "--no-progress"]
    if cookies_from_browser:
        args.extend(["--cookies-from-browser", cookies_from_browser])
    if remote_components:
        args.extend(["--remote-components", remote_components])
    return args


def _download_diagnostics(remote_components: str | None) -> dict[str, object]:
    return {"yt_dlp_remote_components": remote_components or "disabled"}


def fetch_metadata(
    url: str,
    *,
    log_path: Path,
    cookies_from_browser: str | None,
    remote_components: str | None,
    stdout_artifact: Path | None = None,
    pipeline_logger: PipelineLogger | None = None,
    job_id: str | None = None,
    video_id: str | None = None,
    stage: str | None = None,
    ordinal: int | None = None,
) -> dict:
    version_text = ensure_ytdlp_ready(
        log_path=log_path,
        pipeline_logger=pipeline_logger,
        job_id=job_id,
        video_id=video_id,
        stage=stage,
        ordinal=ordinal,
    )
    args = _base_args(cookies_from_browser, remote_components) + ["--dump-single-json", url]
    completed = run_subprocess(
        args,
        error_code="yt_dlp_metadata_failed",
        log_path=log_path,
        pipeline_logger=pipeline_logger,
        job_id=job_id,
        video_id=video_id,
        stage=stage,
        ordinal=ordinal,
        stdout_limit_bytes=0,
        stdout_artifact=stdout_artifact,
        diagnostics=_download_diagnostics(remote_components),
    )
    _raise_for_remote_components_warning(completed.stderr, remote_components=remote_components)
    metadata = json.loads(completed.stdout)
    metadata["_style_kb_ytdlp_version"] = version_text
    metadata["_style_kb_ytdlp_remote_components"] = remote_components or "disabled"
    return metadata


def download_audio(
    url: str,
    *,
    destination: Path,
    audio_format: str,
    audio_quality: str,
    cookies_from_browser: str | None,
    remote_components: str | None,
    log_path: Path,
    pipeline_logger: PipelineLogger | None = None,
    job_id: str | None = None,
    video_id: str | None = None,
    stage: str | None = None,
    ordinal: int | None = None,
) -> str:
    ensure_ytdlp_ready(
        log_path=log_path,
        pipeline_logger=pipeline_logger,
        job_id=job_id,
        video_id=video_id,
        stage=stage,
        ordinal=ordinal,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        template = temp_dir / "source.%(ext)s"
        args = _base_args(cookies_from_browser, remote_components) + [
            "--extract-audio",
            "--audio-format",
            audio_format,
            "--audio-quality",
            audio_quality,
            "-o",
            str(template),
            url,
        ]
        completed = run_subprocess(
            args,
            error_code="yt_dlp_audio_failed",
            log_path=log_path,
            pipeline_logger=pipeline_logger,
            job_id=job_id,
            video_id=video_id,
            stage=stage,
            ordinal=ordinal,
            diagnostics=_download_diagnostics(remote_components),
        )
        _raise_for_remote_components_warning(completed.stderr, remote_components=remote_components)
        candidates = sorted(temp_dir.glob(f"*.{audio_format}"))
        if not candidates:
            candidates = sorted(temp_dir.glob("*"))
        if not candidates:
            raise FileNotFoundError(f"yt-dlp produced no audio file in {temp_dir}")
        candidates[0].replace(destination)


def download_video_proxy(
    url: str,
    *,
    destination: Path,
    height: int,
    video_format: str,
    cookies_from_browser: str | None,
    remote_components: str | None,
    log_path: Path,
    pipeline_logger: PipelineLogger | None = None,
    job_id: str | None = None,
    video_id: str | None = None,
    stage: str | None = None,
    ordinal: int | None = None,
) -> None:
    ensure_ytdlp_ready(
        log_path=log_path,
        pipeline_logger=pipeline_logger,
        job_id=job_id,
        video_id=video_id,
        stage=stage,
        ordinal=ordinal,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        template = temp_dir / "video.%(ext)s"
        format_selector = _video_proxy_format_selector(height)
        args = _base_args(cookies_from_browser, remote_components) + [
            "-f",
            format_selector,
            "--merge-output-format",
            video_format,
            "-o",
            str(template),
            url,
        ]
        completed = run_subprocess(
            args,
            error_code="yt_dlp_video_failed",
            log_path=log_path,
            pipeline_logger=pipeline_logger,
            job_id=job_id,
            video_id=video_id,
            stage=stage,
            ordinal=ordinal,
            diagnostics=_download_diagnostics(remote_components),
        )
        _raise_for_remote_components_warning(completed.stderr, remote_components=remote_components)
        candidates = sorted(temp_dir.glob(f"*.{video_format}"))
        if not candidates:
            candidates = sorted(temp_dir.glob("*"))
        if not candidates:
            raise FileNotFoundError(f"yt-dlp produced no video file in {temp_dir}")
        candidates[0].replace(destination)


def _video_proxy_format_selector(height: int) -> str:
    h264_mp4_filter = f"[height<={height}][ext=mp4][vcodec^=avc1]"
    generic_mp4_filter = f"[height<={height}][ext=mp4]"
    generic_filter = f"[height<={height}]"
    return (
        f"bestvideo{h264_mp4_filter}+bestaudio[ext=m4a]"
        f"/bestvideo{h264_mp4_filter}"
        f"/best{h264_mp4_filter}"
        f"/bestvideo{generic_mp4_filter}+bestaudio[ext=m4a]"
        f"/bestvideo{generic_mp4_filter}"
        f"/best{generic_mp4_filter}"
        f"/best{generic_filter}"
    )


def _raise_for_remote_components_warning(stderr: str, *, remote_components: str | None) -> None:
    if not remote_components:
        return
    warning_lines = [
        line.strip()
        for line in stderr.splitlines()
        if _is_remote_components_warning(line)
    ]
    if not warning_lines:
        return
    details = "\n".join(warning_lines[:10])
    raise ExternalToolError(
        (
            "yt-dlp reported that YouTube remote components are not active. "
            f"Configured download.remote_components={remote_components!r}, but yt-dlp still emitted remote component warnings."
        ),
        error_code="yt_dlp_remote_components_failed",
        details=details,
    )


def _is_remote_components_warning(line: str) -> bool:
    text = line.casefold()
    return (
        "remote components" in text
        or "remote component" in text
        or "remote-components" in text
        or "challenge solver script" in text
        or "challenge solving failed" in text
    )


def ensure_ytdlp_ready(
    *,
    log_path: Path | None = None,
    pipeline_logger: PipelineLogger | None = None,
    job_id: str | None = None,
    video_id: str | None = None,
    stage: str | None = None,
    ordinal: int | None = None,
) -> None:
    version_text = _yt_dlp_version_text(
        log_path=log_path,
        pipeline_logger=pipeline_logger,
        job_id=job_id,
        video_id=video_id,
        stage=stage,
        ordinal=ordinal,
    )
    version = _parse_version_tuple(version_text)
    if version is None or version < MIN_YT_DLP_VERSION:
        raise ExternalToolError(
            (
                "yt-dlp is too old for reliable YouTube downloads. "
                f"Found {version_text!r}, required >= {_format_version(MIN_YT_DLP_VERSION)}. "
                "Upgrade yt-dlp and retry."
            ),
            error_code="yt_dlp_too_old",
            details=version_text,
        )

    if not any(shutil.which(runtime) for runtime in SUPPORTED_JS_RUNTIMES):
        raise ExternalToolError(
            (
                "No supported JavaScript runtime found for yt-dlp YouTube extraction. "
                f"Install one of: {', '.join(SUPPORTED_JS_RUNTIMES)}. "
                "Deno is the recommended option."
            ),
            error_code="yt_dlp_js_runtime_missing",
        )
    return version_text


def _yt_dlp_version_text(
    *,
    log_path: Path | None = None,
    pipeline_logger: PipelineLogger | None = None,
    job_id: str | None = None,
    video_id: str | None = None,
    stage: str | None = None,
    ordinal: int | None = None,
) -> str:
    if log_path is None and pipeline_logger is None:
        return _cached_ytdlp_version_text()
    completed = run_subprocess(
        ["yt-dlp", "--version"],
        error_code="yt_dlp_version_check_failed",
        log_path=log_path,
        pipeline_logger=pipeline_logger,
        job_id=job_id,
        video_id=video_id,
        stage=stage,
        ordinal=ordinal,
    )
    return completed.stdout.strip()


@functools.cache
def _cached_ytdlp_version_text() -> str:
    completed = run_subprocess(
        ["yt-dlp", "--version"],
        error_code="yt_dlp_version_check_failed",
    )
    return completed.stdout.strip()


def _parse_version_tuple(version_text: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(\d{4})\.(\d{2})\.(\d{2})", version_text)
    if not match:
        return None
    return tuple(int(group) for group in match.groups())


def _format_version(version: tuple[int, int, int]) -> str:
    return f"{version[0]:04d}.{version[1]:02d}.{version[2]:02d}"

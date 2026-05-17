from __future__ import annotations

import functools
import json
import re
import shutil
import tempfile
from pathlib import Path

from style_kb.errors import ExternalToolError
from style_kb.utils.process import run_subprocess

MIN_YT_DLP_VERSION = (2025, 11, 12)
SUPPORTED_JS_RUNTIMES = ("deno", "node", "bun", "quickjs")


def _base_args(cookies_from_browser: str | None) -> list[str]:
    args = ["yt-dlp", "--no-playlist", "--no-progress"]
    if cookies_from_browser:
        args.extend(["--cookies-from-browser", cookies_from_browser])
    return args


def fetch_metadata(url: str, *, log_path: Path, cookies_from_browser: str | None) -> dict:
    ensure_ytdlp_ready()
    args = _base_args(cookies_from_browser) + ["--dump-single-json", url]
    completed = run_subprocess(args, error_code="yt_dlp_metadata_failed", log_path=log_path)
    return json.loads(completed.stdout)


def download_audio(
    url: str,
    *,
    destination: Path,
    audio_format: str,
    audio_quality: str,
    cookies_from_browser: str | None,
    log_path: Path,
) -> None:
    ensure_ytdlp_ready()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        template = temp_dir / "source.%(ext)s"
        args = _base_args(cookies_from_browser) + [
            "--extract-audio",
            "--audio-format",
            audio_format,
            "--audio-quality",
            audio_quality,
            "-o",
            str(template),
            url,
        ]
        run_subprocess(args, error_code="yt_dlp_audio_failed", log_path=log_path)
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
    log_path: Path,
) -> None:
    ensure_ytdlp_ready()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        template = temp_dir / "video.%(ext)s"
        format_selector = (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]"
            f"/best[height<={height}][ext=mp4]/best[height<={height}]"
        )
        args = _base_args(cookies_from_browser) + [
            "-f",
            format_selector,
            "--merge-output-format",
            video_format,
            "-o",
            str(template),
            url,
        ]
        run_subprocess(args, error_code="yt_dlp_video_failed", log_path=log_path)
        candidates = sorted(temp_dir.glob(f"*.{video_format}"))
        if not candidates:
            candidates = sorted(temp_dir.glob("*"))
        if not candidates:
            raise FileNotFoundError(f"yt-dlp produced no video file in {temp_dir}")
        candidates[0].replace(destination)


def ensure_ytdlp_ready() -> None:
    version_text = _yt_dlp_version_text()
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


@functools.cache
def _yt_dlp_version_text() -> str:
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

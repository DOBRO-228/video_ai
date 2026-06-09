from __future__ import annotations

import hashlib
import os
import platform
import socket
import subprocess
from pathlib import Path
from typing import Any

from style_kb.config.models import AppConfig


def run_environment_snapshot(*, config: AppConfig, config_path: Path) -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "config": {
            "path": str(config_path),
            "sha256": _file_sha256(config_path),
            "keep_media": config.project.keep_media,
            "keep_frames": config.project.keep_frames,
        },
        "tools": {
            "yt-dlp": _tool_version(["yt-dlp", "--version"]),
            "ffmpeg": _tool_version(["ffmpeg", "-version"]),
            "ffprobe": _tool_version(["ffprobe", "-version"]),
            "deno": _tool_version(["deno", "--version"]),
        },
        "models": {
            "stt": config.stt.model,
            "vision": config.vision.model,
            "speech_segmentation": config.speech_segmentation.model,
            "speech_segmentation_retry_advisor": config.speech_segmentation.retry_advisor_model,
            "chunking": config.chunking.model,
            "chunking_retry_advisor": config.chunking.retry_advisor_model,
            "style_claims": config.style_claims.model,
            "style_claims_retry_advisor": config.style_claims.retry_advisor_model,
        },
        "providers": {
            "stt": config.stt.provider,
            "vision": config.vision.provider,
            "speech_segmentation": config.speech_segmentation.provider,
            "chunking": config.chunking.provider,
            "style_claims": config.style_claims.provider,
        },
        "environment": {
            "OPENAI_API_KEY_present": bool(os.environ.get("OPENAI_API_KEY")),
            "OPENAI_API_KEY_fingerprint": _secret_fingerprint(os.environ.get("OPENAI_API_KEY")),
            "GEMINI_API_KEY_present": bool(os.environ.get("GEMINI_API_KEY")),
            "GEMINI_API_KEY_fingerprint": _secret_fingerprint(os.environ.get("GEMINI_API_KEY")),
            "SONIOX_API_KEY_present": bool(os.environ.get("SONIOX_API_KEY")),
            "SONIOX_API_KEY_fingerprint": _secret_fingerprint(os.environ.get("SONIOX_API_KEY")),
        },
    }


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _tool_version(args: list[str]) -> str | None:
    output = _tool_output(args)
    if output is None:
        return None
    return output.splitlines()[0].strip() if output.splitlines() else ""


def _tool_output(args: list[str]) -> str | None:
    try:
        completed = subprocess.run(args, capture_output=True, check=False)
    except OSError:
        return None
    output = completed.stdout or completed.stderr
    return output.decode("utf-8", errors="replace").strip()


def _secret_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

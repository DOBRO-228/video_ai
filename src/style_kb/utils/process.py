from __future__ import annotations

import subprocess
from pathlib import Path

from style_kb.errors import ExternalToolError


def run_subprocess(
    args: list[str],
    *,
    cwd: Path | None = None,
    error_code: str,
    log_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    if log_path is not None:
        stage_name = _stage_name_from_log_path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        header_lines = [
            f"stage: {stage_name}",
            f"return_code: {completed.returncode}",
            "command: " + " ".join(args),
        ]
        if completed.returncode != 0:
            header_lines.insert(1, f"error_code: {error_code}")
        log_path.write_text(
            "\n".join(
                header_lines
                + [
                    "",
                    "[stdout]",
                    completed.stdout,
                    "",
                    "[stderr]",
                    completed.stderr,
                ]
            ),
            encoding="utf-8",
        )

    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "subprocess failed"
        raise ExternalToolError(
            message,
            error_code=error_code,
            details=message,
            stage_name=_stage_name_from_log_path(log_path) if log_path is not None else None,
        )

    return completed


def _stage_name_from_log_path(log_path: Path) -> str:
    return log_path.stem.split(".", 1)[0]

from __future__ import annotations


class StyleKbError(Exception):
    """Base application error."""


class JobAlreadyExistsError(StyleKbError):
    """Raised when ingest targets an existing job or job artifact directory."""

    error_code = "job_already_exists"

    def __init__(
        self,
        *,
        job_id: str,
        job_dir: str,
        status: str | None = None,
        existing_entries: list[str] | None = None,
    ) -> None:
        super().__init__(f"job already exists: {job_id}")
        self.job_id = job_id
        self.job_dir = job_dir
        self.status = status
        self.existing_entries = existing_entries or []


class ConfigError(StyleKbError):
    """Raised when config is missing or invalid."""


class ValidationError(StyleKbError):
    """Raised when a user or artifact input is invalid."""


class StageExecutionError(StyleKbError):
    """Raised when a pipeline stage fails."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        details: str | None = None,
        stage_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details = details
        self.stage_name = stage_name

    def with_stage(self, stage_name: str) -> "StageExecutionError":
        self.stage_name = stage_name
        return self

    def __str__(self) -> str:
        if self.stage_name:
            return f"[{self.stage_name}] {self.message}"
        return self.message


class ExternalToolError(StageExecutionError):
    """Raised when yt-dlp, ffmpeg, or ffprobe fails."""


class MediaToolError(StageExecutionError):
    """Raised when media outputs are invalid or inconsistent."""


class ProviderError(StageExecutionError):
    """Raised when Soniox or OpenAI calls fail."""


class MissingApiKeyError(StageExecutionError):
    """Raised when an API key is required for a stage but missing."""


class JobLockError(StyleKbError):
    """Raised when a job is already running."""

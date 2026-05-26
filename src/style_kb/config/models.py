from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    output_dir: str
    keep_media: bool
    keep_frames: bool


class DownloadConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_height: int = Field(gt=0)
    video_format: str
    audio_format: str
    audio_quality: str
    cookies_from_browser: str | None = None


class SttContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    terms: list[str]


class SttConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    mode: str
    language_hints: list[str]
    language_hints_strict: bool
    language_identification: bool
    speaker_diarization: bool
    speaker_role_strategy: str
    context: SttContextConfig


class SceneDetectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detector: str
    min_scene_len_seconds: float = Field(gt=0)
    images_per_scene: int = Field(gt=0)
    extra_sample_every_seconds: int = Field(gt=0)
    fallback_scene_seconds: int = Field(gt=0)


class VisionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    detail: str
    batch_size: int = Field(gt=0)
    presenter_bootstrap_enabled: bool
    presenter_bootstrap_prompt_file: str
    presenter_bootstrap_scene_limit: int = Field(gt=0)
    presenter_bootstrap_max_images: int = Field(gt=0)
    presenter_low_confidence_disables_recurrence: bool
    include_nearby_transcript: bool
    transcript_context_before_seconds: int = Field(ge=0)
    transcript_context_after_seconds: int = Field(ge=0)
    ocr: bool
    prompt_file: str


class SpeechSegmentationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    prompt_file: str
    max_segment_seconds: float = Field(gt=0)
    min_segment_seconds: float = Field(gt=0)
    pause_break_ms: int = Field(gt=0)
    max_segment_words: int = Field(gt=0)


class ChunkingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    target_words: int = Field(gt=0)
    max_words: int = Field(gt=0)
    overlap_seconds: int = Field(ge=0)
    max_scenes_per_chunk: int = Field(gt=0)


class ExportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formats: list[str]


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectConfig
    download: DownloadConfig
    stt: SttConfig
    scene_detection: SceneDetectionConfig
    vision: VisionConfig
    speech_segmentation: SpeechSegmentationConfig
    chunking: ChunkingConfig
    export: ExportConfig

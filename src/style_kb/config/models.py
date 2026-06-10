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
    remote_components: str | None = None


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
    detail: str | None = None
    media_resolution: str | None = None
    thinking_level: str | None = None
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
    retry_advisor_model: str
    retry_advisor_prompt_file: str
    prompt_file: str
    max_segment_seconds: float = Field(gt=0)
    min_segment_seconds: float = Field(gt=0)
    pause_break_ms: int = Field(gt=0)
    max_segment_words: int = Field(gt=0)


class ChunkingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    provider: str
    model: str
    retry_advisor_model: str
    retry_advisor_prompt_file: str
    prompt_file: str
    max_words: int = Field(gt=0)
    max_speech_segments_per_chunk: int = Field(gt=0)
    question_answer_merge_seconds: int = Field(ge=0)
    visual_attach_seconds: int = Field(ge=0)
    max_planner_segments_per_call: int = Field(gt=0)
    planner_context_segments: int = Field(ge=0)
    planner_parallel_requests: int = Field(gt=0)
    max_retries: int = Field(gt=0)
    title_max_chars: int = Field(gt=0)
    boundary_reason_max_chars: int = Field(gt=0)
    notes_max_chars: int = Field(gt=0)
    topic_max_chars: int = Field(gt=0)
    max_topics: int = Field(gt=0)


class StyleClaimsCurateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    provider: str
    model: str
    prompt_file: str
    reasoning_effort: str
    max_retries: int = Field(gt=0)


class StyleClaimsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    retry_advisor_model: str
    retry_advisor_prompt_file: str
    prompt_file: str
    max_claims_per_chunk: int = Field(ge=0)
    max_retries: int = Field(gt=0)
    curate: StyleClaimsCurateConfig


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
    style_claims: StyleClaimsConfig
    export: ExportConfig

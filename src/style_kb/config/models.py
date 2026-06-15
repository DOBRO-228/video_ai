from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

_REASONING_EFFORT_VALUES = {None, "none", "minimal", "low", "medium", "high", "xhigh"}


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


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visual_enabled: bool = False


class OpenAIPromptCacheConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    retention: str | None = None

    @model_validator(mode="after")
    def validate_retention(self):
        if self.retention not in {None, "in_memory", "24h"}:
            raise ValueError("openai.prompt_cache.retention must be null, 'in_memory', or '24h'")
        return self


class OpenAIBatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completion_window: str = "24h"
    poll_interval_seconds: float = Field(default=15, gt=0)
    poll_timeout_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_completion_window(self):
        if self.completion_window != "24h":
            raise ValueError("openai.batch.completion_window must be '24h'")
        return self


class OpenAIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_cache: OpenAIPromptCacheConfig = Field(default_factory=OpenAIPromptCacheConfig)
    batch: OpenAIBatchConfig = Field(default_factory=OpenAIBatchConfig)


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


class FrameQualityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    probe_window_seconds: float = Field(default=0.6, gt=0)
    probe_step_seconds: float = Field(default=0.3, gt=0)
    prefer_planned_timestamp_score_ratio: float = Field(default=0.92, ge=0, le=1)
    drop_low_quality: bool = False
    min_quality_frames_per_scene: int = Field(default=1, gt=0)
    central_region_weight: float = Field(default=1.5, gt=0)
    grid_size: int = Field(default=4, gt=0)
    keep_rejected_probe_files: bool = False


class PaletteBoundaryRefinementConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    sample_step_seconds: float = Field(default=0.25, gt=0)
    min_scene_duration_seconds: float = Field(default=6.0, gt=0)
    max_boundary_shift_seconds: float = Field(default=3.0, gt=0)
    min_segment_seconds: float = Field(default=1.5, gt=0)
    stable_window_seconds: float = Field(default=1.0, gt=0)
    edge_guard_seconds: float = Field(default=0.75, ge=0)
    min_saturation_delta: float = Field(default=30.0, ge=0)
    min_colorfulness_delta: float = Field(default=8.0, ge=0)
    min_histogram_distance: float = Field(default=0.25, gt=0)
    min_confidence: float = Field(default=0.75, ge=0, le=1)


class SceneDetectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detector: str
    min_scene_len_seconds: float = Field(gt=0)
    keyframe_edge_inset_seconds: float = Field(default=0.35, ge=0)
    images_per_scene: int = Field(default=4, gt=0)
    extra_sample_every_seconds: int = Field(default=4, gt=0)
    max_frames_per_scene: int = Field(default=8, gt=0)
    intra_scene_dedup: bool = True
    phash_max_distance: int = Field(default=10, ge=0)
    ssim_confirm: float | None = Field(default=0.82, ge=0, le=1)
    min_frames_per_scene: int = Field(default=1, gt=0)
    fallback_scene_seconds: int = Field(gt=0)
    frame_quality: FrameQualityConfig = Field(default_factory=FrameQualityConfig)
    palette_boundary_refinement: PaletteBoundaryRefinementConfig = Field(default_factory=PaletteBoundaryRefinementConfig)

    @model_validator(mode="after")
    def validate_frame_quality_minimum(self):
        if self.frame_quality.min_quality_frames_per_scene < self.min_frames_per_scene:
            raise ValueError("frame_quality.min_quality_frames_per_scene must be >= min_frames_per_scene")
        if self.palette_boundary_refinement.min_segment_seconds < self.palette_boundary_refinement.stable_window_seconds:
            raise ValueError("palette_boundary_refinement.min_segment_seconds must be >= stable_window_seconds")
        return self


class VisionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    detail: str | None = None
    media_resolution: str | None = None
    thinking_level: str | None = None
    thinking_budget: int | None = Field(default=None, ge=-1, le=24576)
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

    @model_validator(mode="after")
    def validate_thinking_config(self):
        if self.thinking_level is not None and self.thinking_budget is not None:
            raise ValueError("vision.thinking_level and vision.thinking_budget are mutually exclusive")
        provider = self.provider.casefold()
        model = self.model.casefold()
        if provider == "gemini" and model.startswith("gemini-2.5") and self.thinking_level is not None:
            raise ValueError("Gemini 2.5 models use vision.thinking_budget, not vision.thinking_level")
        if provider == "gemini" and model.startswith("gemini-3") and self.thinking_budget is not None:
            raise ValueError("Gemini 3 models use vision.thinking_level, not vision.thinking_budget")
        return self


class SpeechSegmentationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    reasoning_effort: str | None = None
    retry_advisor_model: str
    retry_advisor_reasoning_effort: str | None = None
    retry_advisor_prompt_file: str
    prompt_file: str
    max_segment_seconds: float = Field(gt=0)
    min_segment_seconds: float = Field(gt=0)
    pause_break_ms: int = Field(gt=0)
    max_segment_words: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_reasoning_effort(self):
        if self.reasoning_effort not in _REASONING_EFFORT_VALUES:
            raise ValueError(
                "speech_segmentation.reasoning_effort must be null, 'none', 'minimal', 'low', 'medium', 'high', or 'xhigh'"
            )
        if self.retry_advisor_reasoning_effort not in _REASONING_EFFORT_VALUES:
            raise ValueError(
                "speech_segmentation.retry_advisor_reasoning_effort must be null, 'none', 'minimal', 'low', 'medium', 'high', or 'xhigh'"
            )
        return self


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
    pipeline: PipelineConfig
    openai: OpenAIConfig
    download: DownloadConfig
    stt: SttConfig
    scene_detection: SceneDetectionConfig
    vision: VisionConfig
    speech_segmentation: SpeechSegmentationConfig
    chunking: ChunkingConfig
    style_claims: StyleClaimsConfig
    export: ExportConfig

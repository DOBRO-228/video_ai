from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


StageState = Literal["pending", "running", "completed", "failed", "skipped"]
JobState = Literal["pending", "running", "completed", "failed"]
ConfidenceLevel = Literal["low", "medium", "high"]
PresenterRole = Literal["none", "primary_presenter", "other_person"]
PresenterRelevance = Literal["none", "background", "brief", "primary_example"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(StrictModel):
    type: str
    url: str
    start: float | None = None
    end: float | None = None
    title: str | None = None
    modality: str | None = None
    path: str | None = None


class ProviderSource(StrictModel):
    provider: str
    model: str


class PresenterContext(StrictModel):
    present: bool
    role: PresenterRole
    is_recurring: bool
    relevance: PresenterRelevance
    baseline_summary: str
    scene_deltas: list[str]
    narrative_brief: str
    confidence: ConfidenceLevel


class PresenterProfile(StrictModel):
    has_primary_presenter: bool
    confidence: ConfidenceLevel
    baseline_summary: str
    recurring_visual_markers: list[str]
    notes: str


class TimeBoundModel(StrictModel):
    video_id: str
    start: float
    end: float


class GroundedTimeBoundModel(TimeBoundModel):
    timestamp_url: str
    source_refs: list[SourceRef] = Field(default_factory=list)


class VideoInfo(StrictModel):
    job_id: str
    video_id: str
    url: str
    title: str
    channel: str
    duration: float
    description: str | None = None
    thumbnail_url: str | None = None
    upload_date: str | None = None
    timestamp_url: str


class SpeechToken(TimeBoundModel):
    token_index: int
    text: str
    start_ms: int
    end_ms: int
    speaker: str | None = None
    language: str | None = None


class SpeechSegment(GroundedTimeBoundModel):
    segment_id: str
    start_ms: int
    end_ms: int
    text: str
    token_start_index: int
    token_end_index: int
    tokens_count: int
    speaker: str | None = None
    language: str | None = None
    source: ProviderSource


class Scene(GroundedTimeBoundModel):
    scene_id: str
    index: int
    start_frame: int
    end_frame: int
    duration: float


class FrameRef(GroundedTimeBoundModel):
    scene_id: str
    path: str
    timestamp: float
    frame_role: str


class VisualEvent(GroundedTimeBoundModel):
    visual_event_id: str
    scene_id: str
    frames: list[FrameRef]
    presenter_context: PresenterContext
    visual_summary: str
    observations: list[str]
    interpretations: list[str]
    on_screen_text: list[str]
    items: list[str]
    colors: list[str]
    style_topics: list[str]
    confidence: ConfidenceLevel
    notes: str


class TimelineEvent(GroundedTimeBoundModel):
    event_id: str
    title: str
    channel: str
    presenter_context: PresenterContext
    speech_text: str
    visual_summary: str
    on_screen_text: list[str]
    items: list[str]
    colors: list[str]
    topics: list[str]
    scene_id: str
    speech_segment_ids: list[str]


class Chunk(GroundedTimeBoundModel):
    chunk_id: str
    title: str
    channel: str
    url: str
    presenter_brief: str
    speech_text: str
    visual_text: str
    combined_text: str
    on_screen_text: list[str]
    topics: list[str]
    entities: list[str]
    modality: list[str]
    timeline_event_ids: list[str]


class QualityReport(StrictModel):
    job_id: str
    video_id: str
    stage_counts: dict[str, int]
    durations: dict[str, float]
    coverage: dict[str, float]
    mismatches: dict[str, float]
    warnings: list[str]
    errors: list[str]
    artifacts: dict[str, str]


class Job(StrictModel):
    job_id: str
    video_id: str
    url: str
    status: JobState
    current_stage: str | None = None
    title: str | None = None
    channel: str | None = None
    job_dir: str
    config_path: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    lock_pid: int | None = None
    lock_acquired_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None


class StageStatus(StrictModel):
    job_id: str
    stage_name: str
    ordinal: int
    status: StageState
    attempt: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    input_files: list[str] = Field(default_factory=list)
    output_files: list[str] = Field(default_factory=list)
    remote_refs: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ValueEnum(StrEnum):
    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class StageState(ValueEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobState(ValueEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ConfidenceLevel(ValueEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PresenterRole(ValueEnum):
    NONE = "none"
    PRIMARY_PRESENTER = "primary_presenter"
    OTHER_PERSON = "other_person"


class PresenterRelevance(ValueEnum):
    NONE = "none"
    BACKGROUND = "background"
    BRIEF = "brief"
    PRIMARY_EXAMPLE = "primary_example"


class SpeakerRole(ValueEnum):
    HOST = "host"
    OFFSCREEN_QUESTIONER = "offscreen_questioner"
    UNKNOWN = "unknown"


class ClaimType(ValueEnum):
    RULE = "rule"
    RECOMMENDATION = "recommendation"
    WARNING = "warning"
    DEFINITION = "definition"
    EXAMPLE = "example"
    EXCEPTION = "exception"


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


class SpeakerProfile(StrictModel):
    speaker: str
    role: SpeakerRole
    tokens_count: int
    words_count: int
    speech_seconds: float
    first_start: float | None = None
    last_end: float | None = None


class SpeakerDiarization(StrictModel):
    video_id: str
    provider: str
    model: str
    enabled: bool
    detected_speakers: int
    unassigned_tokens_count: int
    role_strategy: str
    speakers: list[SpeakerProfile]


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
    speaker_role: SpeakerRole | None = None
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
    speaker_role: SpeakerRole | None = None
    language: str | None = None
    source: ProviderSource


class SpeechTurn(GroundedTimeBoundModel):
    text: str
    speaker: str | None = None
    speaker_role: SpeakerRole | None = None
    speech_segment_ids: list[str]


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
    style_topics: list[str]
    confidence: ConfidenceLevel
    notes: str


class TimelineEvent(GroundedTimeBoundModel):
    event_id: str
    title: str
    channel: str
    presenter_context: PresenterContext
    speech_text: str
    speech_turns: list[SpeechTurn]
    visual_summary: str
    on_screen_text: list[str]
    items: list[str]
    topics: list[str]
    scene_id: str
    speech_segment_ids: list[str]


class Chunk(GroundedTimeBoundModel):
    chunk_id: str
    speech_segment_ids: list[str]
    chunk_title: str
    boundary_reason: str
    title: str
    channel: str
    url: str
    presenter_brief: str
    speech_text: str
    dialogue_text: str
    visual_text: str
    combined_text: str
    on_screen_text: list[str]
    topics: list[str]
    entities: list[str]
    modality: list[str]
    speaker_roles: list[SpeakerRole]
    timeline_event_ids: list[str]


class ChunkPlanItem(StrictModel):
    chunk_index: int
    speech_segment_ids: list[str]
    title: str
    boundary_reason: str
    topics: list[str]
    notes: str


class ChunkPlan(StrictModel):
    video_id: str
    provider: str
    model: str
    mode: str
    prompt_file: str
    prompt_sha256: str
    max_words: int
    max_speech_segments_per_chunk: int
    question_answer_merge_seconds: int
    visual_attach_seconds: int
    max_planner_segments_per_call: int
    planner_context_segments: int
    title_max_chars: int
    boundary_reason_max_chars: int
    notes_max_chars: int
    topic_max_chars: int
    max_topics: int
    windows_count: int
    attempts: int
    attempts_per_window: list[int]
    max_attempts_in_any_window: int
    chunks: list[ChunkPlanItem]


class StyleClaim(GroundedTimeBoundModel):
    claim_id: str
    chunk_id: str
    timeline_event_ids: list[str]
    claim_type: ClaimType
    subject: str
    claim: str
    rationale: str
    conditions: list[str]
    applies_to: list[str]
    avoid: list[str]
    prefer: list[str]
    evidence: list[str]
    topics: list[str]
    confidence: ConfidenceLevel
    source: ProviderSource


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

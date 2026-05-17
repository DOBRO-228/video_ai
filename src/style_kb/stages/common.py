from __future__ import annotations

from pathlib import Path

from style_kb.models import (
    Chunk,
    FrameRef,
    Scene,
    SourceRef,
    SpeechSegment,
    SpeechToken,
    TimelineEvent,
    VideoInfo,
    VisualEvent,
)
from style_kb.utils.files import read_json
from style_kb.utils.pydantic_io import read_model, read_models_jsonl
from style_kb.utils.time import build_timestamp_url


def load_video_info(path: Path) -> VideoInfo:
    return read_model(path, VideoInfo)


def load_speech_tokens(path: Path) -> list[SpeechToken]:
    return read_models_jsonl(path, SpeechToken)


def load_speech_segments(path: Path) -> list[SpeechSegment]:
    return read_models_jsonl(path, SpeechSegment)


def load_scenes(path: Path) -> list[Scene]:
    return read_models_jsonl(path, Scene)


def load_frame_refs(path: Path) -> list[FrameRef]:
    return read_models_jsonl(path, FrameRef)


def load_visual_events(path: Path) -> list[VisualEvent]:
    return read_models_jsonl(path, VisualEvent)


def load_timeline_events(path: Path) -> list[TimelineEvent]:
    return read_models_jsonl(path, TimelineEvent)


def load_chunks(path: Path) -> list[Chunk]:
    return read_models_jsonl(path, Chunk)


def youtube_source_ref(video_id: str, start: float, end: float, *, title: str | None = None, modality: str | None = None) -> SourceRef:
    return SourceRef(
        type="youtube",
        url=build_timestamp_url(video_id, start),
        start=start,
        end=end,
        title=title,
        modality=modality,
    )


def relative_artifact_path(root: Path, artifact: Path) -> str:
    return str(artifact.relative_to(root))


def read_payload(path: Path) -> dict:
    return read_json(path)


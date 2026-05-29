from __future__ import annotations


def format_seconds_token(value: float) -> str:
    return f"{int(round(value)):06d}"


def speech_segment_id(video_id: str, start: float, end: float) -> str:
    return f"{video_id}_s_{format_seconds_token(start)}_{format_seconds_token(end)}"


def scene_id(video_id: str, index: int) -> str:
    return f"{video_id}_scene_{index:06d}"


def visual_event_id(video_id: str, start: float, end: float) -> str:
    return f"{video_id}_v_{format_seconds_token(start)}_{format_seconds_token(end)}"


def timeline_event_id(video_id: str, start: float, end: float) -> str:
    return f"{video_id}_e_{format_seconds_token(start)}_{format_seconds_token(end)}"


def chunk_id(video_id: str, start: float, end: float) -> str:
    return f"{video_id}_c_{format_seconds_token(start)}_{format_seconds_token(end)}"


def style_claim_id(video_id: str, index: int) -> str:
    return f"{video_id}_claim_{index:06d}"

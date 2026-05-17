from __future__ import annotations

import math


def seconds_to_ms(value: float) -> int:
    return int(round(value * 1000))


def ms_to_seconds(value: int | float) -> float:
    return round(float(value) / 1000.0, 3)


def build_timestamp_url(video_id: str, start_seconds: float) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&t={math.floor(max(start_seconds, 0.0))}s"


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


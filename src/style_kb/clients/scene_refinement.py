from __future__ import annotations

import subprocess

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import cv2
import numpy as np

from style_kb.config.models import PaletteBoundaryRefinementConfig
from style_kb.errors import ExternalToolError, MediaToolError


@dataclass(frozen=True, slots=True)
class SceneBoundary:
    source_scene_index: int
    start_frame: int
    end_frame: int


@dataclass(frozen=True, slots=True)
class PaletteSample:
    timestamp: float
    mean_saturation: float
    saturation_p90: float
    colorfulness: float
    contrast: float
    edge_density: float
    histogram: np.ndarray


@dataclass(frozen=True, slots=True)
class PaletteBoundaryAdjustmentCandidate:
    timestamp: float
    boundary_frame: int
    confidence: float
    saturation_delta: float
    colorfulness_delta: float
    histogram_distance: float
    next_scene_distance: float | None
    stability_score: float
    left_window: tuple[float, float]
    right_window: tuple[float, float]
    post_right_distance: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class PaletteRefinementResult:
    boundaries: list[SceneBoundary]
    report: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PaletteProfile:
    mean_saturation: float
    colorfulness: float
    histogram: np.ndarray


def refine_scene_boundaries(
    video_path: Path,
    boundaries: list[SceneBoundary],
    *,
    config: PaletteBoundaryRefinementConfig,
    frame_rate: float,
    source_width: int,
    source_height: int,
) -> PaletteRefinementResult:
    target_width, target_height = _target_dimensions(source_width, source_height)
    sorted_boundaries = sorted(boundaries, key=lambda item: (item.start_frame, item.end_frame))
    rejected_reasons: Counter[str] = Counter()
    boundary_adjustments: list[dict[str, Any]] = []
    samples_by_index: dict[int, list[PaletteSample]] = {}
    sampled_scenes_count = 0
    sampled_frames_count = 0
    sampling_elapsed_seconds = 0.0

    if not config.enabled:
        return PaletteRefinementResult(
            boundaries=sorted_boundaries,
            report={
                **_config_report(config),
                "source_scenes_count": len(sorted_boundaries),
                "output_scenes_count": len(sorted_boundaries),
                "boundaries_adjusted": 0,
                "sampled_scenes_count": 0,
                "sampled_frames_count": 0,
                "sampling_elapsed_seconds": 0.0,
                "target_width": target_width,
                "target_height": target_height,
                "rejected_candidates": {},
            },
        )

    for index, boundary in enumerate(sorted_boundaries):
        duration = (boundary.end_frame - boundary.start_frame) / frame_rate
        if duration < config.min_scene_duration_seconds:
            continue
        started = perf_counter()
        samples = sample_palette_frames(
            video_path,
            start_seconds=boundary.start_frame / frame_rate,
            duration_seconds=duration,
            config=config,
            target_width=target_width,
            target_height=target_height,
        )
        sampling_elapsed_seconds += perf_counter() - started
        sampled_scenes_count += 1
        sampled_frames_count += len(samples)
        samples_by_index[index] = samples

    adjusted_frames = [boundary.start_frame for boundary in sorted_boundaries]
    if sorted_boundaries:
        adjusted_frames.append(sorted_boundaries[-1].end_frame)

    for index in range(len(sorted_boundaries) - 1):
        previous = sorted_boundaries[index]
        following = sorted_boundaries[index + 1]
        previous_samples = samples_by_index.get(index, [])
        following_samples = samples_by_index.get(index + 1, [])
        if len(previous_samples) < _min_window_samples(config) * 2:
            rejected_reasons["insufficient_previous_samples"] += 1
            continue
        if len(following_samples) < _min_window_samples(config):
            rejected_reasons["insufficient_next_samples"] += 1
            continue
        candidate = _best_boundary_adjustment_candidate(
            previous_samples,
            following_samples,
            previous,
            following,
            config=config,
            frame_rate=frame_rate,
        )
        rejected_reasons.update(candidate.rejected_reasons)
        if candidate.adjustment is None:
            continue
        adjustment = candidate.adjustment
        if adjustment.boundary_frame <= previous.start_frame or adjustment.boundary_frame >= following.end_frame:
            rejected_reasons["invalid_adjusted_boundary"] += 1
            continue
        adjusted_frames[index + 1] = adjustment.boundary_frame
        boundary_adjustments.append(
            {
                "left_source_scene_index": previous.source_scene_index,
                "right_source_scene_index": following.source_scene_index,
                "old_boundary_timestamp": round(previous.end_frame / frame_rate, 3),
                "new_boundary_timestamp": round(adjustment.timestamp, 3),
                "old_boundary_frame": previous.end_frame,
                "new_boundary_frame": adjustment.boundary_frame,
                "confidence": round(adjustment.confidence, 6),
                "saturation_delta": round(adjustment.saturation_delta, 3),
                "colorfulness_delta": round(adjustment.colorfulness_delta, 3),
                "histogram_distance": round(adjustment.histogram_distance, 6),
                "next_scene_distance": round(adjustment.next_scene_distance, 6)
                if adjustment.next_scene_distance is not None
                else None,
                "stability_score": round(adjustment.stability_score, 6),
                "left_window": [round(adjustment.left_window[0], 3), round(adjustment.left_window[1], 3)],
                "right_window": [round(adjustment.right_window[0], 3), round(adjustment.right_window[1], 3)],
                "reason": adjustment.reason,
            }
        )

    final_boundaries: list[SceneBoundary] = []
    for index, boundary in enumerate(sorted_boundaries):
        start_frame = adjusted_frames[index]
        end_frame = adjusted_frames[index + 1]
        if end_frame <= start_frame:
            rejected_reasons["invalid_final_boundary"] += 1
            continue
        final_boundaries.append(
            SceneBoundary(
                source_scene_index=boundary.source_scene_index,
                start_frame=start_frame,
                end_frame=end_frame,
            )
        )

    final_boundaries = sorted(final_boundaries, key=lambda item: (item.start_frame, item.end_frame))
    return PaletteRefinementResult(
        boundaries=final_boundaries,
        report={
            **_config_report(config),
            "source_scenes_count": len(sorted_boundaries),
            "output_scenes_count": len(final_boundaries),
            "boundaries_adjusted": len(boundary_adjustments),
            "sampled_scenes_count": sampled_scenes_count,
            "sampled_frames_count": sampled_frames_count,
            "sampling_elapsed_seconds": round(sampling_elapsed_seconds, 3),
            "target_width": target_width,
            "target_height": target_height,
            "histogram": {
                "space": "HSV",
                "channels": ["H", "S"],
                "bins": [16, 8],
                "ranges": [[0, 180], [0, 256]],
                "normalization": "l1",
                "dtype": "float32",
                "distance": "bhattacharyya",
            },
            "boundary_adjustments": boundary_adjustments,
            "rejected_candidates": dict(sorted(rejected_reasons.items())),
        },
    )


@dataclass(frozen=True, slots=True)
class _CandidateSearchResult:
    adjustment: PaletteBoundaryAdjustmentCandidate | None
    rejected_reasons: Counter[str]


def sample_palette_frames(
    video_path: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
    config: PaletteBoundaryRefinementConfig,
    target_width: int,
    target_height: int,
) -> list[PaletteSample]:
    frame_size = target_width * target_height * 3
    if frame_size <= 0:
        raise MediaToolError("invalid rawvideo frame size", error_code="palette_sampling_invalid_frame_size")
    probe_fps = 1 / config.sample_step_seconds
    args = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{start_seconds:.3f}",
        "-i",
        str(video_path),
        "-t",
        f"{duration_seconds:.3f}",
        "-vf",
        f"fps={probe_fps:.6f},scale={target_width}:{target_height}",
        "-pix_fmt",
        "bgr24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.stdout is None or process.stderr is None:
        raise ExternalToolError("failed to open ffmpeg pipes", error_code="ffmpeg_palette_sampling_failed")

    samples: list[PaletteSample] = []
    frame_index = 0
    while True:
        payload = process.stdout.read(frame_size)
        if not payload:
            break
        if len(payload) != frame_size:
            process.kill()
            raise MediaToolError(
                "ffmpeg rawvideo output ended mid-frame",
                error_code="palette_sampling_partial_frame",
                details=f"expected {frame_size} bytes, got {len(payload)}",
            )
        frame = np.frombuffer(payload, dtype=np.uint8).reshape((target_height, target_width, 3))
        samples.append(_palette_sample(start_seconds + (frame_index * config.sample_step_seconds), frame))
        frame_index += 1

    stderr = process.stderr.read().decode("utf-8", errors="replace")
    return_code = process.wait()
    if return_code != 0:
        raise ExternalToolError(
            stderr.strip() or "ffmpeg palette sampling failed",
            error_code="ffmpeg_palette_sampling_failed",
            details=stderr,
        )
    return samples


def _best_boundary_adjustment_candidate(
    samples: list[PaletteSample],
    next_samples: list[PaletteSample],
    previous: SceneBoundary,
    following: SceneBoundary,
    *,
    config: PaletteBoundaryRefinementConfig,
    frame_rate: float,
) -> _CandidateSearchResult:
    start = previous.start_frame / frame_rate
    end = previous.end_frame / frame_rate
    search_start = max(start, end - config.max_boundary_shift_seconds)
    min_samples = _min_window_samples(config)
    rejected_reasons: Counter[str] = Counter()
    best: PaletteBoundaryAdjustmentCandidate | None = None
    next_start = following.start_frame / frame_rate
    next_profile_samples = _window(
        next_samples,
        next_start,
        next_start + config.stable_window_seconds,
        include_end=True,
    )
    if len(next_profile_samples) < min_samples:
        return _CandidateSearchResult(adjustment=None, rejected_reasons=Counter({"insufficient_next_profile": 1}))
    next_profile = _profile(next_profile_samples)

    for sample in samples:
        timestamp = sample.timestamp
        if timestamp <= start or timestamp >= end:
            continue
        if timestamp < search_start:
            rejected_reasons["outside_boundary_shift_window"] += 1
            continue
        if timestamp - start < config.edge_guard_seconds or end - timestamp < config.edge_guard_seconds:
            rejected_reasons["edge_guard"] += 1
            continue
        if timestamp - start < config.min_segment_seconds or end - timestamp < config.min_segment_seconds:
            rejected_reasons["min_segment"] += 1
            continue

        left_samples = _window(samples, timestamp - config.stable_window_seconds, timestamp, include_end=False)
        right_samples = _window(samples, timestamp, timestamp + config.stable_window_seconds, include_end=True)
        if len(left_samples) < min_samples or len(right_samples) < min_samples:
            rejected_reasons["insufficient_window_samples"] += 1
            continue

        left = _profile(left_samples)
        right = _profile(right_samples)
        histogram_distance = _histogram_distance(left.histogram, right.histogram)
        if histogram_distance < config.min_histogram_distance:
            rejected_reasons["low_histogram_distance"] += 1
            continue
        next_scene_distance = _histogram_distance(right.histogram, next_profile.histogram)
        if next_scene_distance >= config.min_histogram_distance:
            rejected_reasons["right_profile_not_next_scene"] += 1
            continue
        saturation_delta = abs(left.mean_saturation - right.mean_saturation)
        colorfulness_delta = abs(left.colorfulness - right.colorfulness)
        stability_score = _stability_score(left_samples, right_samples, left, right, config=config)
        confidence = (
            0.65 * _normalized(histogram_distance, config.min_histogram_distance)
            + 0.20 * stability_score
            + 0.15
            * max(
                _normalized(saturation_delta, config.min_saturation_delta),
                _normalized(colorfulness_delta, config.min_colorfulness_delta),
            )
        )
        if confidence < config.min_confidence:
            rejected_reasons["low_confidence"] += 1
            continue

        next_right_samples = _window(
            samples,
            timestamp + config.stable_window_seconds,
            timestamp + (2 * config.stable_window_seconds),
            include_end=True,
        )
        post_right_distance: float | None = None
        if len(next_right_samples) >= min_samples:
            post_right_distance = _histogram_distance(right.histogram, _profile(next_right_samples).histogram)
            if post_right_distance >= config.min_histogram_distance:
                rejected_reasons["unstable_post_right_window"] += 1
                continue

        boundary_frame = int(round(timestamp * frame_rate))
        candidate = PaletteBoundaryAdjustmentCandidate(
            timestamp=timestamp,
            boundary_frame=boundary_frame,
            confidence=confidence,
            saturation_delta=saturation_delta,
            colorfulness_delta=colorfulness_delta,
            histogram_distance=histogram_distance,
            next_scene_distance=next_scene_distance,
            stability_score=stability_score,
            left_window=(timestamp - config.stable_window_seconds, timestamp),
            right_window=(timestamp, timestamp + config.stable_window_seconds),
            post_right_distance=post_right_distance,
            reason="right_profile_matches_next_scene",
        )
        if best is None or candidate.confidence > best.confidence:
            best = candidate

    return _CandidateSearchResult(adjustment=best, rejected_reasons=rejected_reasons)


def _palette_sample(timestamp: float, frame: np.ndarray) -> PaletteSample:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    return PaletteSample(
        timestamp=round(timestamp, 3),
        mean_saturation=float(saturation.mean()),
        saturation_p90=float(np.percentile(saturation, 90)),
        colorfulness=_colorfulness(frame),
        contrast=float(gray.std()),
        edge_density=float(np.count_nonzero(edges) / edges.size) if edges.size else 0.0,
        histogram=_hs_histogram(hsv),
    )


def _profile(samples: list[PaletteSample]) -> _PaletteProfile:
    histogram = np.mean(np.array([sample.histogram for sample in samples], dtype=np.float32), axis=0)
    histogram_sum = float(histogram.sum())
    if histogram_sum > 0:
        histogram = histogram / histogram_sum
    return _PaletteProfile(
        mean_saturation=float(np.median([sample.mean_saturation for sample in samples])),
        colorfulness=float(np.median([sample.colorfulness for sample in samples])),
        histogram=histogram.astype(np.float32),
    )


def _stability_score(
    left_samples: list[PaletteSample],
    right_samples: list[PaletteSample],
    left: _PaletteProfile,
    right: _PaletteProfile,
    *,
    config: PaletteBoundaryRefinementConfig,
) -> float:
    left_distances = [_histogram_distance(sample.histogram, left.histogram) for sample in left_samples]
    right_distances = [_histogram_distance(sample.histogram, right.histogram) for sample in right_samples]
    left_stability = 1 - _normalized(float(np.median(left_distances or [0.0])), config.min_histogram_distance)
    right_stability = 1 - _normalized(float(np.median(right_distances or [0.0])), config.min_histogram_distance)
    return _clamp(min(left_stability, right_stability), 0.0, 1.0)


def _window(samples: list[PaletteSample], start: float, end: float, *, include_end: bool) -> list[PaletteSample]:
    if include_end:
        return [sample for sample in samples if start <= sample.timestamp <= end]
    return [sample for sample in samples if start <= sample.timestamp < end]


def _hs_histogram(hsv: np.ndarray) -> np.ndarray:
    histogram = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256]).astype(np.float32)
    total = float(histogram.sum())
    if total > 0:
        histogram = histogram / total
    return histogram.reshape(-1).astype(np.float32)


def _histogram_distance(left: np.ndarray, right: np.ndarray) -> float:
    return float(cv2.compareHist(left.astype(np.float32), right.astype(np.float32), cv2.HISTCMP_BHATTACHARYYA))


def _colorfulness(bgr: np.ndarray) -> float:
    b, g, r = cv2.split(bgr.astype(np.float64))
    rg = np.abs(r - g)
    yb = np.abs(0.5 * (r + g) - b)
    std_root = np.sqrt(rg.std() ** 2 + yb.std() ** 2)
    mean_root = np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
    return float(std_root + 0.3 * mean_root)


def _target_dimensions(source_width: int, source_height: int) -> tuple[int, int]:
    if source_width <= 0 or source_height <= 0:
        raise MediaToolError("ffprobe output has invalid video dimensions", error_code="ffprobe_dimensions_invalid")
    target_width = min(256, int(source_width))
    target_height = int(round(source_height * target_width / source_width))
    if target_height % 2:
        target_height += 1
    return max(1, target_width), max(2, target_height)


def _min_window_samples(config: PaletteBoundaryRefinementConfig) -> int:
    return max(2, int(round((config.stable_window_seconds / config.sample_step_seconds) / 2)))


def _normalized(value: float, threshold: float) -> float:
    if threshold <= 0:
        return 1.0 if value > 0 else 0.0
    return _clamp(value / threshold, 0.0, 1.0)


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _config_report(config: PaletteBoundaryRefinementConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "sample_step_seconds": config.sample_step_seconds,
        "min_scene_duration_seconds": config.min_scene_duration_seconds,
        "max_boundary_shift_seconds": config.max_boundary_shift_seconds,
        "min_segment_seconds": config.min_segment_seconds,
        "stable_window_seconds": config.stable_window_seconds,
        "edge_guard_seconds": config.edge_guard_seconds,
        "min_saturation_delta": config.min_saturation_delta,
        "min_colorfulness_delta": config.min_colorfulness_delta,
        "min_histogram_distance": config.min_histogram_distance,
        "min_confidence": config.min_confidence,
    }

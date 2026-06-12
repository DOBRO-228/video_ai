from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from style_kb.errors import MediaToolError


@dataclass(frozen=True, slots=True)
class FrameStat:
    key: str
    timestamp: float
    phash: int
    sharpness: float
    gray: np.ndarray | None
    protected: bool


@dataclass(frozen=True, slots=True)
class DedupSkip:
    skipped_key: str
    skipped_timestamp: float
    matched_key: str
    matched_timestamp: float
    phash_distance: int
    ssim: float | None
    skip_reason: str


@dataclass(frozen=True, slots=True)
class FrameQualityMetrics:
    score: float
    global_sharpness: float
    contrast: float
    edge_density: float
    mean_saturation: float
    saturation_p90: float
    colorfulness: float
    tile_sharpness_p10: float
    tile_sharpness_p50: float
    tile_sharpness_p90: float
    blurred_tile_ratio: float
    central_blurred_tile_ratio: float
    flags: tuple[str, ...]


def load_gray(path: Path, max_dim: int = 256) -> np.ndarray:
    gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise MediaToolError(f"frame image is unreadable: {path}", error_code="frame_unreadable")
    return _resize_max_dim(gray, max_dim)


def load_bgr(path: Path, max_dim: int = 256) -> np.ndarray:
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise MediaToolError(f"frame image is unreadable: {path}", error_code="frame_unreadable")
    return _resize_max_dim(bgr, max_dim)


def _resize_max_dim(image: np.ndarray, max_dim: int) -> np.ndarray:
    height, width = image.shape[:2]
    largest = max(height, width)
    if largest <= max_dim:
        return image
    scale = max_dim / largest
    target = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(image, target, interpolation=cv2.INTER_AREA)


def _saturation_stats(bgr: np.ndarray | None) -> tuple[float, float]:
    if bgr is None:
        return 0.0, 0.0
    saturation = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[:, :, 1]
    return float(saturation.mean()), float(np.percentile(saturation, 90))


def _colorfulness(bgr: np.ndarray | None) -> float:
    if bgr is None:
        return 0.0
    b, g, r = cv2.split(bgr.astype(np.float64))
    rg = np.abs(r - g)
    yb = np.abs(0.5 * (r + g) - b)
    std_root = np.sqrt(rg.std() ** 2 + yb.std() ** 2)
    mean_root = np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
    return float(std_root + 0.3 * mean_root)


def phash(gray: np.ndarray) -> int:
    resized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    coefficients = cv2.dct(resized)[:8, :8].reshape(-1)
    median = float(np.median(coefficients[1:]))
    value = 0
    for bit_index, coefficient in enumerate(coefficients):
        if coefficient > median:
            value |= 1 << bit_index
    return value


def hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def sharpness(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def contrast(gray: np.ndarray) -> float:
    return float(gray.std())


def edge_density(gray: np.ndarray) -> float:
    edges = cv2.Canny(gray, 50, 150)
    return float(np.count_nonzero(edges) / edges.size) if edges.size else 0.0


def tile_sharpness(gray: np.ndarray, grid_size: int) -> list[float]:
    height, width = gray.shape[:2]
    scores: list[float] = []
    for row in range(grid_size):
        y0 = round(row * height / grid_size)
        y1 = round((row + 1) * height / grid_size)
        for col in range(grid_size):
            x0 = round(col * width / grid_size)
            x1 = round((col + 1) * width / grid_size)
            tile = gray[y0:y1, x0:x1]
            scores.append(sharpness(tile) if tile.size else 0.0)
    return scores


def central_tile_indices(grid_size: int) -> set[int]:
    if grid_size <= 1:
        return {0}
    lower = (grid_size - 1) / 2
    upper = grid_size / 2
    indices: set[int] = set()
    for row in range(grid_size):
        for col in range(grid_size):
            if lower - 0.5 <= row <= upper and lower - 0.5 <= col <= upper:
                indices.add(row * grid_size + col)
    return indices


def frame_quality(
    gray: np.ndarray,
    *,
    grid_size: int,
    central_region_weight: float,
    bgr: np.ndarray | None = None,
) -> FrameQualityMetrics:
    global_sharpness = sharpness(gray)
    contrast_score = contrast(gray)
    edge_score = edge_density(gray)
    mean_saturation, saturation_p90 = _saturation_stats(bgr)
    colorfulness_score = _colorfulness(bgr)
    tile_scores = tile_sharpness(gray, grid_size)
    central_indices = central_tile_indices(grid_size)
    blurred_threshold = 20.0
    blurred_tile_ratio = _ratio(score < blurred_threshold for score in tile_scores)
    central_scores = [score for index, score in enumerate(tile_scores) if index in central_indices]
    central_blurred_tile_ratio = _ratio(score < blurred_threshold for score in central_scores)

    flags: list[str] = []
    if global_sharpness < 35:
        flags.append("low_global_sharpness")
    if contrast_score < 18:
        flags.append("low_contrast")
    if edge_score < 0.035:
        flags.append("low_edge_density")
    if bgr is not None and mean_saturation < 18 and saturation_p90 < 32:
        flags.append("low_saturation")
    if blurred_tile_ratio >= 0.65:
        flags.append("high_blurred_tile_ratio")
    if central_blurred_tile_ratio >= 0.5:
        flags.append("high_central_blurred_tile_ratio")
    if {"low_global_sharpness", "low_contrast"}.issubset(flags) or {
        "low_global_sharpness",
        "low_edge_density",
    }.issubset(flags):
        flags.append("transition_like")
    if "high_central_blurred_tile_ratio" in flags and "low_global_sharpness" not in flags:
        flags.append("partial_blur_like")
    if "low_saturation" in flags and contrast_score < 30 and edge_score < 0.06:
        flags.append("gray_transition_like")

    score = (
        0.55 * _normalized_log(global_sharpness, 180)
        + 0.20 * _normalized_log(contrast_score, 48)
        + 0.15 * _normalized_linear(edge_score, 0.16)
        - 0.10 * blurred_tile_ratio
        - (0.20 * central_region_weight / 1.5) * central_blurred_tile_ratio
        - (0.18 if "gray_transition_like" in flags else 0.0)
    )
    percentiles = np.percentile(np.array(tile_scores or [0.0], dtype=np.float64), [10, 50, 90])
    return FrameQualityMetrics(
        score=max(0.0, round(float(score), 6)),
        global_sharpness=global_sharpness,
        contrast=contrast_score,
        edge_density=edge_score,
        mean_saturation=mean_saturation,
        saturation_p90=saturation_p90,
        colorfulness=colorfulness_score,
        tile_sharpness_p10=float(percentiles[0]),
        tile_sharpness_p50=float(percentiles[1]),
        tile_sharpness_p90=float(percentiles[2]),
        blurred_tile_ratio=blurred_tile_ratio,
        central_blurred_tile_ratio=central_blurred_tile_ratio,
        flags=tuple(flags),
    )


def ssim(left: np.ndarray, right: np.ndarray) -> float:
    left_float = left.astype(np.float64)
    if left.shape != right.shape:
        right = cv2.resize(right, (left.shape[1], left.shape[0]), interpolation=cv2.INTER_AREA)
    right_float = right.astype(np.float64)

    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2

    mu_left = cv2.GaussianBlur(left_float, (11, 11), 1.5)
    mu_right = cv2.GaussianBlur(right_float, (11, 11), 1.5)
    mu_left_sq = mu_left * mu_left
    mu_right_sq = mu_right * mu_right
    mu_left_right = mu_left * mu_right

    sigma_left_sq = cv2.GaussianBlur(left_float * left_float, (11, 11), 1.5) - mu_left_sq
    sigma_right_sq = cv2.GaussianBlur(right_float * right_float, (11, 11), 1.5) - mu_right_sq
    sigma_left_right = cv2.GaussianBlur(left_float * right_float, (11, 11), 1.5) - mu_left_right

    numerator = (2 * mu_left_right + c1) * (2 * sigma_left_right + c2)
    denominator = (mu_left_sq + mu_right_sq + c1) * (sigma_left_sq + sigma_right_sq + c2)
    return float(np.mean(numerator / denominator))


def dedupe_frames(
    stats: list[FrameStat],
    *,
    phash_max_distance: int,
    ssim_confirm: float | None,
    min_frames: int,
) -> tuple[list[str], list[DedupSkip]]:
    kept: list[FrameStat] = []
    skipped: list[tuple[FrameStat, DedupSkip]] = []

    for candidate in sorted(stats, key=lambda stat: stat.timestamp):
        if candidate.protected:
            kept.append(candidate)
            continue
        duplicate = _find_duplicate(candidate, kept, phash_max_distance=phash_max_distance, ssim_confirm=ssim_confirm)
        if duplicate is None:
            kept.append(candidate)
            continue
        matched, phash_distance, ssim_score = duplicate
        skipped.append(
            (
                candidate,
                DedupSkip(
                    skipped_key=candidate.key,
                    skipped_timestamp=candidate.timestamp,
                    matched_key=matched.key,
                    matched_timestamp=matched.timestamp,
                    phash_distance=phash_distance,
                    ssim=ssim_score,
                    skip_reason="duplicate",
                ),
            )
        )

    if len(kept) < min_frames and skipped:
        top_up = sorted((item[0] for item in skipped), key=lambda stat: stat.sharpness, reverse=True)
        restored = {stat.key for stat in top_up[: max(0, min_frames - len(kept))]}
        kept.extend(stat for stat in top_up if stat.key in restored)
        skipped = [item for item in skipped if item[0].key not in restored]

    kept_keys = [stat.key for stat in sorted(kept, key=lambda item: item.timestamp)]
    return kept_keys, [item[1] for item in skipped]


def _find_duplicate(
    candidate: FrameStat,
    kept: list[FrameStat],
    *,
    phash_max_distance: int,
    ssim_confirm: float | None,
) -> tuple[FrameStat, int, float | None] | None:
    for existing in kept:
        distance = hamming(candidate.phash, existing.phash)
        if distance > phash_max_distance:
            continue
        if ssim_confirm is None:
            return existing, distance, None
        if candidate.gray is None or existing.gray is None:
            continue
        score = ssim(candidate.gray, existing.gray)
        if score >= ssim_confirm:
            return existing, distance, score
    return None


def _ratio(values) -> float:
    items = list(values)
    return float(sum(1 for item in items if item) / len(items)) if items else 0.0


def _normalized_log(value: float, reference: float) -> float:
    return min(1.0, float(np.log1p(max(0.0, value)) / np.log1p(reference)))


def _normalized_linear(value: float, reference: float) -> float:
    return min(1.0, max(0.0, value) / reference)

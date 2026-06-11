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


def load_gray(path: Path, max_dim: int = 256) -> np.ndarray:
    gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise MediaToolError(f"frame image is unreadable: {path}", error_code="frame_unreadable")
    height, width = gray.shape[:2]
    largest = max(height, width)
    if largest <= max_dim:
        return gray
    scale = max_dim / largest
    target = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(gray, target, interpolation=cv2.INTER_AREA)


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

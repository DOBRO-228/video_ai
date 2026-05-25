from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from style_kb.clients.openai_segmenter import OpenAISegmenterClient
from style_kb.errors import ProviderError
from style_kb.models import ProviderSource, SpeechSegment, SpeechToken
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import load_speech_segments, load_speech_tokens, read_payload, youtube_source_ref
from style_kb.utils.files import append_text
from style_kb.utils.ids import speech_segment_id
from style_kb.utils.pydantic_io import write_models_jsonl
from style_kb.utils.time import build_timestamp_url

_DURATION_EPSILON_SECONDS = 0.25
_SENTENCE_END_CHARS = frozenset({".", "!", "?", "…"})
_STRONG_CLAUSE_END_CHARS = frozenset({";", ":"})
_TRAILING_WRAPPER_CHARS = frozenset({'"', "'", "»", "”", ")", "]", "}"})
_UNIT_MAX_SECONDS = 12.0
_UNIT_MAX_WORDS = 40
_UNIT_CLAUSE_MIN_SECONDS = 8.0
_UNIT_CLAUSE_MIN_WORDS = 24
_SEGMENTATION_MAX_ATTEMPTS = 3
_SOFT_MAX_SEMANTIC_SECONDS = 40.0
_SOFT_MAX_SEMANTIC_WORDS = 90
_SOFT_MAX_SEMANTIC_SENTENCES = 4
_NON_TERMINAL_END_CHARS = frozenset({",", ";", ":"})
_CONTINUATION_START_WORDS = frozenset(
    {
        "а",
        "но",
        "и",
        "или",
        "либо",
        "потому",
        "поэтому",
        "соответственно",
        "также",
        "то",
        "пожалуйста",
    }
)


@dataclass(slots=True)
class _TranscriptUnit:
    unit_index: int
    token_start_position: int
    token_end_position: int
    start: float
    end: float
    start_ms: int
    end_ms: int
    text: str
    word_count: int


class Stage07BuildSpeechSegments(Stage):
    name = "07_build_speech_segments"
    ordinal = 7

    def input_files(self, context: StageContext) -> list:
        return [context.paths.stt_speech_tokens, _prompt_path(context)]

    def output_files(self, context: StageContext) -> list:
        return [context.paths.stt_speech_segments, context.paths.stt_speech_segments_raw]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.stt_speech_segments.exists() or not context.paths.stt_speech_segments_raw.exists():
            return False
        try:
            read_payload(context.paths.stt_speech_segments_raw)
        except Exception:
            return False

        segments = load_speech_segments(context.paths.stt_speech_segments)
        tokens = load_speech_tokens(context.paths.stt_speech_tokens)
        if not segments or not tokens:
            return False

        token_positions = {token.token_index: index for index, token in enumerate(tokens)}
        max_duration = context.config.speech_segmentation.max_segment_seconds
        min_duration = context.config.speech_segmentation.min_segment_seconds
        max_words = context.config.speech_segmentation.max_segment_words
        previous_end_position = -1

        if any(segment.source.provider != context.config.speech_segmentation.provider for segment in segments):
            return False
        if any(segment.source.model != context.config.speech_segmentation.model for segment in segments):
            return False
        if any(segment.start_ms > segment.end_ms for segment in segments):
            return False
        if any(current.start_ms < previous.start_ms for previous, current in zip(segments, segments[1:])):
            return False

        for segment in segments:
            duration = segment.end - segment.start
            if duration > max_duration + _DURATION_EPSILON_SECONDS:
                return False
            if duration < min_duration - _DURATION_EPSILON_SECONDS:
                return False

            start_position = token_positions.get(segment.token_start_index)
            end_position = token_positions.get(segment.token_end_index)
            if start_position is None or end_position is None or start_position > end_position:
                return False
            if start_position != previous_end_position + 1:
                return False

            group = tokens[start_position : end_position + 1]
            if not group:
                return False
            if _is_whitespace_token(group[0]):
                return False
            if segment.start_ms != group[0].start_ms or segment.end_ms != group[-1].end_ms:
                return False
            if segment.text != _join_tokens(group):
                return False
            if _word_count_from_tokens(group) > max_words:
                return False
            if end_position + 1 < len(tokens) and _is_whitespace_token(tokens[end_position + 1]):
                return False
            previous_end_position = end_position

        if previous_end_position != len(tokens) - 1:
            return False
        return not _semantic_boundary_violations(segments)

    def run(self, context: StageContext) -> StageResult:
        tokens = load_speech_tokens(context.paths.stt_speech_tokens)
        units = _build_transcript_units(tokens, context)
        if not units:
            raise ProviderError("semantic transcript units are empty", error_code="semantic_units_empty")

        base_prompt_text = _prompt_path(context).read_text(encoding="utf-8")
        client = OpenAISegmenterClient(
            os.environ.get("OPENAI_API_KEY"),
            model=context.config.speech_segmentation.model,
        )
        segments, attempts_used = _segment_with_retries(
            context=context,
            client=client,
            base_prompt_text=base_prompt_text,
            tokens=tokens,
            units=units,
        )
        write_models_jsonl(context.paths.stt_speech_segments, segments)
        return StageResult(
            output_files=self.output_files(context),
            metrics={
                "segments_count": len(segments),
                "semantic_units_count": len(units),
                "segmentation_attempts": attempts_used,
            },
        )


def _segment_with_retries(
    *,
    context: StageContext,
    client: OpenAISegmenterClient,
    base_prompt_text: str,
    tokens: list[SpeechToken],
    units: list[_TranscriptUnit],
) -> tuple[list[SpeechSegment], int]:
    previous_violations: list[str] = []
    for attempt in range(1, _SEGMENTATION_MAX_ATTEMPTS + 1):
        payload = client.segment_transcript(
            system_prompt=_build_prompt(base_prompt_text, previous_violations, attempt),
            transcript_text=_join_tokens(tokens),
            units_payload=[_unit_payload(unit) for unit in units],
            constraints_payload={
                "provider": context.config.speech_segmentation.provider,
                "model": context.config.speech_segmentation.model,
                "min_segment_seconds": context.config.speech_segmentation.min_segment_seconds,
                "max_segment_seconds": context.config.speech_segmentation.max_segment_seconds,
                "max_segment_words": context.config.speech_segmentation.max_segment_words,
                "preferred_max_segment_seconds": _SOFT_MAX_SEMANTIC_SECONDS,
                "units_count": len(units),
                "retry_feedback": previous_violations,
            },
            raw_output_path=context.paths.stt_speech_segments_raw,
        )
        segments = _segments_from_payload(payload, units, tokens, context)
        violations = _semantic_boundary_violations(segments)
        if not violations:
            return segments, attempt
        previous_violations = violations
        _log_retry_attempt(context, attempt=attempt, violations=violations)
        if context.progress_callback is not None and attempt < _SEGMENTATION_MAX_ATTEMPTS:
            context.progress_callback(
                f"[07 {Stage07BuildSpeechSegments.name}] retry {attempt + 1}/{_SEGMENTATION_MAX_ATTEMPTS} semantic-boundary-violations={len(violations)}"
            )
    raise ProviderError(
        "OpenAI speech segmentation returned poor semantic boundaries after retries",
        error_code="openai_segmenter_semantic_boundary_failed",
        details="\n".join(previous_violations[:12]),
    )


def _segments_from_payload(
    payload: dict,
    units: list[_TranscriptUnit],
    tokens: list[SpeechToken],
    context: StageContext,
) -> list[SpeechSegment]:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ProviderError(
            "OpenAI speech segmentation returned no segments",
            error_code="openai_segmenter_empty_segments",
        )

    provider = ProviderSource(
        provider=context.config.speech_segmentation.provider,
        model=context.config.speech_segmentation.model,
    )
    max_duration = context.config.speech_segmentation.max_segment_seconds
    min_duration = context.config.speech_segmentation.min_segment_seconds
    max_words = context.config.speech_segmentation.max_segment_words
    expected_start = 1
    results: list[SpeechSegment] = []

    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            raise ProviderError(
                "OpenAI speech segmentation returned an invalid segment payload",
                error_code="openai_segmenter_invalid_segment",
            )
        unit_start_index = int(raw_segment.get("unit_start_index") or 0)
        unit_end_index = int(raw_segment.get("unit_end_index") or 0)
        if unit_start_index != expected_start:
            raise ProviderError(
                "OpenAI speech segmentation returned non-contiguous unit ranges",
                error_code="openai_segmenter_non_contiguous_ranges",
            )
        if unit_end_index < unit_start_index or unit_end_index > len(units):
            raise ProviderError(
                "OpenAI speech segmentation returned an out-of-range unit index",
                error_code="openai_segmenter_unit_range_invalid",
            )

        start_unit = units[unit_start_index - 1]
        end_unit = units[unit_end_index - 1]
        group = tokens[start_unit.token_start_position : end_unit.token_end_position + 1]
        if not group:
            raise ProviderError(
                "OpenAI speech segmentation returned an empty token group",
                error_code="openai_segmenter_empty_group",
            )

        duration = group[-1].end - group[0].start
        words_count = _word_count_from_tokens(group)
        if duration > max_duration + _DURATION_EPSILON_SECONDS:
            raise ProviderError(
                f"OpenAI speech segmentation exceeded max_segment_seconds for range {unit_start_index}-{unit_end_index}",
                error_code="openai_segmenter_duration_exceeded",
            )
        if duration < min_duration - _DURATION_EPSILON_SECONDS:
            raise ProviderError(
                f"OpenAI speech segmentation violated min_segment_seconds for range {unit_start_index}-{unit_end_index}",
                error_code="openai_segmenter_duration_too_short",
            )
        if words_count > max_words:
            raise ProviderError(
                f"OpenAI speech segmentation exceeded max_segment_words for range {unit_start_index}-{unit_end_index}",
                error_code="openai_segmenter_words_exceeded",
            )

        text = _join_tokens(group)
        languages = [token.language for token in group if token.language]
        language = Counter(languages).most_common(1)[0][0] if languages else None
        speakers = [token.speaker for token in group if token.speaker]
        speaker = Counter(speakers).most_common(1)[0][0] if speakers else None
        start = group[0].start
        end = group[-1].end
        results.append(
            SpeechSegment(
                segment_id=speech_segment_id(context.job.video_id, start, end),
                video_id=context.job.video_id,
                start=start,
                end=end,
                start_ms=group[0].start_ms,
                end_ms=group[-1].end_ms,
                text=text,
                token_start_index=group[0].token_index,
                token_end_index=group[-1].token_index,
                tokens_count=len(group),
                speaker=speaker,
                language=language,
                timestamp_url=build_timestamp_url(context.job.video_id, start),
                source=provider,
                source_refs=[youtube_source_ref(context.job.video_id, start, end, modality="audio")],
            )
        )
        expected_start = unit_end_index + 1

    if expected_start != len(units) + 1:
        raise ProviderError(
            "OpenAI speech segmentation did not cover all transcript units",
            error_code="openai_segmenter_incomplete_coverage",
        )
    return results


def _build_prompt(base_prompt_text: str, violations: list[str], attempt: int) -> str:
    if attempt == 1 or not violations:
        return base_prompt_text
    feedback_lines = [
        "",
        "Нарушения предыдущей попытки, которые нужно исправить:",
        *[f"- {violation}" for violation in violations[:12]],
        "",
        "Сделай новое разбиение. Не повторяй эти ошибки. Если граница выглядит сомнительно, объединяй continuation с предыдущим сегментом или сдвигай границу к ближайшему завершённому предложению.",
    ]
    return base_prompt_text.rstrip() + "\n" + "\n".join(feedback_lines)


def _semantic_boundary_violations(segments: list[SpeechSegment]) -> list[str]:
    violations: list[str] = []
    for index, segment in enumerate(segments, start=1):
        duration = segment.end - segment.start
        word_count = len(segment.text.split())
        sentence_count = _sentence_boundary_count(segment.text)
        if (
            duration > _SOFT_MAX_SEMANTIC_SECONDS
            and word_count >= _SOFT_MAX_SEMANTIC_WORDS
            and sentence_count >= _SOFT_MAX_SEMANTIC_SENTENCES
        ):
            violations.append(
                f"segment {index} is semantically too dense: duration={duration:.2f}s words={word_count} sentences={sentence_count}"
            )

    for index, (previous, current) in enumerate(zip(segments, segments[1:]), start=1):
        previous_terminal = _terminal_char(previous.text)
        current_first_word = _first_word(current.text)
        if previous_terminal in _NON_TERMINAL_END_CHARS:
            violations.append(
                f"boundary {index}->{index + 1} ends on non-terminal punctuation {previous_terminal!r}: "
                f"prev_tail={previous.text[-60:]!r} next_head={current.text[:60]!r}"
            )
        if _starts_with_lowercase(current.text) and previous_terminal not in _SENTENCE_END_CHARS:
            violations.append(
                f"boundary {index}->{index + 1} starts with lowercase continuation: "
                f"prev_tail={previous.text[-60:]!r} next_head={current.text[:60]!r}"
            )
        if current_first_word in _CONTINUATION_START_WORDS and previous_terminal not in _SENTENCE_END_CHARS:
            violations.append(
                f"boundary {index}->{index + 1} starts with continuation word {current_first_word!r}: "
                f"prev_tail={previous.text[-60:]!r} next_head={current.text[:60]!r}"
            )
    return violations


def _log_retry_attempt(context: StageContext, *, attempt: int, violations: list[str]) -> None:
    lines = [
        "",
        "[semantic-retry]",
        f"attempt: {attempt}",
        f"violations_count: {len(violations)}",
        "violations:",
        *[f"  - {violation}" for violation in violations[:20]],
        "",
    ]
    append_text(context.paths.stage_log(Stage07BuildSpeechSegments.name), "\n".join(lines), encoding="utf-8")


def _build_transcript_units(tokens: list[SpeechToken], context: StageContext) -> list[_TranscriptUnit]:
    content_positions = [position for position, token in enumerate(tokens) if not _is_whitespace_token(token)]
    if not content_positions:
        return []

    units: list[_TranscriptUnit] = []
    start_content_index = 0
    current_words = 0

    for content_index, position in enumerate(content_positions):
        previous_position = content_positions[content_index - 1] if content_index > start_content_index else None
        if _content_token_starts_word(tokens, position, previous_position):
            current_words += 1
        next_content_position = content_positions[content_index + 1] if content_index + 1 < len(content_positions) else None
        if not _should_break_unit(
            tokens=tokens,
            start_position=content_positions[start_content_index],
            end_position=position,
            next_content_position=next_content_position,
            current_words=current_words,
            context=context,
        ):
            continue
        unit_end_position = _extend_through_trailing_whitespace(tokens, position, next_content_position)
        unit_start_position = content_positions[start_content_index]
        unit_tokens = tokens[unit_start_position : unit_end_position + 1]
        units.append(
            _TranscriptUnit(
                unit_index=len(units) + 1,
                token_start_position=unit_start_position,
                token_end_position=unit_end_position,
                start=unit_tokens[0].start,
                end=unit_tokens[-1].end,
                start_ms=unit_tokens[0].start_ms,
                end_ms=unit_tokens[-1].end_ms,
                text=_join_tokens(unit_tokens),
                word_count=current_words,
            )
        )
        start_content_index = content_index + 1
        current_words = 0
    return units


def _should_break_unit(
    *,
    tokens: list[SpeechToken],
    start_position: int,
    end_position: int,
    next_content_position: int | None,
    current_words: int,
    context: StageContext,
) -> bool:
    current_token = tokens[end_position]
    duration = current_token.end - tokens[start_position].start
    terminal_char = _terminal_char(current_token.text)

    if next_content_position is None:
        return True

    next_content_token = tokens[next_content_position]
    gap_ms = max(0, next_content_token.start_ms - current_token.end_ms)
    if gap_ms >= context.config.speech_segmentation.pause_break_ms:
        return True
    if terminal_char in _SENTENCE_END_CHARS:
        return True
    if terminal_char in _STRONG_CLAUSE_END_CHARS and (
        duration >= _UNIT_CLAUSE_MIN_SECONDS or current_words >= _UNIT_CLAUSE_MIN_WORDS
    ):
        return True
    if duration >= _UNIT_MAX_SECONDS:
        return True
    if current_words >= _UNIT_MAX_WORDS:
        return True
    return False


def _prompt_path(context: StageContext) -> Path:
    return Path(__file__).resolve().parents[1] / "prompts" / context.config.speech_segmentation.prompt_file


def _unit_payload(unit: _TranscriptUnit) -> dict[str, object]:
    return {
        "unit_index": unit.unit_index,
        "start": unit.start,
        "end": unit.end,
        "duration": round(unit.end - unit.start, 3),
        "word_count": unit.word_count,
        "text": unit.text,
    }


def _join_tokens(tokens: list[SpeechToken]) -> str:
    text = "".join(token.text for token in tokens)
    text = re.sub(r"\s+", " ", text, flags=re.UNICODE)
    return text.strip()


def _word_count_from_tokens(tokens: list[SpeechToken]) -> int:
    count = 0
    previous_content_position: int | None = None
    for position, token in enumerate(tokens):
        if _is_whitespace_token(token):
            continue
        if _content_token_starts_word(tokens, position, previous_content_position):
            count += 1
        previous_content_position = position
    return count


def _starts_new_word(token: SpeechToken) -> bool:
    return bool(token.text[:1].isspace())


def _token_starts_word(token: SpeechToken, *, is_first: bool) -> bool:
    stripped = token.text.strip()
    if not stripped:
        return False
    return is_first or _starts_new_word(token)


def _is_whitespace_token(token: SpeechToken) -> bool:
    return not token.text.strip()


def _content_token_starts_word(
    tokens: list[SpeechToken],
    position: int,
    previous_content_position: int | None,
) -> bool:
    token = tokens[position]
    if _is_whitespace_token(token):
        return False
    if previous_content_position is None:
        return True
    if _starts_new_word(token):
        return True
    return any(_is_whitespace_token(item) for item in tokens[previous_content_position + 1 : position])


def _extend_through_trailing_whitespace(
    tokens: list[SpeechToken],
    end_position: int,
    next_content_position: int | None,
) -> int:
    if next_content_position is None:
        return len(tokens) - 1
    trailing_end = next_content_position - 1
    for position in range(end_position + 1, next_content_position):
        if not _is_whitespace_token(tokens[position]):
            return position - 1
    return trailing_end


def _terminal_char(text: str) -> str | None:
    stripped = text.strip()
    while stripped and stripped[-1] in _TRAILING_WRAPPER_CHARS:
        stripped = stripped[:-1].rstrip()
    return stripped[-1] if stripped else None


def _sentence_boundary_count(text: str) -> int:
    return len(re.findall(r"[.!?…]+", text))


def _starts_with_lowercase(text: str) -> bool:
    stripped = text.lstrip()
    return bool(stripped) and stripped[0].isalpha() and stripped[0].islower()


def _first_word(text: str) -> str:
    match = re.search(r"[A-Za-zА-Яа-яЁё-]+", text)
    if match is None:
        return ""
    return match.group(0).strip("-").lower()

from __future__ import annotations

from collections import Counter

from style_kb.models import ProviderSource, SpeechSegment, SpeechToken
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import load_speech_segments, load_speech_tokens, youtube_source_ref
from style_kb.utils.ids import speech_segment_id
from style_kb.utils.pydantic_io import write_models_jsonl
from style_kb.utils.text import word_count
from style_kb.utils.time import build_timestamp_url


class Stage07BuildSpeechSegments(Stage):
    name = "07_build_speech_segments"
    ordinal = 7

    def input_files(self, context: StageContext) -> list:
        return [context.paths.stt_speech_tokens]

    def output_files(self, context: StageContext) -> list:
        return [context.paths.stt_speech_segments]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.stt_speech_segments.exists():
            return False
        return bool(load_speech_segments(context.paths.stt_speech_segments))

    def run(self, context: StageContext) -> StageResult:
        tokens = load_speech_tokens(context.paths.stt_speech_tokens)
        segments = _segment_tokens(tokens, context)
        write_models_jsonl(context.paths.stt_speech_segments, segments)
        return StageResult(output_files=self.output_files(context), metrics={"segments_count": len(segments)})


def _segment_tokens(tokens: list[SpeechToken], context: StageContext) -> list[SpeechSegment]:
    if not tokens:
        return []

    groups: list[list[SpeechToken]] = []
    current: list[SpeechToken] = [tokens[0]]
    max_duration = context.config.speech_segmentation.max_segment_seconds
    min_duration = context.config.speech_segmentation.min_segment_seconds
    pause_break_ms = context.config.speech_segmentation.pause_break_ms
    max_words = context.config.speech_segmentation.max_segment_words

    for token in tokens[1:]:
        current_duration = current[-1].end - current[0].start
        current_words = sum(1 for item in current if item.text.strip())
        gap_ms = max(0, token.start_ms - current[-1].end_ms)
        should_split = (
            current_duration >= max_duration
            or current_words >= max_words
            or (gap_ms >= pause_break_ms and current_duration >= min_duration)
        )
        if should_split:
            groups.append(current)
            current = [token]
        else:
            current.append(token)
    if current:
        groups.append(current)

    merged_groups: list[list[SpeechToken]] = []
    for group in groups:
        duration = group[-1].end - group[0].start
        if duration >= min_duration or not merged_groups:
            merged_groups.append(group)
            continue
        merged_groups[-1].extend(group)

    if len(merged_groups) > 1:
        last_group = merged_groups[-1]
        if last_group[-1].end - last_group[0].start < min_duration:
            merged_groups[-2].extend(last_group)
            merged_groups.pop()

    provider = ProviderSource(provider=context.config.stt.provider, model=context.config.stt.model)
    results: list[SpeechSegment] = []
    for group in merged_groups:
        start = group[0].start
        end = group[-1].end
        text = _join_tokens(group)
        languages = [token.language for token in group if token.language]
        language = Counter(languages).most_common(1)[0][0] if languages else None
        speakers = [token.speaker for token in group if token.speaker]
        speaker = Counter(speakers).most_common(1)[0][0] if speakers else None
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
    return results


def _join_tokens(tokens: list[SpeechToken]) -> str:
    text = ""
    punctuation = {".", ",", "!", "?", ";", ":", ")", "]", "}"}
    for token in tokens:
        part = token.text.strip()
        if not part:
            continue
        if not text:
            text = part
            continue
        if part in punctuation:
            text += part
        else:
            text += " " + part
    return text.strip()

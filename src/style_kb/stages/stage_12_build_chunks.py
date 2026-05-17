from __future__ import annotations

from style_kb.errors import StageExecutionError
from style_kb.models import Chunk
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import load_timeline_events, youtube_source_ref
from style_kb.utils.collections import stable_unique
from style_kb.utils.ids import chunk_id
from style_kb.utils.pydantic_io import write_models_jsonl
from style_kb.utils.text import compact_join, word_count
from style_kb.utils.time import build_timestamp_url


class Stage12BuildChunks(Stage):
    name = "12_build_chunks"
    ordinal = 12

    def input_files(self, context: StageContext) -> list:
        return [context.paths.timeline_events_jsonl]

    def output_files(self, context: StageContext) -> list:
        return [context.paths.chunks_jsonl]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.chunks_jsonl.exists():
            return False
        return bool(load_chunks(context.paths.chunks_jsonl))

    def run(self, context: StageContext) -> StageResult:
        events = load_timeline_events(context.paths.timeline_events_jsonl)
        if not events:
            raise StageExecutionError("cannot build chunks from empty timeline", error_code="empty_timeline")

        chunks: list[Chunk] = []
        index = 0
        while index < len(events):
            chunk_events = _collect_chunk_events(events, index, context)
            start = chunk_events[0].start
            end = chunk_events[-1].end
            speech_text = " ".join(event.speech_text for event in chunk_events if event.speech_text).strip()
            visual_text = compact_join(
                [
                    " ".join(event.visual_summary for event in chunk_events if event.visual_summary).strip(),
                    "\n".join(text for event in chunk_events for text in event.on_screen_text).strip(),
                ]
            )
            combined_text = compact_join([speech_text, visual_text])
            topics = stable_unique(topic for event in chunk_events for topic in event.topics)
            entities = stable_unique(item for event in chunk_events for item in event.items)
            on_screen_text = stable_unique(text for event in chunk_events for text in event.on_screen_text)
            modality = []
            if speech_text:
                modality.append("audio")
            if visual_text or on_screen_text:
                modality.append("visual")
            chunks.append(
                Chunk(
                    chunk_id=chunk_id(context.job.video_id, start, end),
                    video_id=context.job.video_id,
                    title=chunk_events[0].title,
                    channel=chunk_events[0].channel,
                    url=context.job.url,
                    start=start,
                    end=end,
                    timestamp_url=build_timestamp_url(context.job.video_id, start),
                    speech_text=speech_text,
                    visual_text=visual_text,
                    combined_text=combined_text,
                    on_screen_text=on_screen_text,
                    topics=topics,
                    entities=entities,
                    modality=modality,
                    timeline_event_ids=[event.event_id for event in chunk_events],
                    source_refs=[youtube_source_ref(context.job.video_id, start, end, title=chunk_events[0].title)],
                )
            )
            index = _next_chunk_index(events, index, chunk_events[-1], context)

        write_models_jsonl(context.paths.chunks_jsonl, chunks)
        return StageResult(output_files=self.output_files(context), metrics={"chunks_count": len(chunks)})


def _collect_chunk_events(events, start_index: int, context: StageContext):
    selected = [events[start_index]]
    max_words = context.config.chunking.max_words
    target_words = context.config.chunking.target_words
    max_scenes = context.config.chunking.max_scenes_per_chunk
    current_index = start_index + 1
    while current_index < len(events):
        if len(selected) >= max_scenes:
            break
        candidate = selected + [events[current_index]]
        candidate_words = word_count(" ".join(event.speech_text + " " + event.visual_summary for event in candidate))
        if candidate_words > max_words:
            break
        selected.append(events[current_index])
        current_index += 1
        if candidate_words >= target_words:
            break
    return selected


def _next_chunk_index(events, current_start_index: int, last_event, context: StageContext) -> int:
    next_index = current_start_index + 1
    if context.config.chunking.overlap_seconds <= 0:
        return len(events) if last_event == events[-1] else events.index(last_event) + 1

    chunk_end = last_event.end
    overlap_cutoff = chunk_end - context.config.chunking.overlap_seconds
    end_index = events.index(last_event)
    next_index = end_index + 1
    for candidate_index in range(current_start_index + 1, end_index + 1):
        if events[candidate_index].end > overlap_cutoff:
            next_index = candidate_index
            break
    if next_index <= current_start_index:
        next_index = current_start_index + 1
    return next_index

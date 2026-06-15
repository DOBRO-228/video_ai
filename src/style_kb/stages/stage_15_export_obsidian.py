from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from style_kb.export.obsidian import render_obsidian_export
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import (
    effective_style_claims_path,
    load_chunks,
    load_effective_style_claims,
    load_frame_refs,
    load_timeline_events,
    load_video_info,
)


class Stage15ExportObsidian(Stage):
    name = "15_export_obsidian"
    ordinal = 15

    def input_files(self, context: StageContext) -> list:
        inputs = [
            context.paths.metadata_video_info,
            context.paths.timeline_events_jsonl,
            context.paths.chunks_jsonl,
            effective_style_claims_path(context),
        ]
        if context.config.pipeline.visual_enabled:
            inputs.append(context.paths.frame_refs_jsonl)
        return inputs

    def output_files(self, context: StageContext) -> list:
        outputs = [context.paths.obsidian_index]
        outputs.extend(sorted((context.paths.export_obsidian_dir / "videos").glob("*.md")))
        outputs.extend(sorted((context.paths.export_obsidian_dir / "chunks").glob("*.md")))
        return outputs

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.obsidian_index.exists():
            return False
        video_note = context.paths.obsidian_video_note(context.job.video_id)
        if not video_note.exists():
            return False
        expected_chunk_notes = load_chunks(context.paths.chunks_jsonl) if context.paths.chunks_jsonl.exists() else []
        return all(context.paths.obsidian_chunk_note(chunk.chunk_id).exists() for chunk in expected_chunk_notes)

    def run(self, context: StageContext) -> StageResult:
        video = load_video_info(context.paths.metadata_video_info)
        timeline_events = load_timeline_events(context.paths.timeline_events_jsonl)
        chunks = load_chunks(context.paths.chunks_jsonl)
        style_claims = load_effective_style_claims(context)
        frame_refs = load_frame_refs(context.paths.frame_refs_jsonl) if context.config.pipeline.visual_enabled else []
        frame_map: dict[str, list[str]] = defaultdict(list)
        video_note_path = context.paths.obsidian_video_note(context.job.video_id)

        if context.config.pipeline.visual_enabled and context.config.project.keep_frames:
            for frame in frame_refs:
                frame_path = context.paths.job_dir / frame.path
                relative_link = frame_path.relative_to(context.paths.job_dir).as_posix()
                frame_map[frame.scene_id].append(f"../../../{relative_link}")

        outputs = render_obsidian_export(
            templates_dir=Path(__file__).resolve().parents[1] / "templates",
            index_title=context.config.project.name,
            video=video,
            timeline_events=timeline_events,
            chunks=chunks,
            style_claims=style_claims,
            obsidian_index_path=context.paths.obsidian_index,
            video_note_path=video_note_path,
            chunk_note_paths={chunk.chunk_id: context.paths.obsidian_chunk_note(chunk.chunk_id) for chunk in chunks},
            event_frame_links=frame_map,
        )
        return StageResult(output_files=outputs, metrics={"notes_count": len(outputs)})

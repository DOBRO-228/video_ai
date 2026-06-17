from __future__ import annotations

from style_kb.export.claim_surfaces import is_stage_owned_obsidian_chunk_note, render_obsidian_claim_surface
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import (
    effective_style_claims_path,
    load_chunks,
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
        return _expected_outputs(context)

    def validate_outputs(self, context: StageContext) -> bool:
        expected_outputs = _expected_outputs(context)
        if not expected_outputs or not all(path.exists() for path in expected_outputs):
            return False
        if _extra_chunk_note_paths(context):
            return False
        return True

    def run(self, context: StageContext) -> StageResult:
        outputs, removed_stale_notes = render_obsidian_claim_surface(paths=context.paths, config=context.config)
        return StageResult(
            output_files=outputs,
            metrics={
                "notes_count": len(outputs),
                "removed_stale_chunk_notes": removed_stale_notes,
            },
        )


def _expected_outputs(context: StageContext) -> list:
    if not context.paths.metadata_video_info.exists() or not context.paths.chunks_jsonl.exists():
        return []
    video = load_video_info(context.paths.metadata_video_info)
    chunks = load_chunks(context.paths.chunks_jsonl)
    outputs = [
        context.paths.obsidian_index,
        context.paths.obsidian_video_note(video.video_id),
    ]
    outputs.extend(context.paths.obsidian_chunk_note(chunk.chunk_id) for chunk in chunks)
    return outputs


def _extra_chunk_note_paths(context: StageContext) -> list:
    if not context.paths.chunks_jsonl.exists():
        return []
    chunks_dir = context.paths.export_obsidian_dir / "chunks"
    if not chunks_dir.exists():
        return []
    expected_chunk_ids = {chunk.chunk_id for chunk in load_chunks(context.paths.chunks_jsonl)}
    expected_names = {f"{chunk_id}.md" for chunk_id in expected_chunk_ids}
    return [
        path
        for path in chunks_dir.glob("*.md")
        if path.name not in expected_names and is_stage_owned_obsidian_chunk_note(context.paths, path)
    ]

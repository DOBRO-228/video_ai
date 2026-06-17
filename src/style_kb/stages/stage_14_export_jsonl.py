from __future__ import annotations

from style_kb.export.jsonl import export_jsonl_bundle, manifest_claim_metadata_matches
from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.stages.common import effective_style_claims_path, jsonl_rows_equal
from style_kb.utils.files import read_json


class Stage14ExportJsonl(Stage):
    name = "14_export_jsonl"
    ordinal = 14

    def input_files(self, context: StageContext) -> list:
        inputs = [
            context.paths.metadata_video_info,
            context.paths.stt_speaker_diarization,
            context.paths.stt_speech_tokens,
            context.paths.stt_speech_segments,
            context.paths.timeline_events_jsonl,
            context.paths.chunks_jsonl,
            context.paths.chunk_plan,
            effective_style_claims_path(context),
        ]
        if context.config.pipeline.visual_enabled:
            inputs[4:4] = [
                context.paths.scenes_jsonl,
                context.paths.frame_refs_jsonl,
                context.paths.visual_events_jsonl,
            ]
        return inputs

    def output_files(self, context: StageContext) -> list:
        return _expected_outputs(context)

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.config.pipeline.visual_enabled and any(path.exists() for path in _disabled_visual_outputs(context)):
            return False
        if not all(path.exists() for path in _expected_outputs(context)):
            return False
        exported_claims = context.paths.export_jsonl("style_claims.jsonl")
        effective_claims = effective_style_claims_path(context)
        return jsonl_rows_equal(exported_claims, effective_claims) and _manifest_matches_config(context)

    def run(self, context: StageContext) -> StageResult:
        removed_stale_visual_exports = _remove_disabled_visual_outputs(context)
        outputs = export_jsonl_bundle(
            video_info_path=context.paths.metadata_video_info,
            speaker_diarization_path=context.paths.stt_speaker_diarization,
            speech_tokens_path=context.paths.stt_speech_tokens,
            speech_segments_path=context.paths.stt_speech_segments,
            scenes_path=context.paths.scenes_jsonl if context.config.pipeline.visual_enabled else None,
            frame_refs_path=context.paths.frame_refs_jsonl if context.config.pipeline.visual_enabled else None,
            visual_events_path=context.paths.visual_events_jsonl if context.config.pipeline.visual_enabled else None,
            timeline_events_path=context.paths.timeline_events_jsonl,
            chunks_path=context.paths.chunks_jsonl,
            chunk_plan_path=context.paths.chunk_plan,
            style_claims_path=effective_style_claims_path(context),
            export_dir=context.paths.export_jsonl_dir,
            visual_enabled=context.config.pipeline.visual_enabled,
            job_dir=context.paths.job_dir,
        )
        return StageResult(
            output_files=outputs,
            metrics={
                "exported_files": len(outputs),
                "removed_stale_visual_exports": removed_stale_visual_exports,
            },
        )


def _expected_outputs(context: StageContext) -> list:
    outputs = [
        context.paths.export_jsonl("video_info.jsonl"),
        context.paths.export_jsonl("speaker_diarization.jsonl"),
        context.paths.export_jsonl("speech_tokens.jsonl"),
        context.paths.export_jsonl("speech_segments.jsonl"),
        context.paths.export_jsonl("timeline_events.jsonl"),
        context.paths.export_jsonl("chunks.jsonl"),
        context.paths.export_jsonl("chunk_plan.jsonl"),
        context.paths.export_jsonl("style_claims.jsonl"),
        context.paths.export_jsonl("manifest.json"),
    ]
    if context.config.pipeline.visual_enabled:
        outputs[4:4] = [
            context.paths.export_jsonl("scenes.jsonl"),
            context.paths.export_jsonl("frame_refs.jsonl"),
            context.paths.export_jsonl("visual_events.jsonl"),
        ]
    return outputs


def _disabled_visual_outputs(context: StageContext) -> list:
    if context.config.pipeline.visual_enabled:
        return []
    return [
        context.paths.export_jsonl("scenes.jsonl"),
        context.paths.export_jsonl("frame_refs.jsonl"),
        context.paths.export_jsonl("visual_events.jsonl"),
    ]


def _remove_disabled_visual_outputs(context: StageContext) -> int:
    removed = 0
    for path in _disabled_visual_outputs(context):
        if path.exists():
            path.unlink()
            removed += 1
    return removed


def _manifest_matches_config(context: StageContext) -> bool:
    try:
        manifest = read_json(context.paths.export_jsonl("manifest.json"))
    except Exception:
        return False
    if not isinstance(manifest, dict):
        return False
    if manifest.get("visual_enabled") != context.config.pipeline.visual_enabled:
        return False
    filenames = {
        item.get("filename")
        for item in manifest.get("files", [])
        if isinstance(item, dict)
    }
    visual_filenames = {"scenes.jsonl", "frame_refs.jsonl", "visual_events.jsonl"}
    if context.config.pipeline.visual_enabled:
        visual_config_matches = visual_filenames.issubset(filenames)
    else:
        visual_config_matches = filenames.isdisjoint(visual_filenames)
    if not visual_config_matches:
        return False
    return manifest_claim_metadata_matches(
        manifest,
        effective_style_claims_path(context),
        job_dir=context.paths.job_dir,
    )

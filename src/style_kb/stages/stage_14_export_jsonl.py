from __future__ import annotations

from style_kb.export.jsonl import export_jsonl_bundle
from style_kb.pipeline.base import Stage, StageContext, StageResult


class Stage14ExportJsonl(Stage):
    name = "14_export_jsonl"
    ordinal = 14

    def input_files(self, context: StageContext) -> list:
        return [
            context.paths.metadata_video_info,
            context.paths.stt_speaker_diarization,
            context.paths.stt_speech_tokens,
            context.paths.stt_speech_segments,
            context.paths.scenes_jsonl,
            context.paths.frame_refs_jsonl,
            context.paths.visual_events_jsonl,
            context.paths.timeline_events_jsonl,
            context.paths.chunks_jsonl,
            context.paths.chunk_plan,
            context.paths.style_claims_jsonl,
        ]

    def output_files(self, context: StageContext) -> list:
        return _expected_outputs(context)

    def validate_outputs(self, context: StageContext) -> bool:
        return all(path.exists() for path in _expected_outputs(context))

    def run(self, context: StageContext) -> StageResult:
        outputs = export_jsonl_bundle(
            video_info_path=context.paths.metadata_video_info,
            speaker_diarization_path=context.paths.stt_speaker_diarization,
            speech_tokens_path=context.paths.stt_speech_tokens,
            speech_segments_path=context.paths.stt_speech_segments,
            scenes_path=context.paths.scenes_jsonl,
            frame_refs_path=context.paths.frame_refs_jsonl,
            visual_events_path=context.paths.visual_events_jsonl,
            timeline_events_path=context.paths.timeline_events_jsonl,
            chunks_path=context.paths.chunks_jsonl,
            chunk_plan_path=context.paths.chunk_plan,
            style_claims_path=context.paths.style_claims_jsonl,
            export_dir=context.paths.export_jsonl_dir,
        )
        return StageResult(output_files=outputs, metrics={"exported_files": len(outputs)})


def _expected_outputs(context: StageContext) -> list:
    return [
        context.paths.export_jsonl("video_info.jsonl"),
        context.paths.export_jsonl("speaker_diarization.jsonl"),
        context.paths.export_jsonl("speech_tokens.jsonl"),
        context.paths.export_jsonl("speech_segments.jsonl"),
        context.paths.export_jsonl("scenes.jsonl"),
        context.paths.export_jsonl("frame_refs.jsonl"),
        context.paths.export_jsonl("visual_events.jsonl"),
        context.paths.export_jsonl("timeline_events.jsonl"),
        context.paths.export_jsonl("chunks.jsonl"),
        context.paths.export_jsonl("chunk_plan.jsonl"),
        context.paths.export_jsonl("style_claims.jsonl"),
        context.paths.export_jsonl("manifest.json"),
    ]

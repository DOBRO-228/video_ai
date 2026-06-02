from __future__ import annotations

from pathlib import Path

from style_kb.utils.files import copy_file_atomic, read_json, write_json_atomic, write_jsonl_atomic


def export_jsonl_bundle(
    *,
    video_info_path: Path,
    speaker_diarization_path: Path,
    speech_tokens_path: Path,
    speech_segments_path: Path,
    scenes_path: Path,
    frame_refs_path: Path,
    visual_events_path: Path,
    timeline_events_path: Path,
    chunks_path: Path,
    chunk_plan_path: Path,
    style_claims_path: Path,
    export_dir: Path,
) -> list[Path]:
    export_dir.mkdir(parents=True, exist_ok=True)
    outputs = [
        export_dir / "video_info.jsonl",
        export_dir / "speaker_diarization.jsonl",
        export_dir / "speech_tokens.jsonl",
        export_dir / "speech_segments.jsonl",
        export_dir / "scenes.jsonl",
        export_dir / "frame_refs.jsonl",
        export_dir / "visual_events.jsonl",
        export_dir / "timeline_events.jsonl",
        export_dir / "chunks.jsonl",
        export_dir / "chunk_plan.jsonl",
        export_dir / "style_claims.jsonl",
        export_dir / "manifest.json",
    ]
    write_jsonl_atomic(outputs[0], [read_json(video_info_path)])
    write_jsonl_atomic(outputs[1], [read_json(speaker_diarization_path)])
    copy_file_atomic(speech_tokens_path, outputs[2])
    copy_file_atomic(speech_segments_path, outputs[3])
    copy_file_atomic(scenes_path, outputs[4])
    copy_file_atomic(frame_refs_path, outputs[5])
    copy_file_atomic(visual_events_path, outputs[6])
    copy_file_atomic(timeline_events_path, outputs[7])
    copy_file_atomic(chunks_path, outputs[8])
    write_jsonl_atomic(outputs[9], [read_json(chunk_plan_path)])
    copy_file_atomic(style_claims_path, outputs[10])
    write_json_atomic(outputs[11], _export_manifest())
    return outputs


def _export_manifest() -> dict:
    return {
        "schema_version": 1,
        "source_of_truth": "jsonl",
        "kb_import_allowlist": ["chunks.jsonl", "style_claims.jsonl"],
        "files": [
            _manifest_entry("video_info.jsonl", role="audit"),
            _manifest_entry("speaker_diarization.jsonl", role="audit"),
            _manifest_entry("speech_tokens.jsonl", role="audit"),
            _manifest_entry("speech_segments.jsonl", role="audit"),
            _manifest_entry("scenes.jsonl", role="audit"),
            _manifest_entry("frame_refs.jsonl", role="audit"),
            _manifest_entry("visual_events.jsonl", role="audit"),
            _manifest_entry("timeline_events.jsonl", role="audit"),
            _manifest_entry("chunks.jsonl", role="knowledge", kb_import=True),
            _manifest_entry("chunk_plan.jsonl", role="audit"),
            _manifest_entry("style_claims.jsonl", role="knowledge", kb_import=True),
        ],
    }


def _manifest_entry(filename: str, *, role: str, kb_import: bool = False) -> dict:
    return {
        "filename": filename,
        "role": role,
        "kb_import": kb_import,
        "rag": kb_import,
    }

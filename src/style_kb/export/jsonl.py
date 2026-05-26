from __future__ import annotations

from pathlib import Path

from style_kb.utils.files import copy_file_atomic, read_json, write_jsonl_atomic


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
    return outputs

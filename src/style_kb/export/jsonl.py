from __future__ import annotations

from pathlib import Path

from style_kb.stages.common import effective_style_claims_fingerprint
from style_kb.utils.files import copy_file_atomic, read_json, read_jsonl, write_json_atomic, write_jsonl_atomic


def export_jsonl_bundle(
    *,
    video_info_path: Path,
    speaker_diarization_path: Path,
    speech_tokens_path: Path,
    speech_segments_path: Path,
    scenes_path: Path | None,
    frame_refs_path: Path | None,
    visual_events_path: Path | None,
    timeline_events_path: Path,
    chunks_path: Path,
    chunk_plan_path: Path,
    style_claims_path: Path,
    export_dir: Path,
    visual_enabled: bool,
    job_dir: Path | None = None,
) -> list[Path]:
    export_dir.mkdir(parents=True, exist_ok=True)
    outputs = [
        export_dir / "video_info.jsonl",
        export_dir / "speaker_diarization.jsonl",
        export_dir / "speech_tokens.jsonl",
        export_dir / "speech_segments.jsonl",
        export_dir / "timeline_events.jsonl",
        export_dir / "chunks.jsonl",
        export_dir / "chunk_plan.jsonl",
        export_dir / "style_claims.jsonl",
        export_dir / "manifest.json",
    ]
    if visual_enabled:
        outputs[4:4] = [
            export_dir / "scenes.jsonl",
            export_dir / "frame_refs.jsonl",
            export_dir / "visual_events.jsonl",
        ]
    write_jsonl_atomic(outputs[0], [read_json(video_info_path)])
    write_jsonl_atomic(outputs[1], [read_json(speaker_diarization_path)])
    copy_file_atomic(speech_tokens_path, outputs[2])
    copy_file_atomic(speech_segments_path, outputs[3])
    next_index = 4
    if visual_enabled:
        if scenes_path is None or frame_refs_path is None or visual_events_path is None:
            raise ValueError("visual export paths are required when visual_enabled=true")
        copy_file_atomic(scenes_path, outputs[next_index])
        copy_file_atomic(frame_refs_path, outputs[next_index + 1])
        copy_file_atomic(visual_events_path, outputs[next_index + 2])
        next_index += 3
    copy_file_atomic(timeline_events_path, outputs[next_index])
    copy_file_atomic(chunks_path, outputs[next_index + 1])
    write_jsonl_atomic(outputs[next_index + 2], [read_json(chunk_plan_path)])
    write_style_claims_export(export_dir, read_jsonl(style_claims_path))
    write_json_atomic(
        outputs[next_index + 4],
        _export_manifest(
            visual_enabled=visual_enabled,
            style_claims_metadata=style_claims_manifest_metadata(style_claims_path, job_dir=job_dir),
        ),
    )
    return outputs


def write_style_claims_export(export_dir: Path, rows: list[dict]) -> Path:
    output_path = export_dir / "style_claims.jsonl"
    write_jsonl_atomic(output_path, rows)
    return output_path


def patch_style_claims_manifest_metadata(
    *,
    manifest_path: Path,
    style_claims_path: Path,
    job_dir: Path,
) -> dict:
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError(f"manifest must be a JSON object: {manifest_path}")
    patched = {
        **manifest,
        **style_claims_manifest_metadata(style_claims_path, job_dir=job_dir),
    }
    write_json_atomic(manifest_path, patched)
    return patched


def style_claims_manifest_metadata(style_claims_path: Path, *, job_dir: Path | None) -> dict:
    return {
        "style_claims_source": _relative_source_path(style_claims_path, job_dir=job_dir),
        "style_claims_sha256": effective_style_claims_fingerprint(style_claims_path),
        "style_claims_count": len(read_jsonl(style_claims_path)),
    }


def manifest_claim_metadata_matches(manifest: dict, style_claims_path: Path, *, job_dir: Path | None) -> bool:
    try:
        expected = style_claims_manifest_metadata(style_claims_path, job_dir=job_dir)
    except Exception:
        return False
    return all(manifest.get(key) == value for key, value in expected.items())


def _relative_source_path(style_claims_path: Path, *, job_dir: Path | None) -> str:
    if job_dir is None:
        return style_claims_path.as_posix()
    try:
        return style_claims_path.relative_to(job_dir).as_posix()
    except ValueError:
        return style_claims_path.as_posix()


def _export_manifest(*, visual_enabled: bool, style_claims_metadata: dict | None = None) -> dict:
    files = [
        _manifest_entry("video_info.jsonl", role="audit"),
        _manifest_entry("speaker_diarization.jsonl", role="audit"),
        _manifest_entry("speech_tokens.jsonl", role="audit"),
        _manifest_entry("speech_segments.jsonl", role="audit"),
    ]
    if visual_enabled:
        files.extend(
            [
                _manifest_entry("scenes.jsonl", role="audit"),
                _manifest_entry("frame_refs.jsonl", role="audit"),
                _manifest_entry("visual_events.jsonl", role="audit"),
            ]
        )
    files.extend(
        [
            _manifest_entry("timeline_events.jsonl", role="audit"),
            _manifest_entry("chunks.jsonl", role="knowledge", kb_import=True),
            _manifest_entry("chunk_plan.jsonl", role="audit"),
            _manifest_entry("style_claims.jsonl", role="knowledge", kb_import=True),
        ]
    )
    manifest = {
        "schema_version": 1,
        "source_of_truth": "jsonl",
        "visual_enabled": visual_enabled,
        "kb_import_allowlist": ["chunks.jsonl", "style_claims.jsonl"],
        "files": files,
    }
    if style_claims_metadata:
        manifest.update(style_claims_metadata)
    return manifest


def _manifest_entry(filename: str, *, role: str, kb_import: bool = False) -> dict:
    return {
        "filename": filename,
        "role": role,
        "kb_import": kb_import,
        "rag": kb_import,
    }

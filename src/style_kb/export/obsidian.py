from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from style_kb.models import Chunk, StyleClaim, TimelineEvent, VideoInfo
from style_kb.utils.files import write_text_atomic


def render_obsidian_export(
    *,
    templates_dir: Path,
    index_title: str,
    video: VideoInfo,
    timeline_events: list[TimelineEvent],
    chunks: list[Chunk],
    style_claims: list[StyleClaim],
    obsidian_index_path: Path,
    video_note_path: Path,
    chunk_note_paths: dict[str, Path],
    event_frame_links: dict[str, list[str]],
) -> list[Path]:
    env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=False, trim_blocks=True, lstrip_blocks=True)
    index_template = env.get_template("index.md.j2")
    video_template = env.get_template("video.md.j2")
    chunk_template = env.get_template("chunk.md.j2")
    claims_by_chunk: dict[str, list[StyleClaim]] = {}
    for claim in style_claims:
        claims_by_chunk.setdefault(claim.chunk_id, []).append(claim)

    index_rendered = index_template.render(title=index_title, video=video, chunks=chunks)
    write_text_atomic(obsidian_index_path, index_rendered.rstrip() + "\n")

    video_rendered = video_template.render(
        video=video,
        timeline_events=timeline_events,
        style_claims=style_claims,
        event_frame_links=event_frame_links,
    )
    write_text_atomic(video_note_path, video_rendered.rstrip() + "\n")

    outputs = [obsidian_index_path, video_note_path]
    for chunk in chunks:
        path = chunk_note_paths[chunk.chunk_id]
        rendered = chunk_template.render(chunk=chunk, style_claims=claims_by_chunk.get(chunk.chunk_id, []))
        write_text_atomic(path, rendered.rstrip() + "\n")
        outputs.append(path)
    return outputs

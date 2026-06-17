from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from style_kb.models import Chunk, SpeakerRole, SpeechTurn, StyleClaim, TimelineEvent, VideoInfo
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
    write_if_changed: bool = False,
) -> list[Path]:
    env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=False, trim_blocks=True, lstrip_blocks=True)
    index_template = env.get_template("index.md.j2")
    video_template = env.get_template("video.md.j2")
    chunk_template = env.get_template("chunk.md.j2")
    claims_by_chunk: dict[str, list[StyleClaim]] = {}
    for claim in style_claims:
        claims_by_chunk.setdefault(claim.chunk_id, []).append(claim)

    index_rendered = index_template.render(title=index_title, video=video, chunks=chunks)
    _write_rendered_text(obsidian_index_path, index_rendered.rstrip() + "\n", write_if_changed=write_if_changed)

    video_rendered = video_template.render(
        video=video,
        timeline_events=timeline_events,
        style_claims=style_claims,
        event_frame_links=event_frame_links,
        speech_turn_label=_speech_turn_label,
    )
    _write_rendered_text(video_note_path, video_rendered.rstrip() + "\n", write_if_changed=write_if_changed)

    outputs = [obsidian_index_path, video_note_path]
    for chunk in chunks:
        path = chunk_note_paths[chunk.chunk_id]
        rendered = chunk_template.render(chunk=chunk, style_claims=claims_by_chunk.get(chunk.chunk_id, []))
        _write_rendered_text(path, rendered.rstrip() + "\n", write_if_changed=write_if_changed)
        outputs.append(path)
    return outputs


def _write_rendered_text(path: Path, payload: str, *, write_if_changed: bool) -> None:
    if write_if_changed and path.exists():
        try:
            if path.read_text(encoding="utf-8") == payload:
                return
        except Exception:
            pass
    write_text_atomic(path, payload)


def _speech_turn_label(turn: SpeechTurn) -> str:
    if turn.speaker_role == SpeakerRole.HOST:
        return f"Ведущий ({turn.speaker})" if turn.speaker else "Ведущий"
    if turn.speaker_role == SpeakerRole.OFFSCREEN_QUESTIONER:
        return f"Закадровый вопрос ({turn.speaker})" if turn.speaker else "Закадровый вопрос"
    return turn.speaker or "Голос"

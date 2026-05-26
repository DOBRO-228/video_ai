from __future__ import annotations

from pathlib import Path


class JobPaths:
    def __init__(self, output_root: Path, job_id: str) -> None:
        self.output_root = output_root
        self.job_id = job_id
        self.job_dir = output_root / "jobs" / job_id

    @property
    def database_path(self) -> Path:
        return self.output_root / "jobs.sqlite3"

    @property
    def metadata_dir(self) -> Path:
        return self.job_dir / "metadata"

    @property
    def downloads_dir(self) -> Path:
        return self.job_dir / "downloads"

    @property
    def stt_dir(self) -> Path:
        return self.job_dir / "stt"

    @property
    def scenes_dir(self) -> Path:
        return self.job_dir / "scenes"

    @property
    def frames_dir(self) -> Path:
        return self.job_dir / "frames"

    @property
    def visual_dir(self) -> Path:
        return self.job_dir / "visual"

    @property
    def visual_raw_dir(self) -> Path:
        return self.visual_dir / "raw"

    @property
    def timeline_dir(self) -> Path:
        return self.job_dir / "timeline"

    @property
    def chunks_dir(self) -> Path:
        return self.job_dir / "chunks"

    @property
    def exports_dir(self) -> Path:
        return self.job_dir / "exports"

    @property
    def export_jsonl_dir(self) -> Path:
        return self.exports_dir / "jsonl"

    @property
    def export_obsidian_dir(self) -> Path:
        return self.exports_dir / "obsidian"

    @property
    def reports_dir(self) -> Path:
        return self.job_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.job_dir / "logs"

    @property
    def metadata_raw_ytdlp(self) -> Path:
        return self.metadata_dir / "raw_ytdlp.json"

    @property
    def metadata_video_info(self) -> Path:
        return self.metadata_dir / "video_info.json"

    @property
    def downloads_audio(self) -> Path:
        return self.downloads_dir / "audio.mp3"

    @property
    def downloads_audio_ffprobe(self) -> Path:
        return self.downloads_dir / "audio.ffprobe.json"

    @property
    def downloads_video_proxy(self) -> Path:
        return self.downloads_dir / "video_proxy.mp4"

    @property
    def downloads_video_ffprobe(self) -> Path:
        return self.downloads_dir / "video_proxy.ffprobe.json"

    @property
    def stt_upload(self) -> Path:
        return self.stt_dir / "soniox_upload.json"

    @property
    def stt_transcription(self) -> Path:
        return self.stt_dir / "soniox_transcription.json"

    @property
    def stt_transcript_raw(self) -> Path:
        return self.stt_dir / "transcript_raw.json"

    @property
    def stt_speech_tokens(self) -> Path:
        return self.stt_dir / "speech_tokens.jsonl"

    @property
    def stt_speaker_diarization(self) -> Path:
        return self.stt_dir / "speaker_diarization.json"

    @property
    def stt_speech_segments(self) -> Path:
        return self.stt_dir / "speech_segments.jsonl"

    @property
    def stt_speech_segments_raw(self) -> Path:
        return self.stt_dir / "speech_segments_raw.json"

    @property
    def scenes_jsonl(self) -> Path:
        return self.scenes_dir / "scenes.jsonl"

    @property
    def frame_refs_jsonl(self) -> Path:
        return self.frames_dir / "frame_refs.jsonl"

    @property
    def visual_events_jsonl(self) -> Path:
        return self.visual_dir / "visual_events.jsonl"

    @property
    def visual_presenter_profile(self) -> Path:
        return self.visual_dir / "presenter_profile.json"

    @property
    def visual_raw_presenter_profile(self) -> Path:
        return self.visual_raw_dir / "presenter_profile.raw.json"

    @property
    def timeline_events_jsonl(self) -> Path:
        return self.timeline_dir / "timeline_events.jsonl"

    @property
    def chunks_jsonl(self) -> Path:
        return self.chunks_dir / "chunks.jsonl"

    @property
    def quality_report(self) -> Path:
        return self.reports_dir / "quality_report.json"

    @property
    def cleanup_report(self) -> Path:
        return self.reports_dir / "cleanup.json"

    def stage_log(self, stage_name: str) -> Path:
        return self.logs_dir / f"{stage_name}.log"

    def visual_raw_scene(self, scene_id: str) -> Path:
        return self.visual_raw_dir / f"{scene_id}.json"

    def frame_path(self, scene_index: int, image_index: int) -> Path:
        return self.frames_dir / f"scene_{scene_index:06d}_{image_index:02d}.jpg"

    def export_jsonl(self, name: str) -> Path:
        return self.export_jsonl_dir / name

    def obsidian_video_note(self, video_id: str) -> Path:
        return self.export_obsidian_dir / "videos" / f"{video_id}.md"

    def obsidian_chunk_note(self, chunk_id: str) -> Path:
        return self.export_obsidian_dir / "chunks" / f"{chunk_id}.md"

    @property
    def obsidian_index(self) -> Path:
        return self.export_obsidian_dir / "index.md"

    def ensure_directories(self) -> None:
        for path in [
            self.metadata_dir,
            self.downloads_dir,
            self.stt_dir,
            self.scenes_dir,
            self.frames_dir,
            self.visual_raw_dir,
            self.timeline_dir,
            self.chunks_dir,
            self.export_jsonl_dir,
            self.export_obsidian_dir / "videos",
            self.export_obsidian_dir / "chunks",
            self.reports_dir,
            self.logs_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def artifact_summary(self) -> dict[str, str]:
        return {
            "job_dir": str(self.job_dir),
            "timeline_events": str(self.timeline_events_jsonl),
            "chunks": str(self.chunks_jsonl),
            "quality_report": str(self.quality_report),
            "obsidian_index": str(self.obsidian_index),
        }

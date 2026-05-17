from __future__ import annotations

from pathlib import Path

from style_kb.pipeline.base import Stage, StageContext, StageResult
from style_kb.utils.files import read_json, write_json_atomic


class Stage16Cleanup(Stage):
    name = "16_cleanup"
    ordinal = 16

    def input_files(self, context: StageContext) -> list:
        return [context.paths.quality_report]

    def output_files(self, context: StageContext) -> list:
        return [context.paths.cleanup_report]

    def validate_outputs(self, context: StageContext) -> bool:
        if not context.paths.cleanup_report.exists():
            return False
        payload = read_json(context.paths.cleanup_report)
        return payload.get("job_id") == context.job.job_id

    def run(self, context: StageContext) -> StageResult:
        removed_files: list[str] = []
        if not context.config.project.keep_media:
            for path in [
                context.paths.downloads_audio,
                context.paths.downloads_audio_ffprobe,
                context.paths.downloads_video_proxy,
                context.paths.downloads_video_ffprobe,
            ]:
                if path.exists():
                    path.unlink()
                    removed_files.append(str(path))

        if not context.config.project.keep_frames:
            if context.paths.frame_refs_jsonl.exists():
                context.paths.frame_refs_jsonl.unlink()
                removed_files.append(str(context.paths.frame_refs_jsonl))
            for frame_path in sorted(context.paths.frames_dir.glob("*.jpg")):
                frame_path.unlink()
                removed_files.append(str(frame_path))

        payload = {
            "job_id": context.job.job_id,
            "removed_files": removed_files,
            "keep_media": context.config.project.keep_media,
            "keep_frames": context.config.project.keep_frames,
        }
        write_json_atomic(context.paths.cleanup_report, payload)
        return StageResult(output_files=self.output_files(context), metrics={"removed_files": len(removed_files)})

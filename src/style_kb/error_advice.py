from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ErrorAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_code: str
    retryable: bool
    resume_safe_after_fix: bool
    summary: str
    actions: list[str]
    inspect: list[str]


_ADVICE: dict[str, ErrorAdvice] = {
    "missing_openai_api_key": ErrorAdvice(
        error_code="missing_openai_api_key",
        retryable=False,
        resume_safe_after_fix=True,
        summary="OPENAI_API_KEY is not available.",
        actions=["Add OPENAI_API_KEY to .env or environment.", "Run style-kb resume JOB_ID."],
        inspect=[".env", "jobs/JOB_ID/logs/STAGE.log"],
    ),
    "missing_soniox_api_key": ErrorAdvice(
        error_code="missing_soniox_api_key",
        retryable=False,
        resume_safe_after_fix=True,
        summary="SONIOX_API_KEY is not available.",
        actions=["Add SONIOX_API_KEY to .env or environment.", "Run style-kb resume JOB_ID."],
        inspect=[".env", "jobs/JOB_ID/logs/STAGE.log"],
    ),
    "yt_dlp_too_old": ErrorAdvice(
        error_code="yt_dlp_too_old",
        retryable=False,
        resume_safe_after_fix=True,
        summary="Installed yt-dlp is older than the supported minimum.",
        actions=["Upgrade yt-dlp in the runtime environment.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/STAGE.log"],
    ),
    "yt_dlp_js_runtime_missing": ErrorAdvice(
        error_code="yt_dlp_js_runtime_missing",
        retryable=False,
        resume_safe_after_fix=True,
        summary="yt-dlp needs a supported JavaScript runtime for YouTube extraction.",
        actions=["Install deno, node, bun, or quickjs.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/STAGE.log"],
    ),
    "yt_dlp_version_check_failed": ErrorAdvice(
        error_code="yt_dlp_version_check_failed",
        retryable=False,
        resume_safe_after_fix=True,
        summary="yt-dlp could not be executed to check its version.",
        actions=["Ensure yt-dlp is installed and executable in the runtime environment.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/STAGE.log"],
    ),
    "yt_dlp_metadata_failed": ErrorAdvice(
        error_code="yt_dlp_metadata_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="yt-dlp failed while reading video metadata.",
        actions=["Inspect the stage log for yt-dlp stderr.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/01_metadata.log"],
    ),
    "yt_dlp_audio_failed": ErrorAdvice(
        error_code="yt_dlp_audio_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="yt-dlp failed while downloading or extracting audio.",
        actions=["Inspect the audio download stage log.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/02_download_audio.log"],
    ),
    "yt_dlp_video_failed": ErrorAdvice(
        error_code="yt_dlp_video_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="yt-dlp failed while downloading the proxy video.",
        actions=["Inspect the proxy video download stage log.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/03_download_video_proxy.log"],
    ),
    "ffprobe_failed": ErrorAdvice(
        error_code="ffprobe_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="ffprobe failed while inspecting a media file.",
        actions=["Inspect the ffprobe stage log and referenced media file.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/STAGE.ffprobe.log"],
    ),
    "ffmpeg_extract_frame_failed": ErrorAdvice(
        error_code="ffmpeg_extract_frame_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="ffmpeg failed while extracting a keyframe.",
        actions=["Inspect the frame extraction log for ffmpeg stderr.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/09_extract_keyframes.scene_*.log"],
    ),
    "soniox_transcription_timeout": ErrorAdvice(
        error_code="soniox_transcription_timeout",
        retryable=True,
        resume_safe_after_fix=True,
        summary="Soniox transcription did not complete before timeout.",
        actions=["Inspect Soniox transcription state.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/stt/soniox_transcription.json", "jobs/JOB_ID/logs/06_soniox_fetch_transcript.log"],
    ),
    "soniox_transcription_failed": ErrorAdvice(
        error_code="soniox_transcription_failed",
        retryable=False,
        resume_safe_after_fix=True,
        summary="Soniox returned a failed transcription status.",
        actions=["Inspect the Soniox error message.", "Run style-kb resume JOB_ID after fixing the input or environment."],
        inspect=["jobs/JOB_ID/stt/soniox_transcription.json", "jobs/JOB_ID/logs/06_soniox_fetch_transcript.log"],
    ),
    "empty_transcript": ErrorAdvice(
        error_code="empty_transcript",
        retryable=False,
        resume_safe_after_fix=True,
        summary="The transcript payload contains no usable tokens.",
        actions=["Inspect raw transcript and audio inputs.", "Run style-kb resume JOB_ID after fixing the source issue."],
        inspect=["jobs/JOB_ID/stt/transcript_raw.json", "jobs/JOB_ID/downloads/audio.mp3"],
    ),
    "openai_segmenter_semantic_boundary_failed": ErrorAdvice(
        error_code="openai_segmenter_semantic_boundary_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="OpenAI semantic segmentation failed after retries.",
        actions=["Inspect segmenter raw attempt outputs and stage log.", "Run style-kb resume JOB_ID."],
        inspect=[
            "jobs/JOB_ID/stt/speech_segments_raw_attempt_*.json",
            "jobs/JOB_ID/stt/speech_segments_raw.json",
            "jobs/JOB_ID/logs/07_build_speech_segments.log",
        ],
    ),
    "openai_segmenter_failed": ErrorAdvice(
        error_code="openai_segmenter_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="OpenAI segmenter request failed.",
        actions=["Inspect the stage log and raw provider artifacts.", "Run style-kb resume JOB_ID."],
        inspect=[
            "jobs/JOB_ID/logs/07_build_speech_segments.log",
            "jobs/JOB_ID/stt/speech_segments_raw_attempt_*.json",
            "jobs/JOB_ID/stt/speech_segments_raw.json",
        ],
    ),
    "openai_vision_failed": ErrorAdvice(
        error_code="openai_vision_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="OpenAI vision request failed.",
        actions=["Inspect the vision stage log and raw provider artifacts.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/10_describe_visuals.log", "jobs/JOB_ID/visual/raw"],
    ),
    "openai_presenter_profile_failed": ErrorAdvice(
        error_code="openai_presenter_profile_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="OpenAI presenter profile request failed.",
        actions=["Inspect the vision stage log and presenter profile raw artifact.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/10_describe_visuals.log", "jobs/JOB_ID/visual/raw/presenter_profile.raw.json"],
    ),
    "openai_vision_baseline_leakage": ErrorAdvice(
        error_code="openai_vision_baseline_leakage",
        retryable=True,
        resume_safe_after_fix=True,
        summary="Vision response repeated recurring presenter baseline in scene-specific fields.",
        actions=["Inspect the vision content validation block.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/10_describe_visuals.log", "jobs/JOB_ID/visual/raw"],
    ),
    "openai_chunk_planner_invalid_plan": ErrorAdvice(
        error_code="openai_chunk_planner_invalid_plan",
        retryable=True,
        resume_safe_after_fix=True,
        summary="Chunk planner returned a plan that failed validation.",
        actions=["Inspect chunk plan errors and raw planner outputs.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/chunks/chunk_plan_errors.json", "jobs/JOB_ID/chunks/raw"],
    ),
    "openai_chunk_planner_failed": ErrorAdvice(
        error_code="openai_chunk_planner_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="OpenAI chunk planner request failed.",
        actions=["Inspect the chunk planner stage log and raw outputs.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/12_build_chunks.log", "jobs/JOB_ID/chunks/raw"],
    ),
    "openai_claims_failed": ErrorAdvice(
        error_code="openai_claims_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="OpenAI style claims request failed.",
        actions=["Inspect the claims stage log and raw outputs.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/13_extract_style_claims.log", "jobs/JOB_ID/claims/raw"],
    ),
    "openai_claims_invalid_after_retries": ErrorAdvice(
        error_code="openai_claims_invalid_after_retries",
        retryable=True,
        resume_safe_after_fix=True,
        summary="Style claim extraction stayed invalid after retries.",
        actions=["Inspect claims error artifacts and raw outputs.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/claims/style_claims_errors.json", "jobs/JOB_ID/claims/raw"],
    ),
    "audio_video_duration_mismatch": ErrorAdvice(
        error_code="audio_video_duration_mismatch",
        retryable=False,
        resume_safe_after_fix=True,
        summary="Audio and video durations diverge beyond the allowed threshold.",
        actions=["Inspect media duration artifacts.", "Run style-kb resume JOB_ID after fixing media inputs."],
        inspect=["jobs/JOB_ID/downloads/audio.ffprobe.json", "jobs/JOB_ID/downloads/video_proxy.ffprobe.json"],
    ),
    "missing_visual_event": ErrorAdvice(
        error_code="missing_visual_event",
        retryable=False,
        resume_safe_after_fix=True,
        summary="Timeline merge could not find visual evidence for a scene.",
        actions=["Inspect scene, frame, and visual event artifacts.", "Run style-kb resume JOB_ID after fixing upstream artifacts."],
        inspect=["jobs/JOB_ID/scenes/scenes.jsonl", "jobs/JOB_ID/frames/frame_refs.jsonl", "jobs/JOB_ID/visual/visual_events.jsonl"],
    ),
    "soniox_upload_failed": ErrorAdvice(
        error_code="soniox_upload_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="Soniox audio upload request failed.",
        actions=["Inspect the Soniox upload stage log.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/04_soniox_upload_audio.log", "jobs/JOB_ID/stt/soniox_upload.json"],
    ),
    "soniox_create_transcription_failed": ErrorAdvice(
        error_code="soniox_create_transcription_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="Soniox create transcription request failed.",
        actions=["Inspect the Soniox transcription creation log.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/05_soniox_create_transcription.log", "jobs/JOB_ID/stt/soniox_transcription.json"],
    ),
    "soniox_get_transcription_failed": ErrorAdvice(
        error_code="soniox_get_transcription_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="Soniox transcription status request failed.",
        actions=["Inspect the Soniox fetch transcript log.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/06_soniox_fetch_transcript.log", "jobs/JOB_ID/stt/soniox_transcription.json"],
    ),
    "soniox_get_transcript_failed": ErrorAdvice(
        error_code="soniox_get_transcript_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="Soniox transcript download request failed.",
        actions=["Inspect the Soniox fetch transcript log.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/06_soniox_fetch_transcript.log", "jobs/JOB_ID/stt/transcript_raw.json"],
    ),
}


def advice_for_error_code(error_code: str | None, *, job_id: str | None = None, stage_name: str | None = None) -> ErrorAdvice | None:
    if not error_code:
        return None
    advice = _ADVICE.get(error_code)
    if advice is None:
        return None
    if job_id is None and stage_name is None:
        return advice
    return advice.model_copy(
        update={
            "actions": [_replace_placeholders(action, job_id=job_id, stage_name=stage_name) for action in advice.actions],
            "inspect": [_replace_placeholders(path, job_id=job_id, stage_name=stage_name) for path in advice.inspect],
        }
    )


def _replace_placeholders(value: str, *, job_id: str | None, stage_name: str | None) -> str:
    return value.replace("JOB_ID", job_id or "JOB_ID").replace("STAGE", stage_name or "STAGE")

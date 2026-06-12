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
    "missing_gemini_api_key": ErrorAdvice(
        error_code="missing_gemini_api_key",
        retryable=False,
        resume_safe_after_fix=True,
        summary="GEMINI_API_KEY is not available.",
        actions=["Add GEMINI_API_KEY to .env or environment.", "Run style-kb resume JOB_ID."],
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
    "yt_dlp_remote_components_failed": ErrorAdvice(
        error_code="yt_dlp_remote_components_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="yt-dlp could not use configured YouTube remote components.",
        actions=[
            "Inspect the yt-dlp stage log for remote component warnings.",
            "Check download.remote_components in src/style_kb/config/default.yaml.",
            "Upgrade yt-dlp if the installed version does not support remote components.",
            "Run style-kb resume JOB_ID.",
        ],
        inspect=["src/style_kb/config/default.yaml", "jobs/JOB_ID/logs/STAGE.log"],
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
    "openai_segmenter_duration_exceeded": ErrorAdvice(
        error_code="openai_segmenter_duration_exceeded",
        retryable=True,
        resume_safe_after_fix=True,
        summary="OpenAI speech segmentation produced a segment longer than max_segment_seconds.",
        actions=[
            "Inspect the stage log for the offending unit ranges and retry-advisor instruction.",
            "Run style-kb resume JOB_ID.",
        ],
        inspect=[
            "jobs/JOB_ID/logs/07_build_speech_segments.log",
            "jobs/JOB_ID/stt/speech_segments_raw_attempt_*.json",
            "jobs/JOB_ID/stt/speech_segments_retry_advice_attempt_*.json",
        ],
    ),
    "openai_segmenter_words_exceeded": ErrorAdvice(
        error_code="openai_segmenter_words_exceeded",
        retryable=True,
        resume_safe_after_fix=True,
        summary="OpenAI speech segmentation produced a segment with too many words.",
        actions=[
            "Inspect the stage log for the offending unit ranges and retry-advisor instruction.",
            "Run style-kb resume JOB_ID.",
        ],
        inspect=[
            "jobs/JOB_ID/logs/07_build_speech_segments.log",
            "jobs/JOB_ID/stt/speech_segments_raw_attempt_*.json",
            "jobs/JOB_ID/stt/speech_segments_retry_advice_attempt_*.json",
        ],
    ),
    "openai_segmenter_non_contiguous_ranges": ErrorAdvice(
        error_code="openai_segmenter_non_contiguous_ranges",
        retryable=True,
        resume_safe_after_fix=True,
        summary="OpenAI speech segmentation returned ranges that do not cover units contiguously.",
        actions=[
            "Inspect the stage log for the first non-contiguous range.",
            "Run style-kb resume JOB_ID.",
        ],
        inspect=[
            "jobs/JOB_ID/logs/07_build_speech_segments.log",
            "jobs/JOB_ID/stt/speech_segments_raw_attempt_*.json",
            "jobs/JOB_ID/stt/speech_segments_retry_advice_attempt_*.json",
        ],
    ),
    "openai_segmenter_unsafe_unit_boundary": ErrorAdvice(
        error_code="openai_segmenter_unsafe_unit_boundary",
        retryable=True,
        resume_safe_after_fix=True,
        summary="OpenAI speech segmentation ended a segment at a non-segmentable unit boundary.",
        actions=[
            "Inspect the stage log for the boundary unit and retry-advisor instruction.",
            "Run style-kb resume JOB_ID.",
        ],
        inspect=[
            "jobs/JOB_ID/logs/07_build_speech_segments.log",
            "jobs/JOB_ID/stt/speech_segments_raw_attempt_*.json",
            "jobs/JOB_ID/stt/speech_segments_retry_advice_attempt_*.json",
        ],
    ),
    "openai_segmenter_crossed_must_boundary": ErrorAdvice(
        error_code="openai_segmenter_crossed_must_boundary",
        retryable=True,
        resume_safe_after_fix=True,
        summary="OpenAI speech segmentation crossed a required unit boundary such as a speaker change.",
        actions=[
            "Inspect the stage log for the required boundary unit and retry-advisor instruction.",
            "Run style-kb resume JOB_ID.",
        ],
        inspect=[
            "jobs/JOB_ID/logs/07_build_speech_segments.log",
            "jobs/JOB_ID/stt/speech_segments_raw_attempt_*.json",
            "jobs/JOB_ID/stt/speech_segments_retry_advice_attempt_*.json",
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
    "openai_vision_detail_missing": ErrorAdvice(
        error_code="openai_vision_detail_missing",
        retryable=False,
        resume_safe_after_fix=True,
        summary="OpenAI vision detail is missing.",
        actions=["Set vision.detail in src/style_kb/config/default.yaml when using provider openai.", "Run style-kb resume JOB_ID."],
        inspect=["src/style_kb/config/default.yaml"],
    ),
    "openai_presenter_profile_failed": ErrorAdvice(
        error_code="openai_presenter_profile_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="OpenAI presenter profile request failed.",
        actions=["Inspect the vision stage log and presenter profile raw artifact.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/10_describe_visuals.log", "jobs/JOB_ID/visual/raw/presenter_profile.raw.json"],
    ),
    "gemini_sdk_missing": ErrorAdvice(
        error_code="gemini_sdk_missing",
        retryable=False,
        resume_safe_after_fix=True,
        summary="google-genai is not installed.",
        actions=["Install project dependencies.", "Run style-kb resume JOB_ID."],
        inspect=["pyproject.toml", "jobs/JOB_ID/logs/10_describe_visuals.log"],
    ),
    "gemini_media_resolution_invalid": ErrorAdvice(
        error_code="gemini_media_resolution_invalid",
        retryable=False,
        resume_safe_after_fix=True,
        summary="Gemini vision media_resolution is invalid.",
        actions=["Use low, medium, high, or ultra_high in src/style_kb/config/default.yaml.", "Run style-kb resume JOB_ID."],
        inspect=["src/style_kb/config/default.yaml"],
    ),
    "gemini_thinking_level_invalid": ErrorAdvice(
        error_code="gemini_thinking_level_invalid",
        retryable=False,
        resume_safe_after_fix=True,
        summary="Gemini thinking_level is invalid.",
        actions=["Use minimal, low, medium, or high in src/style_kb/config/default.yaml.", "Run style-kb resume JOB_ID."],
        inspect=["src/style_kb/config/default.yaml"],
    ),
    "gemini_thinking_budget_invalid": ErrorAdvice(
        error_code="gemini_thinking_budget_invalid",
        retryable=False,
        resume_safe_after_fix=True,
        summary="Gemini thinking_budget is invalid.",
        actions=["Use -1 for dynamic thinking, 0 to disable thinking, or 1..24576 in src/style_kb/config/default.yaml.", "Run style-kb resume JOB_ID."],
        inspect=["src/style_kb/config/default.yaml"],
    ),
    "gemini_thinking_config_conflict": ErrorAdvice(
        error_code="gemini_thinking_config_conflict",
        retryable=False,
        resume_safe_after_fix=True,
        summary="Gemini thinking_level and thinking_budget cannot both be set.",
        actions=["Set only one Gemini thinking control in src/style_kb/config/default.yaml.", "Run style-kb resume JOB_ID."],
        inspect=["src/style_kb/config/default.yaml"],
    ),
    "gemini_vision_failed": ErrorAdvice(
        error_code="gemini_vision_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="Gemini vision request failed.",
        actions=["Inspect the vision stage log and raw provider artifacts.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/10_describe_visuals.log", "jobs/JOB_ID/visual/raw"],
    ),
    "gemini_presenter_profile_failed": ErrorAdvice(
        error_code="gemini_presenter_profile_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="Gemini presenter profile request failed.",
        actions=["Inspect the vision stage log and presenter profile raw artifact.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/10_describe_visuals.log", "jobs/JOB_ID/visual/raw/presenter_profile.raw.json"],
    ),
    "gemini_vision_json_parse_failed": ErrorAdvice(
        error_code="gemini_vision_json_parse_failed",
        retryable=True,
        resume_safe_after_fix=True,
        summary="Gemini vision response is not valid JSON.",
        actions=["Inspect the raw vision response.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/10_describe_visuals.log", "jobs/JOB_ID/visual/raw"],
    ),
    "gemini_vision_output_missing": ErrorAdvice(
        error_code="gemini_vision_output_missing",
        retryable=True,
        resume_safe_after_fix=True,
        summary="Gemini vision response did not contain output text.",
        actions=["Inspect the raw vision response.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/10_describe_visuals.log", "jobs/JOB_ID/visual/raw"],
    ),
    "vision_scene_analysis_missing": ErrorAdvice(
        error_code="vision_scene_analysis_missing",
        retryable=True,
        resume_safe_after_fix=True,
        summary="Vision provider did not produce a usable scene analysis.",
        actions=["Inspect the vision stage log and raw provider artifacts.", "Run style-kb resume JOB_ID."],
        inspect=["jobs/JOB_ID/logs/10_describe_visuals.log", "jobs/JOB_ID/visual/raw"],
    ),
    "unsupported_vision_provider": ErrorAdvice(
        error_code="unsupported_vision_provider",
        retryable=False,
        resume_safe_after_fix=True,
        summary="Configured vision provider is not supported.",
        actions=["Use gemini or openai in src/style_kb/config/default.yaml.", "Run style-kb resume JOB_ID."],
        inspect=["src/style_kb/config/default.yaml"],
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

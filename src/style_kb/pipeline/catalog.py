from __future__ import annotations

from style_kb.pipeline.base import Stage
from style_kb.stages.stage_01_metadata import Stage01Metadata
from style_kb.stages.stage_02_download_audio import Stage02DownloadAudio
from style_kb.stages.stage_03_download_video_proxy import Stage03DownloadVideoProxy
from style_kb.stages.stage_04_soniox_upload_audio import Stage04SonioxUploadAudio
from style_kb.stages.stage_05_soniox_create_transcription import Stage05SonioxCreateTranscription
from style_kb.stages.stage_06_soniox_fetch_transcript import Stage06SonioxFetchTranscript
from style_kb.stages.stage_07_build_speech_segments import Stage07BuildSpeechSegments
from style_kb.stages.stage_08_detect_scenes import Stage08DetectScenes
from style_kb.stages.stage_09_extract_keyframes import Stage09ExtractKeyframes
from style_kb.stages.stage_10_describe_visuals import Stage10DescribeVisuals
from style_kb.stages.stage_11_merge_timeline import Stage11MergeTimeline
from style_kb.stages.stage_12_build_chunks import Stage12BuildChunks
from style_kb.stages.stage_13_export_jsonl import Stage13ExportJsonl
from style_kb.stages.stage_14_export_obsidian import Stage14ExportObsidian
from style_kb.stages.stage_15_quality_report import Stage15QualityReport
from style_kb.stages.stage_16_cleanup import Stage16Cleanup

STAGES: list[type[Stage]] = [
    Stage01Metadata,
    Stage02DownloadAudio,
    Stage03DownloadVideoProxy,
    Stage04SonioxUploadAudio,
    Stage05SonioxCreateTranscription,
    Stage06SonioxFetchTranscript,
    Stage07BuildSpeechSegments,
    Stage08DetectScenes,
    Stage09ExtractKeyframes,
    Stage10DescribeVisuals,
    Stage11MergeTimeline,
    Stage12BuildChunks,
    Stage13ExportJsonl,
    Stage14ExportObsidian,
    Stage15QualityReport,
    Stage16Cleanup,
]

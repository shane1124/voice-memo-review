"""voice-memo-review：规则显化、四插槽可插拔的录音自动分析引擎与跨 AI 插件（保持既有顶级导出完全向后兼容）。"""
from __future__ import annotations

from voice_review.config import VoiceReviewConfig, load_config
from voice_review.diarizer import (
    DiarizationResult,
    FluidAudioDiarizer,
    NoSpeechDetectedError,
    SpeakerProfile,
    VoiceprintStore,
    cosine_similarity,
    find_fluidaudio_bin,
)
from voice_review.segmenter import (
    Segment,
    SegmenterConfig,
    SplitIdCheck,
    clean_and_merge_segments,
    cut_segments_to_mp3,
    pack_batches,
)
from voice_review.workflow_builder import (
    V2JobResult,
    prepare_v2_job,
    render_workflow_script,
    sanitize_transcript_field,
)

__version__ = "1.3.2"

__all__ = [
    "DiarizationResult",
    "FluidAudioDiarizer",
    "NoSpeechDetectedError",
    "Segment",
    "SegmenterConfig",
    "SpeakerProfile",
    "SplitIdCheck",
    "V2JobResult",
    "VoiceReviewConfig",
    "VoiceprintStore",
    "__version__",
    "clean_and_merge_segments",
    "cosine_similarity",
    "cut_segments_to_mp3",
    "find_fluidaudio_bin",
    "load_config",
    "pack_batches",
    "prepare_v2_job",
    "render_workflow_script",
    "sanitize_transcript_field",
]

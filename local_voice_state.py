"""Shared state models for the local voice control window."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LocalVoiceModelOption:
    """One discovered sherpa-onnx model preset available to the UI/controller."""

    label: str
    model_type: str
    directory: Path
    encoder_path: Path
    decoder_path: Path
    tokens_path: Path
    joiner_path: Path | None = None


@dataclass(frozen=True)
class LocalVoiceTranscriptEntry:
    """One transcript line rendered inside the voice control window."""

    timestamp_text: str
    text: str
    partial: bool


@dataclass(frozen=True)
class LocalVoiceSettingsView:
    """UI-facing snapshot of the active local voice configuration."""

    enabled: bool = False
    speaker_user_id: str = "local-mic"
    speaker_username: str = "Local Mic"
    input_device: str = ""
    engine: str = "aliyun_rasr"
    rasr_model: str = "fun-asr-realtime"
    rasr_ws_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"
    rasr_api_key_env: str = "DASHSCOPE_API_KEY"
    rasr_api_key: str = ""
    rasr_audio_format: str = "pcm"
    rasr_language_hint: str = "zh,en"
    rasr_enable_intermediate_result: bool = True
    rasr_enable_punctuation_prediction: bool = True
    rasr_enable_inverse_text_normalization: bool = True
    rasr_max_sentence_silence_ms: int = 800
    rasr_heartbeat: bool = True
    rasr_route_partials_to_maibot: bool = False
    rasr_speech_noise_threshold: float = 0.3
    rasr_disfluency_removal_enabled: bool = False
    sample_rate_hz: int = 16000
    channels: int = 1
    block_duration_ms: int = 100
    sentence_postprocess_enabled: bool = True
    sentence_flush_inactivity_ms: int = 700
    sentence_force_emit_chars: int = 16
    sentence_auto_punctuation: bool = True
    speech_vad_enabled: bool = True
    speech_noise_reduction_enabled: bool = True
    speech_vad_start_threshold: float = 0.024
    speech_vad_noise_ratio: float = 4.0
    speech_vad_hold_ms: int = 250
    pre_speech_padding_ms: int = 160
    speech_reset_on_silence: bool = True
    speech_noise_floor_adaptation: float = 0.05
    speech_noise_suppression_strength: float = 0.8
    sherpa_model_type: str = "transducer"
    sherpa_provider: str = "cpu"
    sherpa_num_threads: int = 1
    sherpa_model_sample_rate_hz: int = 16000
    sherpa_feature_dim: int = 80
    sherpa_decoding_method: str = "greedy_search"
    sherpa_max_active_paths: int = 4
    sherpa_hotwords_file: str = ""
    sherpa_hotwords_score: float = 1.5
    sherpa_blank_penalty: float = 0.0
    sherpa_enable_endpoint: bool = False
    sherpa_encoder: str = ""
    sherpa_decoder: str = ""
    sherpa_joiner: str = ""
    sherpa_tokens: str = ""
    min_transcript_length: int = 1
    stable_emit_min_chars: int = 1


@dataclass(frozen=True)
class LocalVoiceSnapshot:
    """Full UI snapshot published by the local voice controller."""

    is_listening: bool = False
    current_display_text: str = ""
    last_error: str = ""
    selected_model_label: str = ""
    available_devices: tuple[str, ...] = field(default_factory=tuple)
    available_models: tuple[LocalVoiceModelOption, ...] = field(default_factory=tuple)
    transcript_log: tuple[LocalVoiceTranscriptEntry, ...] = field(default_factory=tuple)
    settings: LocalVoiceSettingsView = field(default_factory=LocalVoiceSettingsView)

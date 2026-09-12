"""Configuration model for the Bilibili live adapter plugin."""

from __future__ import annotations

import platform

from typing import Any, ClassVar

from maibot_sdk import Field, PluginConfigBase
from pydantic import ValidationInfo, field_validator, model_validator

from .constants import (
    DEFAULT_BILIBILI_WS_URL,
    DEFAULT_CONNECT_TIMEOUT_SEC,
    DEFAULT_HEARTBEAT_INTERVAL_SEC,
    DEFAULT_NAPCAT_CONTROL_API_NAME,
    DEFAULT_RECONNECT_DELAY_SEC,
    SUPPORTED_CONFIG_VERSION,
)


class PluginOptions(PluginConfigBase):
    """Top-level plugin switch."""

    __ui_label__: ClassVar[str] = "Plugin"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=False,
        description="Enable the Bilibili live adapter.",
        json_schema_extra={"label": "Enabled", "order": 0},
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="Config schema version.",
        json_schema_extra={"hidden": True, "disabled": True, "label": "Config version", "order": 99},
    )

    @field_validator("config_version", mode="before")
    @classmethod
    def normalize_config_version(cls, value: Any) -> str:
        return normalize_string(value) or SUPPORTED_CONFIG_VERSION


class BilibiliConfig(PluginConfigBase):
    """Bilibili live room connection settings."""

    __ui_label__: ClassVar[str] = "Bilibili"
    __ui_order__: ClassVar[int] = 1

    room_id: int = Field(
        default=0,
        description="Bilibili live room id.",
        json_schema_extra={"label": "Room id", "order": 0, "placeholder": "123456"},
    )
    uid: int = Field(
        default=0,
        description="Client uid used during the danmaku handshake. 0 means anonymous.",
        json_schema_extra={"label": "UID", "order": 1},
    )
    ws_url: str = Field(
        default=DEFAULT_BILIBILI_WS_URL,
        description="Fallback danmaku WebSocket endpoint.",
        json_schema_extra={"label": "Fallback WebSocket URL", "order": 2},
    )
    heartbeat_interval_sec: float = Field(
        default=DEFAULT_HEARTBEAT_INTERVAL_SEC,
        description="Danmaku heartbeat interval in seconds.",
        json_schema_extra={"label": "Heartbeat seconds", "order": 3, "step": 1},
    )
    reconnect_delay_sec: float = Field(
        default=DEFAULT_RECONNECT_DELAY_SEC,
        description="Reconnect delay after a broken danmaku connection.",
        json_schema_extra={"label": "Reconnect seconds", "order": 4, "step": 1},
    )
    connect_timeout_sec: float = Field(
        default=DEFAULT_CONNECT_TIMEOUT_SEC,
        description="Network connect timeout in seconds.",
        json_schema_extra={"label": "Connect timeout", "order": 5, "step": 1},
    )
    danmaku_idle_reconnect_sec: float = Field(
        default=60.0,
        description="Reconnect a quiet danmaku websocket after this many idle seconds. 0 disables idle reconnect.",
        json_schema_extra={"label": "Idle reconnect seconds", "order": 6, "step": 1},
    )
    parallel_ws_connections: int = Field(
        default=4,
        description="Number of parallel danmaku websocket connections used for redundancy.",
        json_schema_extra={"label": "Parallel WebSockets", "order": 7},
    )
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        ),
        description="HTTP User-Agent used when resolving the danmaku server.",
        json_schema_extra={"label": "User-Agent", "order": 8},
    )
    route_gifts_as_messages: bool = Field(
        default=True,
        description="Route gift and super-chat events into the live interaction planner.",
        json_schema_extra={"label": "Route gifts", "order": 9},
    )
    history_fallback_enabled: bool = Field(
        default=False,
        description="Poll recent danmaku history as a fallback when websocket delivery misses messages.",
        json_schema_extra={"label": "History fallback", "order": 10},
    )

    @field_validator("room_id", "uid", "parallel_ws_connections", mode="before")
    @classmethod
    def normalize_ints(cls, value: Any) -> int:
        return normalize_non_negative_int(value)

    @field_validator("ws_url", "user_agent", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator(
        "heartbeat_interval_sec",
        "reconnect_delay_sec",
        "connect_timeout_sec",
        mode="before",
    )
    @classmethod
    def normalize_positive_float(cls, value: Any) -> float:
        return normalize_positive_float(value, DEFAULT_RECONNECT_DELAY_SEC)

    @field_validator("danmaku_idle_reconnect_sec", mode="before")
    @classmethod
    def normalize_idle_reconnect_sec(cls, value: Any) -> float:
        return max(0.0, normalize_float(value, 60.0))


class IdentityConfig(PluginConfigBase):
    """Gateway identity and route settings."""

    __ui_label__: ClassVar[str] = "Identity"
    __ui_order__: ClassVar[int] = 2

    bot_user_id: str = Field(
        default="maibot-live",
        description="Gateway account id reported to Platform IO.",
        json_schema_extra={"label": "Bot user id", "order": 0},
    )
    bot_nickname: str = Field(
        default="MaiBot Live",
        description="Bot display name for route metadata.",
        json_schema_extra={"label": "Bot nickname", "order": 1},
    )
    route_scope: str = Field(
        default="",
        description="Optional Platform IO route scope. Empty means room id is used.",
        json_schema_extra={"label": "Route scope", "order": 2},
    )

    @field_validator("bot_user_id", "bot_nickname", "route_scope", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)


class FilterConfig(PluginConfigBase):
    """Inbound message filtering."""

    __ui_label__: ClassVar[str] = "Filters"
    __ui_order__: ClassVar[int] = 3

    ignored_user_ids: list[str] = Field(
        default_factory=list,
        description="Bilibili user ids ignored before entering MaiBot.",
        json_schema_extra={"label": "Ignored user ids", "order": 0},
    )
    blocked_words: list[str] = Field(
        default_factory=list,
        description="Drop danmaku containing any of these words.",
        json_schema_extra={"label": "Blocked words", "order": 1},
    )
    min_text_length: int = Field(
        default=1,
        description="Drop danmaku shorter than this number of characters.",
        json_schema_extra={"label": "Minimum text length", "order": 2},
    )

    @field_validator("ignored_user_ids", "blocked_words", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str]:
        return normalize_string_list(value)

    @field_validator("min_text_length", mode="before")
    @classmethod
    def normalize_min_text_length(cls, value: Any) -> int:
        return max(0, normalize_non_negative_int(value))


class InteractionConfig(PluginConfigBase):
    """Live-room interaction planner settings."""

    __ui_label__: ClassVar[str] = "Interaction"
    __ui_order__: ClassVar[int] = 4

    enabled: bool = Field(default=True, description="Enable danmaku selection before injecting into MaiBot.")
    window_seconds: float = Field(default=2.0, description="Danmaku selection window seconds.")
    max_batch_size: int = Field(default=30, description="Maximum events retained per selection window.")
    max_selected_per_window: int = Field(default=2, description="Maximum events injected per selection window.")
    route_all_when_pending_leq: int = Field(
        default=3,
        description="Route all buffered danmaku when the current pending message count is at or below this value. 0 disables the shortcut.",
    )
    min_inject_interval_sec: float = Field(default=2.0, description="Minimum interval between MaiBot injections.")
    max_injections_per_minute: int = Field(default=12, description="Maximum MaiBot injections per minute.")
    speaking_slowdown_factor: float = Field(default=0.35, description="Selection score multiplier while AI speaks.")
    bot_names: list[str] = Field(default_factory=lambda: ["maibot", "mai"], description="Names treated as direct calls.")
    keywords: list[str] = Field(default_factory=list, description="Extra keywords that boost selection.")
    llm_enabled: bool = Field(default=True, description="Use MaiBot LLM for dense-window selection.")
    llm_timeout_sec: float = Field(default=4.0, description="Planner LLM timeout hint.")
    idle_topic_enabled: bool = Field(default=True, description="Ask MaiBot to start a topic when live chat is quiet.")
    idle_topic_after_sec: float = Field(
        default=15.0,
        description="Seconds without a routeable danmaku before MaiBot starts a topic.",
    )
    idle_topic_prompt: str = Field(
        default="\u76f4\u64ad\u95f4\u6682\u65f6\u6ca1\u6709\u65b0\u5f39\u5e55\uff0c"
        "\u8bf7\u4e3b\u52a8\u627e\u4e00\u4e2a\u8f7b\u677e\u3001\u6709\u6897\u3001\u80fd\u8ba9\u89c2\u4f17\u9a6c\u4e0a\u63a5\u8bdd\u7684\u8bdd\u9898\u548c\u89c2\u4f17\u804a\u804a\u3002"
        "\u4f18\u5148\u5f3a\u5173\u8054\u73a9\u6897\u3001\u641e\u62bd\u8c61\u3001\u5047\u88c5\u6545\u969c\uff0c\u4e0d\u8981\u592a\u6b63\u7ecf\u3002",
        description="Prompt injected into MaiBot when the live room is quiet.",
    )
    idle_topic_context_enabled: bool = Field(
        default=True,
        description="Include recent live-room records and previous idle topics in idle topic prompts.",
    )
    idle_topic_context_limit: int = Field(default=8, description="Recent live-room records included in idle prompts.")
    idle_topic_history_limit: int = Field(
        default=5,
        description="Previous bot-initiated idle topics included to avoid repeated topics.",
    )
    topic_extension: "TopicExtensionConfig" = Field(default_factory=lambda: TopicExtensionConfig())
    serial_reply_timeout_sec: float = Field(
        default=90.0,
        description="Seconds to keep later live events queued while waiting for the current bot reply to finish.",
    )

    @field_validator("bot_names", "keywords", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str]:
        return normalize_string_list(value)

    @field_validator(
        "window_seconds",
        "min_inject_interval_sec",
        "speaking_slowdown_factor",
        "idle_topic_after_sec",
        mode="before",
    )
    @classmethod
    def normalize_float(cls, value: Any) -> float:
        return normalize_positive_float(value, 1.0)

    @field_validator("serial_reply_timeout_sec", mode="before")
    @classmethod
    def normalize_serial_reply_timeout_sec(cls, value: Any) -> float:
        return max(0.0, normalize_float(value, 90.0))

    @field_validator("idle_topic_prompt", mode="before")
    @classmethod
    def normalize_idle_topic_prompt(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator(
        "max_batch_size",
        "max_selected_per_window",
        "max_injections_per_minute",
        "idle_topic_context_limit",
        "idle_topic_history_limit",
        mode="before",
    )
    @classmethod
    def normalize_counts(cls, value: Any) -> int:
        return max(1, normalize_non_negative_int(value))

    @field_validator("route_all_when_pending_leq", mode="before")
    @classmethod
    def normalize_route_all_threshold(cls, value: Any) -> int:
        return max(0, normalize_non_negative_int(value))


class TopicExtensionConfig(PluginConfigBase):
    """Separate LLM settings for continuing or expanding live topics."""

    enabled: bool = Field(default=False, description="Use a dedicated LLM to expand tailing live topics.")
    api_provider: str = Field(default="BaiLian", description="MaiBot model_config API provider name.")
    model_name: str = Field(default="", description="Optional MaiBot model name to reuse.")
    model_identifier: str = Field(default="qwen3.6-flash", description="Provider model identifier.")
    enable_thinking: bool = Field(default=False, description="Whether to enable provider reasoning mode.")
    temperature: float = Field(default=0.85, description="Topic expansion model temperature.")
    max_tokens: int = Field(default=900, description="Maximum tokens for one topic expansion request.")
    timeout_sec: float = Field(default=45.0, description="Topic expansion request timeout seconds.")

    @field_validator("api_provider", "model_name", "model_identifier", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("temperature", "timeout_sec", mode="before")
    @classmethod
    def normalize_float_fields(cls, value: Any, info: ValidationInfo) -> float:
        defaults = {"temperature": 0.85, "timeout_sec": 45.0}
        return normalize_positive_float(value, defaults.get(info.field_name, 1.0))

    @field_validator("max_tokens", mode="before")
    @classmethod
    def normalize_max_tokens(cls, value: Any) -> int:
        return max(1, normalize_non_negative_int(value) or 900)


class NapCatControlConfig(PluginConfigBase):
    """Optional coordination with the NapCat QQ adapter."""

    __ui_label__: ClassVar[str] = "NapCat"
    __ui_order__: ClassVar[int] = 5

    disable_on_live_connect: bool = Field(
        default=False,
        description="Disable the NapCat WebSocket connection after the live room connection becomes ready.",
        json_schema_extra={"label": "Disable on live connect", "order": 0},
    )
    restore_on_live_disconnect: bool = Field(
        default=False,
        description="Re-enable NapCat when the live room connection disconnects after this plugin disabled it.",
        json_schema_extra={"label": "Restore on live disconnect", "order": 1},
    )
    control_api_name: str = Field(
        default=DEFAULT_NAPCAT_CONTROL_API_NAME,
        description="Public NapCat adapter API used to enable or disable its connection.",
        json_schema_extra={"label": "Control API", "order": 2},
    )

    @field_validator("control_api_name", mode="before")
    @classmethod
    def normalize_control_api_name(cls, value: Any) -> str:
        return normalize_string(value) or DEFAULT_NAPCAT_CONTROL_API_NAME


class JsonBridgeConfig(PluginConfigBase):
    """Shared JSON bridge config."""

    enabled: bool = Field(default=False, description="Enable this bridge.")
    http_url: str = Field(default="", description="Optional HTTP POST endpoint.")
    websocket_url: str = Field(default="", description="Optional WebSocket endpoint.")
    auth_token: str = Field(default="", description="Optional bearer token.")
    connect_timeout_sec: float = Field(default=DEFAULT_CONNECT_TIMEOUT_SEC, description="Network timeout seconds.")

    @field_validator("http_url", "websocket_url", "auth_token", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("connect_timeout_sec", mode="before")
    @classmethod
    def normalize_timeout(cls, value: Any) -> float:
        return normalize_positive_float(value, DEFAULT_CONNECT_TIMEOUT_SEC)


class HubInputConfig(JsonBridgeConfig):
    """Standalone live hub input subscription config."""

    __ui_label__: ClassVar[str] = "Hub Input"
    __ui_order__: ClassVar[int] = 14

    http_url: str = Field(
        default="http://127.0.0.1:18190/api/events",
        description="Optional HTTP endpoint for Hub history sync.",
    )
    websocket_url: str = Field(
        default="ws://127.0.0.1:18190/ws",
        description="Optional WebSocket endpoint for Hub event streaming.",
    )
    presence_url: str = Field(
        default="http://127.0.0.1:18190/api/client/presence",
        description="Optional HTTP endpoint used to register this bot with the Hub.",
    )
    speak_request_url: str = Field(
        default="http://127.0.0.1:18190/api/client/speak-request",
        description="Optional HTTP endpoint used to request a shared Hub speaking turn.",
    )
    speak_complete_url: str = Field(
        default="http://127.0.0.1:18190/api/client/speak-complete",
        description="Optional HTTP endpoint used to release a shared Hub speaking turn.",
    )
    connect_timeout_sec: float = Field(default=2.0, description="Network timeout seconds.")
    presence_heartbeat_sec: float = Field(
        default=10.0,
        description="Seconds between Hub client presence heartbeats.",
        json_schema_extra={"label": "Presence Heartbeat", "order": 0},
    )
    use_as_primary_source: bool = Field(
        default=False,
        description="Use Hub input instead of direct Bilibili transport.",
        json_schema_extra={"label": "Use As Primary", "order": 1},
    )
    speech_coordination_enabled: bool = Field(
        default=True,
        description="Serialize cross-bot local speech through the shared Hub speaking queue.",
        json_schema_extra={"label": "Speech Coordination", "order": 2},
    )
    history_limit: int = Field(
        default=80,
        description="How many recent Hub events to replay on connect.",
        json_schema_extra={"label": "History Limit", "order": 3},
    )
    inject_local_messages: bool = Field(
        default=True,
        description="Inject Hub local-input messages into this live adapter.",
        json_schema_extra={"label": "Inject Local", "order": 4},
    )
    inject_other_bot_replies: bool = Field(
        default=True,
        description="Inject other MaiBot replies received from Hub.",
        json_schema_extra={"label": "Inject Bot Replies", "order": 5},
    )
    ignore_self_bot_replies: bool = Field(
        default=True,
        description="Ignore Hub bot replies sent by this same instance.",
        json_schema_extra={"label": "Ignore Self Replies", "order": 6},
    )
    auto_start_livehub: bool = Field(
        default=False,
        description="插件启动时若 livehub 未在线则自动拉起 livehub 子进程（插件卸载/配置重载时关闭）。",
        json_schema_extra={"label": "Auto Start LiveHub", "order": 7},
    )
    livehub_room_id: int = Field(
        default=0,
        description="自动拉起 livehub 时使用的 B 站直播间 ID；0 表示使用 bilibili.room_id。",
        json_schema_extra={"label": "LiveHub Room Id", "order": 8},
    )

    @field_validator("history_limit", mode="before")
    @classmethod
    def normalize_history_limit(cls, value: Any) -> int:
        return min(500, max(1, normalize_non_negative_int(value) or 80))

    @field_validator("livehub_room_id", mode="before")
    @classmethod
    def normalize_livehub_room_id(cls, value: Any) -> int:
        return max(0, normalize_non_negative_int(value))

    @field_validator("presence_url", "speak_request_url", "speak_complete_url", mode="before")
    @classmethod
    def normalize_presence_url(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("presence_heartbeat_sec", mode="before")
    @classmethod
    def normalize_presence_heartbeat_sec(cls, value: Any) -> float:
        return normalize_positive_float(value, 10.0)


class HubOutputConfig(JsonBridgeConfig):
    """Standalone live hub output forwarding config."""

    __ui_label__: ClassVar[str] = "Hub Output"
    __ui_order__: ClassVar[int] = 15

    client_id: str = Field(
        default="",
        description="Hub-visible sender id. Empty falls back to identity.bot_user_id.",
        json_schema_extra={"label": "Client Id", "order": 0},
    )
    bot_name: str = Field(
        default="",
        description="Hub-visible sender name. Empty falls back to identity.bot_nickname.",
        json_schema_extra={"label": "Bot Name", "order": 1},
    )
    forward_platform_replies: bool = Field(
        default=True,
        description="Forward live-platform bot replies to the standalone hub.",
        json_schema_extra={"label": "Forward Replies", "order": 2},
    )

    @field_validator("client_id", "bot_name", mode="before")
    @classmethod
    def normalize_hub_identity_text(cls, value: Any) -> str:
        return normalize_string(value)


class Live2DAdaptiveConfig(PluginConfigBase):
    """Live2D automatic parameter discovery settings."""

    enabled: bool = Field(default=True, description="Enable adaptive parameter discovery.")
    model_path: str = Field(default="", description="Optional model3.json file or model directory.")
    safety_level: str = Field(default="normal", description="Parameter safety level.")
    min_confidence: float = Field(default=0.6, description="Minimum confidence required for automatic control.")

    @field_validator("model_path", "safety_level", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("min_confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value: Any) -> float:
        parsed = normalize_positive_float(value, 0.6)
        return min(1.0, max(0.0, parsed))


class Live2DSyncConfig(PluginConfigBase):
    """Live2D output synchronization settings."""

    enabled: bool = Field(default=True, description="Synchronize Live2D frames with MaiBot output.")
    chars_per_second: float = Field(default=7.5, description="Fallback text display rate.")
    prepare_ms: int = Field(default=180, description="Pre-speech preparation duration.")
    release_ms: int = Field(default=600, description="Natural return-to-default duration.")
    mouth_update_interval_ms: int = Field(default=40, description="Mouth frame interval.")
    mouth_closed_value: float = Field(
        default=0.0,
        description="Semantic mouth.open value used when the mouth should be fully closed.",
    )
    mouth_open_threshold: float = Field(
        default=0.02,
        description="Audio amplitude values at or below this gate close the mouth completely.",
    )
    mouth_open_gamma: float = Field(
        default=0.65,
        description="Gamma curve for amplitude-to-mouth mapping. Higher values reduce half-open low amplitudes.",
    )
    mouth_open_gain: float = Field(default=2.9, description="Gain applied after the mouth amplitude curve.")
    mouth_open_max: float = Field(default=1.0, description="Maximum semantic mouth.open value during speech.")
    mouth_sync_mode: str = Field(
        default="plugin_local",
        description="Lip-sync strategy. Legacy values normalize to the plugin-local engine.",
    )
    mouth_amplitude_mix: float = Field(
        default=0.80,
        description="How much the audio amplitude controls mouth.open in hybrid mode.",
    )
    mouth_viseme_lead_ms: int = Field(
        default=10,
        description="How far text-inferred visemes lead audio frames in milliseconds.",
    )
    mouth_open_smoothing: float = Field(
        default=0.40,
        description="Legacy fallback temporal smoothing for mouth.open.",
    )
    mouth_open_attack_smoothing: float = Field(
        default=0.06,
        description="Temporal smoothing while opening the mouth. Lower values make vowels open faster.",
    )
    mouth_open_release_smoothing: float = Field(
        default=0.45,
        description="Temporal smoothing while closing the mouth. Higher values reduce jaw jitter.",
    )
    mouth_open_min_delta: float = Field(
        default=0.015,
        description="Ignore mouth.open changes smaller than this semantic delta.",
    )
    mouth_form_smoothing: float = Field(
        default=0.18,
        description="Temporal smoothing for mouth.form between keyframes.",
    )
    mouth_form_min_delta: float = Field(
        default=0.015,
        description="Ignore mouth.form changes smaller than this raw delta.",
    )
    mouth_keyframe_transition_ms: int = Field(
        default=85,
        description="Crossfade window between inferred mouth keyframes in milliseconds.",
    )
    viseme_timeline_enabled: bool = Field(
        default=False,
        description="Generate explicit text/pinyin viseme keyframes for known TTS audio durations.",
    )
    mouth_vowel_a_open: float = Field(default=1.0, description="Calibrated mouth.open for vowel A.")
    mouth_vowel_a_form: float = Field(default=1.0, description="Calibrated mouth.form for vowel A.")
    mouth_vowel_e_open: float = Field(default=0.6, description="Calibrated mouth.open for vowel E.")
    mouth_vowel_e_form: float = Field(default=0.6, description="Calibrated mouth.form for vowel E.")
    mouth_vowel_i_open: float = Field(default=0.2, description="Calibrated mouth.open for vowel I.")
    mouth_vowel_i_form: float = Field(default=0.5, description="Calibrated mouth.form for vowel I.")
    mouth_vowel_o_open: float = Field(default=1.0, description="Calibrated mouth.open for vowel O.")
    mouth_vowel_o_form: float = Field(default=0.0, description="Calibrated mouth.form for vowel O.")
    mouth_vowel_u_open: float = Field(default=0.3, description="Calibrated mouth.open for vowel U.")
    mouth_vowel_u_form: float = Field(default=0.2, description="Calibrated mouth.form for vowel U.")
    parameter_keepalive_ms: int = Field(default=650, description="Parameter keepalive interval.")
    lip_sync_only_mode: bool = Field(
        default=False,
        description="Only drive mouth lip-sync parameters and leave all other motion to VTube Studio idle animation.",
    )
    idle_motion_enabled: bool = Field(
        default=False,
        description="Use the runtime/model idle motion instead of injecting body/head sway parameters.",
    )
    idle_motion_model: str = Field(default="hiyori", description="Idle motion model hint.")
    idle_motion_name: str = Field(default="m01", description="Idle motion name.")
    idle_motion_file: str = Field(default="m01.motion3.json", description="Idle motion3.json filename.")
    idle_motion_interval_ms: int = Field(default=9000, description="Idle motion retrigger interval.")
    idle_sway_enabled: bool = Field(default=True, description="Enable low-intensity idle body/head sway.")
    idle_sway_interval_ms: int = Field(default=900, description="Idle sway update interval.")
    idle_sway_intensity: float = Field(default=0.25, description="Idle sway intensity from 0 to 1.")
    speech_sway_enabled: bool = Field(default=True, description="Enable speech body/head sway.")
    speech_sway_intensity: float = Field(default=0.45, description="Base speech sway intensity from 0 to 1.")
    speech_sway_update_interval_ms: int = Field(default=160, description="Speech sway frame interval.")

    @field_validator("chars_per_second", mode="before")
    @classmethod
    def normalize_rate(cls, value: Any) -> float:
        return normalize_positive_float(value, 7.5)

    @field_validator(
        "prepare_ms",
        "release_ms",
        "mouth_update_interval_ms",
        "mouth_keyframe_transition_ms",
        "parameter_keepalive_ms",
        "idle_motion_interval_ms",
        "idle_sway_interval_ms",
        "speech_sway_update_interval_ms",
        mode="before",
    )
    @classmethod
    def normalize_ms(cls, value: Any) -> int:
        return max(1, normalize_non_negative_int(value))

    @field_validator("mouth_viseme_lead_ms", mode="before")
    @classmethod
    def normalize_optional_ms(cls, value: Any) -> int:
        return max(0, normalize_non_negative_int(value))

    @field_validator(
        "idle_sway_intensity",
        "speech_sway_intensity",
        "mouth_open_threshold",
        "mouth_open_max",
        "mouth_amplitude_mix",
        "mouth_open_smoothing",
        "mouth_open_attack_smoothing",
        "mouth_open_release_smoothing",
        "mouth_open_min_delta",
        "mouth_form_smoothing",
        "mouth_vowel_a_open",
        "mouth_vowel_e_open",
        "mouth_vowel_i_open",
        "mouth_vowel_o_open",
        "mouth_vowel_u_open",
        mode="before",
    )
    @classmethod
    def normalize_intensity(cls, value: Any) -> float:
        parsed = normalize_positive_float(value, 0.0)
        return min(1.0, max(0.0, parsed))

    @field_validator(
        "mouth_vowel_a_form",
        "mouth_vowel_e_form",
        "mouth_vowel_i_form",
        "mouth_vowel_o_form",
        "mouth_vowel_u_form",
        mode="before",
    )
    @classmethod
    def normalize_mouth_form_value(cls, value: Any) -> float:
        parsed = normalize_float(value, 0.0)
        return min(1.0, max(-2.0, parsed))

    @field_validator("mouth_form_min_delta", mode="before")
    @classmethod
    def normalize_mouth_form_delta(cls, value: Any) -> float:
        parsed = normalize_float(value, 0.03)
        return min(2.0, max(0.0, parsed))

    @field_validator("mouth_closed_value", mode="before")
    @classmethod
    def normalize_closed_value(cls, value: Any) -> float:
        parsed = normalize_float(value, 0.0)
        return min(1.0, max(-1.0, parsed))

    @field_validator("mouth_open_gamma", mode="before")
    @classmethod
    def normalize_mouth_gamma(cls, value: Any) -> float:
        parsed = normalize_positive_float(value, 1.45)
        return min(4.0, max(0.2, parsed))

    @field_validator("mouth_open_gain", mode="before")
    @classmethod
    def normalize_mouth_gain(cls, value: Any) -> float:
        parsed = normalize_positive_float(value, 1.15)
        return min(3.0, max(0.1, parsed))

    @field_validator("mouth_sync_mode", mode="before")
    @classmethod
    def normalize_mouth_sync_mode(cls, value: Any) -> str:
        mode = normalize_string(value).lower().replace("-", "_")
        if mode in {
            "",
            "plugin_local",
            "plugin",
            "local",
            "local_clip",
            "timeline",
            "clip",
            "vts_native",
            "vts",
            "native",
            "native_vts",
            "native_lipsync",
            "none",
            "off",
            "disabled",
            "hybrid",
            "hybrid_viseme",
            "viseme_hybrid",
            "viseme",
            "text_viseme",
            "text",
            "amplitude",
            "audio",
            "rms",
        }:
            return "plugin_local"
        return "plugin_local"

    @field_validator("idle_motion_model", "idle_motion_name", "idle_motion_file", mode="before")
    @classmethod
    def normalize_idle_motion_text(cls, value: Any) -> str:
        return normalize_string(value)


class Live2DOverrideConfig(PluginConfigBase):
    """Optional parameter override."""

    role: str = Field(default="", description="Semantic role override.")
    min: float | None = Field(default=None, description="Parameter minimum.")
    max: float | None = Field(default=None, description="Parameter maximum.")
    default: float | None = Field(default=None, description="Parameter default.")
    safe_amplitude: float | None = Field(default=None, description="Maximum automatic amplitude ratio.")
    enabled: bool = Field(default=True, description="Whether this parameter is enabled.")

    @field_validator("role", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)


class Live2DDebugCommandConfig(PluginConfigBase):
    """Admin-only slash command settings for Live2D debugging."""

    enabled: bool = Field(default=False, description="Enable admin-only slash commands for Live2D debugging.")
    prefix: str = Field(default="/l2d", description="Slash command prefix for Live2D debug commands.")
    admin_user_ids: list[str] = Field(
        default_factory=list,
        description="Bilibili user ids allowed to use Live2D debug commands.",
    )
    drop_non_admin_commands: bool = Field(
        default=True,
        description="Consume matching Live2D debug commands from non-admin users before they reach MaiBot.",
    )

    @field_validator("prefix", mode="before")
    @classmethod
    def normalize_prefix(cls, value: Any) -> str:
        command = normalize_string(value)
        if not command:
            return "/l2d"
        return command if command.startswith("/") else f"/{command}"

    @field_validator("admin_user_ids", mode="before")
    @classmethod
    def normalize_admin_user_ids(cls, value: Any) -> list[str]:
        return normalize_string_list(value)


class Live2DBlinkConfig(PluginConfigBase):
    """Automatic blink settings for embodied Live2D output."""

    enabled: bool = Field(default=False, description="Enable automatic blinking in embodied Live2D mode.")
    interval_min_sec: float = Field(default=2.8, description="Minimum seconds between automatic blinks.")
    interval_max_sec: float = Field(default=5.5, description="Maximum seconds between automatic blinks.")
    double_blink_chance: float = Field(default=0.03, description="Chance to emit a double blink.")
    close_ms: int = Field(default=60, description="Blink closing duration in milliseconds.")
    hold_ms: int = Field(default=28, description="Blink hold duration in milliseconds.")
    open_ms: int = Field(default=110, description="Blink reopening duration in milliseconds.")
    double_blink_gap_ms: int = Field(default=140, description="Gap between double blinks in milliseconds.")

    @field_validator("interval_min_sec", "interval_max_sec", mode="before")
    @classmethod
    def normalize_intervals(cls, value: Any) -> float:
        return max(0.1, normalize_float(value, 1.0))

    @field_validator("double_blink_chance", mode="before")
    @classmethod
    def normalize_double_blink_chance(cls, value: Any) -> float:
        return min(1.0, max(0.0, normalize_float(value, 0.03)))

    @field_validator("close_ms", "hold_ms", "open_ms", "double_blink_gap_ms", mode="before")
    @classmethod
    def normalize_ms(cls, value: Any) -> int:
        return max(1, normalize_non_negative_int(value))


class Live2DWinkConfig(PluginConfigBase):
    """Audience-triggered wink settings for embodied Live2D output."""

    enabled: bool = Field(default=False, description="Enable audience-triggered wink actions.")
    close_ms: int = Field(default=65, description="Wink closing duration in milliseconds.")
    hold_ms: int = Field(default=90, description="Wink hold duration in milliseconds.")
    open_ms: int = Field(default=110, description="Wink reopening duration in milliseconds.")
    request_cooldown_sec: float = Field(default=1.8, description="Minimum seconds between wink requests.")
    non_target_eye_drop: float = Field(
        default=0.08,
        description="Small sympathetic openness drop applied to the non-target eye during a wink.",
    )

    @field_validator("close_ms", "hold_ms", "open_ms", mode="before")
    @classmethod
    def normalize_ms(cls, value: Any) -> int:
        return max(1, normalize_non_negative_int(value))

    @field_validator("request_cooldown_sec", mode="before")
    @classmethod
    def normalize_cooldown(cls, value: Any) -> float:
        return max(0.0, normalize_float(value, 1.8))

    @field_validator("non_target_eye_drop", mode="before")
    @classmethod
    def normalize_non_target_eye_drop(cls, value: Any) -> float:
        return min(1.0, max(0.0, normalize_float(value, 0.08)))


class Live2DEmbodiedConfig(PluginConfigBase):
    """Embodied avatar_state subscription settings."""

    enabled: bool = Field(default=False, description="Enable avatar_state-driven embodied Live2D control.")
    source_url: str = Field(
        default="ws://127.0.0.1:8001/api/webui/ws",
        description="MaiBot unified WebSocket endpoint providing avatar_state snapshots.",
    )
    source_token: str = Field(default="", description="Optional WebSocket token for the MaiBot WebUI endpoint.")
    source_session_id: str = Field(default="", description="Only consume avatar_state snapshots from this session.")
    fallback_to_legacy: bool = Field(
        default=True,
        description="Fall back to the legacy non-mouth Live2D behavior when embodied mode fails.",
    )
    mouse_follow_ui_enabled: bool = Field(default=True, description="Show mouse-follow controls in the Live2D UI.")
    mouse_follow_default_enabled: bool = Field(
        default=False,
        description="Start mouse-follow control disabled unless the user turns it on.",
    )
    mouse_follow_return_after_sec: float = Field(
        default=1.2,
        description="Seconds after mouse activity before returning control to AI.",
    )
    mouse_follow_cooldown_sec: float = Field(
        default=0.45,
        description="Seconds to wait before re-enabling mouse follow after a handoff.",
    )
    mouse_follow_poll_interval_ms: int = Field(
        default=12,
        description="Polling interval in milliseconds for mouse-follow state updates.",
    )
    mouse_follow_smoothing_ms: int = Field(
        default=45,
        description="Low-pass smoothing window in milliseconds for mouse-follow output offsets.",
    )
    mouse_follow_eye_gain: float = Field(default=0.55, description="Eye tracking gain for mouse-follow control.")
    mouse_follow_head_gain: float = Field(default=0.28, description="Head tracking gain for mouse-follow control.")
    mouse_follow_body_gain: float = Field(default=0.12, description="Body tracking gain for mouse-follow control.")
    speech_motion_audio_gain: float = Field(default=1.0, description="Audio motion gain during embodied speech.")
    speech_motion_emotion_gain: float = Field(default=1.0, description="Emotion motion gain during embodied speech.")
    speech_motion_vertical_pump_gain: float = Field(default=1.25, description="Vertical motion gain during speech.")
    speech_motion_lateral_gain: float = Field(default=1.25, description="Lateral motion gain during speech.")
    speech_motion_pitch_gain: float = Field(default=1.0, description="Pitch gain during speech motion.")
    speech_motion_yaw_gain: float = Field(default=1.0, description="Yaw gain during speech motion.")
    speech_motion_roll_gain: float = Field(default=0.8, description="Roll gain during speech motion.")
    speech_motion_shoulder_gain: float = Field(default=0.75, description="Shoulder gain during speech motion.")
    blink: Live2DBlinkConfig = Field(default_factory=Live2DBlinkConfig)
    wink: Live2DWinkConfig = Field(default_factory=Live2DWinkConfig)
    debug_commands: Live2DDebugCommandConfig = Field(default_factory=Live2DDebugCommandConfig)

    @field_validator("source_url", "source_token", "source_session_id", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)


class Live2DSoulLinkConfig(PluginConfigBase):
    """SoulLink-style backend settings for VTS-driven Live2D control."""

    enabled: bool = Field(default=False, description="Enable the SoulLink-style Live2D backend.")
    model_name: str = Field(
        default="",
        description="Optional model_config model name to use for SoulLink expression generation.",
    )
    api_provider: str = Field(
        default="",
        description="Fallback model_config API provider when model_name is empty.",
    )
    model_identifier: str = Field(
        default="",
        description="Fallback model identifier when model_name is empty.",
    )
    temperature: float = Field(default=0.35, description="Expression generation temperature.")
    max_tokens: int = Field(default=420, description="Maximum tokens used for SoulLink expression generation.")
    enable_thinking: bool = Field(
        default=False,
        description="Enable provider-side reasoning/thinking mode for SoulLink generation requests.",
    )
    response_format_json_object: bool = Field(
        default=True,
        description="Request JSON-object structured output for SoulLink generation requests when supported.",
    )
    eye_open_binary: bool = Field(
        default=False,
        description="Snap eye-open parameters to min/max only, matching the SoulLink runtime option.",
    )
    joint_motion_boost: float = Field(
        default=1.25,
        description="Amplify head/body/joint-style motion parameters in SoulLink output.",
    )
    tts_motion_enabled: bool = Field(
        default=True,
        description="Generate SoulLink-style continuous reply motion frames for spoken replies.",
    )
    tts_motion_frame_duration_ms: int = Field(
        default=1000,
        description="Frame duration used by SoulLink continuous reply motion output.",
    )
    tts_motion_prefetch_frames: int = Field(
        default=8,
        description="How many SoulLink motion frames to pre-generate ahead of playback to absorb network jitter.",
    )
    transition_sample_interval_ms: int = Field(
        default=16,
        description="Sample interval used when adapting SoulLink sparse targets into eased VTS timeline keyframes.",
    )
    tts_motion_keep_lip_sync: bool = Field(
        default=True,
        description="Keep mouth-open ownership in the plugin and strip SoulLink mouth-open motion output.",
    )
    keep_mouth_form: bool = Field(
        default=True,
        description="Allow SoulLink output to keep mouth-form parameters while mouth-open remains plugin-owned.",
    )
    parameter_weight: float = Field(
        default=0.82,
        description="Weight assigned to SoulLink-converted parameter contributions sent to VTS.",
    )
    custom_prompt: str = Field(
        default="",
        description="Optional additional model-specific prompt appended to SoulLink generation prompts.",
    )

    @field_validator("model_name", "api_provider", "model_identifier", "custom_prompt", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("temperature", mode="before")
    @classmethod
    def normalize_temperature(cls, value: Any) -> float:
        return min(2.0, max(0.0, normalize_float(value, 0.35)))

    @field_validator("joint_motion_boost", mode="before")
    @classmethod
    def normalize_joint_motion_boost(cls, value: Any) -> float:
        return min(3.0, max(1.0, normalize_float(value, 1.25)))

    @field_validator("parameter_weight", mode="before")
    @classmethod
    def normalize_parameter_weight(cls, value: Any) -> float:
        return min(1.0, max(0.0, normalize_float(value, 0.82)))

    @field_validator(
        "max_tokens",
        "tts_motion_frame_duration_ms",
        "tts_motion_prefetch_frames",
        "transition_sample_interval_ms",
        mode="before",
    )
    @classmethod
    def normalize_positive_ints(cls, value: Any) -> int:
        return max(1, normalize_non_negative_int(value))


class Live2DSoulLinkShellConfig(PluginConfigBase):
    """Transparent local SoulLink desktop shell settings."""

    enabled: bool = Field(default=False, description="Enable the local SoulLink transparent shell.")
    host: str = Field(default="127.0.0.1", description="Shell runtime bind host.")
    port: int = Field(default=18183, description="Shell runtime bind port.")
    start_native_window: bool = Field(default=True, description="Automatically open the native shell window on startup.")
    click_through_default: bool = Field(default=True, description="Start shell in click-through mode.")
    always_on_top: bool = Field(default=True, description="Keep the shell window on top.")
    remember_window_geometry: bool = Field(default=True, description="Persist shell window geometry.")
    start_interactive: bool = Field(default=False, description="Start unlocked instead of click-through.")
    open_devtools: bool = Field(default=False, description="Open shell devtools on startup.")
    fallback_to_vts: bool = Field(default=True, description="Fallback to the current SoulLink VTS path if shell init fails.")

    @field_validator("host", mode="before")
    @classmethod
    def normalize_host(cls, value: Any) -> str:
        return normalize_string(value) or "127.0.0.1"

    @field_validator("port", mode="before")
    @classmethod
    def normalize_port(cls, value: Any) -> int:
        return max(1, normalize_non_negative_int(value) or 18183)


class Live2DConfig(JsonBridgeConfig):
    """Live2D bridge settings."""

    __ui_label__: ClassVar[str] = "Live2D"
    __ui_order__: ClassVar[int] = 6

    enabled: bool = Field(
        default=False,
        description="Forward bot replies and parameter controls to a Live2D bridge.",
        json_schema_extra={"label": "Enable Live2D bridge", "order": 0},
    )
    scheme: str = Field(
        default="auto",
        description="Live2D runtime scheme: auto, legacy, embodied, soullink, or soullink_shell.",
        json_schema_extra={"label": "Scheme", "order": 1},
    )
    driver: str = Field(
        default="auto",
        description="Live2D driver: auto, json, vts, model_file, or fallback.",
        json_schema_extra={"label": "Driver", "order": 2},
    )
    http_url: str = Field(
        default="",
        description="HTTP endpoint that receives Live2D JSON events.",
        json_schema_extra={"label": "HTTP URL", "order": 3, "placeholder": "http://127.0.0.1:18080/live2d"},
    )
    websocket_url: str = Field(
        default="",
        description="WebSocket endpoint that receives Live2D JSON events.",
        json_schema_extra={"label": "WebSocket URL", "order": 4, "placeholder": "ws://127.0.0.1:18081/live2d"},
    )
    send_bot_replies: bool = Field(
        default=True,
        description="Forward MaiBot text replies to Live2D as synchronized timeline events.",
        json_schema_extra={"label": "Speak bot replies", "order": 5},
    )
    mirror_other_platform_replies: bool = Field(
        default=True,
        description="Mirror non-Bilibili MaiBot outbound replies, such as QQ test replies, to Live2D.",
        json_schema_extra={"label": "Mirror other platforms", "order": 6},
    )
    forward_inbound_danmaku: bool = Field(
        default=False,
        description="Forward raw inbound danmaku events to Live2D.",
        json_schema_extra={"label": "Forward inbound danmaku", "order": 7},
    )
    adaptive: Live2DAdaptiveConfig = Field(default_factory=Live2DAdaptiveConfig)
    sync: Live2DSyncConfig = Field(default_factory=Live2DSyncConfig)
    embodied: Live2DEmbodiedConfig = Field(default_factory=Live2DEmbodiedConfig)
    soullink: Live2DSoulLinkConfig = Field(default_factory=Live2DSoulLinkConfig)
    soullink_shell: Live2DSoulLinkShellConfig = Field(default_factory=Live2DSoulLinkShellConfig)
    overrides: dict[str, Live2DOverrideConfig] = Field(default_factory=dict)

    @field_validator("scheme", mode="before")
    @classmethod
    def normalize_scheme(cls, value: Any) -> str:
        scheme = normalize_string(value).lower()
        return scheme if scheme in {"auto", "legacy", "embodied", "soullink", "soullink_shell"} else "auto"

    @field_validator("driver", mode="before")
    @classmethod
    def normalize_driver(cls, value: Any) -> str:
        driver = normalize_string(value).lower()
        return driver if driver in {"auto", "json", "vts", "model_file", "fallback"} else "auto"


class GameConfig(JsonBridgeConfig):
    """External game bridge settings."""

    __ui_label__: ClassVar[str] = "Game"
    __ui_order__: ClassVar[int] = 7

    enabled: bool = Field(
        default=False,
        description="Enable the external game JSON bridge.",
        json_schema_extra={"label": "Enable game bridge", "order": 0},
    )
    http_url: str = Field(
        default="",
        description="HTTP endpoint that receives game control JSON events.",
        json_schema_extra={"label": "HTTP URL", "order": 1, "placeholder": "http://127.0.0.1:18090/game"},
    )
    websocket_url: str = Field(
        default="",
        description="WebSocket endpoint that receives game control JSON events.",
        json_schema_extra={"label": "WebSocket URL", "order": 2, "placeholder": "ws://127.0.0.1:18091/game"},
    )
    forward_bot_replies: bool = Field(
        default=False,
        description="Forward MaiBot text replies to the game bridge.",
        json_schema_extra={"label": "Forward bot replies", "order": 3},
    )
    forward_inbound_danmaku: bool = Field(
        default=False,
        description="Forward raw inbound danmaku events to the game bridge.",
        json_schema_extra={"label": "Forward inbound danmaku", "order": 4},
    )
    allowed_actions: list[str] = Field(default_factory=list, description="Allowed game action names.")

    @field_validator("allowed_actions", mode="before")
    @classmethod
    def normalize_allowed_actions(cls, value: Any) -> list[str]:
        return normalize_string_list(value)


class VisionConfig(PluginConfigBase):
    """Desktop screenshot vision tool settings."""

    __ui_label__: ClassVar[str] = "Vision"
    __ui_order__: ClassVar[int] = 8

    enabled: bool = Field(
        default=False,
        description="Expose a tool that screenshots the current monitor and summarizes it with a vision model.",
        json_schema_extra={"label": "Enable desktop vision tool", "order": 0},
    )
    expose_tool: bool = Field(
        default=True,
        description="Expose inspect_desktop as a callable tool for MaiBot planning.",
        json_schema_extra={"label": "Expose Tool", "order": 1},
    )
    api_provider: str = Field(
        default="BaiLian",
        description="MaiBot model_config API provider name used for the vision model.",
        json_schema_extra={"label": "API provider", "order": 2},
    )
    model_name: str = Field(
        default="",
        description="Optional MaiBot model name to reuse. Leave empty to use model_identifier.",
        json_schema_extra={"label": "Model name", "order": 3},
    )
    model_identifier: str = Field(
        default="qwen3-vl-flash",
        description="OpenAI-compatible vision model id. qwen3-vl-flash is the fast low-cost Bailian default.",
        json_schema_extra={"label": "Model identifier", "order": 4},
    )
    enable_thinking: bool = Field(
        default=False,
        description="Whether to enable provider thinking mode for models that support it.",
        json_schema_extra={"label": "Enable thinking", "order": 5},
    )
    temperature: float = Field(
        default=0.1,
        description="Vision summary model temperature.",
        json_schema_extra={"label": "Temperature", "order": 6, "step": 0.1},
    )
    max_tokens: int = Field(
        default=450,
        description="Maximum tokens for one desktop summary.",
        json_schema_extra={"label": "Max tokens", "order": 7},
    )
    timeout_sec: float = Field(
        default=20.0,
        description="Vision model request timeout seconds.",
        json_schema_extra={"label": "Timeout seconds", "order": 8, "step": 1},
    )
    max_image_edge_px: int = Field(
        default=960,
        description="Resize screenshots so the longest edge is at most this many pixels before upload.",
        json_schema_extra={"label": "Max image edge", "order": 9},
    )
    jpeg_quality: int = Field(
        default=65,
        description="JPEG quality used after resizing the screenshot.",
        json_schema_extra={"label": "JPEG quality", "order": 10},
    )
    system_prompt: str = Field(
        default=(
            "You summarize a livestreamer's current desktop screenshot for another AI. "
            "Be concise and factual. Mention visible apps, game state, important UI text, "
            "and anything the AI should react to. Do not invent hidden details."
        ),
        description="System prompt sent to the vision model.",
        json_schema_extra={"label": "System prompt", "order": 11},
    )
    command: "VisionCommandConfig" = Field(
        default_factory=lambda: VisionCommandConfig(),
        description="Slash-command trigger settings for screenshot context injection.",
        json_schema_extra={"label": "Command Trigger", "order": 12},
    )

    @field_validator("api_provider", "model_name", "model_identifier", "system_prompt", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("temperature", "timeout_sec", mode="before")
    @classmethod
    def normalize_float_fields(cls, value: Any, info: ValidationInfo) -> float:
        defaults = {"temperature": 0.1, "timeout_sec": 20.0}
        parsed = normalize_positive_float(value, defaults.get(info.field_name, 1.0))
        if info.field_name == "temperature":
            return min(2.0, max(0.0, parsed))
        return parsed

    @field_validator("max_tokens", "max_image_edge_px", "jpeg_quality", mode="before")
    @classmethod
    def normalize_int_fields(cls, value: Any, info: ValidationInfo) -> int:
        defaults = {"max_tokens": 450, "max_image_edge_px": 960, "jpeg_quality": 65}
        parsed = normalize_non_negative_int(value) or defaults.get(info.field_name, 1)
        if info.field_name == "max_tokens":
            return max(64, parsed)
        if info.field_name == "max_image_edge_px":
            return min(1920, max(256, parsed))
        return min(95, max(35, parsed))


class VisionCommandConfig(PluginConfigBase):
    """Operator-triggered screenshot polling context settings."""

    enabled: bool = Field(
        default=False,
        description="Enable a slash command that starts or stops screenshot polling and injects the latest summary as context.",
    )
    prefix: str = Field(
        default="/vision",
        description="Slash command prefix for screenshot polling control.",
    )
    authorized_identities: list[str] = Field(
        default_factory=list,
        description="Allowed Bilibili user ids, usernames, or canonical aliases for the vision command.",
    )
    allow_hub_local_input: bool = Field(
        default=True,
        description="Allow local Hub injections to use the vision command without a Bilibili identity.",
    )
    drop_non_admin_commands: bool = Field(
        default=True,
        description="Consume matching vision commands from unauthorized users before they reach MaiBot.",
    )
    model_name: str = Field(
        default="",
        description="Optional MaiBot model name override used only for command-triggered vision polling.",
    )
    model_identifier: str = Field(
        default="",
        description="Optional raw model identifier override used only for command-triggered vision polling.",
    )
    poll_interval_sec: float = Field(
        default=8.0,
        description="Seconds between background screenshot refreshes while the vision command is active.",
    )
    stop_aliases: list[str] = Field(
        default_factory=lambda: ["stop", "off"],
        description="Arguments after the vision prefix that stop background screenshot polling.",
    )

    @field_validator("prefix", mode="before")
    @classmethod
    def normalize_prefix(cls, value: Any) -> str:
        command = normalize_string(value)
        if not command:
            return "/vision"
        return command if command.startswith("/") else f"/{command}"

    @field_validator("authorized_identities", mode="before")
    @classmethod
    def normalize_authorized_identities(cls, value: Any) -> list[str]:
        return normalize_string_list(value)

    @field_validator("model_name", "model_identifier", mode="before")
    @classmethod
    def normalize_model_override_fields(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("poll_interval_sec", mode="before")
    @classmethod
    def normalize_poll_interval_sec(cls, value: Any) -> float:
        return max(1.0, normalize_float(value, 8.0))

    @field_validator("stop_aliases", mode="before")
    @classmethod
    def normalize_stop_aliases(cls, value: Any) -> list[str]:
        normalized = normalize_string_list(value)
        return normalized or ["stop", "off"]


class VideoWatchNamedSourceConfig(PluginConfigBase):
    """Named source shortcuts for the idle video-watch flow."""

    name: str = Field(default="", description="Human-readable alias used in /watchvideo commands.")
    source_type: str = Field(
        default="url",
        description="Named source kind: url, recommend, playlist, fav, or up.",
    )
    value: str = Field(default="", description="URL, UID, or page identifier for the named source.")
    description: str = Field(default="", description="Optional operator-facing note for this named source.")

    @field_validator("name", "source_type", "value", "description", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)


class VideoWatchCommandConfig(PluginConfigBase):
    """Manual control settings for the Bilibili video-watch mode."""

    enabled: bool = Field(
        default=False,
        description="Enable a slash command that starts, stops, or queries the idle video-watch mode.",
    )
    prefix: str = Field(
        default="/watchvideo",
        description="Slash command prefix for video-watch control.",
    )
    authorized_identities: list[str] = Field(
        default_factory=list,
        description="Allowed Bilibili user ids, usernames, or canonical aliases for manual video-watch commands.",
    )
    allow_hub_local_input: bool = Field(
        default=True,
        description="Allow local Hub injections to use /watchvideo without a Bilibili identity.",
    )
    super_chat_start_min_price: float = Field(
        default=20.0,
        description="Minimum super-chat price allowed to start video watch with the command prefix.",
    )
    drop_non_admin_commands: bool = Field(
        default=True,
        description="Consume matching /watchvideo commands from unauthorized users before they reach MaiBot.",
    )
    stop_aliases: list[str] = Field(
        default_factory=lambda: ["stop", "off"],
        description="Arguments after the command prefix that stop the active video-watch session.",
    )
    status_aliases: list[str] = Field(
        default_factory=lambda: ["status", "state"],
        description="Arguments after the command prefix that report current video-watch state.",
    )

    @field_validator("prefix", mode="before")
    @classmethod
    def normalize_prefix(cls, value: Any) -> str:
        command = normalize_string(value)
        if not command:
            return "/watchvideo"
        return command if command.startswith("/") else f"/{command}"

    @field_validator("authorized_identities", "stop_aliases", "status_aliases", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str]:
        return normalize_string_list(value)

    @field_validator("super_chat_start_min_price", mode="before")
    @classmethod
    def normalize_min_price(cls, value: Any) -> float:
        return max(0.0, normalize_float(value, 20.0))


class VideoWatchSourceConfig(PluginConfigBase):
    """Source discovery and browser display settings for video watch."""

    default_mode: str = Field(
        default="recommend",
        description="Automatic source mode used when idle watch starts without a manual source request.",
    )
    recommend_url: str = Field(
        default="https://www.bilibili.com/",
        description="Bilibili page used when auto-selecting from recommendations.",
    )
    named_sources: list[VideoWatchNamedSourceConfig] = Field(
        default_factory=list,
        description="Operator-defined named source shortcuts for /watchvideo.",
    )
    visible_browser: bool = Field(
        default=True,
        description="Show the Playwright browser window so it can be captured for stream display.",
    )
    mute_playback: bool = Field(
        default=True,
        description="Mute webpage playback audio. Recommended when the page is display-only.",
    )
    page_timeout_sec: float = Field(
        default=30.0,
        description="Timeout for loading and scraping Bilibili source pages.",
    )
    candidate_scan_limit: int = Field(
        default=24,
        description="Maximum /video anchors inspected when picking a recommendation or list item.",
    )

    @field_validator("default_mode", "recommend_url", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("page_timeout_sec", mode="before")
    @classmethod
    def normalize_timeout(cls, value: Any) -> float:
        return max(5.0, normalize_float(value, 30.0))

    @field_validator("candidate_scan_limit", mode="before")
    @classmethod
    def normalize_candidate_scan_limit(cls, value: Any) -> int:
        return max(1, normalize_non_negative_int(value) or 24)


class VideoWatchAnalysisConfig(PluginConfigBase):
    """Model and sampling settings for offline Bilibili video analysis."""

    api_provider: str = Field(default="BaiLian", description="MaiBot model_config API provider name.")
    model_name: str = Field(default="", description="Optional MaiBot model name to reuse.")
    model_identifier: str = Field(default="qwen3.6-flash", description="Provider model identifier.")
    enable_thinking: bool = Field(default=False, description="Whether to enable provider reasoning mode.")
    temperature: float = Field(default=0.5, description="Video analysis model temperature.")
    max_tokens: int = Field(default=2200, description="Maximum tokens for one video analysis request.")
    timeout_sec: float = Field(default=180.0, description="Video analysis request timeout seconds.")
    direct_video_max_mb: int = Field(
        default=32,
        description="Try direct video upload only when the prepared MP4 is at or below this size.",
    )
    fallback_frame_interval_sec: int = Field(
        default=12,
        description="Frame sampling interval used by the image fallback path when direct video input is unavailable.",
    )
    fallback_max_frames: int = Field(
        default=16,
        description="Maximum sampled frames sent in the fallback image-analysis path.",
    )
    system_prompt: str = Field(
        default=(
            "You analyze a Bilibili video for a livestream host. "
            "Return strict JSON only. Extract timeline beats, reusable jokes, lore, facts, and hookable conversation material. "
            "Write lively but concrete Chinese. Multiple memory entries are allowed and should stay linked to the video source."
        ),
        description="System prompt sent to the video analysis model.",
    )

    @field_validator("api_provider", "model_name", "model_identifier", "system_prompt", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("temperature", "timeout_sec", mode="before")
    @classmethod
    def normalize_float_fields(cls, value: Any, info: ValidationInfo) -> float:
        defaults = {"temperature": 0.5, "timeout_sec": 180.0}
        parsed = normalize_positive_float(value, defaults.get(info.field_name, 1.0))
        if info.field_name == "temperature":
            return min(2.0, max(0.0, parsed))
        return parsed

    @field_validator("max_tokens", "direct_video_max_mb", "fallback_frame_interval_sec", "fallback_max_frames", mode="before")
    @classmethod
    def normalize_int_fields(cls, value: Any, info: ValidationInfo) -> int:
        defaults = {
            "max_tokens": 2200,
            "direct_video_max_mb": 32,
            "fallback_frame_interval_sec": 12,
            "fallback_max_frames": 16,
        }
        parsed = normalize_non_negative_int(value) or defaults.get(info.field_name, 1)
        return max(1, parsed)


class VideoWatchConfig(PluginConfigBase):
    """Idle-time Bilibili video watch settings."""

    __ui_label__: ClassVar[str] = "Video Watch"
    __ui_order__: ClassVar[int] = 9

    enabled: bool = Field(
        default=False,
        description="Enable the idle-time Bilibili video watch subsystem.",
        json_schema_extra={"label": "Enable video watch", "order": 0},
    )
    auto_offer_enabled: bool = Field(
        default=False,
        description="Ask chat whether to start watching a Bilibili video when the room is idle.",
        json_schema_extra={"label": "Auto offer on idle", "order": 1},
    )
    offer_prompt: str = Field(
        default=(
            "弹幕暂时安静了。要不要我开个 B 站视频一起看？"
            "我会先解析视频，再一边放一边按时间轴吐槽。"
            "想开就回 看 / 开，不想开就回 不看 / 别开。"
        ),
        description="Prompt injected when the room is idle and video watch is being offered.",
        json_schema_extra={"label": "Offer prompt", "order": 2},
    )
    yes_aliases: list[str] = Field(
        default_factory=lambda: ["看", "开", "开启", "可以", "行", "yes", "y"],
        description="Exact short replies treated as consent to start video watch.",
        json_schema_extra={"label": "Yes aliases", "order": 3},
    )
    no_aliases: list[str] = Field(
        default_factory=lambda: ["不看", "别开", "不要", "no", "n"],
        description="Exact short replies treated as rejection for the current offer.",
        json_schema_extra={"label": "No aliases", "order": 4},
    )
    decline_cooldown_sec: float = Field(
        default=300.0,
        description="Seconds to wait before auto-offering again after chat explicitly declines.",
        json_schema_extra={"label": "Decline cooldown", "order": 5, "step": 1},
    )
    work_dir: str = Field(
        default="",
        description="Directory used for downloaded videos, extracted frames, and temporary video-watch artifacts.",
        json_schema_extra={"label": "Work dir", "order": 6},
    )
    ffmpeg_command: str = Field(
        default="ffmpeg",
        description="ffmpeg executable used for DASH stream merging and frame extraction.",
        json_schema_extra={"label": "FFmpeg command", "order": 7},
    )
    max_cue_lateness_sec: float = Field(
        default=0.35,
        description="Skip a timeline commentary cue when it is this late or later.",
        json_schema_extra={"label": "Max cue lateness", "order": 8, "step": 0.05},
    )
    command: VideoWatchCommandConfig = Field(
        default_factory=VideoWatchCommandConfig,
        description="Manual /watchvideo control settings.",
        json_schema_extra={"label": "Command", "order": 9},
    )
    sources: VideoWatchSourceConfig = Field(
        default_factory=VideoWatchSourceConfig,
        description="Source discovery and browser playback settings.",
        json_schema_extra={"label": "Sources", "order": 10},
    )
    analysis: VideoWatchAnalysisConfig = Field(
        default_factory=VideoWatchAnalysisConfig,
        description="Offline video analysis model settings.",
        json_schema_extra={"label": "Analysis", "order": 11},
    )

    @field_validator("offer_prompt", "work_dir", "ffmpeg_command", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("yes_aliases", "no_aliases", mode="before")
    @classmethod
    def normalize_alias_lists(cls, value: Any) -> list[str]:
        return normalize_string_list(value)

    @field_validator("decline_cooldown_sec", "max_cue_lateness_sec", mode="before")
    @classmethod
    def normalize_float_fields(cls, value: Any, info: ValidationInfo) -> float:
        defaults = {"decline_cooldown_sec": 300.0, "max_cue_lateness_sec": 0.35}
        parsed = normalize_float(value, defaults.get(info.field_name, 1.0))
        return max(0.0, parsed)


class STS2CommandConfig(PluginConfigBase):
    """Admin command settings for STS2 control."""

    start_command: str = Field(default="/sts2start", description="Admin command that starts STS2 gameplay.")
    stop_command: str = Field(default="/sts2stop", description="Admin command that stops STS2 gameplay.")
    status_command: str = Field(default="/sts2status", description="Admin command that reports STS2 status.")
    admin_user_ids: list[str] = Field(default_factory=list, description="Bilibili user ids allowed to control STS2.")
    super_chat_start_min_price: float = Field(
        default=20.0,
        description="Minimum super-chat price allowed to start STS2 with the start command.",
    )
    drop_non_admin_slash_commands: bool = Field(
        default=True,
        description="Drop slash commands from non-admin users before they enter MaiBot.",
    )

    @field_validator("start_command", "stop_command", "status_command", mode="before")
    @classmethod
    def normalize_command(cls, value: Any) -> str:
        command = normalize_string(value)
        if not command:
            return ""
        return command if command.startswith("/") else f"/{command}"

    @field_validator("admin_user_ids", mode="before")
    @classmethod
    def normalize_admin_user_ids(cls, value: Any) -> list[str]:
        return normalize_string_list(value)

    @field_validator("super_chat_start_min_price", mode="before")
    @classmethod
    def normalize_super_chat_start_min_price(cls, value: Any) -> float:
        return max(0.0, normalize_float(value, 20.0))


class STS2MCPConfig(PluginConfigBase):
    """MCP transport settings for the STS2-Agent server."""

    transport: str = Field(default="stdio", description="MCP transport: stdio, streamable_http, or sse.")
    server_command: str = Field(default="uv", description="Command used to start the stdio MCP server.")
    server_args: list[str] = Field(
        default_factory=lambda: ["run", "sts2-mcp-server"],
        description="Arguments passed to server_command.",
    )
    server_cwd: str = Field(default="", description="Working directory for the STS2-Agent mcp_server folder.")
    streamable_http_url: str = Field(default="", description="Optional streamable HTTP MCP endpoint.")
    sse_url: str = Field(default="", description="Optional SSE MCP endpoint.")
    api_base_url: str = Field(
        default="http://127.0.0.1:18080",
        description="STS2AIAgent Mod HTTP API base URL passed to the MCP server.",
    )
    tool_profile: str = Field(default="guided", description="STS2 MCP tool profile.")
    connect_timeout_sec: float = Field(default=10.0, description="MCP connection timeout seconds.")
    action_timeout_sec: float = Field(default=30.0, description="Timeout for an STS2 action call.")
    wait_actionable_timeout_sec: float = Field(default=120.0, description="Timeout while waiting for actionable state.")

    @field_validator(
        "transport",
        "server_command",
        "server_cwd",
        "streamable_http_url",
        "sse_url",
        "api_base_url",
        "tool_profile",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("server_args", mode="before")
    @classmethod
    def normalize_server_args(cls, value: Any) -> list[str]:
        normalized = normalize_string_list(value)
        return normalized or ["run", "sts2-mcp-server"]

    @field_validator("connect_timeout_sec", "action_timeout_sec", "wait_actionable_timeout_sec", mode="before")
    @classmethod
    def normalize_timeout(cls, value: Any) -> float:
        return normalize_positive_float(value, 10.0)


class STS2LLMConfig(PluginConfigBase):
    """LLM settings for the separate STS2 decision caller."""

    api_provider: str = Field(default="BaiLian", description="MaiBot model_config API provider name.")
    model_name: str = Field(default="", description="Optional MaiBot model name to reuse.")
    model_identifier: str = Field(default="qwen3.6-flash", description="Provider model identifier.")
    enable_thinking: bool = Field(default=False, description="Whether to enable provider reasoning mode.")
    temperature: float = Field(default=0.2, description="Decision model temperature.")
    max_tokens: int = Field(default=1200, description="Maximum tokens for one decision.")
    timeout_sec: float = Field(default=60.0, description="Decision request timeout seconds.")

    @field_validator("api_provider", "model_name", "model_identifier", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("temperature", "timeout_sec", mode="before")
    @classmethod
    def normalize_float_fields(cls, value: Any, info: ValidationInfo) -> float:
        defaults = {"temperature": 0.2, "timeout_sec": 60.0}
        return normalize_positive_float(value, defaults.get(info.field_name, 1.0))

    @field_validator("max_tokens", mode="before")
    @classmethod
    def normalize_max_tokens(cls, value: Any) -> int:
        return max(1, normalize_non_negative_int(value) or 1200)


class STS2NarrationConfig(PluginConfigBase):
    """MaiBot narration and synchronization settings for STS2."""

    priority_over_danmaku: bool = Field(default=True, description="Pause normal danmaku routing while STS2 is active.")
    action_on_audio_start: bool = Field(default=True, description="Execute actions when commentary audio starts.")
    require_audio_start: bool = Field(default=True, description="Abort a pending action if speech cannot start.")
    audio_start_timeout_ms: int = Field(default=180000, description="Maximum wait for a speech-start signal.")
    max_recent_steps: int = Field(default=8, description="Recent STS2 history items retained in decision prompts.")
    combat_action_delay_min_sec: float = Field(
        default=0.0,
        description="Minimum random delay before an in-combat action executes immediately.",
    )
    combat_action_delay_max_sec: float = Field(
        default=1.0,
        description="Maximum random delay before an in-combat action executes immediately.",
    )

    @field_validator("audio_start_timeout_ms", "max_recent_steps", mode="before")
    @classmethod
    def normalize_counts(cls, value: Any, info: ValidationInfo) -> int:
        defaults = {"audio_start_timeout_ms": 180000, "max_recent_steps": 8}
        return max(1, normalize_non_negative_int(value) or defaults.get(info.field_name, 1))

    @field_validator("combat_action_delay_min_sec", "combat_action_delay_max_sec", mode="before")
    @classmethod
    def normalize_combat_action_delay(cls, value: Any, info: ValidationInfo) -> float:
        defaults = {
            "combat_action_delay_min_sec": 0.0,
            "combat_action_delay_max_sec": 1.0,
        }
        return max(0.0, normalize_float(value, defaults.get(info.field_name, 0.0)))

    @model_validator(mode="after")
    def normalize_combat_action_delay_range(self) -> "STS2NarrationConfig":
        if self.combat_action_delay_max_sec < self.combat_action_delay_min_sec:
            self.combat_action_delay_min_sec, self.combat_action_delay_max_sec = (
                self.combat_action_delay_max_sec,
                self.combat_action_delay_min_sec,
            )
        return self


class STS2LoggingConfig(PluginConfigBase):
    """Dedicated STS2-Agent diagnostics output."""

    enabled: bool = Field(default=True, description="Write STS2-Agent diagnostics to a dedicated log file.")
    directory: str = Field(default="logs/sts2agent", description="Local directory for STS2-Agent log files.")
    open_window: bool = Field(default=True, description="Open a separate PowerShell window tailing the log file.")
    window_title: str = Field(default="MaiBot STS2-Agent Logs", description="Title of the STS2 log window.")
    tail_lines: int = Field(default=200, description="Initial lines shown in the STS2 log window.")
    capture_mcp_stderr: bool = Field(default=True, description="Redirect the MCP server stderr stream to this log.")
    file_prefix: str = Field(default="sts2agent", description="Prefix for STS2-Agent log files.")

    @field_validator("directory", "window_title", "file_prefix", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("tail_lines", mode="before")
    @classmethod
    def normalize_tail_lines(cls, value: Any) -> int:
        return max(1, normalize_non_negative_int(value) or 200)


class STS2Config(PluginConfigBase):
    """Slay the Spire 2 autonomous gameplay integration."""

    __ui_label__: ClassVar[str] = "STS2"
    __ui_order__: ClassVar[int] = 9

    enabled: bool = Field(default=False, description="Enable the STS2 gameplay integration.")
    commands: STS2CommandConfig = Field(default_factory=STS2CommandConfig)
    mcp: STS2MCPConfig = Field(default_factory=STS2MCPConfig)
    llm: STS2LLMConfig = Field(default_factory=STS2LLMConfig)
    narration: STS2NarrationConfig = Field(default_factory=STS2NarrationConfig)
    logging: STS2LoggingConfig = Field(default_factory=STS2LoggingConfig)


class SubtitleTranslationConfig(STS2LLMConfig):
    """LLM settings for English-to-Chinese subtitle translation."""

    api_provider: str = Field(default="", description="MaiBot model_config API provider name.")
    model_name: str = Field(default="deepseek-v4-flash", description="Optional MaiBot model name to reuse.")
    model_identifier: str = Field(default="", description="Provider model identifier.")
    temperature: float = Field(default=0.1, description="Subtitle translation model temperature.")
    max_tokens: int = Field(default=800, description="Maximum tokens for one subtitle translation.")
    timeout_sec: float = Field(default=30.0, description="Subtitle translation request timeout seconds.")

    @field_validator("temperature", "timeout_sec", mode="before")
    @classmethod
    def normalize_float_fields(cls, value: Any, info: ValidationInfo) -> float:
        defaults = {"temperature": 0.1, "timeout_sec": 30.0}
        return normalize_positive_float(value, defaults.get(info.field_name, 1.0))

    @field_validator("max_tokens", mode="before")
    @classmethod
    def normalize_max_tokens(cls, value: Any) -> int:
        return max(1, normalize_non_negative_int(value) or 800)


class LanguageConfig(PluginConfigBase):
    """AI spoken/subtitle language mode."""

    __ui_label__: ClassVar[str] = "Language"
    __ui_order__: ClassVar[int] = 9

    mode: str = Field(
        default="chinese",
        description=(
            "AI language mode: chinese, english_voice_chinese_subtitle, cantonese, "
            "cantonese_voice_chinese_subtitle, japanese, or japanese_voice_chinese_subtitle."
        ),
        json_schema_extra={
            "label": "AI language mode",
            "order": 0,
            "enum": [
                "chinese",
                "english_voice_chinese_subtitle",
                "cantonese",
                "cantonese_voice_chinese_subtitle",
                "japanese",
                "japanese_voice_chinese_subtitle",
            ],
        },
    )
    english_system_prompt: str = Field(
        default=(
            "Use English as the main spoken language. If the audience writes Chinese, understand the meaning "
            "internally and answer mostly in English. Short Chinese names, quoted phrases, memes, or brief "
            "punchline words are allowed when they improve the joke, but avoid Chinese-only replies or "
            "multi-sentence Chinese runs in English mode. Chinese subtitles are generated separately. "
            "Keep the streamer personality and short live-reaction style. Do not switch the session language "
            "unless the operator changes the live adapter mode."
        ),
        description="Instruction appended to the live-room planner in English voice mode.",
        json_schema_extra={"label": "English mode prompt", "order": 1},
    )
    cantonese_system_prompt: str = Field(
        default=(
            "Reply in natural Cantonese-English mixed spoken style. Use written Cantonese words such as "
            "咁、唔、佢、喺、嚟、嘅、啱、冇、咗 when appropriate. Do not reply in Mandarin or "
            "standard written Chinese unless the audience explicitly asks for another language. Keep the same "
            "streamer personality, short live-reaction style, and do not mention translation or subtitles."
        ),
        description="Instruction appended to the live-room planner in Cantonese voice mode.",
        json_schema_extra={"label": "Cantonese mode prompt", "order": 2},
    )
    cantonese_tts_text_lang: str = Field(
        default="yue",
        description=(
            "GPT-SoVITS text_lang used in Cantonese modes. Use yue for Cantonese-capable models; "
            "use zh or auto when the current model is silent for yue phones."
        ),
        json_schema_extra={
            "label": "Cantonese TTS text_lang",
            "order": 3,
            "enum": ["yue", "all_yue", "zh", "auto", "auto_yue"],
        },
    )
    japanese_system_prompt: str = Field(
        default=(
            "Reply in natural Japanese spoken style. Use Japanese words and grammar - "
            "do NOT reply in Chinese or English unless the audience explicitly asks for another language. "
            "Keep the same streamer personality, short live-reaction style, and do not mention translation or subtitles."
        ),
        description="Instruction appended to the live-room planner in Japanese voice mode.",
        json_schema_extra={"label": "Japanese mode prompt", "order": 4},
    )
    japanese_tts_text_lang: str = Field(
        default="ja",
        description=(
            "GPT-SoVITS text_lang used in Japanese modes. Use ja for Japanese-capable models; "
            "use zh or auto when the current model is silent for ja phones."
        ),
        json_schema_extra={
            "label": "Japanese TTS text_lang",
            "order": 5,
            "enum": ["ja", "all_ja", "zh", "auto", "auto_ja"],
        },
    )
    translation: SubtitleTranslationConfig = Field(default_factory=SubtitleTranslationConfig)

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value: Any) -> str:
        mode = normalize_string(value).lower().replace("-", "_").replace(" ", "_")
        if mode in {"", "zh", "cn", "chinese", "\u4e2d\u6587", "normal", "default"}:
            return "chinese"
        if mode in {
            "en",
            "english",
            "bilingual",
            "bilingual_subtitle",
            "english_voice",
            "english_voice_chinese_subtitle",
            "english_voice_zh_subtitle",
            "\u4e2d\u5b57\u82f1\u58f0",
        }:
            return "english_voice_chinese_subtitle"
        if mode in {
            "yue",
            "cantonese",
            "cantonese_voice",
            "cantonese_original",
            "\u7ca4\u8bed",
            "\u5ee3\u6771\u8a71",
            "\u5e7f\u4e1c\u8bdd",
        }:
            return "cantonese"
        if mode in {
            "yue_voice_chinese_subtitle",
            "yue_voice_zh_subtitle",
            "cantonese_voice_chinese_subtitle",
            "cantonese_voice_zh_subtitle",
            "cantonese_chinese_subtitle",
            "cantonese_zh_subtitle",
            "\u7ca4\u8bed\u4e2d\u5b57",
            "\u7ca4\u8bed\u4e2d\u6587\u5b57\u5e55",
            "\u7ca4\u8bed\u8bed\u97f3\u4e2d\u6587\u5b57\u5e55",
            "\u7ca4\u8bed\u8f6c\u4e2d\u6587\u5b57\u5e55",
            "\u5ee3\u6771\u8a71\u4e2d\u5b57",
            "\u5e7f\u4e1c\u8bdd\u4e2d\u5b57",
        }:
            return "cantonese_voice_chinese_subtitle"
        if mode in {
            "ja",
            "jp",
            "japanese",
            "\u65e5\u8bed",
            "\u65e5\u6587",
            "\u65e5\u8bed\u6a21\u5f0f",
            "\u65e5\u6587\u6a21\u5f0f",
        }:
            return "japanese"
        if mode in {
            "ja_voice_chinese_subtitle",
            "ja_voice_zh_subtitle",
            "japanese_voice_chinese_subtitle",
            "japanese_voice_zh_subtitle",
            "japanese_chinese_subtitle",
            "japanese_zh_subtitle",
            "\u65e5\u8bed\u4e2d\u5b57",
            "\u65e5\u8bed\u4e2d\u6587\u5b57\u5e55",
            "\u65e5\u8bed\u8bed\u97f3\u4e2d\u6587\u5b57\u5e55",
            "\u65e5\u6587\u4e2d\u5b57",
            "\u65e5\u6587\u4e2d\u6587\u5b57\u5e55",
        }:
            return "japanese_voice_chinese_subtitle"
        return "chinese"

    @field_validator("english_system_prompt", "cantonese_system_prompt", "japanese_system_prompt", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("cantonese_tts_text_lang", mode="before")
    @classmethod
    def normalize_cantonese_tts_text_lang(cls, value: Any) -> str:
        lang = normalize_string(value).lower().replace("-", "_").replace(" ", "_")
        if lang in {"", "yue", "粤英混合", "粵英混合", "cantonese_english", "cantonese_mix"}:
            return "yue"
        if lang in {"all_yue", "all_cantonese", "cantonese", "粤语", "粵語", "廣東話", "广东话"}:
            return "all_yue"
        if lang in {"zh", "cn", "chinese", "mandarin", "中文", "普通话", "普通話"}:
            return "zh"
        if lang in {"auto", "mixed", "multi", "multilingual", "多语种混合", "多語種混合"}:
            return "auto"
        if lang in {"auto_yue", "auto_cantonese"}:
            return "auto_yue"
        return "yue"

    @field_validator("japanese_tts_text_lang", mode="before")
    @classmethod
    def normalize_japanese_tts_text_lang(cls, value: Any) -> str:
        lang = normalize_string(value).lower().replace("-", "_").replace(" ", "_")
        if lang in {"", "ja", "jp", "japanese", "日语", "日文", "日本語"}:
            return "ja"
        if lang in {"all_ja", "all_japanese"}:
            return "all_ja"
        if lang in {"zh", "cn", "chinese", "mandarin", "中文", "普通话", "普通話"}:
            return "zh"
        if lang in {"auto", "mixed", "multi", "multilingual", "多语种混合", "多語種混合"}:
            return "auto"
        if lang in {"auto_ja", "auto_japanese"}:
            return "auto_ja"
        return "ja"

    def is_english_voice_chinese_subtitle(self) -> bool:
        return self.mode == "english_voice_chinese_subtitle"

    def is_cantonese(self) -> bool:
        return self.mode == "cantonese"

    def is_cantonese_voice_chinese_subtitle(self) -> bool:
        return self.mode == "cantonese_voice_chinese_subtitle"

    def is_cantonese_mode(self) -> bool:
        return self.mode in {"cantonese", "cantonese_voice_chinese_subtitle"}

    def is_japanese(self) -> bool:
        return self.mode == "japanese"

    def is_japanese_voice_chinese_subtitle(self) -> bool:
        return self.mode == "japanese_voice_chinese_subtitle"

    def is_japanese_mode(self) -> bool:
        return self.mode in {"japanese", "japanese_voice_chinese_subtitle"}

    def uses_translated_chinese_subtitle(self) -> bool:
        return self.mode in {"english_voice_chinese_subtitle", "cantonese_voice_chinese_subtitle", "japanese_voice_chinese_subtitle"}

    def subtitle_translation_source_language(self) -> str:
        if self.is_cantonese_voice_chinese_subtitle():
            return "Cantonese"
        if self.is_japanese_voice_chinese_subtitle():
            return "Japanese"
        return "English"


def default_netease_model() -> str:
    system_name = normalize_string(platform.system()) or "Windows"
    machine_name = normalize_string(platform.machine()).replace(" ", "") or "x64"
    if machine_name.lower() in {"amd64", "x86_64"}:
        machine_name = "x64"
    return f"{system_name}_{machine_name}_cli"


def default_netease_os_version() -> str:
    return normalize_string(platform.version()) or "10.0"


class SongRequestConfig(PluginConfigBase):
    """RVC song request settings."""

    __ui_label__: ClassVar[str] = "Song Request"
    __ui_order__: ClassVar[int] = 10

    enabled: bool = Field(default=False, description="Enable RVC song requests.")
    hard_disable: bool = Field(
        default=False,
        description="Force-disable all song request and RVC behavior, including queueing, playback, and console.",
    )
    source_provider: str = Field(
        default="audius",
        description="Music source provider for song requests: audius, netease, direct, or local.",
    )
    audius_api_base_url: str = Field(default="https://api.audius.co/v1", description="Audius API base URL.")
    audius_api_key: str = Field(default="", description="Optional Audius API key sent as x-api-key.")
    audius_bearer_token: str = Field(default="", description="Optional Audius bearer token.")
    audius_search_limit: int = Field(default=5, description="Maximum Audius search results to inspect.")
    audius_require_downloadable: bool = Field(
        default=False,
        description="Only accept Audius tracks marked as downloadable.",
    )
    netease_api_base_url: str = Field(
        default="https://openncm.music.163.com",
        description="NetEase Cloud Music OpenNCM/OpenAPI base URL used by the official CLI app.",
    )
    netease_app_id: str = Field(default="", description="NetEase OpenAPI AppID.")
    netease_app_secret: str = Field(default="", description="NetEase OpenAPI AppSecret, kept for console reference.")
    netease_public_key: str = Field(default="", description="NetEase OpenAPI public key.")
    netease_private_key: str = Field(default="", description="NetEase OpenAPI private key for RSA_SHA256 signing.")
    netease_access_token: str = Field(default="", description="Optional preset OpenAPI accessToken.")
    netease_token_cache_path: str = Field(default="", description="Optional OpenAPI token cache path.")
    netease_device_id: str = Field(default="ncmcli_maibotlive001", description="Stable unique deviceId for OpenAPI.")
    netease_device_type: str = Field(default="openapi", description="OpenAPI device.deviceType.")
    netease_os: str = Field(default="ncmcli", description="OpenAPI device.os.")
    netease_app_ver: str = Field(default="0.1.1", description="OpenAPI device.appVer, x.x.x format.")
    netease_channel: str = Field(default="ncmcli", description="OpenAPI device.channel.")
    netease_brand: str = Field(default="ncmcli", description="OpenAPI device.brand.")
    netease_model: str = Field(
        default_factory=default_netease_model,
        description="OpenAPI device.model. Leave as-is unless NetEase assigned a different model string.",
    )
    netease_os_ver: str = Field(
        default_factory=default_netease_os_version,
        description="OpenAPI device.osVer. Leave blank only if you want runtime auto-detection.",
    )
    netease_client_ip: str = Field(
        default="",
        description="OpenAPI device.clientIp. Leave blank to auto-detect the current public IP.",
    )
    netease_flow_flag: str = Field(default="", description="Optional OpenAPI device.flowFlag.")
    netease_cookie: str = Field(default="", description="Deprecated; OpenAPI mode does not use local API cookies.")
    netease_user_agent: str = Field(default="ncm-0.1.1", description="User-Agent used for official NetEase OpenAPI requests.")
    netease_referer: str = Field(default="https://music.163.com/", description="Referer used for official NetEase OpenAPI requests.")
    netease_auto_qr_login_on_unauthorized: bool = Field(
        default=False,
        description="When OpenAPI responds with a login-required code, print a QR login link and retry after authorization.",
    )
    netease_qr_login_timeout_sec: float = Field(
        default=180.0,
        description="Maximum seconds to wait for NetEase QR login confirmation.",
    )
    netease_qr_poll_interval_sec: float = Field(
        default=3.0,
        description="Polling interval in seconds while waiting for NetEase QR login confirmation.",
    )
    console_enabled: bool = Field(default=False, description="Enable a dedicated song-request console window and log channel.")
    console_open_window: bool = Field(default=True, description="Open the song-request console in a separate terminal window.")
    console_window_title: str = Field(default="MaiBot Song Requests", description="Title of the song-request console window.")
    console_directory: str = Field(default="", description="Optional directory for song-request console logs, state, and command files.")
    netease_search_limit: int = Field(default=5, description="Maximum search results to inspect.")
    netease_song_level: str = Field(default="standard", description="NetEase song playback level or bitrate.")
    connect_timeout_sec: float = Field(default=10.0, description="HTTP connect timeout in seconds.")
    request_timeout_sec: float = Field(default=120.0, description="HTTP request timeout in seconds.")
    download_user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        ),
        description="User-Agent used when downloading song audio.",
    )
    max_song_duration_sec: int = Field(default=420, description="Reject songs longer than this duration.")
    max_queue_size: int = Field(default=3, description="Maximum queued song requests.")
    work_dir: str = Field(default="", description="Working directory for generated song audio.")
    ffmpeg_command: str = Field(default="ffmpeg", description="ffmpeg executable or command.")
    ffprobe_command: str = Field(default="ffprobe", description="ffprobe executable or command.")
    separation_command_template: str = Field(
        default=(
            '"{plugin_dir}/data/rvc_runtime_py310/Scripts/python.exe" '
            '"{plugin_dir}/audio_separator_bridge.py" '
            '--input "{input_wav}" --output-dir "{separation_dir}" '
            '--model-file-dir "{plugin_data_dir}/audio_separator_models" '
            '--model-filename "MDX23C-8KFFT-InstVoc_HQ.ckpt" '
            "--sample-rate 44100 --output-format WAV"
        ),
        description="Shell command template that separates vocals and instrumental audio.",
    )
    separated_vocals_path_template: str = Field(
        default="{separation_dir}/{stem_name}/vocals.wav",
        description="Path template for separated vocals wav.",
    )
    separated_instrumental_path_template: str = Field(
        default="{separation_dir}/{stem_name}/no_vocals.wav",
        description="Path template for separated instrumental wav.",
    )
    rvc_command_template: str = Field(
        default=(
            '"{plugin_dir}/data/rvc_runtime_py310/Scripts/python.exe" '
            '"{plugin_dir}/rvc_infer_bridge.py" '
            '--input "{vocals_wav}" --output "{rvc_vocals_wav}" '
            '--model-path "{rvc_model_path}" --index-path "{rvc_index_path}" '
            '--pitch "{rvc_pitch}" --split-audio --f0-method rmvpe '
            "--index-rate 0.45 --filter-radius 5 "
            '--protect 0.33 --export-format WAV --embedder-model contentvec'
        ),
        description="Shell command template that runs RVC on the separated vocal wav.",
    )
    rvc_model_path: str = Field(default="", description="RVC model path or model id.")
    rvc_index_path: str = Field(default="", description="Optional RVC feature index path.")
    rvc_pitch: int = Field(default=0, description="Pitch shift passed to the RVC command template.")
    command_timeout_sec: float = Field(default=1800.0, description="Timeout for separation/RVC/ffmpeg commands.")
    wait_prompt_template: str = Field(
        default=(
            "\u8bf7\u544a\u77e5\u89c2\u4f17\u7a0d\u7b49\uff0c"
            "\u7406\u7531\u662f\u6b63\u5728\u68c0\u7d22\u539f\u66f2\u3001"
            "\u62c6\u4eba\u58f0\u3001\u6362\u58f0\u7ebf\u548c\u91cd\u65b0\u6df7\u97f3\u3002"
        ),
        description="Prompt returned to MaiBot immediately after a request is queued.",
    )
    ready_prompt_template: str = Field(
        default=(
            "\u70b9\u7684\u6b4c\u300a{song_title}\u300b\u5df2\u7ecf\u51c6\u5907\u597d\u4e86\uff0c"
            "Neuro\u9a6c\u4e0a\u5f00\u5531\u3002"
        ),
        description="Text spoken before song playback.",
    )
    failure_prompt_template: str = Field(
        default="\u8fd9\u9996\u6b4c\u6682\u65f6\u5904\u7406\u5931\u8d25\u4e86\uff1a\u300a{song_title}\u300b\u3002",
        description="Text spoken when processing fails.",
    )
    subtitle_template: str = Field(
        default="Neuro\u6b63\u5728\u5531\uff1a{song_title}",
        description="Fixed subtitle text template during song playback.",
    )
    cleanup_successful_tasks: bool = Field(default=False, description="Remove successful working directories.")

    @field_validator(
        "netease_api_base_url",
        "netease_app_id",
        "netease_app_secret",
        "netease_public_key",
        "netease_private_key",
        "source_provider",
        "audius_api_base_url",
        "audius_api_key",
        "audius_bearer_token",
        "netease_access_token",
        "netease_token_cache_path",
        "netease_device_id",
        "netease_device_type",
        "netease_os",
        "netease_app_ver",
        "netease_channel",
        "netease_brand",
        "netease_model",
        "netease_os_ver",
        "netease_client_ip",
        "netease_flow_flag",
        "netease_cookie",
        "netease_user_agent",
        "netease_referer",
        "console_window_title",
        "console_directory",
        "netease_song_level",
        "download_user_agent",
        "work_dir",
        "ffmpeg_command",
        "ffprobe_command",
        "separation_command_template",
        "separated_vocals_path_template",
        "separated_instrumental_path_template",
        "rvc_command_template",
        "rvc_model_path",
        "rvc_index_path",
        "wait_prompt_template",
        "ready_prompt_template",
        "failure_prompt_template",
        "subtitle_template",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("source_provider", mode="before")
    @classmethod
    def normalize_source_provider(cls, value: Any) -> str:
        normalized = normalize_string(value).lower().replace("-", "_").replace(" ", "_")
        if normalized in {"", "audius"}:
            return "audius"
        if normalized in {"netease", "net_ease", "163", "ncm"}:
            return "netease"
        if normalized in {"direct", "url", "http", "https"}:
            return "direct"
        if normalized in {"local", "file", "path"}:
            return "local"
        return normalized

    @field_validator(
        "audius_search_limit",
        "netease_search_limit",
        "max_song_duration_sec",
        "max_queue_size",
        mode="before",
    )
    @classmethod
    def normalize_counts(cls, value: Any, info: ValidationInfo) -> int:
        defaults = {
            "audius_search_limit": 5,
            "netease_search_limit": 5,
            "max_song_duration_sec": 420,
            "max_queue_size": 3,
        }
        parsed = normalize_non_negative_int(value)
        return max(1, parsed or defaults.get(info.field_name, 1))

    @field_validator(
        "connect_timeout_sec",
        "request_timeout_sec",
        "command_timeout_sec",
        "netease_qr_login_timeout_sec",
        "netease_qr_poll_interval_sec",
        mode="before",
    )
    @classmethod
    def normalize_timeouts(cls, value: Any, info: ValidationInfo) -> float:
        defaults = {
            "connect_timeout_sec": 10.0,
            "request_timeout_sec": 120.0,
            "command_timeout_sec": 1800.0,
            "netease_qr_login_timeout_sec": 180.0,
            "netease_qr_poll_interval_sec": 3.0,
        }
        return normalize_positive_float(value, defaults.get(info.field_name, 10.0))

    @field_validator("rvc_pitch", mode="before")
    @classmethod
    def normalize_pitch(cls, value: Any) -> int:
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return 0

    def is_available(self) -> bool:
        return bool(self.enabled and not self.hard_disable)


class LocalVoiceInputConfig(PluginConfigBase):
    """Local microphone capture and transcription settings."""

    __ui_label__: ClassVar[str] = "Local Voice"
    __ui_order__: ClassVar[int] = 12

    enabled: bool = Field(
        default=False,
        description="Continuously capture the local microphone and inject transcripts into MaiBot.",
        json_schema_extra={"label": "Enable local voice", "order": 0},
    )
    speaker_user_id: str = Field(
        default="local-mic",
        description="Synthetic user id used for local microphone transcripts.",
        json_schema_extra={"label": "Speaker user id", "order": 1},
    )
    speaker_username: str = Field(
        default="Local Mic",
        description="Display name used for local microphone transcripts.",
        json_schema_extra={"label": "Speaker name", "order": 2},
    )
    input_device: str = Field(
        default="",
        description="Optional microphone device name fragment. Empty uses the default input device.",
        json_schema_extra={"label": "Input device", "order": 3},
    )
    engine: str = Field(
        default="aliyun_rasr",
        description="Streaming speech recognition engine. aliyun_rasr is the active microphone recognition backend.",
        json_schema_extra={"label": "ASR engine", "order": 4},
    )
    rasr_model: str = Field(
        default="fun-asr-realtime",
        description="Aliyun Model Studio realtime ASR model id. Keep configurable for switching hosted RASR models.",
        json_schema_extra={"label": "Aliyun RASR model", "order": 5},
    )
    rasr_ws_url: str = Field(
        default="wss://dashscope.aliyuncs.com/api-ws/v1/inference/",
        description="Aliyun DashScope realtime ASR WebSocket endpoint.",
        json_schema_extra={"label": "Aliyun RASR WebSocket URL", "order": 6},
    )
    rasr_api_key_env: str = Field(
        default="DASHSCOPE_API_KEY",
        description="Environment variable name that contains the DashScope API key.",
        json_schema_extra={"label": "API key environment variable", "order": 7},
    )
    rasr_api_key: str = Field(
        default="",
        description="Optional DashScope API key override. Prefer leaving empty and using the environment variable.",
        json_schema_extra={"label": "API key override", "order": 8},
    )
    rasr_audio_format: str = Field(
        default="pcm",
        description="Audio format sent to Aliyun RASR. Live microphone streaming uses pcm.",
        json_schema_extra={"label": "RASR audio format", "order": 9},
    )
    rasr_language_hint: str = Field(
        default="zh,en",
        description="Realtime recognition language hint, limited to zh and en for bilingual livestream speech.",
        json_schema_extra={"label": "RASR language hint", "order": 10},
    )
    rasr_enable_intermediate_result: bool = Field(
        default=True,
        description="Ask RASR to return intermediate recognition results for realtime UI display.",
        json_schema_extra={"label": "RASR intermediate results", "order": 11},
    )
    rasr_enable_punctuation_prediction: bool = Field(
        default=True,
        description="Ask RASR to add punctuation when supported by the model.",
        json_schema_extra={"label": "RASR punctuation prediction", "order": 12},
    )
    rasr_enable_inverse_text_normalization: bool = Field(
        default=True,
        description="Ask RASR to normalize numbers and common spoken forms when supported.",
        json_schema_extra={"label": "RASR text normalization", "order": 13},
    )
    rasr_max_sentence_silence_ms: int = Field(
        default=800,
        description="Server-side sentence silence boundary in milliseconds.",
        json_schema_extra={"label": "RASR sentence silence", "order": 14},
    )
    rasr_heartbeat: bool = Field(
        default=True,
        description="Keep the RASR session alive through long silence when supported.",
        json_schema_extra={"label": "RASR heartbeat", "order": 15},
    )
    rasr_route_partials_to_maibot: bool = Field(
        default=False,
        description="Route intermediate RASR partial text to MaiBot. Off by default to avoid repeated inputs.",
        json_schema_extra={"label": "Route partials to MaiBot", "order": 16},
    )
    rasr_speech_noise_threshold: float = Field(
        default=0.3,
        description="Aliyun RASR server-side VAD noise threshold in range [-1.0, 1.0].",
        json_schema_extra={"label": "RASR speech noise threshold", "order": 17, "step": 0.1},
    )
    rasr_disfluency_removal_enabled: bool = Field(
        default=False,
        description="Ask RASR to remove filler words and disfluencies when supported.",
        json_schema_extra={"label": "RASR disfluency removal", "order": 18},
    )
    sample_rate_hz: int = Field(
        default=16000,
        description="Microphone capture sample rate in Hz. 16000 is recommended for Aliyun RASR pcm streaming.",
        json_schema_extra={"label": "Capture sample rate", "order": 19},
    )
    channels: int = Field(
        default=1,
        description="Input channel count requested from sounddevice.",
        json_schema_extra={"label": "Channels", "order": 20},
    )
    block_duration_ms: int = Field(
        default=100,
        description="Microphone callback block size in milliseconds.",
        json_schema_extra={"label": "Block duration", "order": 21},
    )
    sherpa_model_type: str = Field(
        default="transducer",
        description="sherpa-onnx online model family: transducer or paraformer.",
        json_schema_extra={"label": "Sherpa model type", "order": 8},
    )
    sherpa_encoder: str = Field(
        default="",
        description="Path to the sherpa-onnx streaming encoder .onnx model.",
        json_schema_extra={"label": "Sherpa encoder", "order": 9},
    )
    sherpa_decoder: str = Field(
        default="",
        description="Path to the sherpa-onnx streaming decoder .onnx model.",
        json_schema_extra={"label": "Sherpa decoder", "order": 10},
    )
    sherpa_joiner: str = Field(
        default="",
        description="Path to the sherpa-onnx streaming joiner .onnx model. Required for transducer models.",
        json_schema_extra={"label": "Sherpa joiner", "order": 11},
    )
    sherpa_tokens: str = Field(
        default="",
        description="Path to the sherpa-onnx tokens.txt file.",
        json_schema_extra={"label": "Sherpa tokens", "order": 12},
    )
    sherpa_provider: str = Field(
        default="cpu",
        description="sherpa-onnx execution provider, usually cpu or cuda.",
        json_schema_extra={"label": "Sherpa provider", "order": 13},
    )
    sherpa_num_threads: int = Field(
        default=2,
        description="CPU worker threads used by sherpa-onnx.",
        json_schema_extra={"label": "Sherpa threads", "order": 14},
    )
    sherpa_model_sample_rate_hz: int = Field(
        default=16000,
        description="Sample rate expected by the sherpa-onnx model, usually 16000.",
        json_schema_extra={"label": "Sherpa model sample rate", "order": 15},
    )
    sherpa_feature_dim: int = Field(
        default=80,
        description="Feature dimension expected by the sherpa-onnx model.",
        json_schema_extra={"label": "Sherpa feature dim", "order": 16},
    )
    sherpa_decoding_method: str = Field(
        default="greedy_search",
        description="sherpa-onnx decoding method, such as greedy_search or modified_beam_search.",
        json_schema_extra={"label": "Sherpa decoding", "order": 17},
    )
    sherpa_max_active_paths: int = Field(
        default=4,
        description="Maximum active paths for modified_beam_search decoding.",
        json_schema_extra={"label": "Sherpa max active paths", "order": 18},
    )
    sherpa_hotwords_file: str = Field(
        default="",
        description="Optional sherpa-onnx hotwords file.",
        json_schema_extra={"label": "Sherpa hotwords file", "order": 19},
    )
    sherpa_hotwords_score: float = Field(
        default=1.5,
        description="Score used by sherpa-onnx hotword biasing.",
        json_schema_extra={"label": "Sherpa hotwords score", "order": 20, "step": 0.1},
    )
    sherpa_blank_penalty: float = Field(
        default=0.0,
        description="Optional positive blank penalty passed to sherpa-onnx.",
        json_schema_extra={"label": "Sherpa blank penalty", "order": 21, "step": 0.1},
    )
    sherpa_enable_endpoint: bool = Field(
        default=False,
        description="Use sherpa-onnx endpoint detection only to reset long recognition context. App-level segmentation stays disabled.",
        json_schema_extra={"label": "Sherpa endpoint reset", "order": 22},
    )
    stable_emit_min_chars: int = Field(
        default=1,
        description="Minimum stable transcript delta length emitted to MaiBot.",
        json_schema_extra={"label": "Stable emit min chars", "order": 23},
    )
    sentence_postprocess_enabled: bool = Field(
        default=True,
        description="Aggregate streaming transcript deltas into low-latency sentence messages before routing to MaiBot.",
        json_schema_extra={"label": "Sentence postprocess", "order": 24},
    )
    sentence_flush_inactivity_ms: int = Field(
        default=700,
        description="Flush the buffered sentence after this much silence in incoming transcript fragments.",
        json_schema_extra={"label": "Sentence flush inactivity", "order": 25},
    )
    sentence_force_emit_chars: int = Field(
        default=16,
        description="Force-send the buffered sentence once it grows beyond this many semantic characters.",
        json_schema_extra={"label": "Sentence force emit chars", "order": 26},
    )
    sentence_auto_punctuation: bool = Field(
        default=True,
        description="Append lightweight terminal punctuation when a routed sentence does not already end with one.",
        json_schema_extra={"label": "Sentence auto punctuation", "order": 27},
    )
    speech_vad_enabled: bool = Field(
        default=True,
        description="Drop non-speech microphone chunks before they reach sherpa-onnx to reduce noise hallucinations.",
        json_schema_extra={"label": "Speech VAD", "order": 28},
    )
    speech_noise_reduction_enabled: bool = Field(
        default=True,
        description="Apply lightweight microphone noise cleanup before speech recognition.",
        json_schema_extra={"label": "Microphone noise reduction", "order": 29},
    )
    speech_vad_start_threshold: float = Field(
        default=0.024,
        description="Minimum RMS energy treated as speech. Higher values suppress more noise but can miss quiet speech.",
        json_schema_extra={"label": "Speech start threshold", "order": 30, "step": 0.001},
    )
    speech_vad_noise_ratio: float = Field(
        default=4.0,
        description="Speech must be this many times louder than the learned noise floor.",
        json_schema_extra={"label": "Speech noise ratio", "order": 31, "step": 0.1},
    )
    speech_vad_hold_ms: int = Field(
        default=250,
        description="Keep passing audio for this long after speech energy drops to avoid chopping word tails.",
        json_schema_extra={"label": "Speech hold ms", "order": 32},
    )
    speech_noise_floor_adaptation: float = Field(
        default=0.05,
        description="How quickly the learned noise floor follows non-speech background noise.",
        json_schema_extra={"label": "Noise floor adaptation", "order": 33, "step": 0.01},
    )
    speech_noise_suppression_strength: float = Field(
        default=0.8,
        description="How strongly low-energy non-speech audio is attenuated when it is allowed through holdover.",
        json_schema_extra={"label": "Noise suppression strength", "order": 34, "step": 0.05},
    )
    speech_reset_on_silence: bool = Field(
        default=True,
        description="Reset the sherpa streaming context when local VAD closes a speech turn.",
        json_schema_extra={"label": "Reset context on silence", "order": 35},
    )
    pre_speech_padding_ms: int = Field(
        default=160,
        description="Prepend this much audio before detected speech so short utterance starts are not clipped.",
        json_schema_extra={"label": "Pre-speech padding", "order": 36},
    )
    silence_duration_ms: int = Field(
        default=650,
        description="Legacy VAD setting retained for config compatibility; unused by sherpa-onnx streaming input.",
        json_schema_extra={"label": "Legacy silence duration", "order": 91},
    )
    min_phrase_duration_ms: int = Field(
        default=400,
        description="Legacy VAD setting retained for config compatibility; unused by sherpa-onnx streaming input.",
        json_schema_extra={"label": "Legacy minimum phrase", "order": 92},
    )
    max_phrase_duration_ms: int = Field(
        default=12000,
        description="Legacy VAD setting retained for config compatibility; unused by sherpa-onnx streaming input.",
        json_schema_extra={"label": "Legacy maximum phrase", "order": 93},
    )
    speech_threshold: float = Field(
        default=0.02,
        description="Legacy VAD setting retained for config compatibility; unused by sherpa-onnx streaming input.",
        json_schema_extra={"label": "Legacy speech threshold", "order": 94, "step": 0.01},
    )
    min_transcript_length: int = Field(
        default=1,
        description="Drop transcriptions shorter than this many characters.",
        json_schema_extra={"label": "Minimum transcript length", "order": 37},
    )

    @field_validator(
        "speaker_user_id",
        "speaker_username",
        "input_device",
        "engine",
        "rasr_model",
        "rasr_ws_url",
        "rasr_api_key_env",
        "rasr_api_key",
        "rasr_audio_format",
        "rasr_language_hint",
        "sherpa_model_type",
        "sherpa_encoder",
        "sherpa_decoder",
        "sherpa_joiner",
        "sherpa_tokens",
        "sherpa_provider",
        "sherpa_decoding_method",
        "sherpa_hotwords_file",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: Any, info: ValidationInfo) -> str:
        normalized = normalize_string(value)
        defaults = {
            "speaker_user_id": "local-mic",
            "speaker_username": "Local Mic",
            "input_device": "",
            "engine": "aliyun_rasr",
            "rasr_model": "fun-asr-realtime",
            "rasr_ws_url": "wss://dashscope.aliyuncs.com/api-ws/v1/inference/",
            "rasr_api_key_env": "DASHSCOPE_API_KEY",
            "rasr_audio_format": "pcm",
            "rasr_language_hint": "zh,en",
            "sherpa_model_type": "transducer",
            "sherpa_provider": "cpu",
            "sherpa_decoding_method": "greedy_search",
        }
        if normalized:
            return normalized
        return defaults.get(info.field_name, "")

    @field_validator("rasr_language_hint", mode="after")
    @classmethod
    def normalize_rasr_language_hint_value(cls, value: str) -> str:
        allowed = {"zh", "en"}
        tokens: list[str] = []
        normalized = str(value or "").strip().lower()
        if not normalized:
            return "zh,en"
        for chunk in normalized.replace("，", ",").replace("；", ",").replace("/", ",").replace("|", ",").split(","):
            for token in chunk.split():
                candidate = token.strip().lower()
                if candidate in allowed and candidate not in tokens:
                    tokens.append(candidate)
        return ",".join(tokens) if tokens else "zh,en"

    @field_validator(
        "sample_rate_hz",
        "channels",
        "block_duration_ms",
        "rasr_max_sentence_silence_ms",
        "sherpa_num_threads",
        "sherpa_model_sample_rate_hz",
        "sherpa_feature_dim",
        "sherpa_max_active_paths",
        "stable_emit_min_chars",
        "sentence_flush_inactivity_ms",
        "sentence_force_emit_chars",
        "silence_duration_ms",
        "min_phrase_duration_ms",
        "max_phrase_duration_ms",
        "min_transcript_length",
        mode="before",
    )
    @classmethod
    def normalize_positive_counts(cls, value: Any, info: ValidationInfo) -> int:
        defaults = {
            "sample_rate_hz": 16000,
            "channels": 1,
            "block_duration_ms": 100,
            "rasr_max_sentence_silence_ms": 800,
            "sherpa_num_threads": 2,
            "sherpa_model_sample_rate_hz": 16000,
            "sherpa_feature_dim": 80,
            "sherpa_max_active_paths": 4,
            "stable_emit_min_chars": 1,
            "sentence_flush_inactivity_ms": 700,
            "sentence_force_emit_chars": 16,
            "silence_duration_ms": 650,
            "min_phrase_duration_ms": 400,
            "max_phrase_duration_ms": 12000,
            "min_transcript_length": 1,
        }
        parsed = normalize_non_negative_int(value)
        return max(1, parsed or defaults.get(info.field_name, 1))

    @field_validator("pre_speech_padding_ms", "speech_vad_hold_ms", mode="before")
    @classmethod
    def normalize_optional_padding(cls, value: Any) -> int:
        return max(0, normalize_non_negative_int(value))

    @field_validator("speech_threshold", "speech_vad_start_threshold", mode="before")
    @classmethod
    def normalize_threshold(cls, value: Any, info: ValidationInfo) -> float:
        defaults = {
            "speech_threshold": 0.02,
            "speech_vad_start_threshold": 0.024,
        }
        parsed = normalize_float(value, defaults.get(info.field_name, 0.02))
        return min(1.0, max(0.0, parsed))

    @field_validator("speech_vad_noise_ratio", mode="before")
    @classmethod
    def normalize_noise_ratio(cls, value: Any) -> float:
        return max(1.0, normalize_float(value, 4.0))

    @field_validator("speech_noise_floor_adaptation", "speech_noise_suppression_strength", mode="before")
    @classmethod
    def normalize_noise_mix(cls, value: Any, info: ValidationInfo) -> float:
        defaults = {
            "speech_noise_floor_adaptation": 0.05,
            "speech_noise_suppression_strength": 0.8,
        }
        parsed = normalize_float(value, defaults.get(info.field_name, 0.05))
        return min(1.0, max(0.0, parsed))

    @field_validator("rasr_speech_noise_threshold", mode="before")
    @classmethod
    def normalize_rasr_speech_noise_threshold(cls, value: Any) -> float:
        return min(1.0, max(-1.0, normalize_float(value, 0.3)))

    @field_validator("sherpa_hotwords_score", mode="before")
    @classmethod
    def normalize_hotwords_score(cls, value: Any) -> float:
        return max(0.0, normalize_float(value, 1.5))

    @field_validator("sherpa_blank_penalty", mode="before")
    @classmethod
    def normalize_blank_penalty(cls, value: Any) -> float:
        return max(0.0, normalize_float(value, 0.0))

    @model_validator(mode="after")
    def validate_phrase_windows(self) -> "LocalVoiceInputConfig":
        if self.max_phrase_duration_ms < self.min_phrase_duration_ms:
            self.max_phrase_duration_ms = self.min_phrase_duration_ms
        return self


class TTSConfig(PluginConfigBase):
    """Local TTS provider settings."""

    __ui_label__: ClassVar[str] = "TTS"
    __ui_order__: ClassVar[int] = 13

    enabled: bool = Field(
        default=False,
        description="Enable local TTS synthesis for outbound MaiBot replies.",
        json_schema_extra={"label": "Enable TTS", "order": 0},
    )
    provider: str = Field(
        default="gpt_sovits_v2",
        description="TTS provider id. Currently only gpt_sovits_v2 is supported.",
        json_schema_extra={"label": "Provider", "order": 1},
    )
    base_url: str = Field(
        default="http://127.0.0.1:9880",
        description="Base URL of the local GPT-SoVITS v2 API.",
        json_schema_extra={"label": "Base URL", "order": 2},
    )
    connect_timeout_sec: float = Field(
        default=30.0,
        description="TTS HTTP connect timeout in seconds.",
        json_schema_extra={"label": "Connect timeout", "order": 3, "step": 1},
    )
    text_lang: str = Field(
        default="zh",
        description="Language code used for synthesized text.",
        json_schema_extra={"label": "Text language", "order": 4},
    )
    ref_audio_path: str = Field(
        default="",
        description="Reference wav path used by GPT-SoVITS.",
        json_schema_extra={"label": "Reference audio", "order": 5},
    )
    aux_ref_audio_paths: list[str] = Field(
        default_factory=list,
        description="Optional auxiliary reference wav paths for GPT-SoVITS.",
        json_schema_extra={"label": "Aux reference audio", "order": 6},
    )
    prompt_text: str = Field(
        default="",
        description="Reference audio transcript used by GPT-SoVITS.",
        json_schema_extra={"label": "Prompt text", "order": 7},
    )
    prompt_lang: str = Field(
        default="zh",
        description="Language code used for the prompt text.",
        json_schema_extra={"label": "Prompt language", "order": 8},
    )
    text_split_method: str = Field(
        default="cut5",
        description="GPT-SoVITS text split method such as cut0 or cut5.",
        json_schema_extra={"label": "Text split", "order": 9},
    )
    top_k: int = Field(default=5, description="GPT-SoVITS top-k sampling.")
    top_p: float = Field(default=1.0, description="GPT-SoVITS top-p sampling.")
    temperature: float = Field(default=1.0, description="GPT-SoVITS temperature.")
    batch_size: int = Field(default=1, description="GPT-SoVITS batch size.")
    batch_threshold: float = Field(default=0.75, description="GPT-SoVITS batch threshold.")
    split_bucket: bool = Field(default=True, description="Enable GPT-SoVITS split_bucket inference.")
    speed_factor: float = Field(default=1.0, description="GPT-SoVITS speed factor.")
    seed: int = Field(default=-1, description="GPT-SoVITS random seed.")
    parallel_infer: bool = Field(default=True, description="Enable GPT-SoVITS parallel inference.")
    repetition_penalty: float = Field(default=1.35, description="GPT-SoVITS repetition penalty.")
    gpt_weights_path: str = Field(
        default="",
        description="Optional GPT model weights path to switch to via /set_gpt_weights on startup.",
        json_schema_extra={"label": "GPT weights path", "order": 20},
    )
    sovits_weights_path: str = Field(
        default="",
        description="Optional SoVITS model weights path to switch to via /set_sovits_weights on startup.",
        json_schema_extra={"label": "SoVITS weights path", "order": 21},
    )
    amplitude_interval_ms: int = Field(default=80, description="Amplitude envelope interval for Live2D lip sync.")
    amplitude_normalization_enabled: bool = Field(
        default=True,
        description="Normalize each synthesized wav amplitude envelope before Live2D lip sync.",
    )
    amplitude_noise_floor: float = Field(
        default=0.015,
        description="Per-chunk RMS values at or below this level are treated as mouth-closed silence.",
    )
    amplitude_peak_percentile: float = Field(
        default=0.95,
        description="Percentile of per-chunk RMS values used as the utterance peak for normalization.",
    )
    amplitude_normalization_gain: float = Field(
        default=1.0,
        description="Gain applied after per-utterance amplitude normalization.",
    )
    audio_playback_enabled: bool = Field(
        default=False,
        description="Optional local wav playback. Plugin-local lip sync no longer depends on this output path.",
    )
    audio_output_device: str = Field(
        default="",
        description="Optional output device name fragment. Empty uses the default system output device.",
    )
    audio_output_volume: float = Field(
        default=1.0,
        description="Playback volume multiplier for native VTube Studio lip sync audio output.",
    )
    output_dir: str = Field(
        default="",
        description="Optional output directory for synthesized wav files. Empty uses plugin data/tts_output.",
    )

    @field_validator(
        "provider",
        "base_url",
        "text_lang",
        "ref_audio_path",
        "prompt_text",
        "prompt_lang",
        "audio_output_device",
        "gpt_weights_path",
        "sovits_weights_path",
    )
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("text_split_method", "output_dir", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("aux_ref_audio_paths", mode="before")
    @classmethod
    def normalize_audio_paths(cls, value: Any) -> list[str]:
        return normalize_string_list(value)

    @field_validator(
        "connect_timeout_sec",
        "top_p",
        "temperature",
        "batch_threshold",
        "speed_factor",
        "amplitude_noise_floor",
        "amplitude_peak_percentile",
        "amplitude_normalization_gain",
        "audio_output_volume",
        mode="before",
    )
    @classmethod
    def normalize_floats(cls, value: Any, info: ValidationInfo) -> float:
        defaults = {
            "connect_timeout_sec": 30.0,
            "top_p": 1.0,
            "temperature": 1.0,
            "batch_threshold": 0.75,
            "speed_factor": 1.0,
            "amplitude_noise_floor": 0.015,
            "amplitude_peak_percentile": 0.95,
            "amplitude_normalization_gain": 1.0,
            "audio_output_volume": 1.0,
        }
        parsed = normalize_positive_float(value, defaults.get(info.field_name, 1.0))
        if info.field_name in {"amplitude_noise_floor", "amplitude_peak_percentile"}:
            return min(1.0, max(0.0, parsed))
        if info.field_name == "amplitude_normalization_gain":
            return min(4.0, max(0.0, parsed))
        if info.field_name == "audio_output_volume":
            return min(2.0, max(0.0, parsed))
        return parsed

    @field_validator("repetition_penalty", mode="before")
    @classmethod
    def normalize_repetition_penalty(cls, value: Any) -> float:
        return normalize_positive_float(value, 1.35)

    @field_validator("top_k", "batch_size", "amplitude_interval_ms", mode="before")
    @classmethod
    def normalize_counts(cls, value: Any, info: ValidationInfo) -> int:
        defaults = {
            "top_k": 5,
            "batch_size": 1,
            "amplitude_interval_ms": 80,
        }
        parsed = normalize_non_negative_int(value)
        return max(1, parsed or defaults.get(info.field_name, 1))

    @field_validator("seed", mode="before")
    @classmethod
    def normalize_seed(cls, value: Any) -> int:
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return -1

    @field_validator("provider", mode="after")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        return value.lower() or "gpt_sovits_v2"

    def is_usable(self) -> bool:
        return bool(
            self.enabled
            and self.provider == "gpt_sovits_v2"
            and self.base_url
            and self.ref_audio_path
            and self.text_lang
            and self.prompt_lang
        )


class SubtitleWebUIStyleConfig(PluginConfigBase):
    """Default subtitle box style for the standalone WebUI."""

    box_width_px: int = Field(default=960, description="Subtitle box width in pixels.")
    box_height_px: int = Field(default=260, description="Subtitle box height in pixels.")
    left_px: int = Field(default=72, description="Subtitle box distance from the left edge.")
    bottom_px: int = Field(default=72, description="Subtitle box distance from the bottom edge.")
    background_color: str = Field(
        default="rgba(10, 14, 22, 0)",
        description="Subtitle box background color.",
    )
    font_family: str = Field(
        default='"Microsoft YaHei UI", "PingFang SC", sans-serif',
        description="Subtitle text font-family CSS value.",
    )
    font_size_px: int = Field(default=34, description="Subtitle font size in pixels.")
    text_color: str = Field(default="#F7F8FB", description="Subtitle text color.")

    @field_validator("background_color", "font_family", "text_color", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("box_width_px", "box_height_px", "left_px", "bottom_px", "font_size_px", mode="before")
    @classmethod
    def normalize_sizes(cls, value: Any, info: ValidationInfo) -> int:
        defaults = {
            "box_width_px": 960,
            "box_height_px": 260,
            "left_px": 72,
            "bottom_px": 72,
            "font_size_px": 34,
        }
        parsed = normalize_non_negative_int(value)
        return max(1, parsed or defaults.get(info.field_name, 1))


class WebUIConfig(PluginConfigBase):
    """Standalone subtitle WebUI config."""

    __ui_label__: ClassVar[str] = "Web UI"
    __ui_order__: ClassVar[int] = 14

    enabled: bool = Field(
        default=False,
        description="Enable the standalone subtitle WebUI server.",
        json_schema_extra={"label": "Enable WebUI", "order": 0},
    )
    host: str = Field(
        default="127.0.0.1",
        description="WebUI listen host.",
        json_schema_extra={"label": "Host", "order": 1},
    )
    port: int = Field(
        default=18182,
        description="WebUI listen port.",
        json_schema_extra={"label": "Port", "order": 2},
    )
    audio_start_ack_timeout_ms: int = Field(
        default=2500,
        description="How long Live2D waits for the browser to confirm audio playback has started.",
        json_schema_extra={"label": "Audio Start ACK Timeout", "order": 3},
    )
    subtitle: SubtitleWebUIStyleConfig = Field(default_factory=SubtitleWebUIStyleConfig)

    @field_validator("host", mode="before")
    @classmethod
    def normalize_host(cls, value: Any) -> str:
        return normalize_string(value) or "127.0.0.1"

    @field_validator("port", mode="before")
    @classmethod
    def normalize_port(cls, value: Any) -> int:
        parsed = normalize_non_negative_int(value)
        return parsed if parsed > 0 else 18182

    @field_validator("audio_start_ack_timeout_ms", mode="before")
    @classmethod
    def normalize_audio_start_ack_timeout_ms(cls, value: Any) -> int:
        parsed = normalize_non_negative_int(value)
        return max(100, parsed) if parsed else 2500


class SoundboardCueConfig(PluginConfigBase):
    """One configured sound effect cue."""

    id: str = Field(default="", description="Stable sound cue id used by tools and URLs.")
    label: str = Field(default="", description="Human-readable cue label.")
    keywords: list[str] = Field(
        default_factory=list,
        description="Keywords that trigger this cue when MaiBot's own reply text matches them.",
    )
    audio_path: str = Field(
        default="",
        description="Optional sound file path. Relative paths resolve under soundboard.audio_base_dir.",
    )
    media_path: str = Field(
        default="",
        description="Optional GIF, image, or video shown on the green-screen WebUI together with this cue.",
    )
    usage_hint: str = Field(
        default="",
        description="Short guidance for MaiBot describing when this cue fits the current chat moment.",
    )
    match_mode: str = Field(default="contains", description="Keyword match mode: contains, exact, or regex.")
    priority: int = Field(default=0, description="Higher priority wins when multiple cues match.")
    duration_ms: int = Field(default=1600, description="Fallback overlay lifetime in milliseconds.")
    cooldown_sec: float = Field(default=4.0, description="Per-cue cooldown in seconds.")
    volume: float = Field(default=1.0, description="Per-cue volume multiplier.")
    enabled: bool = Field(default=True, description="Enable this cue.")
    play_audio: bool = Field(
        default=True,
        description="Play this cue's audio when triggered, including embedded browser audio from a video asset.",
    )
    show_effect: bool = Field(default=True, description="Broadcast this cue to the green-screen WebUI.")

    @field_validator("id", "label", "audio_path", "media_path", "usage_hint", "match_mode", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("keywords", mode="before")
    @classmethod
    def normalize_keywords(cls, value: Any) -> list[str]:
        return normalize_string_list(value)

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @field_validator("duration_ms", mode="before")
    @classmethod
    def normalize_duration_ms(cls, value: Any) -> int:
        parsed = normalize_non_negative_int(value)
        return max(120, parsed) if parsed else 1600

    @field_validator("cooldown_sec", mode="before")
    @classmethod
    def normalize_cooldown_sec(cls, value: Any) -> float:
        return max(0.0, normalize_float(value, 4.0))

    @field_validator("volume", mode="before")
    @classmethod
    def normalize_volume(cls, value: Any) -> float:
        return min(2.0, max(0.0, normalize_float(value, 1.0)))

    @field_validator("match_mode")
    @classmethod
    def normalize_match_mode(cls, value: str) -> str:
        normalized = normalize_string(value).lower()
        return normalized if normalized in {"contains", "exact", "regex"} else "contains"


class SoundboardAutoSelectLLMConfig(PluginConfigBase):
    """Optional LLM selector that chooses a soundboard cue before heuristic fallback."""

    enabled: bool = Field(
        default=False,
        description="Use a dedicated OpenAI-compatible model to choose a cue for send_sound_effect before heuristic fallback.",
    )
    api_provider: str = Field(default="DeepSeek", description="MaiBot model_config API provider name.")
    model_name: str = Field(default="deepseek-v4-flash", description="Optional MaiBot model name to reuse.")
    model_identifier: str = Field(default="deepseek-v4-flash", description="Provider model identifier.")
    enable_thinking: bool = Field(default=False, description="Whether to enable provider reasoning mode.")
    temperature: float = Field(default=0.2, description="Cue selection model temperature.")
    max_tokens: int = Field(default=220, description="Maximum tokens for one cue selection request.")
    timeout_sec: float = Field(default=15.0, description="Cue selection request timeout seconds.")

    @field_validator("api_provider", "model_name", "model_identifier", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("temperature", "timeout_sec", mode="before")
    @classmethod
    def normalize_float_fields(cls, value: Any, info: ValidationInfo) -> float:
        defaults = {"temperature": 0.2, "timeout_sec": 15.0}
        return normalize_positive_float(value, defaults.get(info.field_name, 1.0))

    @field_validator("max_tokens", mode="before")
    @classmethod
    def normalize_max_tokens(cls, value: Any) -> int:
        return max(1, normalize_non_negative_int(value) or 220)


class SoundboardCommandConfig(PluginConfigBase):
    """Admin command settings for soundboard control."""

    enabled: bool = Field(
        default=True,
        description="Enable admin slash commands that toggle the soundboard on and off.",
    )
    prefix: str = Field(
        default="/soundboard",
        description="Slash command prefix for soundboard control.",
    )
    authorized_identities: list[str] = Field(
        default_factory=lambda: ["Vedal"],
        description="Allowed Bilibili user ids, usernames, or canonical aliases for soundboard control.",
    )
    allow_hub_local_input: bool = Field(
        default=True,
        description="Allow local Hub injections to use soundboard commands without a Bilibili identity.",
    )
    drop_non_admin_commands: bool = Field(
        default=True,
        description="Consume matching soundboard commands from unauthorized users before they reach MaiBot.",
    )
    on_aliases: list[str] = Field(
        default_factory=lambda: ["on", "enable", "start", "1"],
        description="Arguments after the command prefix that turn the soundboard on.",
    )
    off_aliases: list[str] = Field(
        default_factory=lambda: ["off", "disable", "stop", "0"],
        description="Arguments after the command prefix that turn the soundboard off.",
    )
    status_aliases: list[str] = Field(
        default_factory=lambda: ["status", "state", "?"],
        description="Arguments after the command prefix that report the current soundboard state.",
    )

    @field_validator("prefix", mode="before")
    @classmethod
    def normalize_prefix(cls, value: Any) -> str:
        command = normalize_string(value)
        if not command:
            return "/soundboard"
        return command if command.startswith("/") else f"/{command}"

    @field_validator("authorized_identities", "on_aliases", "off_aliases", "status_aliases", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str]:
        return normalize_string_list(value)


class SoundboardScoringConfig(PluginConfigBase):
    """Scoring weights for soundboard auto-matching."""

    keyword_match_weight: float = Field(default=1.4, description="Weight for keyword match in scoring.")
    cue_mention_weight: float = Field(default=0.9, description="Weight for cue name mention in scoring.")
    label_overlap_weight: float = Field(default=0.3, description="Weight for label text overlap in scoring.")
    usage_hint_weight: float = Field(default=0.2, description="Weight for usage hint overlap in scoring.")
    priority_weight: float = Field(default=0.05, description="Per-priority-level weight in scoring.")
    min_auto_score: float = Field(default=160.0, description="Minimum score for automatic reply-triggered cue selection.")
    min_user_request_score: float = Field(default=80.0, description="Minimum score when user explicitly requested a sound effect.")

    @field_validator(
        "keyword_match_weight", "cue_mention_weight", "label_overlap_weight",
        "usage_hint_weight", "priority_weight", "min_auto_score", "min_user_request_score",
        mode="before",
    )
    @classmethod
    def normalize_weights(cls, value: Any) -> float:
        return max(0.0, normalize_float(value, 1.0))


class SoundboardConfig(PluginConfigBase):
    """Sound effect box and chroma-key overlay settings."""

    __ui_label__: ClassVar[str] = "Soundboard"
    __ui_order__: ClassVar[int] = 15

    enabled: bool = Field(default=False, description="Enable the soundboard runtime.")
    expose_tool: bool = Field(default=True, description="Expose play_sound_effect to MaiBot with optional intent-based cue selection.")
    scoring: SoundboardScoringConfig = Field(
        default_factory=SoundboardScoringConfig,
        description="Scoring weights for soundboard cue auto-matching.",
    )
    bracket_pairs: list[list[str]] = Field(
        default_factory=lambda: [["*", "*"], ["（", "）"], ["【", "】"], ["(", ")"], ["[", "]"]],
        description="Bracket pairs used for stage-direction detection in bot replies. Each entry is [open, close].",
    )
    keyword_triggers_enabled: bool = Field(
        default=True,
        description="Trigger cues when MaiBot's own reply text matches cue keywords.",
    )
    reply_auto_triggers_enabled: bool = Field(
        default=True,
        description="When no soundboard tool or direct keyword trigger was already used, automatically choose at most one cue from the current reply for better timing.",
    )
    force_route_inbound_requests: bool = Field(
        default=True,
        description="Immediately route inbound chat messages that explicitly ask for a sound effect so MaiBot can answer them reliably.",
    )
    audio_playback_enabled: bool = Field(default=True, description="Play cue audio locally through sounddevice.")
    audio_output_device: str = Field(default="", description="Optional output device name fragment for local playback.")
    audio_output_volume: float = Field(default=1.0, description="Global local audio volume.")
    audio_base_dir: str = Field(default="data/soundboard", description="Base directory for relative cue audio paths.")
    webui_enabled: bool = Field(default=True, description="Serve the green-screen overlay WebUI.")
    webui_host: str = Field(default="127.0.0.1", description="Soundboard WebUI listen host.")
    webui_port: int = Field(default=18184, description="Soundboard WebUI listen port.")
    webui_auto_open_on_startup: bool = Field(
        default=True,
        description="Open the soundboard WebUI in the default browser when MaiBot starts.",
    )
    browser_audio_enabled: bool = Field(default=True, description="Allow the WebUI browser source to play cue audio.")
    browser_audio_volume: float = Field(default=1.0, description="Browser-source audio volume.")
    global_cooldown_sec: float = Field(default=1.0, description="Minimum seconds between any two cue triggers.")
    command: SoundboardCommandConfig = Field(
        default_factory=SoundboardCommandConfig,
        description="Admin slash-command settings for runtime soundboard toggle.",
    )
    cues: list[SoundboardCueConfig] = Field(
        default_factory=list,
        description="Legacy inline cue definitions. Runtime cue discovery now loads data/soundboard/cues/*/cue.toml automatically.",
    )

    @field_validator("audio_output_device", "audio_base_dir", "webui_host", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("webui_host")
    @classmethod
    def normalize_host(cls, value: str) -> str:
        return normalize_string(value) or "127.0.0.1"

    @field_validator("webui_port", mode="before")
    @classmethod
    def normalize_webui_port(cls, value: Any) -> int:
        parsed = normalize_non_negative_int(value)
        return parsed if parsed > 0 else 18184

    @field_validator("audio_output_volume", "browser_audio_volume", mode="before")
    @classmethod
    def normalize_volume(cls, value: Any) -> float:
        return min(2.0, max(0.0, normalize_float(value, 1.0)))

    @field_validator("global_cooldown_sec", mode="before")
    @classmethod
    def normalize_global_cooldown_sec(cls, value: Any) -> float:
        return max(0.0, normalize_float(value, 1.0))


class TachieConfig(PluginConfigBase):
    """立绘 (tachi-e) LLM-driven expression switching settings."""

    __ui_label__: ClassVar[str] = "Tachi-e"
    __ui_order__: ClassVar[int] = 17

    enabled: bool = Field(
        default=False,
        description="Enable LLM-driven tachi-e expression switching during bot replies.",
        json_schema_extra={"label": "Enable Tachi-e", "order": 0},
    )
    api_provider: str = Field(
        default="DeepSeek",
        description="MaiBot model_config API provider name for tachi-e selection.",
        json_schema_extra={"label": "API provider", "order": 1},
    )
    model_name: str = Field(
        default="deepseek-v4-flash",
        description="Optional MaiBot model name to reuse.",
        json_schema_extra={"label": "Model name", "order": 2},
    )
    model_identifier: str = Field(
        default="deepseek-v4-flash",
        description="Provider model identifier.",
        json_schema_extra={"label": "Model identifier", "order": 3},
    )
    enable_thinking: bool = Field(
        default=False,
        description="Whether to enable provider reasoning mode.",
        json_schema_extra={"label": "Enable thinking", "order": 4},
    )
    temperature: float = Field(
        default=0.3,
        description="Tachi-e selection model temperature.",
        json_schema_extra={"label": "Temperature", "order": 5, "step": 0.1},
    )
    max_tokens: int = Field(
        default=200,
        description="Maximum tokens for one tachi-e selection request.",
        json_schema_extra={"label": "Max tokens", "order": 6},
    )
    timeout_sec: float = Field(
        default=8.0,
        description="Tachi-e selection request timeout seconds.",
        json_schema_extra={"label": "Timeout seconds", "order": 7, "step": 1},
    )
    tachie_dir: str = Field(
        default="data/Character tachi-e",
        description="Directory containing tachi-e PNG files organized by pose subdirectories.",
        json_schema_extra={"label": "Tachi-e directory", "order": 8},
    )
    default_pose: str = Field(
        default="hands down",
        description="Default pose subdirectory when idle or resetting.",
        json_schema_extra={"label": "Default pose", "order": 9},
    )
    default_emotion: str = Field(
        default="idle",
        description="Default emotion when resetting to idle.",
        json_schema_extra={"label": "Default emotion", "order": 10},
    )
    display_window_enabled: bool = Field(
        default=True,
        description="Open a transparent desktop window to display the selected tachi-e.",
        json_schema_extra={"label": "Display window", "order": 11},
    )
    display_host: str = Field(
        default="127.0.0.1",
        description="Host for the tachi-e display HTTP server.",
        json_schema_extra={"label": "Display host", "order": 12},
    )
    display_port: int = Field(
        default=18185,
        description="Port for the tachi-e display HTTP server.",
        json_schema_extra={"label": "Display port", "order": 13},
    )
    window_width: int = Field(
        default=800,
        description="Tachi-e display window width in pixels.",
        json_schema_extra={"label": "Window width", "order": 14},
    )
    window_height: int = Field(
        default=1000,
        description="Tachi-e display window height in pixels.",
        json_schema_extra={"label": "Window height", "order": 15},
    )
    window_x: int = Field(
        default=100,
        description="Tachi-e display window initial X position.",
        json_schema_extra={"label": "Window X", "order": 16},
    )
    window_y: int = Field(
        default=40,
        description="Tachi-e display window initial Y position.",
        json_schema_extra={"label": "Window Y", "order": 17},
    )
    window_on_top: bool = Field(
        default=True,
        description="Keep the tachi-e display window always on top.",
        json_schema_extra={"label": "Always on top", "order": 18},
    )
    window_click_through: bool = Field(
        default=True,
        description="Enable click-through mode so the window does not intercept mouse events.",
        json_schema_extra={"label": "Click-through", "order": 19},
    )
    window_transparent: bool = Field(
        default=True,
        description="Make the window background transparent for OBS capture.",
        json_schema_extra={"label": "Transparent", "order": 20},
    )
    window_frameless: bool = Field(
        default=True,
        description="Remove window title bar and borders.",
        json_schema_extra={"label": "Frameless", "order": 21},
    )
    idle_reset_after_sec: float = Field(
        default=12.0,
        description="Seconds after the last reply before resetting tachi-e to the default expression. 0 disables auto-reset.",
        json_schema_extra={"label": "Idle reset seconds", "order": 22, "step": 1},
    )

    @field_validator("api_provider", "model_name", "model_identifier", "tachie_dir", "default_pose",
                      "default_emotion", "display_host", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_string(value)

    @field_validator("temperature", "timeout_sec", "idle_reset_after_sec", mode="before")
    @classmethod
    def normalize_float_fields(cls, value: Any, info: ValidationInfo) -> float:
        defaults = {"temperature": 0.3, "timeout_sec": 8.0, "idle_reset_after_sec": 12.0}
        parsed = normalize_positive_float(value, defaults.get(info.field_name, 1.0))
        if info.field_name == "temperature":
            return min(2.0, max(0.0, parsed))
        return parsed

    @field_validator("max_tokens", "display_port", "window_width", "window_height", "window_x", "window_y", mode="before")
    @classmethod
    def normalize_int_fields(cls, value: Any, info: ValidationInfo) -> int:
        defaults = {"max_tokens": 200, "display_port": 18185, "window_width": 800, "window_height": 1000,
                     "window_x": 100, "window_y": 40}
        parsed = normalize_non_negative_int(value)
        return max(1, parsed or defaults.get(info.field_name, 1))


class LiveAdapterSettings(PluginConfigBase):
    """Complete plugin configuration."""

    plugin: PluginOptions = Field(default_factory=PluginOptions)
    bilibili: BilibiliConfig = Field(default_factory=BilibiliConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    interaction: InteractionConfig = Field(default_factory=InteractionConfig)
    napcat: NapCatControlConfig = Field(default_factory=NapCatControlConfig)
    live2d: Live2DConfig = Field(default_factory=Live2DConfig)
    game: GameConfig = Field(default_factory=GameConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    video_watch: VideoWatchConfig = Field(default_factory=VideoWatchConfig)
    sts2: STS2Config = Field(default_factory=STS2Config)
    language: LanguageConfig = Field(default_factory=LanguageConfig)
    song_request: SongRequestConfig = Field(default_factory=SongRequestConfig)
    local_voice: LocalVoiceInputConfig = Field(default_factory=LocalVoiceInputConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    webui: WebUIConfig = Field(default_factory=WebUIConfig)
    soundboard: SoundboardConfig = Field(default_factory=SoundboardConfig)
    hub_input: HubInputConfig = Field(default_factory=HubInputConfig)
    hub_output: HubOutputConfig = Field(default_factory=HubOutputConfig)
    tachie: TachieConfig = Field(default_factory=TachieConfig)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_shape(cls, raw_config: Any) -> dict[str, Any]:
        raw = dict(raw_config) if isinstance(raw_config, dict) else {}
        return {
            "plugin": dict(raw.get("plugin") or {}),
            "bilibili": dict(raw.get("bilibili") or {}),
            "identity": dict(raw.get("identity") or {}),
            "filters": dict(raw.get("filters") or {}),
            "interaction": dict(raw.get("interaction") or {}),
            "napcat": dict(raw.get("napcat") or {}),
            "live2d": dict(raw.get("live2d") or {}),
            "game": dict(raw.get("game") or {}),
            "vision": dict(raw.get("vision") or {}),
            "video_watch": dict(raw.get("video_watch") or {}),
            "sts2": dict(raw.get("sts2") or {}),
            "language": dict(raw.get("language") or {}),
            "song_request": dict(raw.get("song_request") or {}),
            "local_voice": dict(raw.get("local_voice") or {}),
            "tts": dict(raw.get("tts") or {}),
            "webui": dict(raw.get("webui") or {}),
            "soundboard": dict(raw.get("soundboard") or {}),
            "hub_input": dict(raw.get("hub_input") or {}),
            "hub_output": dict(raw.get("hub_output") or {}),
            "tachie": dict(raw.get("tachie") or {}),
        }

    def should_connect(self) -> bool:
        return bool(self.plugin.enabled)

    def uses_hub_input_source(self) -> bool:
        return bool(self.hub_input.enabled and self.hub_input.use_as_primary_source)

    def route_scope(self) -> str:
        return self.identity.route_scope or str(self.bilibili.room_id)

    def validate_runtime_config(self, logger: Any) -> bool:
        if self.plugin.config_version != SUPPORTED_CONFIG_VERSION:
            logger.error(
                "Bilibili live adapter config version mismatch: "
                f"{self.plugin.config_version} != {SUPPORTED_CONFIG_VERSION}"
            )
            return False
        if self.uses_hub_input_source() and not (self.hub_input.http_url or self.hub_input.websocket_url):
            logger.warning(
                "Bilibili live adapter hub input is primary but hub_input.http_url / websocket_url are empty."
            )
            return False
        if self.bilibili.room_id <= 0:
            logger.warning("Bilibili live adapter is enabled but bilibili.room_id is empty.")
            return False
        if self.tts.enabled and not self.tts.is_usable():
            logger.warning(
                "Bilibili live adapter TTS is enabled but missing required GPT-SoVITS fields: "
                "tts.base_url / tts.ref_audio_path / tts.text_lang / tts.prompt_lang."
            )
        if self.sts2.enabled and not self.sts2.commands.admin_user_ids:
            logger.warning("STS2 integration is enabled but sts2.commands.admin_user_ids is empty.")
        if self.live2d.embodied.debug_commands.enabled and not self.live2d.embodied.debug_commands.admin_user_ids:
            logger.warning("Live2D debug commands are enabled but live2d.embodied.debug_commands.admin_user_ids is empty.")
        if self.vision.enabled and not (self.vision.model_name or self.vision.model_identifier):
            logger.warning("Desktop vision tool is enabled but vision.model_identifier is empty.")
        if self.video_watch.enabled and not (self.video_watch.analysis.model_name or self.video_watch.analysis.model_identifier):
            logger.warning("Video watch is enabled but video_watch.analysis.model_identifier is empty.")
        return True


def normalize_string(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_non_negative_int(value: Any) -> int:
    if isinstance(value, int):
        return max(0, value)
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def normalize_positive_float(value: Any, default: float) -> float:
    if isinstance(value, (int, float)) and float(value) > 0:
        return float(value)
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def normalize_float(value: Any, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen = set()
    for item in value:
        text = normalize_string(item)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result

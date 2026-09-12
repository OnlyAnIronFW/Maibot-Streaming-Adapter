"""MaiBot Bilibili live adapter plugin."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Mapping, Sequence, cast

from urllib.parse import urlparse
from uuid import uuid4

try:
    from aiohttp import ClientSession, ClientTimeout

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - optional runtime dependency
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    from pypinyin import Style as PinyinStyle, lazy_pinyin
except ImportError:  # pragma: no cover - optional runtime dependency
    PinyinStyle = None  # type: ignore[assignment]
    lazy_pinyin = None  # type: ignore[assignment]

import asyncio
import contextlib
import hashlib
import json
import os
import random
import re
import sys
import time
import webbrowser
import wave

from maibot_sdk import API, HookHandler, MaiBotPlugin, MessageGateway, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from src.config.config import global_config

from .bilibili_transport import BilibiliDanmakuTransport
from .bridge_client import JsonBridgeClient
from .audio_output import LocalAudioOutputPlayer
from .config import LiveAdapterSettings, SoundboardConfig, VisionConfig
from .constants import DEFAULT_AVATAR_STATE_WS_TOKEN, DEFAULT_VTS_WS_URL, GATEWAY_NAME, PLATFORM_NAME, PROTOCOL_NAME
from .tachie_controller import TachieController
from .event_router import LiveEventRouter
from .hub_input_client import HubInputClient, normalize_hub_record_to_live_event
from .interaction_planner import LiveInteractionPlanner
from .live2d_adaptive import (
    CapabilityProbe,
    EmbodiedLive2DRuntime,
    JsonLive2DBridge,
    Live2DController,
    SoulLinkLive2DController,
    resolve_live2d_scheme,
)
from .live2d_adaptive.soullink import ShellSoulLinkSink, VtsSoulLinkSink
from .live2d_adaptive.embodied import normalize_special_move_name
from .live2d_adaptive.speech_timeline import build_text_viseme_timeline, split_text_segments
from .live2d_control_state import DEFAULT_LIVE2D_CONTROL_STATE, Live2DControlState, Live2DControlStateStore
from .message_codec import (
    build_local_voice_message_dict,
    build_message_dict,
    extract_live_output_text_from_message,
    resolve_live_identity,
    sanitize_model_reserved_tokens,
)
from .runtime_state import LiveAdapterRuntimeState
from .soundboard import SoundboardResolvedCue, SoundboardService, list_soundboard_cues, match_soundboard_keyword
from .soundboard import _find_explicit_cue_mention, _keyword_matches, _soundboard_overlap_score
from .soundboard import load_soundboard_cues_from_files, resolve_soundboard_cue
from .soundboard import soundboard_text_matches_cue
from .soundboard_selection_client import SoundboardAutoSelectClient
from .sts2_controller import STS2Controller
from .sts2_llm_client import STS2DecisionClient
from .sts2_logging import STS2LogSession
from .sts2_mcp_client import STS2MCPClient
from .subtitle_webui import (
    SubtitleSegment,
    SubtitleWebUIService,
    build_shell_control_payload,
    estimate_subtitle_duration_ms,
)
from .translation_client import SubtitleTranslationClient
from .tts_provider import (
    GPTSoVITSTTSProvider,
    SynthesizedSpeech,
    TTSProviderProtocol,
    build_synthesized_speech_from_wav,
)
from .video_watch import VideoAnalysisResult, VideoMemoryEntry, VideoWatchController, VideoWatchSourceSpec
from .vision_tool import VisionDesktopInspector
from .live2d_shell_protocol import ShellExpressionMessage, message_to_payload
from livehub.bilibili_protocol import normalize_epoch_seconds

if TYPE_CHECKING:
    from .local_voice_controller import LocalVoiceController
    from .live2d_shell_runtime import SoulLinkShellRuntime
    from .song_request_console import SongRequestConsoleSession
    from .song_request_service import RvcSongRequestService


_LIVE2D_DEBUG_EMOTION_ALIASES: dict[str, str] = {
    "happy": "react_happy",
    "surprised": "react_surprised",
    "surprise": "react_surprised",
    "excited": "react_surprised",
    "shy": "react_shy",
    "confused": "react_confused",
    "emphasis": "react_emphasis",
    "sad": "react_sad",
    "angry": "react_angry",
    "neutral": "neutral",
    "none": "neutral",
    "clear": "neutral",
}


@dataclass(frozen=True)
class HubSpeechLease:
    request_id: str
    client_id: str
    bot_name: str


@dataclass(frozen=True)
class PendingVisualContext:
    summary: str
    focus_question: str
    requested_by: str
    created_at: float


@dataclass(frozen=True)
class PendingSoundboardTrigger:
    trigger_id: str
    cue: str
    repeat_count: int
    reason: str
    source_text: str
    triggered_by: str
    created_at: float
    target_segment_index: int = 0


class BilibiliLiveAdapterPlugin(MaiBotPlugin):
    """Input-only Bilibili live adapter with synchronized Live2D/Game output."""

    config_model: ClassVar[type[PluginConfigBase] | None] = LiveAdapterSettings

    def __init__(self) -> None:
        super().__init__()
        self._runtime_state: LiveAdapterRuntimeState | None = None
        self._planner: LiveInteractionPlanner | None = None
        self._router: LiveEventRouter | None = None
        self._transport: BilibiliDanmakuTransport | None = None
        self._hub_input_client: HubInputClient | None = None
        self._live2d_controller: Live2DController | SoulLinkLive2DController | None = None
        self._embodied_live2d_runtime: EmbodiedLive2DRuntime | None = None
        self._soullink_shell_runtime: SoulLinkShellRuntime | None = None
        self._game_bridge: JsonBridgeClient | None = None
        self._hub_output_bridge: JsonBridgeClient | None = None
        self._sts2_controller: STS2Controller | None = None
        self._sts2_log_session: STS2LogSession | None = None
        self._tts_provider: TTSProviderProtocol | None = None
        self._audio_output_player: LocalAudioOutputPlayer | None = None
        self._subtitle_webui: SubtitleWebUIService | None = None
        self._subtitle_translator: Any | None = None
        self._live2d_control_state_store: Live2DControlStateStore | None = None
        self._live2d_control_state: Live2DControlState = DEFAULT_LIVE2D_CONTROL_STATE
        self._song_request_service: RvcSongRequestService | None = None
        self._song_request_console_session: SongRequestConsoleSession | None = None
        self._soundboard: SoundboardService | None = None
        self._soundboard_admin_disabled = False
        self._soundboard_webui_opened_url = ""
        self._local_voice_controller: LocalVoiceController | None = None
        self._local_delivery_lock = asyncio.Lock()
        self._pending_local_delivery_wait_task: asyncio.Task[None] | None = None
        self._pending_gateway_disconnect_task: asyncio.Task[None] | None = None
        self._hub_output_tasks: set[asyncio.Task[Any]] = set()
        self._background_audio_playback_tasks: set[asyncio.Task[bool]] = set()
        self._inflight_locally_rendered_reply_batches: dict[str, float] = {}
        self._locally_rendered_reply_batches: dict[str, float] = {}
        self._hub_output_forwarded_batches: dict[str, float] = {}
        self._logged_reply_latency_batches: set[str] = set()
        self._runtime_warmup_task: asyncio.Task[None] | None = None
        self._hub_participants: list[dict[str, Any]] = []
        self._hub_speaking_state: dict[str, Any] = {}
        self._hub_speech_waiters: dict[str, asyncio.Event] = {}
        self._napcat_disabled_for_live = False
        self._pending_visual_contexts: dict[str, PendingVisualContext] = {}
        self._visual_context_poll_tasks: dict[str, asyncio.Task[None]] = {}
        self._video_watch_controller: VideoWatchController | None = None
        self._pending_soundboard_triggers: list[PendingSoundboardTrigger] = []
        self._pending_soundboard_fallback_tasks: dict[str, asyncio.Task[None]] = {}
        self._tachie_controller: TachieController | None = None
        self._livehub_process: asyncio.subprocess.Process | None = None
        self._livehub_log_handle: Any | None = None
        self._livehub_start_lock = asyncio.Lock()

    async def on_load(self) -> None:
        """Start configured bridges and the Bilibili input transport."""

        await self._restart_runtime()

    async def on_unload(self) -> None:
        """Stop all runtime components."""

        await self._stop_runtime()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """Reload plugin config and restart runtime if the plugin config changed."""

        if scope != "self":
            return
        normalized_settings = LiveAdapterSettings.model_validate(config_data)
        current_settings = self._load_raw_settings()
        normalized_config = normalized_settings.model_dump(mode="python")
        if normalized_config == current_settings.model_dump(mode="python"):
            self.set_plugin_config(normalized_config)
            if version:
                self._logger().debug(
                    "Bilibili live adapter config update matches in-memory state; "
                    f"skip restart for version={version}"
                )
            return
        self.set_plugin_config(normalized_config)
        if version:
            self._logger().debug(f"Bilibili live adapter config update received: {version}")
        await self._restart_runtime()

    @MessageGateway(
        name=GATEWAY_NAME,
        route_type="duplex",
        platform=PLATFORM_NAME,
        protocol=PROTOCOL_NAME,
        description="Bilibili live duplex gateway; outbound replies drive Live2D/Game only.",
    )
    async def handle_bilibili_gateway(
        self,
        message: dict[str, Any],
        route: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Handle MaiBot outbound messages without sending them back to Bilibili."""

        del route
        text = extract_live_output_text_from_message(message)
        if not text:
            return {"success": False, "error": "empty outbound message"}
        settings = self._load_settings()
        render_metadata = _build_render_metadata(message, metadata)
        render_result = await self._deliver_text_reply_serialized(
            text,
            settings=settings,
            source_platform=PLATFORM_NAME,
            metadata=render_metadata,
            kwargs=kwargs,
            on_audio_start=self._build_sts2_audio_start_callback(PLATFORM_NAME, render_metadata),
            on_segment_audio_start=self._build_sts2_segment_audio_start_callback(PLATFORM_NAME, render_metadata),
            on_audio_complete=self._build_sts2_audio_complete_callback(PLATFORM_NAME, render_metadata),
            force_interrupt=_should_force_interrupt_live_reply(render_metadata),
        )
        if self._router is not None and not bool(render_result.get("suppressed_reply")):
            history_text = str(render_result.get("subtitle_text") or text).strip()
            if history_text:
                self._router.record_bot_output_history(history_text)

        return {
            "success": True,
            "external_message_id": f"bilibili-live-local-{uuid4().hex}",
            "metadata": render_result,
        }

    @HookHandler(
        "send_service.before_send",
        name="live2d_mirror_before_send",
        description="Mirror ordinary MaiBot outbound replies to Live2D before platform delivery.",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=999000,
        error_policy=ErrorPolicy.LOG,
    )
    async def mirror_outbound_reply_to_live2d(
        self,
        message: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Mirror non-live-gateway outbound replies so QQ tests also drive Live2D."""
        song_service = self._song_request_service
        if song_service is not None and song_service.is_playback_active:
            return {
                "success": True,
                "action": "abort",
                "custom_result": {"treat_as_sent": True, "song_playback_suppressed": True},
            }
        if not isinstance(message, Mapping):
            return {"success": True, "action": "continue"}
        settings = self._load_settings()
        should_mirror_live2d = bool(
            settings.live2d.enabled
            and settings.live2d.send_bot_replies
            and settings.live2d.mirror_other_platform_replies
            and self._live2d_controller is not None
        )
        should_publish_webui = bool(settings.webui.enabled and self._subtitle_webui is not None)
        if not settings.plugin.enabled or (not should_mirror_live2d and not should_publish_webui):
            return {"success": True, "action": "continue"}
        play_local_audio = _should_play_local_audio(settings)
        platform = _extract_platform(message)
        modified_kwargs = _build_local_render_only_modified_kwargs(kwargs) if platform == PLATFORM_NAME else None
        if platform == PLATFORM_NAME:
            skipped_batch_result = self._build_locally_rendered_reply_batch_skip_result(message)
            if skipped_batch_result is not None:
                return {
                    "success": True,
                    "action": "abort",
                    "modified_kwargs": modified_kwargs,
                    "custom_result": skipped_batch_result,
                }
            text = extract_live_output_text_from_message(message)
            if not text:
                return {
                    "success": True,
                    "action": "abort",
                    "modified_kwargs": modified_kwargs,
                    "custom_result": {"treat_as_sent": True},
                }
            replyer_segments = _replyer_streaming_segments_from_metadata(message)
            render_text = "".join(replyer_segments) if replyer_segments is not None else text
            tracked_reply_batch = self._mark_inflight_locally_rendered_reply_batch(message)
            try:
                render_result = await self._deliver_text_reply_serialized(
                    render_text,
                    settings=settings,
                    source_platform=platform,
                    metadata=message,
                    kwargs={},
                    on_audio_start=self._build_sts2_audio_start_callback(platform, message),
                    on_segment_audio_start=self._build_sts2_segment_audio_start_callback(platform, message),
                    on_audio_complete=self._build_sts2_audio_complete_callback(platform, message),
                    segmented_reply_segments=replyer_segments,
                    force_interrupt=_should_force_interrupt_live_reply(message),
                )
            except Exception:
                if tracked_reply_batch:
                    self._clear_inflight_locally_rendered_reply_batch(message)
                raise
            self._mark_locally_rendered_reply_batch(message)
            if tracked_reply_batch:
                self._clear_inflight_locally_rendered_reply_batch(message)
            if self._router is not None and not bool(render_result.get("suppressed_reply")):
                history_text = str(render_result.get("subtitle_text") or render_text).strip()
                if history_text:
                    self._router.record_bot_output_history(history_text)
            return {
                "success": True,
                "action": "abort",
                "modified_kwargs": modified_kwargs,
                "custom_result": {
                    "treat_as_sent": True,
                    **render_result,
                },
            }
        text = extract_live_output_text_from_message(message)
        if not text:
            return {"success": True, "action": "continue"}
        speech_text, subtitle_text, audio_timeline, synthesized_speech = await self._prepare_reply_delivery(
            text,
            settings=settings,
            metadata=message,
            kwargs={},
        )
        webui_published, webui_audio_started = await self._publish_reply_to_webui(
            subtitle_text,
            settings=settings,
            source_platform=platform,
            speech_text=_webui_original_text_for_subtitle(speech_text, subtitle_text, settings),
            audio_timeline=audio_timeline,
            synthesized_speech=synthesized_speech,
            wait_for_audio_start=should_mirror_live2d and not play_local_audio,
            on_audio_start=None if play_local_audio else self._build_sts2_audio_start_callback(platform, message),
        )
        timeline = None
        if should_mirror_live2d:
            live2d_controller = self._live2d_controller
        else:
            live2d_controller = None
        if live2d_controller is not None:
            play_kwargs: dict[str, Any] = {}
            if webui_audio_started:
                play_kwargs["timeline_prepare_ms"] = 0
            timeline = await live2d_controller.play_reply(
                speech_text,
                audio_timeline=audio_timeline,
                emotion_intent=_infer_emotion_intent(speech_text),
                **play_kwargs,
            )
        audio_playback_task = self._create_audio_playback_task(
            synthesized_speech,
            settings=settings,
            enabled=play_local_audio,
            on_audio_start=self._build_sts2_audio_start_callback(platform, message) if play_local_audio else None,
        )
        audio_played_to_vts = False
        if audio_playback_task is not None:
            self._track_background_audio_playback_task(audio_playback_task)
        if timeline is not None:
            self._logger().info(
                "Mirrored outbound reply to Live2D: "
                f"platform={platform or 'unknown'} timeline_id={timeline.timeline_id} text={text[:40]!r}"
            )
        return {
            "success": True,
            "action": "continue",
            "custom_result": {
                "live2d_synchronized": timeline is not None,
                "timeline_id": timeline.timeline_id if timeline is not None else "",
                "platform": platform,
                "audio_ref": synthesized_speech.audio_ref if synthesized_speech is not None else "",
                "webui_published": webui_published,
                "webui_audio_started": webui_audio_started,
                "audio_played_to_vts": audio_played_to_vts,
                "language_mode": settings.language.mode,
                "speech_text": speech_text,
                "subtitle_text": subtitle_text,
            },
        }

    @HookHandler(
        "maisaka.planner.before_request",
        name="bilibili_live_language_before_request",
        description="Force Bilibili live replies to English spoken text in bilingual mode.",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.LOG,
    )
    async def enforce_live_language_prompt(
        self,
        messages: list[dict[str, Any]] | None = None,
        tool_definitions: list[dict[str, Any]] | None = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Append the active live language instruction to the Bilibili live planner request."""

        del kwargs
        normalized_messages = [dict(message) for message in (messages or []) if isinstance(message, Mapping)]
        normalized_tools = list(tool_definitions or [])
        settings = self._load_settings()
        is_live_session = settings.plugin.enabled and str(session_id or "").strip() == _build_live_chat_id(settings)
        if is_live_session:
            normalized_tools = _filter_unavailable_tool_definitions(
                normalized_tools,
                settings,
                filter_finish=True,
            )
            normalized_messages, normalized_tools = _ensure_live_soundboard_tools_visible(
                normalized_messages,
                normalized_tools,
                settings.soundboard,
                admin_disabled=self._soundboard_admin_disabled,
            )
            normalized_tools = _inject_soundboard_tool_hints(
                normalized_tools, settings.soundboard,
                admin_disabled=self._soundboard_admin_disabled,
            )
        if not is_live_session:
            return {"messages": normalized_messages, "tool_definitions": normalized_tools}
        language_prompt = self._build_live_session_prompt(settings)
        if not language_prompt:
            return {"messages": normalized_messages, "tool_definitions": normalized_tools}
        return {
            "messages": _append_language_prompt(normalized_messages, language_prompt),
            "tool_definitions": normalized_tools,
        }

    @HookHandler(
        "maisaka.planner.after_response",
        name="bilibili_live_language_reply_reference",
        description="Pass the active Bilibili live language instruction to the replyer.",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.LOG,
    )
    async def enforce_live_language_prompt_for_replyer(
        self,
        tool_calls: list[dict[str, Any]] | None = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Append the active live language instruction to reply tool reference info."""

        del kwargs
        settings = self._load_settings()
        is_live_session = settings.plugin.enabled and str(session_id or "").strip() == _build_live_chat_id(settings)
        if not is_live_session:
            return {"action": "continue"}
        if _tool_calls_include_finish(tool_calls):
            router = self._router
            release_pending = getattr(router, "record_live_reply_finished_without_output", None)
            if callable(release_pending):
                release_pending(reason="finish")
        visual_prompt = self._pending_visual_context_prompt(settings)
        video_watch_prompt = self._video_watch_prompt()
        language_prompt = _merge_prompt_sections(
            _live_language_prompt(settings),
            _live_capability_boundary_prompt(settings),
            self._hub_multi_ai_prompt(settings),
            visual_prompt,
            video_watch_prompt,
        )
        if not language_prompt:
            return {"action": "continue"}
        updated_tool_calls = _append_reply_reference_info(tool_calls, language_prompt)
        if visual_prompt and not self._is_visual_context_polling_active(session_id):
            self._clear_pending_visual_context(session_id)
        if updated_tool_calls is None:
            return {"action": "continue"}
        return {
            "action": "continue",
            "modified_kwargs": {
                "session_id": str(session_id or "").strip(),
                "tool_calls": updated_tool_calls,
            },
        }

    async def _render_local_reply(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings,
        source_platform: str,
        metadata: Mapping[str, Any] | None,
        kwargs: Mapping[str, Any],
        on_audio_start: Callable[[], None] | None = None,
        on_segment_audio_start: Callable[[int, int], None] | None = None,
        tts_request_overrides: Mapping[str, Any] | None = None,
        allow_segment_streaming: bool = True,
        segmented_reply_segments: Sequence[str] | None = None,
        scheduled_soundboard_triggers_by_segment: Mapping[int, Sequence[PendingSoundboardTrigger]] | None = None,
        initial_segment_prepare_task: asyncio.Task[
            tuple[str, str, dict[str, Any] | None, SynthesizedSpeech | None]
        ] | None = None,
        initial_segment_prepare_started_at: float | None = None,
    ) -> dict[str, Any]:
        if allow_segment_streaming:
            streamed_result = await self._render_segmented_local_reply(
                text,
                settings=settings,
                source_platform=source_platform,
                metadata=metadata,
                on_audio_start=on_audio_start,
                on_segment_audio_start=on_segment_audio_start,
                tts_request_overrides=tts_request_overrides,
                segmented_reply_segments=segmented_reply_segments,
                scheduled_soundboard_triggers_by_segment=scheduled_soundboard_triggers_by_segment,
                initial_segment_prepare_task=initial_segment_prepare_task,
                initial_segment_prepare_started_at=initial_segment_prepare_started_at,
            )
            if streamed_result is not None:
                return streamed_result
        render_text = _sanitize_soundboard_reply_segment_text(
            text,
            settings=settings,
            planned_triggers=None,
        )
        speech_text, subtitle_text, audio_timeline, synthesized_speech = await self._prepare_reply_delivery(
            render_text,
            settings=settings,
            metadata=metadata,
            kwargs=kwargs,
            tts_request_overrides=tts_request_overrides,
        )
        return await self._render_prepared_local_reply(
            text,
            speech_text=speech_text,
            subtitle_text=subtitle_text,
            audio_timeline=audio_timeline,
            synthesized_speech=synthesized_speech,
            settings=settings,
            source_platform=source_platform,
            metadata=metadata,
            on_audio_start=on_audio_start,
        )

    async def _render_prepared_local_reply(
        self,
        text: str,
        *,
        speech_text: str,
        subtitle_text: str,
        audio_timeline: Mapping[str, Any] | None,
        synthesized_speech: SynthesizedSpeech | None,
        settings: LiveAdapterSettings,
        source_platform: str,
        metadata: Mapping[str, Any] | None,
        on_audio_start: Callable[[], None] | None = None,
        allow_soundboard_bot_output_trigger: bool = True,
        wait_for_soundboard_before_reply: bool = False,
    ) -> dict[str, Any]:
        timeline_id = _extract_timeline_id(metadata)
        audio_start_callback = self._build_local_voice_echo_start_callback(
            str(subtitle_text or speech_text or text).strip(),
            downstream=on_audio_start,
        )
        play_local_audio = _should_play_local_audio(settings)

        should_play_live2d = bool(
            settings.live2d.enabled and settings.live2d.send_bot_replies and self._live2d_controller is not None
        )
        soundboard_triggered = False
        if allow_soundboard_bot_output_trigger:
            if metadata is not None and bool(metadata.get("paid_event_acknowledgement")):
                allow_soundboard_bot_output_trigger = False
        if allow_soundboard_bot_output_trigger:
            soundboard_triggered = await self._trigger_soundboard_for_bot_output(
                subtitle_text=subtitle_text,
                speech_text=speech_text,
                source_text=text,
            )
        if wait_for_soundboard_before_reply or soundboard_triggered:
            await self._wait_for_soundboard_idle(settings=settings)
        if not str(speech_text or "").strip() and not str(subtitle_text or "").strip():
            return {
                "bilibili_sent": False,
                "timeline_id": timeline_id,
                "live2d_synchronized": False,
                "audio_ref": "",
                "audio_duration_ms": 0,
                "webui_published": False,
                "webui_audio_started": False,
                "audio_played_to_vts": False,
                "local_delivery": True,
                "language_mode": settings.language.mode,
                "speech_text": "",
                "subtitle_text": "",
                "delivery_waited": False,
            }
        webui_published, webui_audio_started = await self._publish_reply_to_webui(
            subtitle_text,
            settings=settings,
            source_platform=source_platform,
            speech_text=_webui_original_text_for_subtitle(speech_text, subtitle_text, settings),
            audio_timeline=audio_timeline,
            synthesized_speech=synthesized_speech,
            wait_for_audio_start=should_play_live2d and not play_local_audio,
            on_audio_start=audio_start_callback if not play_local_audio else None,
        )
        timeline = None
        if should_play_live2d:
            live2d_controller = self._live2d_controller
        else:
            live2d_controller = None
        if live2d_controller is not None:
            play_kwargs: dict[str, Any] = {}
            if webui_audio_started:
                play_kwargs["timeline_prepare_ms"] = 0
            timeline = await live2d_controller.play_reply(
                speech_text,
                audio_timeline=audio_timeline,
                emotion_intent=_infer_emotion_intent(speech_text),
                **play_kwargs,
            )
        audio_playback_task = self._create_audio_playback_task(
            synthesized_speech,
            settings=settings,
            enabled=play_local_audio,
            on_audio_start=audio_start_callback if play_local_audio else None,
        )
        audio_played_to_vts = False
        if audio_playback_task is not None:
            self._track_background_audio_playback_task(audio_playback_task)
        return {
            "bilibili_sent": False,
            "timeline_id": timeline.timeline_id if timeline is not None else timeline_id,
            "live2d_synchronized": timeline is not None,
            "audio_ref": synthesized_speech.audio_ref if synthesized_speech is not None else "",
            "audio_duration_ms": synthesized_speech.audio_duration_ms if synthesized_speech is not None else 0,
            "webui_published": webui_published,
            "webui_audio_started": webui_audio_started,
            "audio_played_to_vts": audio_played_to_vts,
            "local_delivery": True,
            "language_mode": settings.language.mode,
            "speech_text": speech_text,
            "subtitle_text": subtitle_text,
            "delivery_waited": False,
        }

    async def _render_segmented_local_reply(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings,
        source_platform: str,
        metadata: Mapping[str, Any] | None,
        on_audio_start: Callable[[], None] | None = None,
        on_segment_audio_start: Callable[[int, int], None] | None = None,
        tts_request_overrides: Mapping[str, Any] | None = None,
        segmented_reply_segments: Sequence[str] | None = None,
        scheduled_soundboard_triggers_by_segment: Mapping[int, Sequence[PendingSoundboardTrigger]] | None = None,
        initial_segment_prepare_task: asyncio.Task[
            tuple[str, str, dict[str, Any] | None, SynthesizedSpeech | None]
        ] | None = None,
        initial_segment_prepare_started_at: float | None = None,
    ) -> dict[str, Any] | None:
        source_segments = (
            list(segmented_reply_segments)
            if segmented_reply_segments is not None
            else self._collect_segmented_local_reply_segments(text, settings=settings, metadata=metadata)
        )
        if not source_segments:
            return None
        segments = _sanitize_soundboard_reply_segments(
            source_segments,
            settings=settings,
            scheduled_soundboard_triggers_by_segment=scheduled_soundboard_triggers_by_segment,
        )
        loop = asyncio.get_running_loop()
        segment_count = len(source_segments)
        aggregated_subtitle_parts: list[str] = []
        combined_audio_duration_ms = 0
        last_timeline_id = ""
        webui_published = False
        webui_audio_started = False
        audio_played_to_vts = False
        live2d_synchronized = False
        pending_prepare_task = initial_segment_prepare_task
        pending_prepare_started_at = initial_segment_prepare_started_at
        if pending_prepare_task is None:
            pending_prepare_task, pending_prepare_started_at = self._start_segmented_local_reply_prepare_task(
                segments[0],
                settings=settings,
                tts_request_overrides=tts_request_overrides,
                segment_index=1,
                segment_count=segment_count,
            )
        try:
            for index, source_segment_text in enumerate(source_segments):
                render_segment_text = segments[index]
                segment_plans = (
                    scheduled_soundboard_triggers_by_segment.get(index + 1)
                    if scheduled_soundboard_triggers_by_segment is not None
                    else None
                )
                current_prepare_task = pending_prepare_task
                current_prepare_started_at = pending_prepare_started_at
                pending_prepare_task = None
                pending_prepare_started_at = None
                assert current_prepare_task is not None
                speech_text, subtitle_text, audio_timeline, synthesized_speech = await current_prepare_task
                if index + 1 < len(segments):
                    next_segment_index = index + 2
                    pending_prepare_task, pending_prepare_started_at = self._start_segmented_local_reply_prepare_task(
                        segments[index + 1],
                        settings=settings,
                        tts_request_overrides=tts_request_overrides,
                        segment_index=next_segment_index,
                        segment_count=segment_count,
                    )
                segment_audio_start = on_audio_start if index == 0 else None
                if on_segment_audio_start is not None:
                    current_segment_index = index + 1

                    def _segment_callback(
                        *,
                        segment_index: int = current_segment_index,
                        current_segment_count: int = segment_count,
                    ) -> None:
                        on_segment_audio_start(segment_index, current_segment_count)

                    segment_audio_start = _chain_callbacks(segment_audio_start, _segment_callback)
                await self._trigger_scheduled_soundboard_triggers(segment_plans, settings=settings)
                segment_result = await self._render_prepared_local_reply(
                    source_segment_text,
                    speech_text=speech_text,
                    subtitle_text=subtitle_text,
                    audio_timeline=audio_timeline,
                    synthesized_speech=synthesized_speech,
                    settings=settings,
                    source_platform=source_platform,
                    metadata={},
                    on_audio_start=segment_audio_start,
                    allow_soundboard_bot_output_trigger=not bool(segment_plans),
                    wait_for_soundboard_before_reply=bool(segment_plans),
                )
                subtitle_text = str(segment_result.get("subtitle_text") or "").strip()
                if subtitle_text:
                    aggregated_subtitle_parts.append(subtitle_text)
                combined_audio_duration_ms += max(0, int(segment_result.get("audio_duration_ms") or 0))
                last_timeline_id = str(segment_result.get("timeline_id") or last_timeline_id)
                webui_published = bool(segment_result.get("webui_published")) or webui_published
                webui_audio_started = bool(segment_result.get("webui_audio_started")) or webui_audio_started
                audio_played_to_vts = bool(segment_result.get("audio_played_to_vts")) or audio_played_to_vts
                live2d_synchronized = bool(segment_result.get("live2d_synchronized")) or live2d_synchronized
                delivery_wait_started_at = loop.time()
                await self._wait_for_local_delivery_completion(segment_result, settings=settings)
                self._log_reply_timing(
                    "delivery_wait_done",
                    text=subtitle_text or speech_text or render_segment_text or source_segment_text,
                    segment_index=index + 1,
                    segment_count=segment_count,
                    prepare_task_elapsed_ms=(
                        int((delivery_wait_started_at - current_prepare_started_at) * 1000)
                        if current_prepare_started_at is not None
                        else None
                    ),
                    wait_elapsed_ms=int((loop.time() - delivery_wait_started_at) * 1000),
                    audio_duration_ms=int(segment_result.get("audio_duration_ms") or 0),
                )
        finally:
            if pending_prepare_task is not None:
                if not pending_prepare_task.done():
                    pending_prepare_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending_prepare_task
        return {
            "bilibili_sent": False,
            "timeline_id": last_timeline_id or _extract_timeline_id(metadata),
            "live2d_synchronized": live2d_synchronized,
            "audio_ref": "",
            "audio_duration_ms": combined_audio_duration_ms,
            "webui_published": webui_published,
            "webui_audio_started": webui_audio_started,
            "audio_played_to_vts": audio_played_to_vts,
            "local_delivery": True,
            "language_mode": settings.language.mode,
            "speech_text": str(text or "").strip(),
            "subtitle_text": "".join(aggregated_subtitle_parts),
            "delivery_waited": True,
        }

    async def _render_external_audio_reply(
        self,
        caption_text: str,
        synthesized_speech: SynthesizedSpeech,
        *,
        settings: LiveAdapterSettings,
        source_platform: str,
        metadata: Mapping[str, Any] | None,
        on_audio_start: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        timeline_id = _extract_timeline_id(metadata)
        audio_timeline = synthesized_speech.to_audio_timeline()
        if _uses_vts_native_lip_sync(settings):
            audio_timeline.pop("visemes", None)
        else:
            audio_timeline = _with_text_visemes(audio_timeline, caption_text, settings=settings)
        audio_start_callback = self._build_local_voice_echo_start_callback(caption_text, downstream=on_audio_start)
        play_local_audio = _should_play_local_audio(settings)
        should_play_live2d = bool(
            settings.live2d.enabled and settings.live2d.send_bot_replies and self._live2d_controller is not None
        )
        webui_published, webui_audio_started = await self._publish_reply_to_webui(
            caption_text,
            settings=settings,
            source_platform=source_platform,
            speech_text="",
            audio_timeline=audio_timeline,
            synthesized_speech=synthesized_speech,
            wait_for_audio_start=should_play_live2d and not play_local_audio,
            on_audio_start=audio_start_callback if not play_local_audio else None,
        )
        timeline = None
        if should_play_live2d:
            live2d_controller = self._live2d_controller
        else:
            live2d_controller = None
        if live2d_controller is not None:
            play_kwargs: dict[str, Any] = {}
            if webui_audio_started:
                play_kwargs["timeline_prepare_ms"] = 0
            timeline = await live2d_controller.play_reply(
                caption_text,
                audio_timeline=audio_timeline,
                emotion_intent="",
                **play_kwargs,
            )
        audio_playback_task = self._create_audio_playback_task(
            synthesized_speech,
            settings=settings,
            enabled=play_local_audio,
            on_audio_start=audio_start_callback if play_local_audio else None,
        )
        audio_played_to_vts = await audio_playback_task if audio_playback_task is not None else False
        if audio_playback_task is None and synthesized_speech.audio_duration_ms > 0:
            await asyncio.sleep(max(0.0, synthesized_speech.audio_duration_ms / 1000.0))
        return {
            "bilibili_sent": False,
            "timeline_id": timeline.timeline_id if timeline is not None else timeline_id,
            "live2d_synchronized": timeline is not None,
            "audio_ref": synthesized_speech.audio_ref,
            "audio_duration_ms": synthesized_speech.audio_duration_ms,
            "webui_published": webui_published,
            "webui_audio_started": webui_audio_started,
            "audio_played_to_vts": audio_played_to_vts,
            "local_delivery": True,
            "rvc_song": True,
            "delivery_waited": audio_playback_task is None and synthesized_speech.audio_duration_ms > 0,
        }

    async def _deliver_text_reply_serialized(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings,
        source_platform: str,
        metadata: Mapping[str, Any] | None,
        kwargs: Mapping[str, Any],
        on_audio_start: Callable[[], None] | None = None,
        on_segment_audio_start: Callable[[int, int], None] | None = None,
        on_audio_complete: Callable[[], None] | None = None,
        tts_request_overrides: Mapping[str, Any] | None = None,
        segmented_reply_segments: Sequence[str] | None = None,
        force_interrupt: bool = False,
    ) -> dict[str, Any]:
        busy_started = self._mark_router_live_reply_started(
            source_platform=source_platform,
            metadata=metadata,
            reason="serialized_delivery_started",
        )
        busy_handed_off = False
        hub_speech_handed_off = False
        hub_speech_lease: HubSpeechLease | None = None
        release_reason = "delivery_complete"
        full_reply_text = self._resolve_hub_output_reply_text(text, metadata=metadata)
        effective_on_audio_start = self._build_reply_latency_audio_start_callback(
            reply_text=full_reply_text or text,
            metadata=metadata,
            downstream=on_audio_start,
        )
        try:
            if _should_suppress_local_voice_self_judgment_reply(text, metadata):
                release_reason = "suppressed_reply"
                return _build_suppressed_local_voice_reply_result(settings=settings, metadata=metadata)
            text = await self._maybe_compact_parallel_live_reply_text(
                text,
                source_platform=source_platform,
                metadata=metadata,
            )
            if metadata is not None and bool(metadata.get("paid_event_acknowledgement")):
                pending_soundboard_plans: list[PendingSoundboardTrigger] = []
            else:
                pending_soundboard_plans = self._consume_pending_soundboard_triggers()
            resolved_segmented_reply_segments = (
                list(segmented_reply_segments) if segmented_reply_segments is not None else None
            )
            if resolved_segmented_reply_segments is None:
                resolved_segmented_reply_segments = self._collect_segmented_local_reply_segments(
                    text,
                    settings=settings,
                    metadata=metadata,
                )
            planning_segments = (
                resolved_segmented_reply_segments
                if resolved_segmented_reply_segments is not None
                else ([str(text or "").strip()] if str(text or "").strip() else [])
            )
            pending_soundboard_plans.extend(
                self._build_reply_auto_soundboard_plans(
                    text=text,
                    segments=planning_segments,
                    metadata=metadata,
                    settings=settings,
                    existing_plans=pending_soundboard_plans,
                )
            )
            self._fire_tachie_selection_task(text, settings=settings, metadata=metadata)
            if resolved_segmented_reply_segments is not None:
                scheduled_soundboard_triggers_by_segment = self._build_soundboard_segment_schedule(
                    plans=pending_soundboard_plans,
                    segments=resolved_segmented_reply_segments,
                    settings=settings,
                )
                prepared_segmented_reply_segments = _sanitize_soundboard_reply_segments(
                    resolved_segmented_reply_segments,
                    settings=settings,
                    scheduled_soundboard_triggers_by_segment=scheduled_soundboard_triggers_by_segment,
                )
                initial_prepare_task, initial_prepare_started_at = self._start_segmented_local_reply_prepare_task(
                    prepared_segmented_reply_segments[0],
                    settings=settings,
                    tts_request_overrides=tts_request_overrides,
                    segment_index=1,
                    segment_count=len(resolved_segmented_reply_segments),
                )
                initial_prepare_task_handed_off = False
                try:
                    async def render_segmented_local_reply() -> dict[str, Any]:
                        nonlocal initial_prepare_task_handed_off
                        initial_prepare_task_handed_off = True
                        return await self._render_local_reply(
                            text,
                            settings=settings,
                            source_platform=source_platform,
                            metadata=metadata,
                            kwargs=kwargs,
                            on_audio_start=effective_on_audio_start,
                            on_segment_audio_start=on_segment_audio_start,
                            tts_request_overrides=tts_request_overrides,
                            segmented_reply_segments=resolved_segmented_reply_segments,
                            scheduled_soundboard_triggers_by_segment=scheduled_soundboard_triggers_by_segment,
                            initial_segment_prepare_task=initial_prepare_task,
                            initial_segment_prepare_started_at=initial_prepare_started_at,
                        )

                    result = await self._run_serialized_delivery(
                        render_segmented_local_reply,
                        settings=settings,
                        source_platform=source_platform,
                        metadata=metadata,
                        hub_reply_text=text,
                        on_audio_complete=on_audio_complete,
                        force_interrupt=force_interrupt,
                    )
                finally:
                    if not initial_prepare_task_handed_off:
                        if not initial_prepare_task.done():
                            initial_prepare_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await initial_prepare_task
                self._schedule_hub_output_forward(
                    reply_text=str(result.get("subtitle_text") or text).strip(),
                    settings=settings,
                    source_platform=source_platform,
                    metadata=metadata,
                )
                return result
            render_text = _sanitize_soundboard_reply_segment_text(
                text,
                settings=settings,
                planned_triggers=pending_soundboard_plans or None,
            )
            wait_for_soundboard_before_reply = bool(pending_soundboard_plans)
            speech_text, subtitle_text, audio_timeline, synthesized_speech = await self._prepare_reply_delivery(
                render_text,
                settings=settings,
                metadata=metadata,
                kwargs=kwargs,
                tts_request_overrides=tts_request_overrides,
            )
            hub_speech_lease = await self._acquire_hub_speech_turn(
                settings=settings,
                source_platform=source_platform,
                metadata=metadata,
                reply_text=subtitle_text or speech_text or text,
                expected_duration_ms=self._expected_hub_speech_duration_ms(
                    audio_timeline=audio_timeline,
                    synthesized_speech=synthesized_speech,
                ),
            )
            async with self._local_delivery_lock:
                if force_interrupt:
                    await self._interrupt_current_local_delivery()
                else:
                    await self._await_pending_local_delivery_wait()
                    await self._wait_for_existing_speech_completion(settings=settings)
                await self._trigger_scheduled_soundboard_triggers(pending_soundboard_plans, settings=settings)
                result = await self._render_prepared_local_reply(
                    text,
                    speech_text=speech_text,
                    subtitle_text=subtitle_text,
                    audio_timeline=audio_timeline,
                    synthesized_speech=synthesized_speech,
                    settings=settings,
                    source_platform=source_platform,
                    metadata=metadata,
                    on_audio_start=effective_on_audio_start,
                    allow_soundboard_bot_output_trigger=not wait_for_soundboard_before_reply,
                    wait_for_soundboard_before_reply=wait_for_soundboard_before_reply,
                )
                self._schedule_pending_local_delivery_wait(
                    result,
                    settings=settings,
                    on_audio_complete=on_audio_complete,
                    release_router_live_reply=busy_started,
                    hub_speech_lease=hub_speech_lease,
                )
                self._schedule_hub_output_forward(
                    reply_text=str(result.get("subtitle_text") or text).strip(),
                    settings=settings,
                    source_platform=source_platform,
                    metadata=metadata,
                )
                busy_handed_off = busy_started
                hub_speech_handed_off = hub_speech_lease is not None
                return result
        except asyncio.CancelledError:
            release_reason = "delivery_cancelled"
            raise
        except Exception:
            release_reason = "delivery_error"
            raise
        finally:
            if hub_speech_lease is not None and not hub_speech_handed_off:
                await self._complete_hub_speech_turn(
                    hub_speech_lease,
                    status="cancelled" if release_reason == "delivery_cancelled" else "failed",
                )
            if busy_started and not busy_handed_off:
                self._release_router_live_reply(reason=release_reason)

    async def _deliver_external_audio_reply_serialized(
        self,
        caption_text: str,
        synthesized_speech: SynthesizedSpeech,
        *,
        settings: LiveAdapterSettings,
        source_platform: str,
        metadata: Mapping[str, Any] | None,
        on_audio_start: Callable[[], None] | None = None,
        on_audio_complete: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        busy_started = self._mark_router_live_reply_started(
            source_platform=source_platform,
            metadata=metadata,
            reason="external_audio_delivery_started",
        )
        release_reason = "delivery_complete"
        try:
            result = await self._run_serialized_delivery(
                lambda: self._render_external_audio_reply(
                    caption_text,
                    synthesized_speech,
                    settings=settings,
                    source_platform=source_platform,
                    metadata=metadata,
                    on_audio_start=on_audio_start,
                ),
                settings=settings,
                source_platform=source_platform,
                metadata=metadata,
                hub_reply_text=caption_text,
                expected_duration_ms=max(0, int(synthesized_speech.audio_duration_ms or 0)),
                on_audio_complete=on_audio_complete,
            )
            self._schedule_hub_output_forward(
                reply_text=str(caption_text or "").strip(),
                settings=settings,
                source_platform=source_platform,
                metadata=metadata,
            )
            return result
        except Exception:
            release_reason = "delivery_error"
            raise
        finally:
            if busy_started:
                self._release_router_live_reply(reason=release_reason)

    async def _run_serialized_delivery(
        self,
        renderer: Callable[[], Any],
        *,
        settings: LiveAdapterSettings,
        source_platform: str,
        metadata: Mapping[str, Any] | None,
        hub_reply_text: str,
        expected_duration_ms: int = 0,
        on_audio_complete: Callable[[], None] | None = None,
        force_interrupt: bool = False,
    ) -> dict[str, Any]:
        hub_speech_lease = await self._acquire_hub_speech_turn(
            settings=settings,
            source_platform=source_platform,
            metadata=metadata,
            reply_text=hub_reply_text,
            expected_duration_ms=expected_duration_ms,
        )
        hub_speech_status = "completed"
        async with self._local_delivery_lock:
            try:
                if force_interrupt:
                    await self._interrupt_current_local_delivery()
                else:
                    await self._wait_for_existing_speech_completion(settings=settings)
                result = await renderer()
                await self._wait_for_local_delivery_completion(result, settings=settings)
                self._invoke_audio_complete_callback(on_audio_complete)
                return result
            except asyncio.CancelledError:
                hub_speech_status = "cancelled"
                raise
            except Exception:
                hub_speech_status = "failed"
                raise
            finally:
                await self._complete_hub_speech_turn(hub_speech_lease, status=hub_speech_status)

    def _should_stream_segmented_local_reply(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings,
        metadata: Mapping[str, Any] | None,
    ) -> bool:
        return self._collect_segmented_local_reply_segments(text, settings=settings, metadata=metadata) is not None

    def _collect_segmented_local_reply_segments(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings,
        metadata: Mapping[str, Any] | None,
    ) -> list[str] | None:
        if _extract_audio_timeline(metadata, {}) is not None:
            return None
        if not settings.webui.enabled or self._subtitle_webui is None:
            return None
        replyer_segments = _replyer_streaming_segments_from_metadata(metadata)
        if replyer_segments is not None:
            return replyer_segments
        segments = _streaming_reply_segments(text, metadata, logger=self._logger())
        return segments if len(segments) > 1 else None

    def _build_locally_rendered_reply_batch_skip_result(
        self,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        batch_id, segment_index, segment_count = _extract_replyer_batch_identity(metadata)
        if not batch_id or segment_count <= 1:
            return None
        now = time.monotonic()
        self._prune_locally_rendered_reply_batches(now=now)
        self._prune_inflight_locally_rendered_reply_batches(now=now)
        inflight = batch_id in self._inflight_locally_rendered_reply_batches
        rendered = batch_id in self._locally_rendered_reply_batches
        if not inflight and not rendered:
            return None
        self._logger().info(
            "Skipping duplicate local reply batch render: "
            f"batch_id={batch_id} segment_index={segment_index} segment_count={segment_count} "
            f"inflight={inflight} rendered={rendered}"
        )
        return {
            "treat_as_sent": True,
            "local_delivery": True,
            "reply_batch_skipped": True,
            "replyer_batch_id": batch_id,
            "replyer_segment_index": segment_index,
            "replyer_segment_count": segment_count,
        }

    def _mark_inflight_locally_rendered_reply_batch(self, metadata: Mapping[str, Any] | None) -> bool:
        batch_id, _segment_index, segment_count = _extract_replyer_batch_identity(metadata)
        if not batch_id or segment_count <= 1:
            return False
        now = time.monotonic()
        self._prune_inflight_locally_rendered_reply_batches(now=now)
        self._inflight_locally_rendered_reply_batches[batch_id] = now
        return True

    def _clear_inflight_locally_rendered_reply_batch(self, metadata: Mapping[str, Any] | None) -> None:
        batch_id, _segment_index, segment_count = _extract_replyer_batch_identity(metadata)
        if not batch_id or segment_count <= 1:
            return
        self._inflight_locally_rendered_reply_batches.pop(batch_id, None)

    def _mark_locally_rendered_reply_batch(self, metadata: Mapping[str, Any] | None) -> None:
        batch_id, _segment_index, segment_count = _extract_replyer_batch_identity(metadata)
        if not batch_id or segment_count <= 1:
            return
        now = time.monotonic()
        self._prune_locally_rendered_reply_batches(now=now)
        self._locally_rendered_reply_batches[batch_id] = now
        self._inflight_locally_rendered_reply_batches.pop(batch_id, None)

    def _prune_locally_rendered_reply_batches(self, *, now: float) -> None:
        if not self._locally_rendered_reply_batches:
            return
        expiry_before = now - 300.0
        expired_keys = [
            batch_id
            for batch_id, rendered_at in self._locally_rendered_reply_batches.items()
            if rendered_at < expiry_before
        ]
        for batch_id in expired_keys:
            self._locally_rendered_reply_batches.pop(batch_id, None)
        if len(self._locally_rendered_reply_batches) <= 1024:
            return
        overflow = len(self._locally_rendered_reply_batches) - 1024
        oldest_batches = sorted(
            self._locally_rendered_reply_batches.items(),
            key=lambda item: item[1],
        )[:overflow]
        for batch_id, _rendered_at in oldest_batches:
            self._locally_rendered_reply_batches.pop(batch_id, None)

    def _prune_inflight_locally_rendered_reply_batches(self, *, now: float) -> None:
        if not self._inflight_locally_rendered_reply_batches:
            return
        expiry_before = now - 300.0
        expired_keys = [
            batch_id
            for batch_id, started_at in self._inflight_locally_rendered_reply_batches.items()
            if started_at < expiry_before
        ]
        for batch_id in expired_keys:
            self._inflight_locally_rendered_reply_batches.pop(batch_id, None)
        if len(self._inflight_locally_rendered_reply_batches) <= 1024:
            return
        overflow = len(self._inflight_locally_rendered_reply_batches) - 1024
        oldest_batches = sorted(
            self._inflight_locally_rendered_reply_batches.items(),
            key=lambda item: item[1],
        )[:overflow]
        for batch_id, _started_at in oldest_batches:
            self._inflight_locally_rendered_reply_batches.pop(batch_id, None)

    def _start_segmented_local_reply_prepare_task(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings,
        tts_request_overrides: Mapping[str, Any] | None,
        segment_index: int,
        segment_count: int,
    ) -> tuple[asyncio.Task[tuple[str, str, dict[str, Any] | None, SynthesizedSpeech | None]], float]:
        async def prepare_segment_delivery() -> tuple[str, str, dict[str, Any] | None, SynthesizedSpeech | None]:
            return await self._prepare_reply_delivery(
                text,
                settings=settings,
                metadata={},
                kwargs={},
                tts_request_overrides=tts_request_overrides,
                timing_fields={
                    "segment_index": segment_index,
                    "segment_count": segment_count,
                },
            )

        loop = asyncio.get_running_loop()
        task = asyncio.create_task(
            prepare_segment_delivery(),
            name=f"maibot_bilibili_live_adapter.segment_prepare_{segment_index - 1}",
        )
        self._log_reply_timing(
            "prefetch_scheduled",
            text=text,
            segment_index=segment_index,
            segment_count=segment_count,
        )
        return task, loop.time()

    def _should_track_router_live_reply(
        self,
        *,
        source_platform: str,
        metadata: Mapping[str, Any] | None,
    ) -> bool:
        del metadata
        return str(source_platform or "").strip() == PLATFORM_NAME

    def _mark_router_live_reply_started(
        self,
        *,
        source_platform: str,
        metadata: Mapping[str, Any] | None,
        reason: str,
    ) -> bool:
        if self._router is None:
            return False
        if not self._should_track_router_live_reply(source_platform=source_platform, metadata=metadata):
            return False
        self._router.record_live_reply_output_started(reason=reason)
        return True

    def _release_router_live_reply(self, *, reason: str) -> None:
        if self._router is None:
            return
        self._router.record_live_reply_finished_without_output(reason=reason)

    def _parallel_turns_enabled(self) -> bool:
        # 新版宿主（1.1.x）已整体移除 parallel_turns 机制与对应配置字段，
        # getattr 兜底使本方法恒返回 False；保留方法以兼容既有调用点，
        # 若宿主未来恢复该机制可在此重新接线。
        if not bool(getattr(global_config.chat, "parallel_turns_enabled", False)):
            return False
        return str(getattr(global_config.chat, "parallel_turns_start_stage", "") or "").strip() == "planner_complete"

    def _should_use_parallel_live_reply_compaction(
        self,
        *,
        source_platform: str,
        metadata: Mapping[str, Any] | None,
    ) -> bool:
        if not self._parallel_turns_enabled():
            return False
        if str(source_platform or "").strip() != PLATFORM_NAME:
            return False
        additional_config = _extract_additional_config(metadata)
        live_event_type = str(additional_config.get("live_event_type") or "").strip().lower()
        if not live_event_type or live_event_type.startswith("sts2"):
            return False
        if live_event_type == "local_voice" or bool(additional_config.get("local_voice_input")):
            return False
        return live_event_type in {"danmaku", "super_chat", "gift", "guard", "idle_topic"}

    async def _get_parallel_live_reply_backlog_count(self) -> int:
        planner = self._planner
        if planner is None:
            return 0
        try:
            pending_count = await planner.get_pending_message_count()
        except Exception as exc:
            self._logger().warning(f"Parallel live reply backlog query failed: {exc}")
            return 0
        try:
            return max(0, int(pending_count or 0))
        except (TypeError, ValueError):
            return 0

    async def _maybe_compact_parallel_live_reply_text(
        self,
        text: str,
        *,
        source_platform: str,
        metadata: Mapping[str, Any] | None,
    ) -> str:
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return normalized_text
        if not self._should_use_parallel_live_reply_compaction(
            source_platform=source_platform,
            metadata=metadata,
        ):
            return normalized_text
        pending_count = await self._get_parallel_live_reply_backlog_count()
        if pending_count <= 0:
            return normalized_text
        compacted_text = _compact_parallel_live_reply_text(
            normalized_text,
            metadata,
            pending_count=pending_count,
            logger=self._logger(),
        )
        if compacted_text != normalized_text:
            self._log_reply_timing(
                "parallel_backlog_compacted",
                text=normalized_text,
                compacted_text_len=len(compacted_text),
                pending_count=pending_count,
            )
        return compacted_text

    async def _await_pending_local_delivery_wait(self) -> None:
        task = self._pending_local_delivery_wait_task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger().warning(f"Pending local delivery wait failed: {exc}")
        finally:
            if self._pending_local_delivery_wait_task is task and task.done():
                self._pending_local_delivery_wait_task = None

    def _schedule_pending_local_delivery_wait(
        self,
        render_result: Mapping[str, Any],
        *,
        settings: LiveAdapterSettings,
        on_audio_complete: Callable[[], None] | None = None,
        release_router_live_reply: bool = False,
        hub_speech_lease: HubSpeechLease | None = None,
    ) -> None:
        wait_started_at = asyncio.get_running_loop().time()
        task = asyncio.create_task(
            self._complete_pending_local_delivery_wait(
                render_result,
                settings=settings,
                wait_started_at=wait_started_at,
                on_audio_complete=on_audio_complete,
                release_router_live_reply=release_router_live_reply,
                hub_speech_lease=hub_speech_lease,
            ),
            name="maibot_bilibili_live_adapter.local_delivery_wait",
        )
        self._pending_local_delivery_wait_task = task

        def _on_done(done_task: asyncio.Task[None]) -> None:
            if self._pending_local_delivery_wait_task is done_task:
                self._pending_local_delivery_wait_task = None
            with contextlib.suppress(asyncio.CancelledError):
                exc = done_task.exception()
                if exc is not None:
                    self._logger().warning(f"Pending local delivery wait failed: {exc}")

        task.add_done_callback(_on_done)

    async def _complete_pending_local_delivery_wait(
        self,
        render_result: Mapping[str, Any],
        *,
        settings: LiveAdapterSettings,
        wait_started_at: float,
        on_audio_complete: Callable[[], None] | None = None,
        release_router_live_reply: bool = False,
        hub_speech_lease: HubSpeechLease | None = None,
    ) -> None:
        release_reason = "playback_finished"
        try:
            await self._wait_for_local_delivery_completion(render_result, settings=settings)
            self._log_reply_timing(
                "delivery_wait_done",
                text=str(
                    render_result.get("subtitle_text")
                    or render_result.get("speech_text")
                    or ""
                ),
                wait_elapsed_ms=int((asyncio.get_running_loop().time() - wait_started_at) * 1000),
                audio_duration_ms=int(render_result.get("audio_duration_ms") or 0),
                serialized_delivery=True,
            )
            self._invoke_audio_complete_callback(on_audio_complete)
        except asyncio.CancelledError:
            release_reason = "delivery_wait_cancelled"
            raise
        except Exception:
            release_reason = "delivery_wait_failed"
            raise
        finally:
            await self._complete_hub_speech_turn(
                hub_speech_lease,
                status=(
                    "completed"
                    if release_reason == "playback_finished"
                    else "cancelled" if release_reason == "delivery_wait_cancelled" else "failed"
                ),
            )
            if release_router_live_reply:
                self._release_router_live_reply(reason=release_reason)

    def _invoke_audio_complete_callback(self, callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:
            self._logger().warning(f"Audio complete callback failed: {exc}")

    async def _cancel_pending_local_delivery_wait(self) -> None:
        task = self._pending_local_delivery_wait_task
        self._pending_local_delivery_wait_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _interrupt_current_local_delivery(self) -> None:
        await self._cancel_pending_local_delivery_wait()
        player = self._audio_output_player
        if player is not None:
            with contextlib.suppress(Exception):
                await player.stop()

    async def _wait_for_existing_speech_completion(self, *, settings: LiveAdapterSettings) -> None:
        controller = self._live2d_controller
        if controller is None or not bool(getattr(controller, "is_speaking", False)):
            return
        timeout_sec = max(0.25, (max(0, int(settings.live2d.sync.release_ms)) + 1500) / 1000.0)
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while bool(getattr(controller, "is_speaking", False)) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

    def _hub_speech_client_id(self, settings: LiveAdapterSettings) -> str:
        return str(settings.hub_output.client_id or settings.identity.bot_user_id or "maibot-live").strip()

    def _hub_speech_bot_name(self, settings: LiveAdapterSettings) -> str:
        return (
            str(settings.hub_output.bot_name or "").strip()
            or str(settings.identity.bot_nickname or "").strip()
            or self._hub_speech_client_id(settings)
        )

    def _should_use_hub_speech_coordination(
        self,
        *,
        settings: LiveAdapterSettings,
        source_platform: str,
    ) -> bool:
        if not settings.uses_hub_input_source():
            return False
        if not settings.hub_input.speech_coordination_enabled:
            return False
        if self._hub_input_client is None:
            return False
        if self._hub_has_only_self_participant(settings):
            return False
        return str(source_platform or "").strip() == PLATFORM_NAME

    def _hub_has_only_self_participant(self, settings: LiveAdapterSettings) -> bool:
        participants = tuple(item for item in self._hub_participants if isinstance(item, Mapping))
        if not participants:
            return False
        self_client_ids = {value.casefold() for value in self._self_hub_client_ids(settings)}
        self_bot_names = {value.casefold() for value in self._self_hub_bot_names(settings)}
        saw_self = False
        for participant in participants:
            if self._participant_belongs_to_self(
                participant,
                client_id_keys=self_client_ids,
                bot_name_keys=self_bot_names,
            ):
                saw_self = True
                continue
            return False
        return saw_self

    def _current_hub_speaking_request(self) -> dict[str, Any]:
        current = self._hub_speaking_state.get("current")
        if not isinstance(current, Mapping):
            return {}
        return {str(key): value for key, value in current.items()}

    def _hub_speech_request_is_granted(self, *, request_id: str, client_id: str) -> bool:
        current = self._current_hub_speaking_request()
        return (
            str(current.get("request_id") or "").strip() == str(request_id or "").strip()
            and str(current.get("client_id") or "").strip() == str(client_id or "").strip()
        )

    def _update_hub_speaking_state(self, speaking: Any) -> None:
        if isinstance(speaking, Mapping):
            self._hub_speaking_state = {str(key): value for key, value in speaking.items()}
        else:
            self._hub_speaking_state = {}
        current_request_id = str(self._current_hub_speaking_request().get("request_id") or "").strip()
        if not current_request_id:
            return
        waiter = self._hub_speech_waiters.get(current_request_id)
        if waiter is not None:
            waiter.set()

    def _wake_hub_speech_waiters(self) -> None:
        for waiter in tuple(self._hub_speech_waiters.values()):
            waiter.set()

    def _expected_hub_speech_duration_ms(
        self,
        *,
        audio_timeline: Mapping[str, Any] | None = None,
        synthesized_speech: SynthesizedSpeech | None = None,
    ) -> int:
        durations = [0]
        if isinstance(audio_timeline, Mapping):
            with contextlib.suppress(TypeError, ValueError):
                durations.append(int(audio_timeline.get("duration_ms") or 0))
        if synthesized_speech is not None:
            with contextlib.suppress(TypeError, ValueError):
                durations.append(int(synthesized_speech.audio_duration_ms or 0))
        return max(durations)

    async def _acquire_hub_speech_turn(
        self,
        *,
        settings: LiveAdapterSettings,
        source_platform: str,
        metadata: Mapping[str, Any] | None,
        reply_text: str,
        expected_duration_ms: int = 0,
    ) -> HubSpeechLease | None:
        if not self._should_use_hub_speech_coordination(settings=settings, source_platform=source_platform):
            return None
        client = self._hub_input_client
        if client is None:
            return None
        client_id = self._hub_speech_client_id(settings)
        if not client_id:
            return None
        lease = HubSpeechLease(
            request_id=f"hub-speak-{uuid4().hex}",
            client_id=client_id,
            bot_name=self._hub_speech_bot_name(settings),
        )
        additional_config = _extract_additional_config(metadata)
        live_event_type = str(additional_config.get("live_event_type") or "").strip()
        if not live_event_type and isinstance(metadata, Mapping):
            live_event_type = str(metadata.get("type") or "").strip()
        payload = {
            "request_id": lease.request_id,
            "client_id": lease.client_id,
            "bot_name": lease.bot_name,
            "text": str(reply_text or "").strip(),
            "expected_duration_ms": max(0, int(expected_duration_ms or 0)),
            "room_id": str(settings.bilibili.room_id),
            "live_event_type": live_event_type,
        }
        waiter: asyncio.Event | None = None
        acquired = False
        try:
            response = await client.request_speak_turn(payload)
            if isinstance(response, Mapping):
                self._update_hub_speaking_state(response.get("speaking"))
            if bool(response.get("granted")) or self._hub_speech_request_is_granted(
                request_id=lease.request_id,
                client_id=lease.client_id,
            ):
                acquired = True
                return lease
            if self._hub_has_only_self_participant(settings):
                self._logger().warning(
                    "Bypassing Hub speech coordination because this adapter is the only active Hub participant."
                )
                return None
            waiter = asyncio.Event()
            self._hub_speech_waiters[lease.request_id] = waiter
            if self._hub_speech_request_is_granted(request_id=lease.request_id, client_id=lease.client_id):
                waiter.set()
            while True:
                await waiter.wait()
                if self._hub_speech_request_is_granted(request_id=lease.request_id, client_id=lease.client_id):
                    acquired = True
                    return lease
                if self._hub_input_client is None:
                    return None
                waiter.clear()
        except asyncio.CancelledError:
            if not acquired:
                with contextlib.suppress(Exception):
                    await self._complete_hub_speech_turn(lease, status="cancelled")
            raise
        except Exception:
            if not acquired:
                with contextlib.suppress(Exception):
                    await self._complete_hub_speech_turn(lease, status="failed")
            raise
        finally:
            if waiter is not None and self._hub_speech_waiters.get(lease.request_id) is waiter:
                self._hub_speech_waiters.pop(lease.request_id, None)

    async def _complete_hub_speech_turn(
        self,
        lease: HubSpeechLease | None,
        *,
        status: str,
    ) -> None:
        if lease is None:
            return
        client = self._hub_input_client
        if client is None:
            return
        try:
            response = await client.complete_speak_turn(
                {
                    "request_id": lease.request_id,
                    "client_id": lease.client_id,
                    "bot_name": lease.bot_name,
                    "status": str(status or "completed").strip() or "completed",
                }
            )
        except Exception as exc:
            self._logger().warning(
                f"Hub speech turn release failed for client={lease.client_id} request_id={lease.request_id}: {exc}"
            )
            return
        if isinstance(response, Mapping):
            self._update_hub_speaking_state(response.get("speaking"))

    async def _wait_for_local_delivery_completion(
        self,
        render_result: Mapping[str, Any],
        *,
        settings: LiveAdapterSettings,
    ) -> None:
        if bool(render_result.get("delivery_waited")):
            return
        if bool(render_result.get("audio_played_to_vts")):
            return
        controller = self._live2d_controller
        if bool(render_result.get("live2d_synchronized")) and controller is not None:
            timeout_sec = max(
                0.25,
                (
                    max(0, int(render_result.get("audio_duration_ms") or 0))
                    + max(0, int(settings.live2d.sync.release_ms))
                    + 1500
                )
                / 1000.0,
            )
            deadline = asyncio.get_running_loop().time() + timeout_sec
            while bool(getattr(controller, "is_speaking", False)) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            return
        audio_duration_ms = max(0, int(render_result.get("audio_duration_ms") or 0))
        if audio_duration_ms > 0:
            await asyncio.sleep(audio_duration_ms / 1000.0)

    def _log_reply_timing(self, event: str, *, text: str = "", **fields: Any) -> None:
        normalized_text = str(text or "").replace("\r", " ").replace("\n", " ").strip()
        parts = [f"event={event}"]
        if normalized_text:
            preview_text = normalized_text if len(normalized_text) <= 32 else f"{normalized_text[:32]}..."
            parts.append(f"text_len={len(normalized_text)}")
            parts.append(f"text_preview={preview_text!r}")
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, bool):
                formatted_value = "true" if value else "false"
            elif isinstance(value, float):
                formatted_value = f"{value:.1f}"
            else:
                formatted_value = str(value)
            parts.append(f"{key}={formatted_value}")
        with contextlib.suppress(Exception):
            self._logger().info("Reply timing: " + " ".join(parts))

    async def _handle_paid_event_acknowledgement(self, event: Mapping[str, Any]) -> bool:
        settings = self._load_settings()
        if not settings.plugin.enabled:
            return False
        thank_text = _build_paid_acknowledgement_text(event)
        if not thank_text:
            return False
        render_result = await self._deliver_text_reply_serialized(
            thank_text,
            settings=settings,
            source_platform=PLATFORM_NAME,
            metadata={
                "event_id": str(event.get("event_id") or ""),
                "paid_event_acknowledgement": True,
                "paid_event_type": str(event.get("type") or ""),
            },
            kwargs={},
            tts_request_overrides=_PAID_ACKNOWLEDGEMENT_TTS_REQUEST_OVERRIDES,
            force_interrupt=str(event.get("type") or "").strip() == "super_chat",
        )
        if self._router is not None:
            history_text = str(render_result.get("subtitle_text") or thank_text).strip()
            if history_text:
                self._router.record_bot_output_history(history_text)
        self._logger().info(
            "Acknowledged paid live event with fixed TTS reply: "
            f"type={event.get('type') or ''} user={event.get('username') or event.get('user_id') or 'anonymous'} "
            f"text={thank_text!r}"
        )
        return True

    @Tool(
        "play_sound_effect",
        description=(
            "Play a configured live soundboard cue and show its green-screen media overlay. "
            "Provide a specific cue id, or describe your intent and let the system auto-select the best match."
        ),
        parameters={
            "cue": {
                "type": "string",
                "default": "",
                "description": "Exact cue id or label. Leave empty to auto-select based on intent.",
            },
            "intent": {
                "type": "string",
                "default": "",
                "description": "Short reaction intent for auto-selection: laugh, surprise, awkward, dramatic, etc. Only used when cue is empty.",
            },
            "repeat_count": {
                "type": "integer",
                "default": 1,
                "description": "How many times to play this cue.",
            },
            "reason": {"type": "string", "default": "", "description": "Brief reason this cue fits the moment."},
            "text": {"type": "string", "default": "", "description": "Optional chat or reply text that prompted it."},
        },
    )
    async def play_sound_effect(
        self,
        cue: str = "",
        intent: str = "",
        repeat_count: int = 1,
        reason: str = "",
        text: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Unified soundboard tool: explicit cue or intent-based auto-select."""

        del kwargs
        settings = self._load_settings()
        if not settings.soundboard.enabled:
            return {"success": False, "error": "soundboard is not enabled in config"}
        if self._soundboard_admin_disabled:
            return {"success": False, "error": "soundboard is currently disabled by admin command. Use /soundboard on to re-enable."}
        service = self._soundboard
        if service is None:
            return {
                "success": False,
                "error": "soundboard runtime is not started",
                "available_cues": list_soundboard_cues(
                    settings.soundboard,
                    plugin_dir=Path(__file__).resolve().parent,
                    available_only=True,
                ),
            }

        resolved_cue = None
        selected_by = "explicit"
        selection_reason = ""

        if not cue.strip():
            intent_text = str(intent or "").strip()
            reason_text = str(reason or "").strip()
            match_text = str(text or intent_text or "").strip()
            if not match_text:
                return {
                    "success": False,
                    "error": "provide either a cue id or an intent for auto-selection",
                    "available_cues": list_soundboard_cues(
                        settings.soundboard, plugin_dir=Path(__file__).resolve().parent, available_only=True,
                    ),
                }
            resolved_cue = self._heuristic_select_soundboard_cue(
                settings.soundboard,
                intent=intent_text,
                reason=reason_text,
                text=match_text,
            )
            if resolved_cue is not None:
                selected_by = "heuristic"
                selection_reason = f"auto-selected from intent={intent_text!r}"
            else:
                return {
                    "success": False,
                    "error": f"no matching sound cue found for intent: {intent_text}",
                    "available_cues": list_soundboard_cues(
                        settings.soundboard, plugin_dir=Path(__file__).resolve().parent, available_only=True,
                    ),
                }

        cue_id = resolved_cue.cue_id if resolved_cue is not None else cue.strip()
        plan = self._queue_pending_soundboard_trigger(
            cue_id,
            repeat_count=repeat_count,
            reason=reason,
            text=text or (intent if resolved_cue is not None else cue_id),
            triggered_by="maibot_tool",
            settings=settings,
        )
        resolved = resolve_soundboard_cue(
            settings.soundboard,
            cue_id,
            plugin_dir=Path(__file__).resolve().parent,
            available_only=True,
        )
        if resolved is None:
            self._discard_pending_soundboard_trigger(plan.trigger_id)
            return {
                "success": False,
                "error": f"unknown sound cue: {cue_id}",
                "available_cues": list_soundboard_cues(
                    settings.soundboard,
                    plugin_dir=Path(__file__).resolve().parent,
                    available_only=True,
                ),
            }
        result: dict[str, Any] = {
            "success": True,
            "queued": True,
            "trigger_id": plan.trigger_id,
            "cue_id": resolved.cue_id,
            "label": resolved.cue.label or resolved.cue_id,
            "repeat_count": max(1, int(repeat_count or 1)),
        }
        if selected_by != "explicit":
            result["selected_by"] = selected_by
            if selection_reason:
                result["selection_reason"] = selection_reason
        return result

    def _heuristic_select_soundboard_cue(
        self,
        soundboard_settings: SoundboardConfig,
        *,
        intent: str,
        reason: str,
        text: str,
    ) -> SoundboardResolvedCue | None:
        """Heuristic cue selection without an external LLM call."""
        plugin_dir = Path(__file__).resolve().parent
        cue_metadata = list_soundboard_cues(soundboard_settings, plugin_dir=plugin_dir, available_only=True)
        if not cue_metadata:
            return None
        best_score = 0.0
        best_cue: SoundboardResolvedCue | None = None
        scoring = soundboard_settings.scoring

        for meta in cue_metadata:
            resolved = resolve_soundboard_cue(soundboard_settings, meta["id"], plugin_dir=plugin_dir, available_only=True)
            if resolved is None:
                continue
            score = float(resolved.cue.priority) * scoring.priority_weight
            for field_text, weight in [
                (intent, scoring.keyword_match_weight * 4.0),
                (reason, scoring.keyword_match_weight * 3.0),
                (text, scoring.keyword_match_weight * 2.0),
            ]:
                if not field_text:
                    continue
                for keyword in resolved.cue.keywords:
                    if _keyword_matches(field_text, keyword, match_mode=resolved.cue.match_mode):
                        score += 140.0 * weight / scoring.keyword_match_weight
                if _find_explicit_cue_mention(field_text, cue_id=resolved.cue_id, cue_label=resolved.cue.label):
                    score += 90.0 * weight / scoring.keyword_match_weight
                overlap = _soundboard_overlap_score(
                    field_text,
                    " ".join([resolved.cue_id, resolved.cue.label or ""]
                             + list(resolved.cue.keywords)
                             + ([resolved.cue.usage_hint] if resolved.cue.usage_hint else [])),
                )
                score += overlap * scoring.label_overlap_weight * (weight / scoring.keyword_match_weight)
            if score > best_score:
                best_score = score
                best_cue = resolved

        threshold = scoring.min_user_request_score if intent or reason else scoring.min_auto_score
        if best_score < threshold:
            return None
        if best_cue is None and cue_metadata:
            fallback = resolve_soundboard_cue(
                soundboard_settings, cue_metadata[0]["id"], plugin_dir=plugin_dir, available_only=True,
            )
            if fallback is not None and (intent or reason):
                return fallback
            return None
        return best_cue

    async def _auto_select_soundboard_cue(
        self,
        soundboard_settings: SoundboardConfig,
        *,
        intent: str = "",
        reason: str = "",
        text: str = "",
    ) -> tuple[SoundboardResolvedCue | None, str, str]:
        plugin_dir = Path(__file__).resolve().parent
        cue_metadata = list_soundboard_cues(
            soundboard_settings,
            plugin_dir=plugin_dir,
            available_only=True,
        )
        if soundboard_settings.auto_select_llm.enabled and cue_metadata:
            logger = self._logger()
            client = SoundboardAutoSelectClient(soundboard_settings.auto_select_llm, logger=logger)
            try:
                llm_result = await client.select_cue(
                    cue_summaries=cue_metadata,
                    intent=intent,
                    reason=reason,
                    text=text,
                )
            except Exception as exc:
                if logger is not None and hasattr(logger, "warning"):
                    logger.warning(f"Soundboard auto-select LLM failed, falling back to heuristic selection: {exc}")
            else:
                if llm_result is not None and llm_result.cue_id and not llm_result.no_match:
                    resolved = resolve_soundboard_cue(
                        soundboard_settings,
                        llm_result.cue_id,
                        plugin_dir=plugin_dir,
                        available_only=True,
                    )
                    if resolved is not None:
                        return resolved, "auto_llm", llm_result.reason
                elif llm_result is not None and llm_result.no_match and llm_result.reason:
                    if logger is not None and hasattr(logger, "info"):
                        logger.info(f"Soundboard auto-select LLM returned no_match, falling back to heuristic: {llm_result.reason}")
        selected = _select_soundboard_auto_tool_cue(
            soundboard_settings,
            intent=intent,
            reason=reason,
            text=text,
        )
        if selected is None:
            return None, "auto_none", ""
        return selected, "auto_heuristic", ""

    def _queue_pending_soundboard_trigger(
        self,
        cue: str,
        *,
        repeat_count: int,
        reason: str,
        text: str,
        triggered_by: str,
        settings: LiveAdapterSettings,
        target_segment_index: int = 0,
    ) -> PendingSoundboardTrigger:
        normalized_cue = str(cue or "").strip()
        normalized_repeat_count = max(1, int(repeat_count or 1))
        normalized_reason = str(reason or "")
        normalized_text = str(text or "")
        normalized_triggered_by = str(triggered_by or "").strip() or "soundboard"
        normalized_target_segment_index = max(0, int(target_segment_index or 0))
        created_at = time.monotonic()
        for index, existing in enumerate(list(self._pending_soundboard_triggers)):
            if existing.cue != normalized_cue or (created_at - existing.created_at) > 2.5:
                continue
            merged = PendingSoundboardTrigger(
                trigger_id=existing.trigger_id,
                cue=existing.cue,
                repeat_count=max(existing.repeat_count, normalized_repeat_count),
                reason=normalized_reason or existing.reason,
                source_text=normalized_text or existing.source_text,
                triggered_by=(
                    "maibot_tool"
                    if "maibot_tool" in {existing.triggered_by, normalized_triggered_by}
                    else normalized_triggered_by
                ),
                created_at=existing.created_at,
                target_segment_index=existing.target_segment_index or normalized_target_segment_index,
            )
            self._pending_soundboard_triggers[index] = merged
            self._schedule_soundboard_trigger_fallback(merged, settings=settings)
            return merged
        plan = PendingSoundboardTrigger(
            trigger_id=uuid4().hex,
            cue=normalized_cue,
            repeat_count=normalized_repeat_count,
            reason=normalized_reason,
            source_text=normalized_text,
            triggered_by=normalized_triggered_by,
            created_at=created_at,
            target_segment_index=normalized_target_segment_index,
        )
        self._pending_soundboard_triggers.append(plan)
        self._schedule_soundboard_trigger_fallback(plan, settings=settings)
        return plan

    def _schedule_soundboard_trigger_fallback(
        self,
        plan: PendingSoundboardTrigger,
        *,
        settings: LiveAdapterSettings,
    ) -> None:
        existing_task = self._pending_soundboard_fallback_tasks.pop(plan.trigger_id, None)
        if existing_task is not None:
            existing_task.cancel()
        task = asyncio.create_task(
            self._play_pending_soundboard_trigger_after_delay(plan, settings=settings),
            name=f"maibot_bilibili_live_adapter.soundboard_fallback.{plan.trigger_id}",
        )
        self._pending_soundboard_fallback_tasks[plan.trigger_id] = task

        def _on_done(done_task: asyncio.Task[None]) -> None:
            self._pending_soundboard_fallback_tasks.pop(plan.trigger_id, None)
            with contextlib.suppress(asyncio.CancelledError):
                exc = done_task.exception()
                if exc is not None:
                    logger = self._logger()
                    if logger is not None and hasattr(logger, "warning"):
                        logger.warning(f"Soundboard fallback trigger failed: {exc}")

        task.add_done_callback(_on_done)

    async def _play_pending_soundboard_trigger_after_delay(
        self,
        plan: PendingSoundboardTrigger,
        *,
        settings: LiveAdapterSettings,
    ) -> None:
        await asyncio.sleep(2.0)
        pending_plan = self._discard_pending_soundboard_trigger(plan.trigger_id)
        if pending_plan is None:
            return
        await self._trigger_soundboard_tool_plan(pending_plan, settings=settings)

    def _discard_pending_soundboard_trigger(self, trigger_id: str) -> PendingSoundboardTrigger | None:
        normalized_trigger_id = str(trigger_id or "").strip()
        if not normalized_trigger_id:
            return None
        for index, plan in enumerate(list(self._pending_soundboard_triggers)):
            if plan.trigger_id != normalized_trigger_id:
                continue
            self._pending_soundboard_triggers.pop(index)
            fallback_task = self._pending_soundboard_fallback_tasks.pop(normalized_trigger_id, None)
            if fallback_task is not None:
                fallback_task.cancel()
            return plan
        return None

    def _consume_pending_soundboard_triggers(self) -> list[PendingSoundboardTrigger]:
        plans = list(self._pending_soundboard_triggers)
        self._pending_soundboard_triggers.clear()
        for plan in plans:
            fallback_task = self._pending_soundboard_fallback_tasks.pop(plan.trigger_id, None)
            if fallback_task is not None:
                fallback_task.cancel()
        return plans

    async def _trigger_soundboard_tool_plan(
        self,
        plan: PendingSoundboardTrigger,
        *,
        settings: LiveAdapterSettings,
    ) -> dict[str, Any]:
        service = self._soundboard
        if service is None:
            return {"success": False, "error": "soundboard runtime is not started"}
        return await service.trigger(
            plan.cue,
            repeat_count=plan.repeat_count,
            reason=plan.reason,
            source_text=plan.source_text,
            triggered_by=plan.triggered_by,
        )

    async def _wait_for_soundboard_idle(self, *, settings: LiveAdapterSettings, timeout_sec: float = 30.0) -> bool:
        if not settings.soundboard.enabled:
            return False
        service = self._soundboard
        if service is None:
            return False
        try:
            return await service.wait_until_idle(timeout_sec=timeout_sec)
        except Exception as exc:
            logger = self._logger()
            if logger is not None and hasattr(logger, "warning"):
                logger.warning(f"Soundboard idle wait failed: {exc}")
            return False

    def _build_reply_auto_soundboard_plans(
        self,
        *,
        text: str,
        segments: Sequence[str],
        metadata: Mapping[str, Any] | None,
        settings: LiveAdapterSettings,
        existing_plans: Sequence[PendingSoundboardTrigger],
    ) -> list[PendingSoundboardTrigger]:
        if existing_plans or not settings.soundboard.enabled or not settings.soundboard.reply_auto_triggers_enabled:
            return []
        normalized_segments = [str(segment or "").strip() for segment in segments if str(segment or "").strip()]
        normalized_reply_text = str(text or "").strip()
        if not normalized_segments and not normalized_reply_text:
            return []
        additional_config = _extract_additional_config(metadata)
        request_detected = bool(additional_config.get("soundboard_request_detected"))
        live_event_type = str(additional_config.get("live_event_type") or "").strip().lower()
        if not request_detected and live_event_type not in {"danmaku", "super_chat", "hub_local_input", "hub_bot_reply"}:
            return []
        plugin_dir = Path(__file__).resolve().parent
        request_text = str(additional_config.get("soundboard_request_text") or "").strip()
        requested_cue = str(additional_config.get("soundboard_requested_cue") or "").strip()
        for candidate_text in _iter_soundboard_trigger_texts(normalized_reply_text, *normalized_segments):
            matched_reply_cue = _find_explicit_soundboard_cue_mention(
                settings.soundboard,
                candidate_text,
                plugin_dir=plugin_dir,
            )
            if matched_reply_cue is None:
                matched_reply_cue = match_soundboard_keyword(
                    settings.soundboard,
                    candidate_text,
                    plugin_dir=plugin_dir,
                    available_only=True,
                )
            if matched_reply_cue is None:
                continue
            if request_detected and not requested_cue:
                return [
                    PendingSoundboardTrigger(
                        trigger_id=uuid4().hex,
                        cue=matched_reply_cue.cue_id,
                        repeat_count=1,
                        reason="user_requested_soundboard",
                        source_text=request_text or candidate_text,
                        triggered_by="reply_auto_request",
                        created_at=time.monotonic(),
                        target_segment_index=_default_soundboard_target_segment_index(normalized_segments),
                    )
                ]
            if not request_detected:
                return []
        if requested_cue:
            resolved_requested_cue = resolve_soundboard_cue(
                settings.soundboard,
                requested_cue,
                plugin_dir=plugin_dir,
                available_only=True,
            )
            if resolved_requested_cue is not None:
                return [
                    PendingSoundboardTrigger(
                        trigger_id=uuid4().hex,
                        cue=resolved_requested_cue.cue_id,
                        repeat_count=1,
                        reason="user_requested_soundboard",
                        source_text=request_text or normalized_reply_text,
                        triggered_by="reply_auto_request",
                        created_at=time.monotonic(),
                        target_segment_index=_default_soundboard_target_segment_index(normalized_segments),
                    )
                ]
        auto_candidate = self._select_reply_auto_soundboard_candidate(
            settings.soundboard,
            reply_text=normalized_reply_text,
            segments=normalized_segments or ([normalized_reply_text] if normalized_reply_text else []),
            request_text=request_text,
        )
        if auto_candidate is None:
            return []
        resolved_cue, target_segment_index, selected_score, selected_source_text = auto_candidate
        min_score = 80.0 if request_detected else 160.0
        if selected_score < min_score:
            if not request_detected:
                return []
            available_cues = list_soundboard_cues(settings.soundboard, plugin_dir=plugin_dir, available_only=True)
            if len(available_cues) != 1:
                return []
        logger = self._logger()
        if logger is not None and hasattr(logger, "info"):
            logger.info(
                "Soundboard reply-auto cue selected: cue=%s score=%.1f request=%s segment=%s text=%r",
                resolved_cue.cue_id,
                selected_score,
                int(request_detected),
                target_segment_index,
                selected_source_text[:80],
            )
        return [
            PendingSoundboardTrigger(
                trigger_id=uuid4().hex,
                cue=resolved_cue.cue_id,
                repeat_count=1,
                reason="user_requested_soundboard" if request_detected else "reply_auto_reaction",
                source_text=selected_source_text or request_text or normalized_reply_text,
                triggered_by="reply_auto_request" if request_detected else "reply_auto",
                created_at=time.monotonic(),
                target_segment_index=target_segment_index,
            )
        ]

    def _select_reply_auto_soundboard_candidate(
        self,
        soundboard_settings: SoundboardConfig,
        *,
        reply_text: str,
        segments: Sequence[str],
        request_text: str,
    ) -> tuple[SoundboardResolvedCue, int, float, str] | None:
        plugin_dir = Path(__file__).resolve().parent
        cue_metadata = list_soundboard_cues(
            soundboard_settings,
            plugin_dir=plugin_dir,
            available_only=True,
        )
        if not cue_metadata:
            return None
        resolved_cues: list[SoundboardResolvedCue] = []
        for cue_summary in cue_metadata:
            cue_id = str(cue_summary.get("id") or "").strip()
            if not cue_id:
                continue
            resolved = resolve_soundboard_cue(
                soundboard_settings,
                cue_id,
                plugin_dir=plugin_dir,
                available_only=True,
            )
            if resolved is not None:
                resolved_cues.append(resolved)
        if not resolved_cues:
            return None
        normalized_segments = [str(segment or "").strip() for segment in segments if str(segment or "").strip()]
        if not normalized_segments:
            normalized_segments = [str(reply_text or "").strip()]
        default_segment_index = _default_soundboard_target_segment_index(normalized_segments)
        best: tuple[float, int, SoundboardResolvedCue, int, str] | None = None
        for segment_index, segment_text in enumerate(normalized_segments, start=1):
            normalized_segment_text = str(segment_text or "").strip()
            if not normalized_segment_text:
                continue
            segment_preference = -abs(segment_index - default_segment_index)
            for resolved in resolved_cues:
                score = _score_soundboard_auto_tool_cue(
                    resolved,
                    intent=request_text,
                    reason=reply_text if request_text else "",
                    text=normalized_segment_text,
                )
                candidate = (score, segment_preference, resolved, segment_index, normalized_segment_text)
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
        if best is not None and best[0] > 0:
            return best[2], best[3], best[0], best[4]
        if request_text and len(resolved_cues) == 1:
            return (
                resolved_cues[0],
                default_segment_index,
                1.0,
                str(request_text or reply_text or "").strip(),
            )
        return None

    async def special_move(
        self,
        move: str,
        duration_sec: float = 10.0,
        action: str = "Special_move",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Internal handler for named Live2D special moves."""

        del kwargs
        runtime = self._embodied_live2d_runtime
        if runtime is None:
            return {"success": False, "error": "Live2D embodied runtime is not enabled"}
        normalized_action = str(action or "").strip() or "Special_move"
        normalized_move = normalize_special_move_name(move)
        normalized_duration = max(0.1, float(duration_sec))
        accepted = await runtime.request_special_move(
            action=normalized_action,
            move=normalized_move,
            duration_sec=normalized_duration,
        )
        if not accepted:
            return {
                "success": False,
                "error": f"special move rejected: {normalized_action}:{normalized_move}",
            }
        return {
            "success": True,
            "action": normalized_action,
            "move": normalized_move,
            "duration_sec": normalized_duration,
        }

    async def handle_live2d_debug_command(self, event: Mapping[str, Any]) -> bool:
        settings = self._load_settings()
        command_settings = settings.live2d.embodied.debug_commands
        if not command_settings.enabled:
            return False
        text = str(event.get("text") or event.get("summary") or "").strip()
        prefix = command_settings.prefix
        if not _matches_slash_command_prefix(text, prefix):
            return False
        try:
            user_id = str(event.get("user_id") or "").strip()
            is_admin = bool(user_id and user_id in command_settings.admin_user_ids)
            if not is_admin:
                if command_settings.drop_non_admin_commands:
                    await self._publish_live2d_debug_feedback(
                        "Live2D 调试指令仅管理员可用 (admin only).",
                        settings=settings,
                    )
                    return True
                return False

            raw_args = text[len(prefix) :].strip()
            parts = raw_args.split() if raw_args else []
            command = parts[0].lower() if parts else "help"
            args = parts[1:] if parts else []

            if command == "status":
                await self._publish_live2d_debug_feedback(
                    self._format_live2d_debug_status(settings),
                    settings=settings,
                )
                return True
            if command == "mouse":
                await self._handle_live2d_debug_mouse_command(args, settings=settings)
                return True
            if command == "reset":
                await self._handle_live2d_debug_reset_command(settings=settings)
                return True
            if command == "emotion":
                await self._handle_live2d_debug_emotion_command(args, settings=settings)
                return True

            await self._publish_live2d_debug_feedback(
                "Live2D 调试命令: /l2d status | /l2d mouse on|off|toggle | /l2d reset | /l2d emotion happy 0.8",
                settings=settings,
            )
            return True
        except Exception as exc:
            logger = self._logger()
            if logger is not None and hasattr(logger, "warning"):
                logger.warning(f"Live2D debug command failed: {exc}")
            return True

    async def handle_soundboard_command(self, event: Mapping[str, Any]) -> bool:
        settings = self._load_settings()
        command_settings = settings.soundboard.command
        if not command_settings.enabled:
            return False
        text = str(event.get("text") or event.get("summary") or "").strip()
        prefix = command_settings.prefix
        if not _matches_slash_command_prefix(text, prefix):
            return False
        try:
            if not self._is_soundboard_command_authorized(event, settings=settings):
                logger = self._logger()
                if logger is not None and hasattr(logger, "warning"):
                    logger.warning(
                        "Soundboard command rejected: type=%s user_id=%s username=%s text=%r",
                        event.get("type") or "",
                        event.get("user_id") or "",
                        event.get("username") or "",
                        text,
                    )
                return bool(command_settings.drop_non_admin_commands)

            raw_args = text[len(prefix):].strip()
            args_lower = raw_args.lower()
            if not raw_args or args_lower in {alias.lower() for alias in command_settings.status_aliases}:
                await self._handle_soundboard_status_command(settings=settings)
                return True
            if args_lower in {alias.lower() for alias in command_settings.on_aliases}:
                await self._handle_soundboard_enable_command(settings=settings)
                return True
            if args_lower in {alias.lower() for alias in command_settings.off_aliases}:
                await self._handle_soundboard_disable_command(settings=settings)
                return True
            await self._publish_soundboard_command_feedback(
                f"Soundboard 命令: {prefix} on|off|status",
                settings=settings,
            )
            return True
        except Exception as exc:
            logger = self._logger()
            if logger is not None and hasattr(logger, "warning"):
                logger.warning(f"Soundboard command failed: {exc}")
            return True

    def _is_soundboard_command_authorized(
        self,
        event: Mapping[str, Any],
        *,
        settings: LiveAdapterSettings,
    ) -> bool:
        command_settings = settings.soundboard.command
        event_type = str(event.get("type") or "").strip().lower()
        if event_type == "hub_local_input" and command_settings.allow_hub_local_input:
            return True
        allowed_identities = {
            str(value or "").strip().casefold()
            for value in command_settings.authorized_identities
            if str(value or "").strip()
        }
        if not allowed_identities:
            return False
        source_user_id = str(event.get("user_id") or "").strip()
        source_username = str(event.get("username") or source_user_id).strip()
        resolved_user_id, resolved_username = resolve_live_identity(
            user_id=source_user_id,
            username=source_username,
        )
        candidate_identities = {
            value.casefold()
            for value in {
                source_user_id,
                source_username,
                resolved_user_id,
                resolved_username,
            }
            if value
        }
        return bool(candidate_identities & allowed_identities)

    async def _handle_soundboard_status_command(self, *, settings: LiveAdapterSettings) -> None:
        config_enabled = bool(settings.soundboard.enabled)
        admin_disabled = bool(self._soundboard_admin_disabled)
        runtime_running = self._soundboard is not None
        if config_enabled and not admin_disabled and runtime_running:
            state_text = "Soundboard 已启用 (ENABLED)"
        elif config_enabled and admin_disabled:
            state_text = "Soundboard 已通过管理员命令暂停 (ADMIN DISABLED)"
        elif not config_enabled:
            state_text = "Soundboard 已在配置中关闭 (CONFIG DISABLED)"
        else:
            state_text = "Soundboard 配置已启用但运行时未就绪 (NOT READY)"
        await self._publish_soundboard_command_feedback(state_text, settings=settings)

    async def _handle_soundboard_enable_command(self, *, settings: LiveAdapterSettings) -> None:
        if not settings.soundboard.enabled:
            await self._publish_soundboard_command_feedback(
                "Soundboard 无法启用：配置中 soundboard.enabled 为 false，请先在 WebUI 中开启。",
                settings=settings,
            )
            return
        if not self._soundboard_admin_disabled and self._soundboard is not None:
            await self._publish_soundboard_command_feedback(
                "Soundboard 已经在运行中，无需重复启用。",
                settings=settings,
            )
            return
        self._soundboard_admin_disabled = False
        if self._soundboard is None:
            try:
                await self._start_soundboard_runtime(settings)
            except Exception as exc:
                logger = self._logger()
                if logger is not None and hasattr(logger, "warning"):
                    logger.warning(f"Soundboard enable command failed to start runtime: {exc}")
                await self._publish_soundboard_command_feedback(
                    f"Soundboard 启用失败：{exc}",
                    settings=settings,
                )
                return
        await self._publish_soundboard_command_feedback(
            "Soundboard 已启用 (ENABLED)",
            settings=settings,
        )
        logger = self._logger()
        if logger is not None and hasattr(logger, "info"):
            logger.info("Soundboard enabled via admin command")

    async def _handle_soundboard_disable_command(self, *, settings: LiveAdapterSettings) -> None:
        if self._soundboard_admin_disabled and self._soundboard is None:
            await self._publish_soundboard_command_feedback(
                "Soundboard 已经处于暂停状态，无需重复关闭。",
                settings=settings,
            )
            return
        self._soundboard_admin_disabled = True
        try:
            await self._stop_soundboard_runtime()
        except Exception as exc:
            logger = self._logger()
            if logger is not None and hasattr(logger, "warning"):
                logger.warning(f"Soundboard disable command failed to stop runtime: {exc}")
        await self._publish_soundboard_command_feedback(
            "Soundboard 已暂停 (ADMIN DISABLED)",
            settings=settings,
        )
        logger = self._logger()
        if logger is not None and hasattr(logger, "info"):
            logger.info("Soundboard disabled via admin command")

    async def _start_soundboard_runtime(self, settings: LiveAdapterSettings) -> None:
        soundboard = SoundboardService(
            settings.soundboard,
            plugin_dir=Path(__file__).resolve().parent,
            logger=self._logger(),
        )
        await soundboard.start()
        self._soundboard = soundboard
        await self._maybe_auto_open_soundboard_webui(settings, soundboard.url)

    async def _stop_soundboard_runtime(self) -> None:
        for task in list(self._pending_soundboard_fallback_tasks.values()):
            task.cancel()
        if self._pending_soundboard_fallback_tasks:
            await asyncio.gather(*self._pending_soundboard_fallback_tasks.values(), return_exceptions=True)
        self._pending_soundboard_fallback_tasks.clear()
        self._pending_soundboard_triggers.clear()
        if self._soundboard is not None:
            await self._soundboard.stop()
        self._soundboard = None

    async def _publish_soundboard_command_feedback(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings,
    ) -> None:
        gateway = self.ctx.gateway
        if gateway is None:
            return
        event_id = f"soundboard-cmd-{uuid4().hex[:12]}"
        room_id = str(settings.bilibili.room_id)
        feedback_message = {
            "message_id": event_id,
            "timestamp": str(time.time()),
            "platform": PLATFORM_NAME,
            "message_info": {
                "user_info": {
                    "user_id": "system",
                    "user_nickname": "System",
                    "user_cardname": None,
                },
                "group_info": {
                    "group_id": room_id,
                    "group_name": f"bilibili_live_{room_id}",
                },
                "additional_config": {
                    "platform_io_account_id": settings.identity.bot_user_id,
                    "platform_io_scope": settings.route_scope(),
                    "maibot_memory_platform": "qq",
                    "maibot_memory_group_id": room_id,
                    "maibot_local_render_only": True,
                    "soundboard_command_feedback": True,
                },
            },
            "raw_message": [{"type": "text", "data": text}],
            "is_mentioned": False,
            "is_at": False,
            "is_emoji": False,
            "is_picture": False,
            "is_command": False,
            "is_notify": False,
            "session_id": "",
            "processed_plain_text": text,
            "display_message": text,
        }
        try:
            await gateway.route_message(
                GATEWAY_NAME,
                feedback_message,
                route_metadata={
                    "platform_io_account_id": settings.identity.bot_user_id,
                    "platform_io_scope": settings.route_scope(),
                },
                external_message_id=event_id,
            )
        except Exception as exc:
            logger = self._logger()
            if logger is not None and hasattr(logger, "warning"):
                logger.warning(f"Failed to route soundboard command feedback: {exc}")

    async def handle_visual_context_command(self, event: Mapping[str, Any]) -> bool:
        settings = self._load_settings()
        command_settings = settings.vision.command
        if not settings.vision.enabled or not command_settings.enabled:
            return False
        text = str(event.get("text") or event.get("summary") or "").strip()
        prefix = command_settings.prefix
        if not _matches_slash_command_prefix(text, prefix):
            return False
        if not self._is_visual_context_command_authorized(event, settings=settings):
            logger = self._logger()
            if logger is not None and hasattr(logger, "warning"):
                logger.warning(
                    "Visual context command rejected: type=%s user_id=%s username=%s text=%r",
                    event.get("type") or "",
                    event.get("user_id") or "",
                    event.get("username") or "",
                    text,
                )
            return bool(command_settings.drop_non_admin_commands)

        focus_question = text[len(prefix) :].strip()
        if self._is_visual_context_stop_command(focus_question, settings=settings):
            await self._stop_visual_context_polling(settings=settings)
            logger = self._logger()
            if logger is not None and hasattr(logger, "info"):
                logger.info(
                    "Visual context polling stopped: user=%s",
                    event.get("user_id") or event.get("username") or event.get("type") or "anonymous",
                )
            return True

        started = await self._start_visual_context_polling(
            settings=settings,
            focus_question=focus_question,
            event=event,
        )
        if not started:
            return True

        if focus_question:
            followup_event = dict(event)
            followup_event["text"] = focus_question
            followup_event["summary"] = focus_question
            await self._route_visual_command_followup_message(
                event=followup_event,
                settings=settings,
            )
        logger = self._logger()
        if logger is not None and hasattr(logger, "info"):
            logger.info(
                "Visual context polling started: user=%s question=%r",
                event.get("user_id") or event.get("username") or event.get("type") or "anonymous",
                focus_question,
            )
        return True

    async def handle_video_watch_event(self, event: Mapping[str, Any]) -> bool:
        settings = self._load_settings()
        controller = self._video_watch_controller
        if controller is None or not settings.video_watch.enabled:
            return False
        text = str(event.get("text") or event.get("summary") or "").strip()
        if not text:
            return False
        requested_by = self._video_watch_requested_by(event)
        if controller.is_waiting_for_consent():
            consumed = await controller.handle_consent_reply(text=text, requested_by=requested_by)
            if consumed:
                logger = self._logger()
                if logger is not None and hasattr(logger, "info"):
                    logger.info(
                        "Video watch consent handled: user=%s text=%r",
                        requested_by,
                        text,
                    )
                return True
        command_settings = settings.video_watch.command
        if not command_settings.enabled:
            return False
        if not _matches_slash_command_prefix(text, command_settings.prefix):
            return False
        if not self._is_video_watch_command_authorized(event, settings=settings):
            logger = self._logger()
            if logger is not None and hasattr(logger, "warning"):
                logger.warning(
                    "Video watch command rejected: type=%s user_id=%s username=%s text=%r",
                    event.get("type") or "",
                    event.get("user_id") or "",
                    event.get("username") or "",
                    text,
                )
            return bool(command_settings.drop_non_admin_commands)
        trigger = str(event.get("type") or "manual_command").strip() or "manual_command"
        handled = await controller.handle_manual_command(
            command_text=text,
            requested_by=requested_by,
            trigger=trigger,
        )
        if handled:
            logger = self._logger()
            if logger is not None and hasattr(logger, "info"):
                logger.info(
                    "Video watch command handled: user=%s text=%r",
                    requested_by,
                    text,
                )
        return handled

    async def handle_video_watch_idle_trigger(self) -> bool:
        settings = self._load_settings()
        controller = self._video_watch_controller
        if controller is None or not settings.video_watch.enabled:
            return False
        return await controller.maybe_offer()

    def _should_block_idle_topic_for_video_watch(self) -> bool:
        controller = self._video_watch_controller
        return bool(controller is not None and controller.enabled and controller.should_block_idle_topic())

    def _is_video_watch_command_authorized(
        self,
        event: Mapping[str, Any],
        *,
        settings: LiveAdapterSettings,
    ) -> bool:
        command_settings = settings.video_watch.command
        event_type = str(event.get("type") or "").strip().lower()
        if event_type == "hub_local_input" and command_settings.allow_hub_local_input:
            return True
        text = str(event.get("text") or event.get("summary") or "").strip()
        if (
            _matches_slash_command_prefix(text, command_settings.prefix)
            and event_type == "super_chat"
        ):
            try:
                price = float(event.get("price") or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            if price >= float(command_settings.super_chat_start_min_price):
                return True
        allowed_identities = {
            str(value or "").strip().casefold()
            for value in command_settings.authorized_identities
            if str(value or "").strip()
        }
        if not allowed_identities:
            return False
        source_user_id = str(event.get("user_id") or "").strip()
        source_username = str(event.get("username") or source_user_id).strip()
        resolved_user_id, resolved_username = resolve_live_identity(
            user_id=source_user_id,
            username=source_username,
        )
        candidate_identities = {
            value.casefold()
            for value in {
                source_user_id,
                source_username,
                resolved_user_id,
                resolved_username,
            }
            if value
        }
        return bool(candidate_identities & allowed_identities)

    def _video_watch_requested_by(self, event: Mapping[str, Any]) -> str:
        source_user_id = str(event.get("user_id") or "").strip()
        source_username = str(event.get("username") or source_user_id).strip()
        resolved_user_id, resolved_username = resolve_live_identity(
            user_id=source_user_id,
            username=source_username,
        )
        return (
            resolved_username
            or resolved_user_id
            or source_username
            or source_user_id
            or str(event.get("type") or "").strip()
            or "anonymous"
        )

    def _is_video_watch_commentary_lane_busy(self) -> bool:
        if self._local_delivery_lock.locked():
            return True
        router = self._router
        if router is None:
            return False
        return bool(getattr(router, "has_live_reply_busy", lambda: False)())

    async def _deliver_video_watch_system_message(
        self,
        text: str,
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return False
        settings = self._load_settings()
        render_metadata = {
            "message_id": f"video-watch-{reason}-{uuid4().hex}",
            "platform": PLATFORM_NAME,
            "message_info": {
                "additional_config": {
                    "live_event_type": reason,
                    "video_watch_generated": True,
                    **dict(metadata or {}),
                }
            },
        }
        render_result = await self._deliver_text_reply_serialized(
            normalized_text,
            settings=settings,
            source_platform=PLATFORM_NAME,
            metadata=render_metadata,
            kwargs={},
            force_interrupt=False,
        )
        if bool(render_result.get("suppressed_reply")):
            return False
        router = self._router
        if router is not None:
            router.record_bot_output_history(str(render_result.get("subtitle_text") or normalized_text).strip())
        return True

    async def _ingest_video_watch_memories(
        self,
        source: VideoWatchSourceSpec,
        analysis: VideoAnalysisResult,
    ) -> None:
        entries = list(analysis.memory_entries)
        if not entries:
            entries.extend(self._build_video_watch_memory_fallback_entries(source=source, analysis=analysis))
        if not entries:
            return
        base_metadata = {
            "kind": "video_watch_summary",
            "source_type": "bilibili_video_watch",
            "video_url": source.video_url,
            "video_title": source.title,
            "video_id": source.canonical_id,
            "page_url": source.page_url,
            "up_name": source.up_name,
            "model_label": analysis.model_label,
            "source_metadata": dict(source.metadata),
        }
        for index, entry in enumerate(entries, start=1):
            entry_title = str(entry.title or "").strip()
            entry_text = str(entry.text or "").strip()
            if not entry_text:
                continue
            payload_text = f"{entry_title}\n{entry_text}".strip() if entry_title else entry_text
            metadata = dict(base_metadata)
            metadata["entry_index"] = index
            metadata["entry_title"] = entry_title
            metadata["entry_tags"] = list(entry.tags)
            metadata["time_start"] = entry.start_sec
            metadata["time_end"] = entry.end_sec
            external_seed = f"{source.canonical_id}|{entry_title}|{entry_text}|{entry.start_sec}|{entry.end_sec}"
            external_id = f"video-watch:{source.canonical_id}:{index}:{hashlib.sha1(external_seed.encode('utf-8', errors='ignore')).hexdigest()[:12]}"
            memorix_service = self._resolve_memorix_host_service()
            if memorix_service is None:
                logger = self._logger()
                if logger is not None and hasattr(logger, "warning"):
                    logger.warning(
                        "Video watch memory ingest skipped: A_memorix host service unavailable in this process (video=%s)",
                        source.video_url,
                    )
                continue
            try:
                await memorix_service.invoke(
                    "ingest_summary",
                    args={
                        "external_id": external_id,
                        "chat_id": source.memory_chat_id,
                        "text": payload_text,
                        "participants": [source.up_name] if source.up_name else [],
                        "time_start": entry.start_sec,
                        "time_end": entry.end_sec,
                        "tags": ["video_watch", source.source_type, *list(entry.tags)],
                        "metadata": metadata,
                        "respect_filter": False,
                    },
                )
            except Exception as exc:
                logger = self._logger()
                if logger is not None and hasattr(logger, "warning"):
                    logger.warning(
                        "Video watch memory ingest failed: video=%s index=%s error=%s",
                        source.video_url,
                        index,
                        exc,
                    )

    def _build_video_watch_memory_fallback_entries(
        self,
        *,
        source: VideoWatchSourceSpec,
        analysis: VideoAnalysisResult,
    ) -> list[VideoMemoryEntry]:
        entries: list[VideoMemoryEntry] = []
        summary = str(analysis.summary or "").strip()
        if summary:
            entries.append(VideoMemoryEntry(title=f"{source.title} 总结", text=summary, tags=("summary",)))
        if analysis.conversation_hooks:
            entries.append(
                VideoMemoryEntry(
                    title=f"{source.title} 话题钩子",
                    text="；".join(analysis.conversation_hooks[:8]),
                    tags=("conversation_hook",),
                )
            )
        return entries

    async def _handle_soundboard_keyword_event(self, event: Mapping[str, Any]) -> bool:
        event_type = str(event.get("type") or "").strip().lower()
        text = _extract_soundboard_trigger_text(event)
        if not text:
            return False
        if event_type in {"bot_output", "bot_reply", "outbound_reply"}:
            return await self._trigger_soundboard_for_bot_output(source_text=text)
        if event_type not in {"danmaku", "super_chat", "hub_local_input"}:
            return False
        settings = self._load_settings()
        if not settings.soundboard.enabled or not settings.soundboard.keyword_triggers_enabled:
            return False
        event_payload = event if isinstance(event, dict) else None
        resolved = _find_explicit_soundboard_cue_mention(
            settings.soundboard,
            text,
            plugin_dir=Path(__file__).resolve().parent,
        )
        if resolved is not None:
            if event_payload is not None:
                event_payload["_soundboard_request_detected"] = True
                event_payload["_soundboard_request_mode"] = "explicit"
                event_payload["_soundboard_requested_cue"] = resolved.cue_id
                event_payload["_soundboard_request_text"] = text
            plan = self._queue_pending_soundboard_trigger(
                resolved.cue_id,
                repeat_count=1,
                reason="user_requested_soundboard",
                text=text,
                triggered_by=f"{event_type}_mention",
                settings=settings,
            )
            logger = self._logger()
            if logger is not None and hasattr(logger, "info"):
                logger.info(
                    "Soundboard explicit inbound mention queued: cue=%s event_type=%s text=%r trigger_id=%s",
                    resolved.cue_id,
                    event_type,
                    text[:80],
                    plan.trigger_id,
                )
            return True
        if not _looks_like_soundboard_request_text(text):
            return False
        if event_payload is not None:
            event_payload["_soundboard_request_detected"] = True
            event_payload["_soundboard_request_mode"] = "generic"
            event_payload["_soundboard_request_text"] = text
        return True

    async def _trigger_soundboard_for_bot_output(
        self,
        *,
        subtitle_text: str = "",
        speech_text: str = "",
        source_text: str = "",
    ) -> bool:
        settings = self._load_settings()
        if not settings.soundboard.enabled or not settings.soundboard.keyword_triggers_enabled:
            return False
        service = self._soundboard
        if service is None:
            return False
        plugin_dir = Path(__file__).resolve().parent
        for candidate_text in _iter_soundboard_trigger_texts(subtitle_text, speech_text, source_text):
            directive_text = _extract_soundboard_bot_output_directive(
                candidate_text,
                settings=settings,
                plugin_dir=plugin_dir,
            )
            if directive_text is None:
                continue
            resolved = _find_explicit_soundboard_cue_mention(
                settings.soundboard,
                directive_text,
                plugin_dir=plugin_dir,
            )
            if resolved is not None:
                result = await service.trigger(
                    resolved.cue_id,
                    reason="bot_output_mention",
                    source_text=directive_text,
                    triggered_by="bot_output",
                )
                if result.get("success"):
                    logger = self._logger()
                    if logger is not None and hasattr(logger, "info"):
                        logger.info(
                            "Soundboard bot-output cue mention triggered: cue=%s text=%r",
                            resolved.cue_id,
                            directive_text[:80],
                        )
                    return True
                if result.get("skipped"):
                    return True
        return False

    def _build_soundboard_segment_schedule(
        self,
        *,
        plans: Sequence[PendingSoundboardTrigger],
        segments: Sequence[str],
        settings: LiveAdapterSettings,
    ) -> dict[int, list[PendingSoundboardTrigger]]:
        normalized_segments = [str(segment or "").strip() for segment in segments if str(segment or "").strip()]
        if not plans or not normalized_segments:
            return {}
        plugin_dir = Path(__file__).resolve().parent
        segment_count = len(normalized_segments)
        fallback_segment_index = max(1, (segment_count + 1) // 2)
        schedule: dict[int, list[PendingSoundboardTrigger]] = {}
        for plan in plans:
            if 1 <= int(plan.target_segment_index or 0) <= segment_count:
                schedule.setdefault(int(plan.target_segment_index), []).append(plan)
                continue
            target_segment_index = fallback_segment_index
            resolved_cue = resolve_soundboard_cue(settings.soundboard, plan.cue, plugin_dir=plugin_dir)
            if resolved_cue is not None:
                for index, segment_text in enumerate(normalized_segments, start=1):
                    if soundboard_text_matches_cue(segment_text, resolved_cue.cue) or _soundboard_segment_mentions_cue(
                        segment_text,
                        cue_id=resolved_cue.cue_id,
                        cue_label=resolved_cue.cue.label,
                    ):
                        target_segment_index = index
                        break
            schedule.setdefault(target_segment_index, []).append(plan)
        return schedule

    async def _trigger_scheduled_soundboard_triggers(
        self,
        plans: Sequence[PendingSoundboardTrigger] | None,
        *,
        settings: LiveAdapterSettings,
    ) -> None:
        if not plans:
            return
        logger = self._logger()
        for plan in plans:
            result = await self._trigger_soundboard_tool_plan(plan, settings=settings)
            if result.get("success"):
                continue
            if logger is not None and hasattr(logger, "warning"):
                logger.warning(
                    "Deferred soundboard trigger failed: cue=%s error=%s",
                    plan.cue,
                    result.get("error") or result.get("reason") or "unknown",
                )

    def _is_visual_context_command_authorized(
        self,
        event: Mapping[str, Any],
        *,
        settings: LiveAdapterSettings,
    ) -> bool:
        command_settings = settings.vision.command
        event_type = str(event.get("type") or "").strip().lower()
        if event_type == "hub_local_input" and command_settings.allow_hub_local_input:
            return True
        allowed_identities = {
            str(value or "").strip().casefold()
            for value in command_settings.authorized_identities
            if str(value or "").strip()
        }
        if not allowed_identities:
            return False
        source_user_id = str(event.get("user_id") or "").strip()
        source_username = str(event.get("username") or source_user_id).strip()
        resolved_user_id, resolved_username = resolve_live_identity(
            user_id=source_user_id,
            username=source_username,
        )
        candidate_identities = {
            value.casefold()
            for value in {
                source_user_id,
                source_username,
                resolved_user_id,
                resolved_username,
            }
            if value
        }
        return bool(candidate_identities & allowed_identities)

    def _store_pending_visual_context(
        self,
        *,
        settings: LiveAdapterSettings,
        summary: str,
        focus_question: str,
        event: Mapping[str, Any],
    ) -> None:
        session_id = _build_live_chat_id(settings)
        requested_by = (
            str(event.get("username") or "").strip()
            or str(event.get("user_id") or "").strip()
            or str(event.get("type") or "").strip()
            or "anonymous"
        )
        self._pending_visual_contexts[session_id] = PendingVisualContext(
            summary=str(summary or "").strip(),
            focus_question=str(focus_question or "").strip(),
            requested_by=requested_by,
            created_at=time.time(),
        )

    def _is_visual_context_stop_command(
        self,
        command_text: str,
        *,
        settings: LiveAdapterSettings,
    ) -> bool:
        normalized_text = str(command_text or "").strip().casefold()
        if not normalized_text:
            return False
        stop_aliases = {
            str(value or "").strip().casefold()
            for value in settings.vision.command.stop_aliases
            if str(value or "").strip()
        }
        return normalized_text in stop_aliases

    def _resolve_visual_context_vision_config(self, settings: LiveAdapterSettings) -> VisionConfig:
        command_settings = settings.vision.command
        updates: dict[str, Any] = {}
        if command_settings.model_name:
            updates["model_name"] = command_settings.model_name
        if command_settings.model_identifier:
            updates["model_identifier"] = command_settings.model_identifier
            if not command_settings.model_name:
                updates["model_name"] = ""
        if not updates:
            return settings.vision
        return settings.vision.model_copy(update=updates)

    def _is_visual_context_polling_active(self, session_id: str) -> bool:
        normalized_session_id = str(session_id or "").strip()
        task = self._visual_context_poll_tasks.get(normalized_session_id)
        return task is not None and not task.done()

    async def _start_visual_context_polling(
        self,
        *,
        settings: LiveAdapterSettings,
        focus_question: str,
        event: Mapping[str, Any],
    ) -> bool:
        session_id = _build_live_chat_id(settings)
        requested_by = (
            str(event.get("username") or "").strip()
            or str(event.get("user_id") or "").strip()
            or str(event.get("type") or "").strip()
            or "anonymous"
        )
        await self._stop_visual_context_polling(session_id=session_id, clear_context=True)

        inspector = VisionDesktopInspector(self._resolve_visual_context_vision_config(settings), logger=self._logger())
        await self._capture_visual_context_once(
            settings=settings,
            inspector=inspector,
            focus_question=focus_question,
            requested_by=requested_by,
        )

        task = asyncio.create_task(
            self._run_visual_context_poll_loop(
                settings=settings,
                inspector=inspector,
                focus_question=focus_question,
                requested_by=requested_by,
            ),
            name=f"maibot_bilibili_live_adapter.visual_poll.{session_id}",
        )
        self._visual_context_poll_tasks[session_id] = task

        def _on_done(done_task: asyncio.Task[None]) -> None:
            self._handle_visual_context_poll_task_done(session_id, done_task)

        task.add_done_callback(_on_done)
        return True

    async def _stop_visual_context_polling(
        self,
        *,
        settings: LiveAdapterSettings | None = None,
        session_id: str = "",
        clear_context: bool = True,
    ) -> bool:
        resolved_session_id = str(session_id or "").strip()
        if not resolved_session_id and settings is not None:
            resolved_session_id = _build_live_chat_id(settings)
        if not resolved_session_id:
            return False
        task = self._visual_context_poll_tasks.pop(resolved_session_id, None)
        if clear_context:
            self._clear_pending_visual_context(resolved_session_id)
        if task is None:
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return True

    async def _stop_all_visual_context_polling(self) -> None:
        tasks = list(self._visual_context_poll_tasks.values())
        self._visual_context_poll_tasks.clear()
        self._pending_visual_contexts.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run_visual_context_poll_loop(
        self,
        *,
        settings: LiveAdapterSettings,
        inspector: VisionDesktopInspector,
        focus_question: str,
        requested_by: str,
    ) -> None:
        interval_sec = settings.vision.command.poll_interval_sec
        while True:
            await asyncio.sleep(interval_sec)
            await self._capture_visual_context_once(
                settings=settings,
                inspector=inspector,
                focus_question=focus_question,
                requested_by=requested_by,
            )

    async def _capture_visual_context_once(
        self,
        *,
        settings: LiveAdapterSettings,
        inspector: VisionDesktopInspector,
        focus_question: str,
        requested_by: str,
    ) -> bool:
        try:
            result = await inspector.inspect(focus_question)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger = self._logger()
            if logger is not None and hasattr(logger, "warning"):
                logger.warning(f"Visual context polling capture failed: {exc}")
            return False

        if not bool(result.get("success")):
            logger = self._logger()
            if logger is not None and hasattr(logger, "warning"):
                logger.warning(f"Visual context polling capture failed: {result.get('error') or result}")
            return False

        summary = str(result.get("summary") or "").strip()
        if not summary:
            return False
        self._store_pending_visual_context(
            settings=settings,
            summary=summary,
            focus_question=focus_question,
            event={"username": requested_by},
        )
        return True

    def _handle_visual_context_poll_task_done(self, session_id: str, task: asyncio.Task[None]) -> None:
        current_task = self._visual_context_poll_tasks.get(session_id)
        if current_task is task:
            self._visual_context_poll_tasks.pop(session_id, None)
            self._clear_pending_visual_context(session_id)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger = self._logger()
        if logger is not None and hasattr(logger, "warning"):
            logger.warning(f"Visual context polling task failed: {exc}")

    def _pending_visual_context_prompt(self, settings: LiveAdapterSettings) -> str:
        session_id = _build_live_chat_id(settings)
        context = self._pending_visual_contexts.get(session_id)
        if context is None:
            return ""
        if self._is_visual_context_polling_active(session_id):
            source_line = "附加视觉上下文：以下内容来自操作者已开启的桌面视觉轮询，代表最近一次截图分析。"
            trigger_line = "只有当它对当前这次直播回复确实相关时，才自然利用这些信息；如果不相关就忽略。"
        else:
            source_line = "附加视觉上下文：以下内容来自操作者刚刚手动触发的一次游戏画面截图分析。"
            trigger_line = "只有当它对当前这次直播回复确实相关时，才自然利用这些信息；如果不相关就忽略。"
        prompt_lines = [
            source_line,
            "这不是观众消息，也不是你必须单独回复的对象。",
            trigger_line,
            f"截图观察：{context.summary}",
        ]
        if context.focus_question:
            prompt_lines.append(f"当前轮询关注点：{context.focus_question}")
        if context.requested_by:
            prompt_lines.append(f"触发来源：{context.requested_by}")
        return "\n".join(prompt_lines).strip()

    def _video_watch_prompt(self) -> str:
        controller = self._video_watch_controller
        if controller is None:
            return ""
        return str(controller.build_prompt_context() or "").strip()

    def _clear_pending_visual_context(self, session_id: str) -> None:
        self._pending_visual_contexts.pop(str(session_id or "").strip(), None)

    async def handle_live2d_wink_request(self, event: Mapping[str, Any]) -> bool:
        runtime = self._embodied_live2d_runtime
        side = str(event.get("wink_side") or "").strip().lower()
        accepted = False
        if runtime is not None:
            accepted = await runtime.request_wink(side=side)
        elif self._soullink_shell_runtime is not None:
            accepted = await self._play_soullink_shell_wink(side=side)
        logger = self._logger()
        if accepted and logger is not None and hasattr(logger, "info"):
            logger.info(
                "Live2D wink request accepted: user=%s side=%s text=%r",
                event.get("user_id") or event.get("username") or "anonymous",
                side or "auto",
                str(event.get("text") or event.get("summary") or "").strip(),
            )
        return accepted

    async def _play_soullink_shell_wink(self, *, side: str) -> bool:
        runtime = self._soullink_shell_runtime
        if runtime is None:
            return False

        normalized_side = str(side or "").strip().lower()
        if normalized_side not in {"left", "right"}:
            normalized_side = random.choice(("left", "right"))

        close_parameters = {
            "ParamEyeLOpen": 0.0 if normalized_side == "left" else 1.0,
            "ParamEyeROpen": 0.0 if normalized_side == "right" else 1.0,
        }
        reopen_parameters = {
            "ParamEyeLOpen": 1.0,
            "ParamEyeROpen": 1.0,
        }

        await runtime.broadcast(
            message_to_payload(
                ShellExpressionMessage(
                    parameters=close_parameters,
                    duration_ms=90,
                )
            )
        )
        await asyncio.sleep(0.1)
        await runtime.broadcast(
            message_to_payload(
                ShellExpressionMessage(
                    parameters=reopen_parameters,
                    duration_ms=140,
                )
            )
        )
        return True

    async def handle_live2d_special_move_request(self, event: Mapping[str, Any]) -> bool:
        runtime = self._embodied_live2d_runtime
        if runtime is None:
            return False
        payload = event.get("live2d_action")
        if not isinstance(payload, Mapping):
            return False
        accepted = await runtime.request_special_move(
            action=str(payload.get("action") or "").strip(),
            move=str(payload.get("move") or "").strip(),
            duration_sec=float(payload.get("duration_sec") or 0.0),
        )
        logger = self._logger()
        if accepted and logger is not None and hasattr(logger, "info"):
            logger.info(
                "Live2D special move accepted: user=%s action=%s move=%s duration=%.2f text=%r",
                event.get("user_id") or event.get("username") or "anonymous",
                str(payload.get("action") or "").strip(),
                str(payload.get("move") or "").strip(),
                float(payload.get("duration_sec") or 0.0),
                str(event.get("text") or event.get("summary") or "").strip(),
            )
        return accepted

    async def _handle_live2d_debug_mouse_command(
        self,
        args: list[str],
        *,
        settings: LiveAdapterSettings,
    ) -> None:
        current_state = self._load_live2d_control_state()
        action = args[0].lower() if args else "toggle"
        if action not in {"on", "off", "toggle"}:
            await self._publish_live2d_debug_feedback(
                "Live2D 鼠标跟随命令格式: /l2d mouse on|off|toggle",
                settings=settings,
            )
            return
        if action == "toggle":
            enabled = not current_state.mouse_follow_enabled
        else:
            enabled = action == "on"
        updated_state = self._handle_live2d_control_state_patch({"mouse_follow_enabled": enabled})
        status = "on" if updated_state.mouse_follow_enabled else "off"
        await self._publish_live2d_debug_feedback(
            f"Live2D mouse follow -> {status}",
            settings=settings,
        )

    async def _handle_live2d_debug_reset_command(self, *, settings: LiveAdapterSettings) -> None:
        runtime = self._embodied_live2d_runtime
        if runtime is None:
            await self._publish_live2d_debug_feedback(
                "Live2D embodied runtime 未运行，无法执行 reset。",
                settings=settings,
            )
            return
        result = await runtime.debug_reset_pose()
        if bool(result.get("success")):
            await self._publish_live2d_debug_feedback("Live2D reset 完成。", settings=settings)
            return
        reason = str(result.get("reason") or "unknown")
        await self._publish_live2d_debug_feedback(
            f"Live2D reset 失败: {reason}",
            settings=settings,
        )

    async def _handle_live2d_debug_emotion_command(
        self,
        args: list[str],
        *,
        settings: LiveAdapterSettings,
    ) -> None:
        if not args:
            await self._publish_live2d_debug_feedback(
                "Live2D 情绪命令格式: /l2d emotion happy|surprised|shy|confused|emphasis|sad|angry|neutral [gain]",
                settings=settings,
            )
            return
        emotion_name = str(args[0] or "").strip().lower()
        emotion_intent = _LIVE2D_DEBUG_EMOTION_ALIASES.get(emotion_name)
        if emotion_intent is None:
            await self._publish_live2d_debug_feedback(
                "未知 Live2D 情绪调试值，可用: happy, surprised, shy, confused, emphasis, sad, angry, neutral",
                settings=settings,
            )
            return
        runtime = self._embodied_live2d_runtime
        if runtime is None:
            await self._publish_live2d_debug_feedback(
                "Live2D embodied runtime 未运行，无法预览 emotion。",
                settings=settings,
            )
            return
        emotion_gain = _clamp_live2d_debug_gain(args[1] if len(args) > 1 else 1.0)
        result = await runtime.debug_apply_emotion(emotion_intent=emotion_intent, emotion_gain=emotion_gain)
        if bool(result.get("success")):
            await self._publish_live2d_debug_feedback(
                f"Live2D emotion preview -> {emotion_name} ({emotion_gain:.2f})",
                settings=settings,
            )
            return
        reason = str(result.get("reason") or "unknown")
        await self._publish_live2d_debug_feedback(
            f"Live2D emotion preview 失败: {reason}",
            settings=settings,
        )

    async def _publish_live2d_debug_feedback(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings | None = None,
    ) -> None:
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return
        current_settings = settings or self._load_settings()
        logger = self._logger()
        if logger is not None:
            logger.info(f"Live2D debug command: {normalized_text}")
        await self._publish_reply_to_webui(
            normalized_text,
            settings=current_settings,
            source_platform=PLATFORM_NAME,
            speech_text="",
            audio_timeline=None,
            synthesized_speech=None,
        )

    def _format_live2d_debug_status(self, settings: LiveAdapterSettings) -> str:
        runtime = self._embodied_live2d_runtime
        control_state = self._load_live2d_control_state()
        if runtime is None:
            return (
                "Live2D debug status: runtime=off "
                f"embodied={'on' if settings.live2d.embodied.enabled else 'off'} "
                f"mouse={'on' if control_state.mouse_follow_enabled else 'off'} "
                f"session={settings.live2d.embodied.source_session_id or '-'}"
            )
        status = runtime.debug_status()
        latest_targets = status.get("latest_targets") or {}
        head_x = ""
        if isinstance(latest_targets, Mapping) and "head.x" in latest_targets:
            head_x = f" head.x={float(latest_targets.get('head.x') or 0.0):+.2f}"
        return (
            "Live2D debug status: "
            f"runtime=on connected={'yes' if status.get('subscriber_connected') else 'no'} "
            f"mouse={'on' if status.get('mouse_follow_enabled') else 'off'} "
            f"snapshot={'yes' if status.get('has_snapshot') else 'no'} "
            f"session={status.get('source_session_id') or '-'} "
            f"disabled={status.get('disabled_reason') or '-'}{head_x}"
        )

    async def control_game(
        self,
        action: str,
        payload: Mapping[str, Any],
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Internal handler for game JSON bridge commands."""

        del kwargs
        settings = self._load_settings()
        normalized_action = str(action or "").strip()
        if not normalized_action:
            return {"success": False, "error": "action is required"}
        allowed_actions = set(settings.game.allowed_actions)
        if allowed_actions and normalized_action not in allowed_actions:
            return {"success": False, "error": f"game action is not allowed: {normalized_action}"}
        if not settings.game.enabled or self._game_bridge is None:
            return {"success": False, "error": "game bridge is not enabled"}
        return await self._game_bridge.send(
            "game_command",
            {"action": normalized_action, "payload": dict(payload or {}), "stream_id": str(stream_id or "")},
        )

    @Tool(
        "inspect_desktop",
        description="Capture the current monitor, compress it, and summarize it with the configured vision model.",
        parameters={
            "question": {
                "type": "string",
                "default": "",
                "description": "Optional focus question for the desktop screenshot summary.",
            },
        },
    )
    async def inspect_desktop(
        self,
        question: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Tool handler for desktop screenshot vision summaries."""

        del kwargs
        settings = self._load_settings()
        inspector = VisionDesktopInspector(settings.vision, logger=self._logger())
        return await inspector.inspect(question)

    async def request_rvc_song(
        self,
        song_keyword: str,
        stream_id: str = "",
        requester: str = "",
        artist: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Internal handler for RVC song requests."""

        del kwargs
        settings = self._load_settings()
        service = self._song_request_service
        if service is None:
            if settings.song_request.hard_disable:
                prompt = "\u70b9\u6b4c\u548cRVC\u529f\u80fd\u5f53\u524d\u88ab\u603b\u5f00\u5173\u7981\u7528\u4e86\u3002"
                return {"success": False, "queued": False, "prompt": prompt, "message": prompt}
            if not settings.song_request.enabled:
                prompt = "\u70b9\u6b4c\u529f\u80fd\u8fd8\u6ca1\u6709\u5f00\u542f\u3002"
                return {"success": False, "queued": False, "prompt": prompt, "message": prompt}
            service = self._build_song_request_service(settings)
            self._song_request_service = service
            await service.start()
        return await service.submit(
            song_keyword=song_keyword,
            stream_id=stream_id,
            requester=requester,
            artist=artist,
        )

    @API("play_sound_effect", description="API wrapper for the live soundboard.", public=True)
    async def api_play_sound_effect(self, **kwargs: Any) -> dict[str, Any]:
        """API wrapper for other plugins."""

        return await self.play_sound_effect(**kwargs)

    @API("send_sound_effect", description="API wrapper for the unified live soundboard tool (backward-compat).", public=True)
    async def api_send_sound_effect(self, **kwargs: Any) -> dict[str, Any]:
        """API wrapper — delegates to play_sound_effect for backward compatibility."""
        intent_val = kwargs.pop("intent", "")
        if not kwargs.get("cue", "").strip() and intent_val:
            kwargs["cue"] = ""
            kwargs["intent"] = intent_val
        return await self.play_sound_effect(**kwargs)

    @API("special_move", description="API wrapper for named Live2D special moves.", public=True)
    async def api_special_move(self, **kwargs: Any) -> dict[str, Any]:
        """API wrapper for other plugins."""

        return await self.special_move(**kwargs)

    @API("control_game", description="API wrapper for external game JSON bridge control.", public=True)
    async def api_control_game(self, **kwargs: Any) -> dict[str, Any]:
        """API wrapper for other plugins."""

        return await self.control_game(**kwargs)

    async def _maybe_auto_start_livehub(self, settings: LiveAdapterSettings) -> None:
        """若配置了 auto_start_livehub 且 livehub 未在线，则自动拉起 livehub 子进程。"""
        if not settings.hub_input.enabled or not settings.hub_input.auto_start_livehub:
            return
        if await self._is_livehub_online(settings):
            return
        await self._start_livehub_process(settings)

    def _livehub_address(self, settings: LiveAdapterSettings) -> tuple[str, int]:
        """从 hub_input.http_url 解析 livehub 地址，缺省 127.0.0.1:18190。

        URL 缺失 scheme / 端口非法时记录 warning 并回退默认值，避免中断运行时装配。
        """
        raw_url = str(settings.hub_input.http_url or "").strip()
        parsed = urlparse(raw_url)
        if not parsed.scheme or not parsed.hostname:
            self._logger().warning(f"hub_input.http_url 缺少 scheme/host，回退默认地址 127.0.0.1:18190: {raw_url!r}")
            return "127.0.0.1", 18190
        try:
            port = parsed.port or 18190
        except ValueError:
            self._logger().warning(f"hub_input.http_url 端口非法，回退默认端口 18190: {raw_url!r}")
            return parsed.hostname, 18190
        return parsed.hostname, port

    async def _is_livehub_online(self, settings: LiveAdapterSettings) -> bool:
        """应用层探测 livehub 是否在线（GET /api/health 校验 success 字段）。

        相比纯 TCP 探测，可避免端口被无关程序占用时的误判；auth_token 配置时携带鉴权头。
        """
        if not AIOHTTP_AVAILABLE:
            return False
        host, port = self._livehub_address(settings)
        url = f"http://{host}:{port}/api/health"
        headers: dict[str, str] = {}
        token = str(settings.hub_input.auth_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = ClientTimeout(total=2.0)
        try:
            async with ClientSession(headers=headers, timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return False
                    payload = await response.json()
        except Exception:
            return False
        return isinstance(payload, Mapping) and bool(payload.get("success"))

    async def _start_livehub_process(self, settings: LiveAdapterSettings) -> None:
        """拉起 livehub 子进程（python -m livehub）并等待 HTTP 就绪。"""
        async with self._livehub_start_lock:
            await self._start_livehub_process_locked(settings)

    async def _start_livehub_process_locked(self, settings: LiveAdapterSettings) -> None:
        host, port = self._livehub_address(settings)
        room_id = settings.hub_input.livehub_room_id or settings.bilibili.room_id
        if not room_id:
            self._logger().warning("auto_start_livehub 已开启但未配置直播间 ID，跳过自动拉起")
            return
        log_dir = _plugin_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_handle = None
        try:
            log_handle = (log_dir / "livehub.log").open("a", encoding="utf-8")
        except OSError as exc:
            self._logger().warning(f"livehub 日志文件打开失败: {exc}")
        plugin_dir = Path(__file__).resolve().parent
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "livehub",
                "--host",
                str(host),
                "--port",
                str(port),
                "--room-id",
                str(room_id),
                cwd=str(plugin_dir),
                stdout=log_handle,
                stderr=asyncio.subprocess.STDOUT,
            )
        except asyncio.CancelledError:
            if log_handle is not None:
                with contextlib.suppress(OSError):
                    log_handle.close()
            raise
        except Exception as exc:
            self._logger().warning(f"livehub 自动拉起失败: {exc}")
            if log_handle is not None:
                with contextlib.suppress(OSError):
                    log_handle.close()
            return
        self._livehub_process = process
        self._livehub_log_handle = log_handle
        self._logger().info(f"livehub 自动拉起 pid={process.pid} {host}:{port} room_id={room_id}")
        for _ in range(20):
            await asyncio.sleep(0.5)
            if await self._is_livehub_online(settings):
                self._logger().info("livehub 已就绪")
                return
        if process.returncode is not None:
            self._logger().warning(
                f"livehub 启动后异常退出 code={process.returncode}，请检查 data/logs/livehub.log"
            )
            if log_handle is not None:
                with contextlib.suppress(OSError):
                    log_handle.close()
                self._livehub_log_handle = None
            self._livehub_process = None
        else:
            self._logger().warning("livehub 就绪探测超时，插件将继续按重连机制连接")

    async def _stop_livehub_process(self) -> None:
        """关闭由插件拉起的 livehub 子进程（仅管理自己拉起的进程，不动手动启动的实例）。"""
        async with self._livehub_start_lock:
            await self._stop_livehub_process_locked()

    async def _stop_livehub_process_locked(self) -> None:
        process = self._livehub_process
        log_handle = self._livehub_log_handle
        self._livehub_process = None
        self._livehub_log_handle = None
        if process is None or process.returncode is not None:
            if log_handle is not None:
                with contextlib.suppress(OSError):
                    log_handle.close()
            return
        self._logger().info(f"关闭插件拉起的 livehub pid={process.pid}")
        # 进程可能在我们检查 returncode 后自行退出，terminate/kill/wait 均需容忍进程已消失
        with contextlib.suppress(ProcessLookupError, OSError):
            process.terminate()
        with contextlib.suppress(ProcessLookupError, OSError, asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5.0)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            with contextlib.suppress(ProcessLookupError, OSError):
                await process.wait()
        if log_handle is not None:
            with contextlib.suppress(OSError):
                log_handle.close()

    async def _restart_runtime(self) -> None:
        settings = self._load_settings()
        await self._stop_runtime()
        await self._maybe_auto_start_livehub(settings)
        if self._runtime_state is None:
            self._runtime_state = self._create_runtime_state()
        control_state = self._load_live2d_control_state()

        live2d_scheme = resolve_live2d_scheme(settings.live2d)
        if settings.live2d.enabled:
            self._live2d_controller = await self._build_live2d_controller(settings)
            if live2d_scheme == "embodied" and settings.live2d.embodied.enabled:
                if settings.live2d.embodied.source_session_id.strip():
                    # 新版宿主已移除 global_config.maisaka，仅使用插件自身配置（缺失时回退默认 token）
                    embodied_source_token = (
                        settings.live2d.embodied.source_token
                        or DEFAULT_AVATAR_STATE_WS_TOKEN
                    )
                    self._embodied_live2d_runtime = EmbodiedLive2DRuntime(
                        controller=self._live2d_controller,
                        source_url=settings.live2d.embodied.source_url,
                        source_token=embodied_source_token,
                        source_session_id=settings.live2d.embodied.source_session_id,
                        fallback_to_legacy=settings.live2d.embodied.fallback_to_legacy,
                        mouse_follow_enabled=control_state.mouse_follow_enabled,
                        mouse_follow_poll_interval_ms=settings.live2d.embodied.mouse_follow_poll_interval_ms,
                        mouse_follow_smoothing_ms=settings.live2d.embodied.mouse_follow_smoothing_ms,
                        mouse_follow_return_after_sec=settings.live2d.embodied.mouse_follow_return_after_sec,
                        mouse_follow_cooldown_sec=settings.live2d.embodied.mouse_follow_cooldown_sec,
                        mouse_follow_eye_gain=settings.live2d.embodied.mouse_follow_eye_gain,
                        mouse_follow_head_gain=settings.live2d.embodied.mouse_follow_head_gain,
                        mouse_follow_body_gain=settings.live2d.embodied.mouse_follow_body_gain,
                        blink_enabled=settings.live2d.embodied.blink.enabled,
                        blink_interval_min_sec=settings.live2d.embodied.blink.interval_min_sec,
                        blink_interval_max_sec=settings.live2d.embodied.blink.interval_max_sec,
                        blink_double_blink_chance=settings.live2d.embodied.blink.double_blink_chance,
                        blink_close_ms=settings.live2d.embodied.blink.close_ms,
                        blink_hold_ms=settings.live2d.embodied.blink.hold_ms,
                        blink_open_ms=settings.live2d.embodied.blink.open_ms,
                        blink_double_blink_gap_ms=settings.live2d.embodied.blink.double_blink_gap_ms,
                        wink_enabled=settings.live2d.embodied.wink.enabled,
                        wink_close_ms=settings.live2d.embodied.wink.close_ms,
                        wink_hold_ms=settings.live2d.embodied.wink.hold_ms,
                        wink_open_ms=settings.live2d.embodied.wink.open_ms,
                        wink_request_cooldown_sec=settings.live2d.embodied.wink.request_cooldown_sec,
                        wink_non_target_eye_drop=settings.live2d.embodied.wink.non_target_eye_drop,
                        logger=self._logger(),
                    )
                    await self._embodied_live2d_runtime.start()
                else:
                    self._logger().warning(
                        "Live2D embodied mode is enabled but live2d.embodied.source_session_id is empty; "
                        "keeping legacy Live2D behavior."
                    )
        if settings.game.enabled:
            self._game_bridge = JsonBridgeClient(
                name="game",
                http_url=settings.game.http_url,
                websocket_url=settings.game.websocket_url,
                auth_token=settings.game.auth_token,
                connect_timeout_sec=settings.game.connect_timeout_sec,
                logger=self._logger(),
            )
            await self._game_bridge.start()
        if settings.hub_output.enabled:
            self._hub_output_bridge = JsonBridgeClient(
                name="hub_output",
                http_url=settings.hub_output.http_url,
                websocket_url=settings.hub_output.websocket_url,
                auth_token=settings.hub_output.auth_token,
                connect_timeout_sec=settings.hub_output.connect_timeout_sec,
                logger=self._logger(),
            )
            await self._hub_output_bridge.start()
        if settings.sts2.enabled:
            self._sts2_log_session = STS2LogSession(
                settings.sts2.logging,
                base_dir=_project_root(),
                parent_logger=self._logger(),
            ).start()
            sts2_logger: Any = self._sts2_log_session if settings.sts2.logging.enabled else self._logger()
            if self._sts2_log_session.log_path is not None:
                self._logger().info(f"STS2-Agent logs are written to {self._sts2_log_session.log_path}")
            self._sts2_controller = STS2Controller(
                gateway=self.ctx.gateway,
                settings=settings,
                mcp_client=STS2MCPClient(
                    settings.sts2.mcp,
                    logger=sts2_logger,
                    stderr=self._sts2_log_session.stderr if self._sts2_log_session is not None else None,
                ),
                decision_client=STS2DecisionClient(settings.sts2.llm, logger=sts2_logger),
                runtime_state=self._runtime_state,
                logger=sts2_logger,
            )
        if settings.language.uses_translated_chinese_subtitle():
            self._subtitle_translator = SubtitleTranslationClient(settings.language.translation, logger=self._logger())
        if settings.tts.enabled and settings.tts.is_usable():
            self._tts_provider = await self._build_tts_provider(settings)
            if settings.tts.audio_playback_enabled:
                self._audio_output_player = LocalAudioOutputPlayer(
                    output_device=settings.tts.audio_output_device,
                    volume=settings.tts.audio_output_volume,
                    logger=self._logger(),
                )
        if settings.webui.enabled:
            await self._start_subtitle_webui(settings)
        if settings.soundboard.enabled and not self._soundboard_admin_disabled:
            await self._start_soundboard(settings)
        if settings.song_request.is_available():
            self._song_request_service = self._build_song_request_service(settings)
            await self._song_request_service.start()
        self._schedule_runtime_service_warmup()

        self._planner = LiveInteractionPlanner(
            settings.interaction,
            llm=_ctx_attr(self, "llm"),
            logger=self._logger(),
            message_capability=_ctx_attr(self, "message"),
            chat_id=_build_live_chat_id(settings),
        )
        self._video_watch_controller = VideoWatchController(
            settings.video_watch,
            route_reply=self._deliver_video_watch_system_message,
            memory_ingest=self._ingest_video_watch_memories,
            commentary_lane_available=self._is_video_watch_commentary_lane_busy,
            logger=self._logger(),
        )
        if settings.tachie.enabled:
            if self._tachie_controller is None:
                self._tachie_controller = TachieController(
                    settings.tachie,
                    plugin_dir=str(Path(__file__).resolve().parent),
                    logger=self._logger(),
                )
            await self._tachie_controller.start()
        self._router = LiveEventRouter(
            gateway=self.ctx.gateway,
            settings=settings,
            planner=self._planner,
            live2d_controller=self._live2d_controller,
            game_bridge=self._game_bridge,
            sts2_controller=self._sts2_controller,
            live2d_debug_command_handler=self.handle_live2d_debug_command,
            visual_context_command_handler=self.handle_visual_context_command,
            video_watch_event_handler=self.handle_video_watch_event,
            video_watch_idle_offer_handler=self.handle_video_watch_idle_trigger,
            video_watch_idle_blocker=self._should_block_idle_topic_for_video_watch,
            soundboard_keyword_handler=self._handle_soundboard_keyword_event,
            soundboard_command_handler=self.handle_soundboard_command,
            live2d_wink_handler=self.handle_live2d_wink_request,
            live2d_special_move_handler=self.handle_live2d_special_move_request,
            paid_event_handler=self._handle_paid_event_acknowledgement,
            logger=self._logger(),
        )

        if not settings.should_connect():
            self._logger().info("Bilibili live adapter is disabled; bridges and transport stay idle.")
            return
        if settings.local_voice.enabled:
            from .local_voice_controller import LocalVoiceController

            self._local_voice_controller = LocalVoiceController(
                settings=settings,
                on_transcript_route=self._route_local_voice_text,
                on_settings_changed=self._handle_local_voice_settings_changed,
                logger=self._logger(),
            )
            await self._local_voice_controller.start()
        if not settings.validate_runtime_config(self._logger()):
            return
        if settings.uses_hub_input_source():
            self._hub_input_client = HubInputClient(
                http_url=settings.hub_input.http_url,
                websocket_url=settings.hub_input.websocket_url,
                presence_url=settings.hub_input.presence_url,
                speak_request_url=settings.hub_input.speak_request_url,
                speak_complete_url=settings.hub_input.speak_complete_url,
                auth_token=settings.hub_input.auth_token,
                connect_timeout_sec=settings.hub_input.connect_timeout_sec,
                history_limit=settings.hub_input.history_limit,
                presence_heartbeat_sec=settings.hub_input.presence_heartbeat_sec,
                client_id=str(settings.hub_output.client_id or settings.identity.bot_user_id or "maibot-live").strip(),
                bot_name=str(settings.hub_output.bot_name or settings.identity.bot_nickname or "maibot-live").strip(),
                on_event=lambda record: self._handle_hub_input_record(record, settings=settings),
                on_state=self._handle_hub_input_state,
                on_connection_opened=lambda: self._handle_live_connection_opened(settings),
                on_connection_closed=lambda: self._handle_live_connection_closed(settings),
                logger=self._logger(),
            )
            await self._hub_input_client.start()
            return

        self._transport = BilibiliDanmakuTransport(
            on_event=self._router.handle_event,
            on_connection_opened=lambda: self._handle_live_connection_opened(settings),
            on_connection_closed=lambda: self._handle_live_connection_closed(settings),
            logger=self._logger(),
        )
        self._transport.configure(settings.bilibili)
        await self._transport.start()

    def _create_runtime_state(self) -> LiveAdapterRuntimeState:
        return LiveAdapterRuntimeState(self.ctx.gateway, self._logger())

    def _gateway_disconnect_grace_sec(self, settings: LiveAdapterSettings) -> float:
        return max(0.1, float(settings.bilibili.reconnect_delay_sec) + 2.0)

    async def _cancel_pending_gateway_disconnect(self) -> None:
        task = self._pending_gateway_disconnect_task
        if task is None:
            return
        self._pending_gateway_disconnect_task = None
        if task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _cancel_pending_hub_output_tasks(self) -> None:
        tasks = tuple(self._hub_output_tasks)
        self._hub_output_tasks.clear()
        for task in tasks:
            if task.done():
                continue
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _cancel_background_audio_playback_tasks(self) -> None:
        tasks = tuple(self._background_audio_playback_tasks)
        self._background_audio_playback_tasks.clear()
        for task in tasks:
            if task.done():
                continue
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _schedule_runtime_service_warmup(self) -> None:
        if self._runtime_warmup_task is not None and not self._runtime_warmup_task.done():
            return
        self._runtime_warmup_task = asyncio.create_task(
            self._warm_runtime_services(),
            name="maibot_bilibili_live_adapter.runtime_warmup",
        )

    async def _cancel_runtime_warmup_task(self) -> None:
        task = self._runtime_warmup_task
        self._runtime_warmup_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _warm_runtime_services(self) -> None:
        await asyncio.sleep(0)
        translator = self._subtitle_translator
        if translator is not None:
            start_method = getattr(translator, "start", None)
            if callable(start_method):
                with contextlib.suppress(Exception):
                    await start_method()
        provider = self._tts_provider
        if provider is not None:
            start_method = getattr(provider, "start", None)
            if callable(start_method):
                with contextlib.suppress(Exception):
                    await start_method()

    async def _schedule_gateway_disconnect(self, settings: LiveAdapterSettings) -> None:
        await self._cancel_pending_gateway_disconnect()

        async def delayed_disconnect() -> None:
            try:
                await asyncio.sleep(self._gateway_disconnect_grace_sec(settings))
                if self._runtime_state is not None:
                    await self._runtime_state.report_disconnected()
                await self._restore_napcat_after_live_if_needed(settings)
            finally:
                if self._pending_gateway_disconnect_task is asyncio.current_task():
                    self._pending_gateway_disconnect_task = None

        self._pending_gateway_disconnect_task = asyncio.create_task(
            delayed_disconnect(),
            name="bilibili_live.gateway_disconnect_grace",
        )

    async def _handle_live_connection_opened(self, settings: LiveAdapterSettings) -> None:
        await self._cancel_pending_gateway_disconnect()
        await self._disable_napcat_for_live_if_needed(settings)
        if self._runtime_state is None:
            self._runtime_state = self._create_runtime_state()
        ready = await self._runtime_state.report_ready(settings)
        if ready and self._router is not None:
            self._router.start_idle_topic_watch()

    async def _handle_live_connection_closed(self, settings: LiveAdapterSettings) -> None:
        if self._router is not None:
            self._router.stop_idle_topic_watch()
        await self._schedule_gateway_disconnect(settings)

    async def _disable_napcat_for_live_if_needed(self, settings: LiveAdapterSettings) -> None:
        if not settings.napcat.disable_on_live_connect:
            return
        disabled = await self._set_napcat_connection_enabled(
            settings,
            enabled=False,
            reason="bilibili_live_connected",
        )
        if disabled:
            self._napcat_disabled_for_live = True

    async def _restore_napcat_after_live_if_needed(self, settings: LiveAdapterSettings) -> None:
        if not (settings.napcat.restore_on_live_disconnect and self._napcat_disabled_for_live):
            return
        restored = await self._set_napcat_connection_enabled(
            settings,
            enabled=True,
            reason="bilibili_live_disconnected",
        )
        if restored:
            self._napcat_disabled_for_live = False

    async def _set_napcat_connection_enabled(
        self,
        settings: LiveAdapterSettings,
        *,
        enabled: bool,
        reason: str,
    ) -> bool:
        api_name = settings.napcat.control_api_name
        api = _ctx_attr(self, "api")
        if api is None or not hasattr(api, "call"):
            self._logger().warning("NapCat connection control skipped: api.call capability is unavailable.")
            return False
        try:
            result = await api.call(api_name, enabled=enabled, reason=reason)
        except Exception as exc:
            self._logger().warning(f"NapCat connection control failed: {exc}")
            return False
        if isinstance(result, Mapping) and result.get("success") is False:
            self._logger().warning(f"NapCat connection control was rejected: {result.get('error') or result}")
            return False
        return True

    def _fire_tachie_selection_task(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        """Fire a background task to select and display a tachi-e based on the current reply text.

        This runs fully in parallel with reply delivery - never blocks or delays the main flow.
        """
        controller = self._tachie_controller
        if controller is None or not controller.enabled:
            return
        reply_text = str(text or "").strip()
        if not reply_text:
            return
        context_text = ""
        if isinstance(metadata, Mapping):
            context_text = str(metadata.get("context") or metadata.get("user_message") or "").strip()
        task = asyncio.ensure_future(
            controller.select_and_switch(reply_text=reply_text, context_text=context_text)
        )
        task.add_done_callback(self._handle_tachie_selection_done)

    def _handle_tachie_selection_done(self, task: asyncio.Task[Any]) -> None:
        """Log tachi-e selection results or errors."""
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
        if exc is not None:
            self._logger().warning(f"Tachi-e selection task failed: {exc}")
        else:
            result = task.result()
            if result is not None:
                self._logger().debug(
                    f"Tachi-e switched: {result.pose}/{result.emotion} "
                    f"(reason: {result.reason})"
                )

    async def _stop_runtime(self) -> None:
        settings = self._load_settings()
        await self._cancel_runtime_warmup_task()
        self._logged_reply_latency_batches.clear()
        await self._stop_all_visual_context_polling()
        if self._video_watch_controller is not None:
            await self._video_watch_controller.stop()
        self._video_watch_controller = None
        await self._cancel_pending_gateway_disconnect()
        await self._cancel_pending_local_delivery_wait()
        await self._cancel_pending_hub_output_tasks()
        await self._cancel_background_audio_playback_tasks()
        await self._stop_soundboard_runtime()
        if self._tachie_controller is not None:
            await self._tachie_controller.stop()
        self._tachie_controller = None
        if self._song_request_service is not None:
            await self._song_request_service.stop()
        self._song_request_service = None
        if self._song_request_console_session is not None:
            self._song_request_console_session.stop()
        self._song_request_console_session = None
        if self._local_voice_controller is not None:
            await self._local_voice_controller.stop()
        self._local_voice_controller = None
        if self._hub_input_client is not None:
            await self._hub_input_client.stop()
        self._hub_input_client = None
        self._hub_participants = []
        self._hub_speaking_state = {}
        self._wake_hub_speech_waiters()
        if self._transport is not None:
            await self._transport.stop()
        self._transport = None
        await self._cancel_pending_gateway_disconnect()
        if self._router is not None:
            self._router.reset()
        self._router = None
        self._planner = None
        if self._embodied_live2d_runtime is not None:
            await self._embodied_live2d_runtime.stop()
        self._embodied_live2d_runtime = None
        if self._soullink_shell_runtime is not None:
            await self._soullink_shell_runtime.stop()
        self._soullink_shell_runtime = None
        if self._live2d_controller is not None:
            await self._live2d_controller.stop()
        self._live2d_controller = None
        if self._game_bridge is not None:
            await self._game_bridge.stop()
        self._game_bridge = None
        if self._hub_output_bridge is not None:
            await self._hub_output_bridge.stop()
        self._hub_output_bridge = None
        if self._sts2_controller is not None:
            await self._sts2_controller.stop()
        self._sts2_controller = None
        if self._sts2_log_session is not None:
            self._sts2_log_session.stop()
        self._sts2_log_session = None
        if self._tts_provider is not None:
            await self._tts_provider.stop()
        self._tts_provider = None
        translator = self._subtitle_translator
        self._subtitle_translator = None
        stop_method = getattr(translator, "stop", None)
        if callable(stop_method):
            with contextlib.suppress(Exception):
                await stop_method()
        self._audio_output_player = None
        if self._subtitle_webui is not None:
            await self._subtitle_webui.stop()
        self._subtitle_webui = None
        if self._runtime_state is not None:
            await self._runtime_state.report_disconnected()
        await self._restore_napcat_after_live_if_needed(settings)
        await self._stop_livehub_process()

    async def _build_live2d_controller(self, settings: LiveAdapterSettings) -> Live2DController:
        websocket_url = settings.live2d.websocket_url
        if not websocket_url and settings.live2d.driver in {"auto", "vts"}:
            websocket_url = DEFAULT_VTS_WS_URL
        live2d_scheme = resolve_live2d_scheme(settings.live2d)
        controller_scheme = "soullink" if live2d_scheme == "soullink_shell" else live2d_scheme
        soullink_mode = controller_scheme == "soullink" and bool(settings.live2d.soullink.enabled)
        embodied_mode = controller_scheme in {"embodied", "soullink"} and bool(
            settings.live2d.embodied.source_session_id.strip() or soullink_mode
        )
        self._logger().info(
            "Live2D startup scheme resolved: "
            f"scheme={live2d_scheme} driver={settings.live2d.driver} "
            f"soullink_enabled={bool(settings.live2d.soullink.enabled)} "
            f"embodied_enabled={bool(settings.live2d.embodied.enabled)}"
        )
        bridge = JsonLive2DBridge(
            http_url=settings.live2d.http_url,
            websocket_url=websocket_url,
            auth_token=settings.live2d.auth_token,
            connect_timeout_sec=settings.live2d.connect_timeout_sec,
            logger=self._logger(),
        )
        await bridge.start()
        probe = CapabilityProbe(bridge, logger=self._logger())
        profile = await probe.discover(
            driver=settings.live2d.driver,
            model_path=settings.live2d.adaptive.model_path,
            min_confidence=settings.live2d.adaptive.min_confidence,
            overrides=settings.live2d.overrides,
        )
        controller = Live2DController(
            bridge=bridge,
            profile=profile,
            chars_per_second=settings.live2d.sync.chars_per_second,
            prepare_ms=settings.live2d.sync.prepare_ms,
            release_ms=settings.live2d.sync.release_ms,
            mouth_update_interval_ms=settings.live2d.sync.mouth_update_interval_ms,
            mouth_closed_value=settings.live2d.sync.mouth_closed_value,
            mouth_open_threshold=settings.live2d.sync.mouth_open_threshold,
            mouth_open_gamma=settings.live2d.sync.mouth_open_gamma,
            mouth_open_gain=settings.live2d.sync.mouth_open_gain,
            mouth_open_max=settings.live2d.sync.mouth_open_max,
            mouth_sync_mode=settings.live2d.sync.mouth_sync_mode,
            mouth_amplitude_mix=settings.live2d.sync.mouth_amplitude_mix,
            mouth_viseme_lead_ms=settings.live2d.sync.mouth_viseme_lead_ms,
            mouth_open_smoothing=settings.live2d.sync.mouth_open_smoothing,
            mouth_open_attack_smoothing=settings.live2d.sync.mouth_open_attack_smoothing,
            mouth_open_release_smoothing=settings.live2d.sync.mouth_open_release_smoothing,
            mouth_open_min_delta=settings.live2d.sync.mouth_open_min_delta,
            mouth_form_smoothing=settings.live2d.sync.mouth_form_smoothing,
            mouth_form_min_delta=settings.live2d.sync.mouth_form_min_delta,
            mouth_keyframe_transition_ms=settings.live2d.sync.mouth_keyframe_transition_ms,
            mouth_vowel_shapes={
                "a": {
                    "open": settings.live2d.sync.mouth_vowel_a_open,
                    "form": settings.live2d.sync.mouth_vowel_a_form,
                },
                "e": {
                    "open": settings.live2d.sync.mouth_vowel_e_open,
                    "form": settings.live2d.sync.mouth_vowel_e_form,
                },
                "i": {
                    "open": settings.live2d.sync.mouth_vowel_i_open,
                    "form": settings.live2d.sync.mouth_vowel_i_form,
                },
                "o": {
                    "open": settings.live2d.sync.mouth_vowel_o_open,
                    "form": settings.live2d.sync.mouth_vowel_o_form,
                },
                "u": {
                    "open": settings.live2d.sync.mouth_vowel_u_open,
                    "form": settings.live2d.sync.mouth_vowel_u_form,
                },
            },
            parameter_keepalive_ms=settings.live2d.sync.parameter_keepalive_ms,
            lip_sync_only_mode=settings.live2d.sync.lip_sync_only_mode,
            idle_motion_enabled=settings.live2d.sync.idle_motion_enabled,
            idle_motion_model=settings.live2d.sync.idle_motion_model,
            idle_motion_name=settings.live2d.sync.idle_motion_name,
            idle_motion_file=settings.live2d.sync.idle_motion_file,
            idle_motion_interval_ms=settings.live2d.sync.idle_motion_interval_ms,
            idle_sway_enabled=settings.live2d.sync.idle_sway_enabled,
            idle_sway_interval_ms=settings.live2d.sync.idle_sway_interval_ms,
            idle_sway_intensity=settings.live2d.sync.idle_sway_intensity,
            speech_sway_enabled=settings.live2d.sync.speech_sway_enabled,
            speech_sway_intensity=settings.live2d.sync.speech_sway_intensity,
            speech_sway_update_interval_ms=settings.live2d.sync.speech_sway_update_interval_ms,
            embodied_mode=embodied_mode,
            logger=self._logger(),
        )
        if live2d_scheme == "soullink_shell":
            if not settings.live2d.soullink_shell.enabled:
                self._logger().warning(
                    "Live2D scheme is set to soullink_shell but live2d.soullink_shell.enabled is false; "
                    "falling back to current soullink/VTS path."
                )
            else:
                await self._start_soullink_shell_runtime(settings)
            if not settings.live2d.soullink.enabled:
                self._logger().warning(
                    "Live2D scheme is set to soullink_shell but live2d.soullink.enabled is false; "
                    "falling back to legacy controller."
                )
                await controller.set_embodied_mode(False)
                await controller.start()
                return controller
            try:
                soullink_sink = (
                    ShellSoulLinkSink(
                        runtime=self._soullink_shell_runtime,
                        profile=profile,
                        model_path=settings.live2d.adaptive.model_path,
                    )
                    if self._soullink_shell_runtime is not None
                    else VtsSoulLinkSink(base_controller=controller, profile=profile)
                )
                soullink_controller = SoulLinkLive2DController(
                    base_controller=controller,
                    profile=profile,
                    config=settings.live2d.soullink,
                    sink=soullink_sink,
                    logger=self._logger(),
                )
                await soullink_controller.start()
                return soullink_controller
            except Exception as exc:
                await self._stop_soullink_shell_runtime()
                self._logger().warning(
                    f"SoulLink Live2D adapter init failed after soullink_shell selection; "
                    f"falling back to legacy controller: {exc}"
                )
                await controller.set_embodied_mode(False)
                await controller.start()
                return controller
        if live2d_scheme == "soullink":
            if not settings.live2d.soullink.enabled:
                self._logger().warning(
                    "Live2D scheme is set to soullink but live2d.soullink.enabled is false; "
                    "falling back to legacy controller."
                )
                await controller.set_embodied_mode(False)
                await controller.start()
                return controller
            try:
                soullink_controller = SoulLinkLive2DController(
                    base_controller=controller,
                    profile=profile,
                    config=settings.live2d.soullink,
                    sink=VtsSoulLinkSink(base_controller=controller, profile=profile),
                    logger=self._logger(),
                )
                await soullink_controller.start()
                return soullink_controller
            except Exception as exc:
                self._logger().warning(f"SoulLink Live2D adapter init failed; falling back to legacy controller: {exc}")
                await controller.set_embodied_mode(False)
                await controller.start()
                return controller
        await controller.start()
        return controller

    async def _start_soullink_shell_runtime(self, settings: LiveAdapterSettings) -> None:
        runtime_module_name = f"{__package__}.live2d_shell_runtime"
        runtime = None
        try:
            from .live2d_shell_runtime import SoulLinkShellRuntime

            runtime = SoulLinkShellRuntime(
                config=settings.live2d.soullink_shell,
                logger=self._logger(),
            )
            self._soullink_shell_runtime = runtime
            await runtime.start(start_window=bool(getattr(settings.live2d.soullink_shell, "start_native_window", True)))
            self._refresh_soullink_shell_controls()
        except ModuleNotFoundError as exc:
            await self._cleanup_failed_soullink_shell_runtime(runtime)
            if exc.name not in {runtime_module_name, "live2d_shell_runtime"}:
                raise
            self._logger().warning(
                "SoulLink shell unavailable in this build; "
                "falling back to current soullink/VTS path."
            )
        except Exception as exc:
            await self._cleanup_failed_soullink_shell_runtime(runtime)
            if not settings.live2d.soullink_shell.fallback_to_vts:
                raise
            self._logger().warning(
                "SoulLink shell runtime init failed; falling back to current soullink/VTS path: "
                f"{exc}"
            )

    async def _cleanup_failed_soullink_shell_runtime(self, runtime: Any) -> None:
        if runtime is not None:
            with contextlib.suppress(Exception):
                await runtime.stop()
        self._soullink_shell_runtime = None
        self._refresh_soullink_shell_controls()

    async def _stop_soullink_shell_runtime(self) -> None:
        runtime = self._soullink_shell_runtime
        self._soullink_shell_runtime = None
        if runtime is None:
            self._refresh_soullink_shell_controls()
            return
        with contextlib.suppress(Exception):
            await runtime.stop()
        self._refresh_soullink_shell_controls()

    async def _build_tts_provider(self, settings: LiveAdapterSettings) -> TTSProviderProtocol:
        output_dir = str(_resolve_optional_path(settings.tts.output_dir) or (_plugin_data_dir() / "tts_output"))
        provider = GPTSoVITSTTSProvider(
            base_url=settings.tts.base_url,
            connect_timeout_sec=settings.tts.connect_timeout_sec,
            output_dir=output_dir,
            gpt_weights_path=settings.tts.gpt_weights_path,
            sovits_weights_path=settings.tts.sovits_weights_path,
            amplitude_interval_ms=settings.tts.amplitude_interval_ms,
            amplitude_normalization_enabled=settings.tts.amplitude_normalization_enabled,
            amplitude_noise_floor=settings.tts.amplitude_noise_floor,
            amplitude_peak_percentile=settings.tts.amplitude_peak_percentile,
            amplitude_normalization_gain=settings.tts.amplitude_normalization_gain,
            request_defaults=_tts_request_defaults(settings),
            logger=self._logger(),
        )
        await provider.start()
        return provider

    def _build_song_request_service(self, settings: LiveAdapterSettings) -> RvcSongRequestService:
        if not settings.song_request.is_available():
            raise RuntimeError("Song request and RVC functionality is disabled by configuration.")
        from .music_source_provider import build_music_source_provider
        from .rvc_song_pipeline import RvcSongPipeline
        from .song_request_console import SongRequestConsoleSession
        from .song_request_service import RvcSongRequestService

        song_logger: Any = self._logger()
        console_session = self._song_request_console_session
        if settings.song_request.console_enabled:
            if console_session is None:
                console_session = SongRequestConsoleSession(
                    settings.song_request,
                    base_dir=_project_root(),
                    parent_logger=self._logger(),
                ).start()
                self._song_request_console_session = console_session
                if console_session.log_path is not None:
                    self._logger().info(f"Song request console logs are written to {console_session.log_path}")
            song_logger = console_session
        music_source_provider = build_music_source_provider(settings.song_request, logger=song_logger)
        pipeline = RvcSongPipeline(settings.song_request, logger=song_logger)

        def build_song_speech(path: Path, caption_text: str) -> SynthesizedSpeech:
            current_settings = self._load_settings()
            return build_synthesized_speech_from_wav(
                path,
                caption_text,
                provider="rvc_song",
                amplitude_interval_ms=current_settings.tts.amplitude_interval_ms,
                amplitude_normalization_enabled=current_settings.tts.amplitude_normalization_enabled,
                amplitude_noise_floor=current_settings.tts.amplitude_noise_floor,
                amplitude_peak_percentile=current_settings.tts.amplitude_peak_percentile,
                amplitude_normalization_gain=current_settings.tts.amplitude_normalization_gain,
            )

        async def render_ready(text: str, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
            current_settings = self._load_settings()
            return await self._deliver_text_reply_serialized(
                text,
                settings=current_settings,
                source_platform=PLATFORM_NAME,
                metadata=metadata,
                kwargs={},
                on_audio_start=self._build_sts2_audio_start_callback(PLATFORM_NAME, metadata),
                on_segment_audio_start=self._build_sts2_segment_audio_start_callback(PLATFORM_NAME, metadata),
                on_audio_complete=self._build_sts2_audio_complete_callback(PLATFORM_NAME, metadata),
            )

        async def render_song(
            caption_text: str,
            synthesized_speech: SynthesizedSpeech,
            metadata: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            current_settings = self._load_settings()
            return await self._deliver_external_audio_reply_serialized(
                caption_text,
                synthesized_speech,
                settings=current_settings,
                source_platform=PLATFORM_NAME,
                metadata=metadata,
                on_audio_start=self._build_sts2_audio_start_callback(PLATFORM_NAME, metadata),
                on_audio_complete=self._build_sts2_audio_complete_callback(PLATFORM_NAME, metadata),
            )

        return RvcSongRequestService(
            settings=settings.song_request,
            music_source_provider=music_source_provider,
            pipeline=pipeline,
            build_speech=build_song_speech,
            render_ready_reply=render_ready,
            render_song_reply=render_song,
            logger=song_logger,
            console_session=console_session,
        )

    async def _prepare_reply_delivery(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings,
        metadata: Mapping[str, Any] | None,
        kwargs: Mapping[str, Any],
        tts_request_overrides: Mapping[str, Any] | None = None,
        timing_fields: Mapping[str, Any] | None = None,
    ) -> tuple[str, str, dict[str, Any] | None, SynthesizedSpeech | None]:
        loop = asyncio.get_running_loop()
        prepare_started_at = loop.time()
        extra_timing_fields = dict(timing_fields or {})
        speech_text = str(text or "").strip()
        if not speech_text:
            self._log_reply_timing(
                "prepare_done",
                text="",
                total_elapsed_ms=int((loop.time() - prepare_started_at) * 1000),
                audio_elapsed_ms=0,
                subtitle_elapsed_ms=0,
                subtitle_translated=False,
                audio_duration_ms=0,
                **extra_timing_fields,
            )
            return "", "", None, None
        if not settings.language.uses_translated_chinese_subtitle():
            audio_started_at = loop.time()
            if tts_request_overrides is None:
                audio_timeline, synthesized_speech = await self._resolve_reply_audio_timeline(
                    speech_text,
                    settings=settings,
                    metadata=metadata,
                    kwargs=kwargs,
                )
            else:
                audio_timeline, synthesized_speech = await self._resolve_reply_audio_timeline(
                    speech_text,
                    settings=settings,
                    metadata=metadata,
                    kwargs=kwargs,
                    tts_request_overrides=tts_request_overrides,
                )
            audio_elapsed_ms = int((loop.time() - audio_started_at) * 1000)
            self._log_reply_timing(
                "prepare_done",
                text=speech_text,
                total_elapsed_ms=int((loop.time() - prepare_started_at) * 1000),
                audio_elapsed_ms=audio_elapsed_ms,
                subtitle_elapsed_ms=0,
                subtitle_translated=False,
                audio_duration_ms=(
                    int(synthesized_speech.audio_duration_ms)
                    if synthesized_speech is not None
                    else 0
                ),
                **extra_timing_fields,
            )
            return speech_text, speech_text, audio_timeline, synthesized_speech

        audio_elapsed_ms = 0
        subtitle_elapsed_ms = 0

        async def resolve_audio_timing() -> tuple[dict[str, Any] | None, SynthesizedSpeech | None]:
            nonlocal audio_elapsed_ms
            audio_started_at = loop.time()
            try:
                if tts_request_overrides is None:
                    return await self._resolve_reply_audio_timeline(
                        speech_text,
                        settings=settings,
                        metadata=metadata,
                        kwargs=kwargs,
                    )
                return await self._resolve_reply_audio_timeline(
                    speech_text,
                    settings=settings,
                    metadata=metadata,
                    kwargs=kwargs,
                    tts_request_overrides=tts_request_overrides,
                )
            finally:
                audio_elapsed_ms = int((loop.time() - audio_started_at) * 1000)

        async def resolve_subtitle_timing() -> str:
            nonlocal subtitle_elapsed_ms
            subtitle_started_at = loop.time()
            try:
                return await self._prepare_subtitle_text(speech_text, settings=settings)
            finally:
                subtitle_elapsed_ms = int((loop.time() - subtitle_started_at) * 1000)

        audio_task = asyncio.create_task(
            resolve_audio_timing(),
            name="maibot_bilibili_live_adapter.reply_tts",
        )
        subtitle_task = asyncio.create_task(
            resolve_subtitle_timing(),
            name="maibot_bilibili_live_adapter.subtitle_translation",
        )
        (audio_timeline, synthesized_speech), subtitle_text = await asyncio.gather(audio_task, subtitle_task)
        self._log_reply_timing(
            "prepare_done",
            text=speech_text,
            total_elapsed_ms=int((loop.time() - prepare_started_at) * 1000),
            audio_elapsed_ms=audio_elapsed_ms,
            subtitle_elapsed_ms=subtitle_elapsed_ms,
            subtitle_translated=subtitle_text != speech_text,
            audio_duration_ms=(
                int(synthesized_speech.audio_duration_ms)
                if synthesized_speech is not None
                else 0
            ),
            **extra_timing_fields,
        )
        return speech_text, subtitle_text, audio_timeline, synthesized_speech

    async def _prepare_subtitle_text(self, text: str, *, settings: LiveAdapterSettings) -> str:
        speech_text = str(text or "").strip()
        if not settings.language.uses_translated_chinese_subtitle():
            return speech_text
        translator = self._subtitle_translator
        if translator is None or not speech_text:
            return speech_text
        try:
            try:
                translated = await translator.translate_to_chinese(
                    speech_text,
                    source_language=settings.language.subtitle_translation_source_language(),
                )
            except TypeError as exc:
                if "source_language" not in str(exc):
                    raise
                translated = await translator.translate_to_chinese(speech_text)
        except Exception as exc:
            self._logger().warning(f"Subtitle translation failed; falling back to spoken text: {exc}")
            return speech_text
        return str(translated or "").strip() or speech_text

    async def _resolve_reply_audio_timeline(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings,
        metadata: Mapping[str, Any] | None,
        kwargs: Mapping[str, Any],
        tts_request_overrides: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, SynthesizedSpeech | None]:
        audio_timeline = _extract_audio_timeline(metadata, kwargs)
        if audio_timeline is not None:
            timeline_payload = dict(audio_timeline)
            if _uses_vts_native_lip_sync(settings):
                timeline_payload.pop("visemes", None)
            else:
                timeline_payload = _with_text_visemes(timeline_payload, text, settings=settings)
            return timeline_payload, None
        sts2_segments = _split_sts2_tts_segments(text, metadata, logger=self._logger())
        if len(sts2_segments) > 1:
            if _should_parallelize_segment_tts(settings):
                if tts_request_overrides is None:
                    segment_tasks = [
                        self._synthesize_reply_speech(segment_text, settings=settings)
                        for segment_text in sts2_segments
                    ]
                else:
                    segment_tasks = [
                        self._synthesize_reply_speech(
                            segment_text,
                            settings=settings,
                            tts_request_overrides=tts_request_overrides,
                        )
                        for segment_text in sts2_segments
                    ]
                segment_results = await asyncio.gather(*segment_tasks)
            else:
                segment_results = []
                for segment_text in sts2_segments:
                    if tts_request_overrides is None:
                        result = await self._synthesize_reply_speech(segment_text, settings=settings)
                    else:
                        result = await self._synthesize_reply_speech(
                            segment_text,
                            settings=settings,
                            tts_request_overrides=tts_request_overrides,
                        )
                    segment_results.append(result)
            if all(result is not None for result in segment_results):
                segment_speeches = [cast(SynthesizedSpeech, result) for result in segment_results]
                merged_speech = _merge_synthesized_speeches(
                    segment_speeches,
                    text=text,
                    settings=settings,
                )
                if merged_speech is not None:
                    timeline_payload = merged_speech.to_audio_timeline()
                    timeline_payload["reply_segments"] = [
                        _build_reply_segment_payload(segment_text, speech)
                        for segment_text, speech in zip(sts2_segments, segment_speeches, strict=False)
                    ]
                    if _uses_vts_native_lip_sync(settings):
                        timeline_payload.pop("visemes", None)
                    else:
                        timeline_payload = _with_text_visemes(timeline_payload, text, settings=settings)
                    self._logger().info(
                        "Synthesized segmented STS2 GPT-SoVITS reply audio: "
                        f"segments={len(segment_speeches)} duration_ms={merged_speech.audio_duration_ms} "
                        f"audio_ref={merged_speech.audio_ref}"
                    )
                    return timeline_payload, merged_speech
            self._logger().warning(
                "Segmented STS2 GPT-SoVITS synthesis failed; falling back to single-pass synthesis."
            )
        if tts_request_overrides is None:
            synthesized_speech = await self._synthesize_reply_speech(text, settings=settings)
        else:
            synthesized_speech = await self._synthesize_reply_speech(
                text,
                settings=settings,
                tts_request_overrides=tts_request_overrides,
            )
        if synthesized_speech is None:
            return None, None
        timeline_payload = synthesized_speech.to_audio_timeline()
        if _uses_vts_native_lip_sync(settings):
            timeline_payload.pop("visemes", None)
        else:
            timeline_payload = _with_text_visemes(timeline_payload, text, settings=settings)
        return timeline_payload, synthesized_speech

    async def _synthesize_reply_speech(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings,
        tts_request_overrides: Mapping[str, Any] | None = None,
    ) -> SynthesizedSpeech | None:
        provider = self._tts_provider
        if provider is None or not settings.tts.enabled or not settings.tts.is_usable():
            return None
        tts_text = _sanitize_tts_speech_text(text)
        explicit_overrides = dict(tts_request_overrides or {})
        explicit_overrides.setdefault("text_lang", _tts_text_lang(settings, text=tts_text))
        explicit_overrides["text_split_method"] = "cut0"
        request_overrides = _tts_language_speed_request_overrides(
            settings,
            text=tts_text,
            text_lang=explicit_overrides.get("text_lang"),
        )
        request_overrides.update(explicit_overrides)
        try:
            result = await provider.synthesize(tts_text, request_overrides=request_overrides)
        except Exception as exc:
            retry_plan = _tts_retry_fallback_plan(
                settings,
                text=tts_text,
                request_overrides=request_overrides,
            )
            if retry_plan is not None:
                retry_text, retry_request_overrides = retry_plan
                retry_text_lang = str(retry_request_overrides.get("text_lang") or "").strip().lower()
                self._logger().warning(
                    "GPT-SoVITS synthesis failed; retrying with fallback "
                    f"text_lang={retry_text_lang!r}: {exc}"
                )
                try:
                    result = await provider.synthesize(retry_text, request_overrides=retry_request_overrides)
                except Exception as retry_exc:
                    self._logger().warning(
                        "GPT-SoVITS synthesis failed after fallback retry: "
                        f"text_lang={retry_text_lang!r} error={retry_exc}"
                    )
                    return None
            else:
                self._logger().warning(f"GPT-SoVITS synthesis failed: {exc}")
                return None
        self._logger().info(
            "Synthesized GPT-SoVITS reply audio: "
            f"duration_ms={result.audio_duration_ms} audio_ref={result.audio_ref}"
        )
        return result

    async def _start_subtitle_webui(self, settings: LiveAdapterSettings) -> None:
        control_state = self._load_live2d_control_state()
        webui = SubtitleWebUIService(
            host=settings.webui.host,
            port=settings.webui.port,
            subtitle_defaults={
                "box_width_px": settings.webui.subtitle.box_width_px,
                "box_height_px": settings.webui.subtitle.box_height_px,
                "left_px": settings.webui.subtitle.left_px,
                "bottom_px": settings.webui.subtitle.bottom_px,
                "background_color": settings.webui.subtitle.background_color,
                "font_family": settings.webui.subtitle.font_family,
                "font_size_px": settings.webui.subtitle.font_size_px,
                "text_color": settings.webui.subtitle.text_color,
            },
            control_state=control_state,
            on_control_state_changed=self._handle_live2d_control_state_patch,
            shell_controls=self._build_soullink_shell_control_payload(),
            on_shell_action_requested=self._handle_soullink_shell_action_requested,
            logger=self._logger(),
        )
        try:
            await webui.start()
        except Exception as exc:
            self._logger().warning(f"Subtitle WebUI failed to start: {exc}")
            return
        self._subtitle_webui = webui
        self._refresh_soullink_shell_controls()

    async def _start_soundboard(self, settings: LiveAdapterSettings) -> None:
        soundboard = SoundboardService(
            settings.soundboard,
            plugin_dir=Path(__file__).resolve().parent,
            logger=self._logger(),
        )
        try:
            await soundboard.start()
        except Exception as exc:
            self._logger().warning(f"Soundboard failed to start: {exc}")
            return
        self._soundboard = soundboard
        await self._maybe_auto_open_soundboard_webui(settings, soundboard.url)

    async def _maybe_auto_open_soundboard_webui(self, settings: LiveAdapterSettings, url: str) -> None:
        normalized_url = str(url or "").strip()
        if not _should_auto_open_soundboard_webui(
            settings.soundboard.webui_auto_open_on_startup,
            normalized_url,
            self._soundboard_webui_opened_url,
        ):
            return
        try:
            opened = await asyncio.to_thread(_open_local_webui_url, normalized_url)
        except Exception as exc:
            self._logger().warning(f"Soundboard WebUI auto-open failed: {exc}")
            return
        if opened:
            self._soundboard_webui_opened_url = normalized_url
            self._logger().info(f"Soundboard WebUI opened in browser: {normalized_url}")
        else:
            self._logger().warning(f"Soundboard WebUI auto-open was rejected by the browser: {normalized_url}")

    def _handle_live2d_control_state_patch(self, patch: Mapping[str, Any] | None) -> Live2DControlState:
        store = self._live2d_control_state_store_for_plugin()
        self._live2d_control_state = store.merge_patch(patch)
        if self._embodied_live2d_runtime is not None:
            self._embodied_live2d_runtime.set_mouse_follow_enabled(self._live2d_control_state.mouse_follow_enabled)
        self._refresh_soullink_shell_controls()
        return self._live2d_control_state

    def _build_soullink_shell_control_payload(self) -> dict[str, Any]:
        runtime = self._soullink_shell_runtime
        if runtime is None:
            return build_shell_control_payload(enabled=False)
        build_payload = getattr(runtime, "build_control_surface_payload", None)
        if callable(build_payload):
            try:
                payload = build_payload()
                if isinstance(payload, Mapping):
                    return build_shell_control_payload(
                        enabled=bool(payload.get("enabled")),
                        click_through=bool(payload.get("click_through", True)),
                        interactive=bool(payload.get("interactive", False)),
                        actions=payload.get("actions") if isinstance(payload.get("actions"), Sequence) else (),
                    )
            except Exception as exc:
                self._logger().debug(f"Failed to build SoulLink shell control payload: {exc}")
        return build_shell_control_payload(enabled=False)

    def _refresh_soullink_shell_controls(self) -> None:
        webui = self._subtitle_webui
        if webui is None:
            return
        webui.update_shell_controls(self._build_soullink_shell_control_payload())

    def _handle_soullink_shell_action_requested(self, action: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._run_soullink_shell_action_requested(action),
            name=f"maibot_bilibili_live_adapter.soullink_shell_action.{action}",
        )
        task.add_done_callback(self._finalize_soullink_shell_action_task)

    async def _run_soullink_shell_action_requested(self, action: str) -> None:
        normalized = str(action or "").strip()
        if not normalized:
            return
        if normalized == "open_settings":
            webui = self._subtitle_webui
            if webui is not None:
                webui.show_windows()
            return
        runtime = self._soullink_shell_runtime
        if runtime is None:
            return
        await runtime.dispatch_control_action(normalized)
        self._refresh_soullink_shell_controls()

    def _finalize_soullink_shell_action_task(self, task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                self._logger().warning(f"SoulLink shell action failed: {exc}")

    def _load_live2d_control_state(self) -> Live2DControlState:
        store = self._live2d_control_state_store_for_plugin()
        self._live2d_control_state = store.load()
        return self._live2d_control_state

    def _live2d_control_state_store_for_plugin(self) -> Live2DControlStateStore:
        store = self._live2d_control_state_store
        if store is None:
            store = Live2DControlStateStore(_plugin_data_dir() / "live2d_control_state.json")
            self._live2d_control_state_store = store
        return store

    async def _publish_reply_to_webui(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings,
        source_platform: str,
        audio_timeline: Mapping[str, Any] | None,
        synthesized_speech: SynthesizedSpeech | None,
        speech_text: str = "",
        wait_for_audio_start: bool = False,
        on_audio_start: Callable[[], None] | None = None,
    ) -> tuple[bool, bool]:
        webui = self._subtitle_webui
        if webui is None or not settings.webui.enabled:
            return False, False
        reply_id = uuid4().hex
        segments = await self._build_webui_segments(
            text,
            settings=settings,
            speech_text=speech_text,
            audio_timeline=audio_timeline,
            synthesized_speech=synthesized_speech,
        )
        if not segments:
            return False, False
        await webui.publish_reply(
            reply_id=reply_id,
            text=text,
            segments=segments,
            source_platform=source_platform,
        )
        audio_started = False
        can_wait_for_audio = bool(wait_for_audio_start and getattr(webui, "has_clients", False))
        wait_for_audio = getattr(webui, "wait_for_audio_start", None)
        if can_wait_for_audio and callable(wait_for_audio):
            timeout_sec = max(0.1, settings.webui.audio_start_ack_timeout_ms / 1000.0)
            try:
                audio_start_event = await wait_for_audio(reply_id, timeout_sec=timeout_sec)
                audio_started = audio_start_event is not None
                if audio_started:
                    if on_audio_start is not None:
                        try:
                            on_audio_start()
                        except Exception as exc:
                            self._logger().warning(f"STS2 audio start callback failed: {exc}")
                    self._logger().debug(
                        "Subtitle WebUI audio start ACK received: "
                        f"reply_id={reply_id} segment={audio_start_event.get('segment_index', 0)}"
                    )
                else:
                    self._logger().warning(
                        "Subtitle WebUI audio start ACK timed out; "
                        "Live2D will use the normal prepare offset."
                    )
            except Exception as exc:
                self._logger().debug(f"Subtitle WebUI audio start wait failed: {exc}")
        return True, audio_started

    async def _build_webui_segments(
        self,
        text: str,
        *,
        settings: LiveAdapterSettings,
        audio_timeline: Mapping[str, Any] | None,
        synthesized_speech: SynthesizedSpeech | None,
        speech_text: str = "",
    ) -> list[SubtitleSegment]:
        normalized_speech_text = str(speech_text or "").strip()
        pre_synthesized_segments = _extract_reply_segment_payloads(audio_timeline)
        if pre_synthesized_segments:
            webui = self._subtitle_webui
            expose_webui_audio = _should_expose_webui_audio(settings)
            segments: list[SubtitleSegment] = []
            for index, payload in enumerate(pre_synthesized_segments):
                segment_text = str(payload.get("text") or "").strip()
                if not segment_text:
                    continue
                speech = _segment_audio_from_timeline(payload)
                duration_ms = (
                    speech.audio_duration_ms
                    if speech is not None
                    else estimate_subtitle_duration_ms(
                        segment_text,
                        chars_per_second=settings.live2d.sync.chars_per_second,
                    )
                )
                audio_ref = speech.audio_ref if speech is not None else ""
                audio_url = ""
                provider = speech.provider if speech is not None else ""
                segment_speech_text = str(
                    payload.get("speech_text") or payload.get("english_text") or payload.get("original_text") or ""
                ).strip()
                if not segment_speech_text and len(pre_synthesized_segments) == 1:
                    segment_speech_text = normalized_speech_text
                if expose_webui_audio and webui is not None and audio_ref:
                    try:
                        audio_url = webui.register_audio_asset(Path(audio_ref))
                    except Exception as exc:
                        self._logger().warning(f"Subtitle WebUI audio registration failed: {exc}")
                segments.append(
                    SubtitleSegment(
                        index=index,
                        text=segment_text,
                        duration_ms=max(120, int(duration_ms)),
                        audio_ref=audio_ref,
                        audio_url=audio_url,
                        provider=provider,
                        speech_text=segment_speech_text,
                    )
                )
            if segments:
                return segments
        raw_segments = split_text_segments(text)
        if not raw_segments:
            return []
        webui = self._subtitle_webui
        expose_webui_audio = _should_expose_webui_audio(settings)
        full_reply_speech = synthesized_speech or _segment_audio_from_timeline(audio_timeline)
        if full_reply_speech is not None:
            audio_url = ""
            if expose_webui_audio and webui is not None and full_reply_speech.audio_ref:
                try:
                    audio_url = webui.register_audio_asset(Path(full_reply_speech.audio_ref))
                except Exception as exc:
                    self._logger().warning(f"Subtitle WebUI audio registration failed: {exc}")
            return [
                SubtitleSegment(
                    index=0,
                    text=str(text or ""),
                    duration_ms=max(120, int(full_reply_speech.audio_duration_ms)),
                    audio_ref=full_reply_speech.audio_ref,
                    audio_url=audio_url,
                    provider=str(getattr(full_reply_speech, "provider", "") or ""),
                    speech_text=normalized_speech_text,
                )
            ]
        if settings.language.uses_translated_chinese_subtitle() and normalized_speech_text:
            segment_text = str(text or "")
            return [
                SubtitleSegment(
                    index=0,
                    text=segment_text,
                    duration_ms=max(
                        120,
                        estimate_subtitle_duration_ms(
                            segment_text,
                            chars_per_second=settings.live2d.sync.chars_per_second,
                        ),
                    ),
                    audio_ref="",
                    audio_url="",
                    provider="",
                    speech_text=normalized_speech_text,
                )
            ]
        segments: list[SubtitleSegment] = []
        single_segment_audio = _segment_audio_from_timeline(audio_timeline) if len(raw_segments) == 1 else None
        for index, segment_text in enumerate(raw_segments):
            speech = synthesized_speech if len(raw_segments) == 1 else None
            if speech is None and index == 0 and single_segment_audio is not None:
                speech = single_segment_audio
            if speech is None and self._tts_provider is not None and settings.tts.enabled and settings.tts.is_usable():
                speech = await self._synthesize_reply_speech(segment_text, settings=settings)
            duration_ms = (
                speech.audio_duration_ms
                if speech is not None
                else estimate_subtitle_duration_ms(segment_text, chars_per_second=settings.live2d.sync.chars_per_second)
            )
            audio_ref = speech.audio_ref if speech is not None else ""
            audio_url = ""
            provider = speech.provider if speech is not None else ""
            if expose_webui_audio and webui is not None and audio_ref:
                try:
                    audio_url = webui.register_audio_asset(Path(audio_ref))
                except Exception as exc:
                    self._logger().warning(f"Subtitle WebUI audio registration failed: {exc}")
            segments.append(
                SubtitleSegment(
                    index=index,
                    text=segment_text,
                    duration_ms=max(120, int(duration_ms)),
                    audio_ref=audio_ref,
                    audio_url=audio_url,
                    provider=provider,
                    speech_text=normalized_speech_text if len(raw_segments) == 1 else "",
                )
            )
        return segments

    def _create_audio_playback_task(
        self,
        synthesized_speech: SynthesizedSpeech | None,
        *,
        settings: LiveAdapterSettings,
        enabled: bool,
        on_audio_start: Callable[[], None] | None = None,
    ) -> asyncio.Task[bool] | None:
        player = self._audio_output_player
        if (
            not enabled
            or player is None
            or synthesized_speech is None
            or not settings.tts.enabled
            or not settings.tts.audio_playback_enabled
            or not synthesized_speech.audio_ref
        ):
            return None
        return asyncio.create_task(
            player.play(
                synthesized_speech.audio_ref,
                duration_ms=synthesized_speech.audio_duration_ms,
                on_audio_start=on_audio_start,
            ),
            name="maibot_bilibili_live_adapter.audio_output",
        )

    def _track_background_audio_playback_task(self, task: asyncio.Task[bool]) -> None:
        self._background_audio_playback_tasks.add(task)

        def _on_done(done_task: asyncio.Task[bool]) -> None:
            self._background_audio_playback_tasks.discard(done_task)
            with contextlib.suppress(asyncio.CancelledError):
                exc = done_task.exception()
                if exc is not None:
                    self._logger().warning(f"Background audio playback failed: {exc}")

        task.add_done_callback(_on_done)

    def _self_hub_identity_values(self, settings: LiveAdapterSettings) -> tuple[set[str], set[str]]:
        client_ids = {
            value
            for value in {
                str(settings.hub_output.client_id or "").strip(),
                str(settings.identity.bot_user_id or "").strip(),
            }
            if value
        }
        bot_names = {
            value
            for value in {
                str(settings.hub_output.bot_name or "").strip(),
                str(settings.identity.bot_nickname or "").strip(),
            }
            if value
        }
        client_id_keys = {value.casefold() for value in client_ids}
        bot_name_keys = {value.casefold() for value in bot_names}
        for participant in self._hub_participants:
            if not isinstance(participant, Mapping):
                continue
            if not self._participant_belongs_to_self(
                participant,
                client_id_keys=client_id_keys,
                bot_name_keys=bot_name_keys,
            ):
                continue
            forwarded_user_id = str(participant.get("forward_user_id") or "").strip()
            forwarded_username = str(participant.get("forward_username") or "").strip()
            if forwarded_user_id:
                client_ids.add(forwarded_user_id)
                client_id_keys.add(forwarded_user_id.casefold())
            if forwarded_username:
                bot_names.add(forwarded_username)
                bot_name_keys.add(forwarded_username.casefold())
        return client_ids, bot_names

    def _self_hub_client_ids(self, settings: LiveAdapterSettings) -> set[str]:
        client_ids, _ = self._self_hub_identity_values(settings)
        return client_ids

    def _self_hub_bot_names(self, settings: LiveAdapterSettings) -> set[str]:
        _, bot_names = self._self_hub_identity_values(settings)
        return bot_names

    def _build_live_session_prompt(self, settings: LiveAdapterSettings) -> str:
        return _merge_prompt_sections(
            _live_language_prompt(settings),
            _live_capability_boundary_prompt(settings),
            self._hub_multi_ai_prompt(settings),
            self._pending_visual_context_prompt(settings),
            self._video_watch_prompt(),
        )

    def _hub_multi_ai_prompt(self, settings: LiveAdapterSettings) -> str:
        other_bot_names = self._hub_other_bot_names(settings)
        if not other_bot_names:
            return ""
        self_name = (
            str(settings.hub_output.bot_name or "").strip()
            or str(settings.identity.bot_nickname or "").strip()
            or str(settings.hub_output.client_id or settings.identity.bot_user_id or "this AI").strip()
        )
        visible_names = ", ".join(other_bot_names[:4])
        if len(other_bot_names) > 4:
            visible_names = f"{visible_names}, and {len(other_bot_names) - 4} more"
        return (
            f"You are not the only AI in this live room right now. You are {self_name}. "
            f"Other AI bots currently connected through the shared live Hub: {visible_names}. "
            "Their forwarded messages may appear in the shared live chat as separate speakers. "
            "Treat them as distinct AI identities, do not claim to be the only AI on stream, "
            "and do not confuse their forwarded replies with your own voice."
        )

    def _hub_other_bot_names(self, settings: LiveAdapterSettings) -> list[str]:
        self_client_ids = {value.casefold() for value in self._self_hub_client_ids(settings)}
        self_bot_names = {value.casefold() for value in self._self_hub_bot_names(settings)}
        names: list[str] = []
        seen: set[str] = set()
        for participant in self._hub_participants:
            if not isinstance(participant, Mapping):
                continue
            if self._participant_belongs_to_self(
                participant,
                client_id_keys=self_client_ids,
                bot_name_keys=self_bot_names,
            ):
                continue
            normalized_name = str(
                participant.get("forward_username") or participant.get("bot_name") or participant.get("client_id") or ""
            ).strip()
            if not normalized_name:
                continue
            key = normalized_name.casefold()
            if key in seen:
                continue
            seen.add(key)
            names.append(normalized_name)
        return names

    @staticmethod
    def _participant_belongs_to_self(
        participant: Mapping[str, Any],
        *,
        client_id_keys: set[str],
        bot_name_keys: set[str],
    ) -> bool:
        """判断参与者是否属于本 bot 身份（client_id / bot_name / forward_username 任一匹配）。

        传入的集合均为 casefold 后的键。
        """
        client_id = str(participant.get("client_id") or "").strip().casefold()
        bot_name = str(participant.get("bot_name") or "").strip().casefold()
        forward_username = str(participant.get("forward_username") or "").strip().casefold()
        return (
            bool(client_id and client_id in client_id_keys)
            or bool(bot_name and bot_name in bot_name_keys)
            or bool(forward_username and forward_username in bot_name_keys)
        )

    async def _handle_hub_input_state(self, state: Mapping[str, Any]) -> None:
        self._update_hub_speaking_state(state.get("speaking"))
        participants = state.get("participants")
        if not isinstance(participants, list):
            return
        normalized_participants = [
            dict(item)
            for item in participants
            if isinstance(item, Mapping) and str(item.get("client_id") or "").strip()
        ]
        normalized_participants.sort(
            key=lambda item: (
                str(item.get("forward_username") or item.get("bot_name") or "").strip().casefold(),
                str(item.get("client_id") or "").strip().casefold(),
            )
        )
        self._hub_participants = normalized_participants

    def _decorate_hub_context_event(
        self,
        event: Mapping[str, Any],
        *,
        settings: LiveAdapterSettings,
    ) -> dict[str, Any]:
        other_bot_names = self._hub_other_bot_names(settings)
        active_bot_count = len(other_bot_names) + 1 if other_bot_names else len(self._hub_participants)
        if active_bot_count <= 1:
            return dict(event)
        updated_event = dict(event)
        updated_event["hub_active_bot_count"] = max(2, active_bot_count)
        updated_event["hub_other_bot_names"] = other_bot_names
        return updated_event

    async def _handle_hub_input_record(
        self,
        record: Mapping[str, Any],
        *,
        settings: LiveAdapterSettings,
    ) -> None:
        router = self._router
        if router is None:
            return
        event = normalize_hub_record_to_live_event(
            record,
            self_client_ids=self._self_hub_client_ids(settings),
            self_bot_names=self._self_hub_bot_names(settings),
            inject_local_messages=settings.hub_input.inject_local_messages,
            inject_other_bot_replies=settings.hub_input.inject_other_bot_replies,
            ignore_self_bot_replies=settings.hub_input.ignore_self_bot_replies,
        )
        if event is None:
            return
        event = self._decorate_hub_context_event(event, settings=settings)
        await router.handle_event(event)

    def _schedule_hub_output_forward(
        self,
        *,
        reply_text: str,
        settings: LiveAdapterSettings,
        source_platform: str,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        bridge = self._hub_output_bridge
        if self._should_skip_hub_output_forward(metadata):
            return
        normalized_text = self._resolve_hub_output_reply_text(
            reply_text,
            metadata=metadata,
        )
        if bridge is None or not normalized_text:
            return
        if not settings.hub_output.enabled or not settings.hub_output.forward_platform_replies:
            return
        if str(source_platform or "").strip() != PLATFORM_NAME:
            return
        task = asyncio.create_task(
            self._forward_reply_to_hub(
                reply_text=normalized_text,
                settings=settings,
                metadata=metadata,
            ),
            name="maibot_bilibili_live_adapter.hub_output",
        )
        self._hub_output_tasks.add(task)
        task.add_done_callback(self._handle_hub_output_task_done)

    def _should_skip_hub_output_forward(self, metadata: Mapping[str, Any] | None) -> bool:
        additional_config = _extract_additional_config(metadata)
        batch_id = str(additional_config.get("replyer_batch_id") or "").strip()
        segment_index = _optional_int(additional_config.get("replyer_segment_index")) or 0
        segment_count = max(1, _optional_int(additional_config.get("replyer_segment_count")) or 0)
        now = time.monotonic()
        self._prune_hub_output_forwarded_batches(now=now)
        if batch_id:
            if batch_id in self._hub_output_forwarded_batches:
                return True
            self._hub_output_forwarded_batches[batch_id] = now
            return False
        return segment_count > 1 and segment_index > 1

    def _prune_hub_output_forwarded_batches(self, *, now: float) -> None:
        if not self._hub_output_forwarded_batches:
            return
        expiry_before = now - 300.0
        expired_keys = [
            batch_id
            for batch_id, forwarded_at in self._hub_output_forwarded_batches.items()
            if forwarded_at < expiry_before
        ]
        for batch_id in expired_keys:
            self._hub_output_forwarded_batches.pop(batch_id, None)
        if len(self._hub_output_forwarded_batches) <= 1024:
            return
        overflow = len(self._hub_output_forwarded_batches) - 1024
        oldest_batches = sorted(
            self._hub_output_forwarded_batches.items(),
            key=lambda item: item[1],
        )[:overflow]
        for batch_id, _ in oldest_batches:
            self._hub_output_forwarded_batches.pop(batch_id, None)

    def _handle_hub_output_task_done(self, task: asyncio.Task[Any]) -> None:
        self._hub_output_tasks.discard(task)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()

    async def _forward_reply_to_hub(
        self,
        *,
        reply_text: str,
        settings: LiveAdapterSettings,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        bridge = self._hub_output_bridge
        if bridge is None:
            return
        payload = self._build_hub_output_payload(
            reply_text=reply_text,
            settings=settings,
            metadata=metadata,
        )
        result = await bridge.send("client_reply", payload)
        if not bool(result.get("success")):
            self._logger().warning(f"Hub output forwarding failed: {result.get('error') or result}")

    def _resolve_hub_output_reply_text(
        self,
        reply_text: str,
        *,
        metadata: Mapping[str, Any] | None,
    ) -> str:
        if isinstance(metadata, Mapping):
            additional_config = _extract_additional_config(metadata)
            replyer_full_text = str(additional_config.get("replyer_full_text") or "").strip()
            if replyer_full_text:
                return replyer_full_text
            source_text = extract_live_output_text_from_message(metadata)
            if source_text:
                return source_text
        return str(reply_text or "").strip()

    def _build_reply_latency_audio_start_callback(
        self,
        *,
        reply_text: str,
        metadata: Mapping[str, Any] | None,
        downstream: Callable[[], None] | None = None,
    ) -> Callable[[], None] | None:
        source_text = _extract_reply_latency_source_text(metadata)
        source_timestamp = _extract_reply_latency_source_timestamp(metadata)
        batch_id = _extract_reply_latency_batch_id(metadata)
        normalized_reply_text = str(reply_text or "").strip()
        if not source_text or source_timestamp is None or not normalized_reply_text:
            return downstream

        def _callback() -> None:
            if batch_id:
                if batch_id in self._logged_reply_latency_batches:
                    if downstream is not None:
                        downstream()
                    return
                self._logged_reply_latency_batches.add(batch_id)
            audio_start_timestamp = time.time()
            self._append_local_reply_latency_log(
                message_text=source_text,
                message_timestamp=source_timestamp,
                audio_start_timestamp=audio_start_timestamp,
                reply_text=normalized_reply_text,
            )
            if downstream is not None:
                downstream()

        return _callback

    def _append_local_reply_latency_log(
        self,
        *,
        message_text: str,
        message_timestamp: float,
        audio_start_timestamp: float,
        reply_text: str,
    ) -> None:
        latency_ms = max(0, int((audio_start_timestamp - message_timestamp) * 1000))
        record = {
            "message_text": str(message_text or "").strip(),
            "message_timestamp": round(float(message_timestamp), 3),
            "audio_start_timestamp": round(float(audio_start_timestamp), 3),
            "reply_text": str(reply_text or "").strip(),
            "audible_latency_ms": latency_ms,
        }
        try:
            log_path = _plugin_data_dir() / "logs" / "live_reply_latency.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            self._logger().warning(f"Failed to append local reply latency log: {exc}")

    def _build_hub_output_payload(
        self,
        *,
        reply_text: str,
        settings: LiveAdapterSettings,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        additional_config = _extract_additional_config(metadata)
        client_id = str(settings.hub_output.client_id or settings.identity.bot_user_id or "maibot-live").strip()
        bot_name = str(settings.hub_output.bot_name or settings.identity.bot_nickname or client_id).strip() or client_id
        return {
            "client_id": client_id,
            "bot_name": bot_name,
            "text": self._resolve_hub_output_reply_text(reply_text, metadata=metadata),
            "room_id": str(settings.bilibili.room_id),
            "live_event_type": str(additional_config.get("live_event_type") or "").strip(),
            "route_scope": str(settings.route_scope() or "").strip(),
        }

    def _build_sts2_audio_start_callback(
        self,
        source_platform: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> Callable[[], None] | None:
        controller = self._sts2_controller
        if (
            source_platform != PLATFORM_NAME
            or controller is None
            or not controller.has_pending_decision
        ):
            return None
        if not _metadata_matches_pending_sts2_decision(metadata, controller):
            return None
        return controller.build_audio_start_callback()

    def _build_sts2_audio_complete_callback(
        self,
        source_platform: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> Callable[[], None] | None:
        controller = self._sts2_controller
        if (
            source_platform != PLATFORM_NAME
            or controller is None
            or not controller.has_pending_decision
        ):
            return None
        if not _metadata_matches_pending_sts2_decision(metadata, controller):
            return None
        return controller.build_audio_complete_callback()

    def _build_sts2_segment_audio_start_callback(
        self,
        source_platform: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> Callable[[int, int], None] | None:
        controller = self._sts2_controller
        if (
            source_platform != PLATFORM_NAME
            or controller is None
            or not controller.has_pending_decision
        ):
            return None
        if not _metadata_matches_pending_sts2_decision(metadata, controller):
            return None
        return controller.build_segment_audio_start_callback()

    def _build_local_voice_echo_start_callback(
        self,
        text: str,
        *,
        downstream: Callable[[], None] | None = None,
    ) -> Callable[[], None] | None:
        normalized_text = str(text or "").strip()
        if not normalized_text and downstream is None:
            return None

        def _callback() -> None:
            controller = self._local_voice_controller
            if controller is not None and normalized_text:
                try:
                    controller.record_recent_bot_output(normalized_text)
                except Exception as exc:
                    logger = self._logger()
                    if logger is not None:
                        logger.warning(f"Local voice echo guard callback failed: {exc}")
            if downstream is not None:
                downstream()

        return _callback

    def _load_raw_settings(self) -> LiveAdapterSettings:
        try:
            return cast(LiveAdapterSettings, self.config)
        except RuntimeError:
            return LiveAdapterSettings.model_validate(self.get_default_config())

    def _load_settings(self) -> LiveAdapterSettings:
        settings = self._load_raw_settings()
        soundboard_settings = load_soundboard_cues_from_files(
            settings.soundboard,
            plugin_dir=Path(__file__).resolve().parent,
        )
        return settings.model_copy(update={"soundboard": soundboard_settings})

    async def _route_visual_command_followup_message(
        self,
        *,
        event: Mapping[str, Any],
        settings: LiveAdapterSettings,
    ) -> bool:
        normalized_text = str(event.get("text") or event.get("summary") or "").strip()
        if not normalized_text:
            return False
        message_id = str(event.get("event_id") or f"vision-command-{uuid4().hex}").strip()
        normalized_event = dict(event)
        normalized_event["event_id"] = message_id
        normalized_event["text"] = normalized_text
        normalized_event["summary"] = normalized_text
        message = build_message_dict(
            normalized_event,
            settings,
            reason="visual_context_command",
        )
        route_metadata = {
            "source": "bilibili_live",
            "room_id": settings.bilibili.room_id,
            "selection_reason": "visual_context_command",
            "selection_score": 1_000_000.0,
            "visual_context_command": True,
        }
        accepted = await self.ctx.gateway.route_message(
            GATEWAY_NAME,
            message,
            route_metadata=route_metadata,
            external_message_id=message_id,
            dedupe_key=message_id,
        )
        return bool(accepted)

    def _handle_local_voice_settings_changed(self, settings: LiveAdapterSettings) -> None:
        self.set_plugin_config(settings.model_dump(mode="python"))

    async def _route_local_voice_text(self, text: str, metadata: Mapping[str, Any] | None = None) -> bool:
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return False
        settings = self._load_settings()
        normalized_metadata = dict(metadata or {})
        message_id = str(normalized_metadata.get("phrase_id") or f"local-voice-{uuid4().hex}").strip()
        controller = self._sts2_controller
        if (
            settings.sts2.enabled
            and controller is not None
            and controller.has_pending_decision
        ):
            logger = self._logger()
            if logger is not None:
                logger.info(
                    "Local microphone transcript deferred while STS2 commentary is waiting for audio start: "
                    f"phrase_id={message_id} text={normalized_text[:80]!r}"
                )
            return False
        message = build_local_voice_message_dict(
            settings,
            text=normalized_text,
            event_id=message_id,
            metadata=normalized_metadata,
        )
        route_metadata = {
            "source": "local_voice",
            "room_id": settings.bilibili.room_id,
            "selection_reason": "local_voice_priority",
            "selection_score": 1_000_000.0,
            "local_voice_priority": True,
        }
        accepted = await self.ctx.gateway.route_message(
            GATEWAY_NAME,
            message,
            route_metadata=route_metadata,
            external_message_id=message_id,
            dedupe_key=message_id,
        )
        if accepted:
            logger = self._logger()
            if logger is not None:
                logger.info(
                    "Local microphone transcript injected: "
                    f"phrase_id={message_id} text={normalized_text[:80]!r}"
                )
        return bool(accepted)

    def _logger(self) -> Any:
        try:
            return self.ctx.logger
        except RuntimeError:
            import logging

            return logging.getLogger("maibot_bilibili_live_adapter")

    def _resolve_memorix_host_service(self) -> Any | None:
        """延迟解析 A_memorix 宿主服务；不可用时返回 None（调用方跳过记忆写入并告警）。

        插件运行在 Runner 子进程中，模块级导入只能拿到从未 start 的单例副本
        （内部 _kernel 为 None，invoke 静默返回失败）。此处延迟导入并检测内核状态，
        仅在服务真正可用时才返回实例。
        """
        try:
            from src.A_memorix.host_service import a_memorix_host_service
        except Exception as exc:
            logger = self._logger()
            if logger is not None and hasattr(logger, "warning"):
                logger.warning(f"A_memorix host service import failed: {exc}")
            return None
        kernel = getattr(a_memorix_host_service, "_kernel", None)
        if kernel is None:
            return None
        return a_memorix_host_service


def create_plugin() -> BilibiliLiveAdapterPlugin:
    """Create the plugin instance."""

    return BilibiliLiveAdapterPlugin()


def _ctx_attr(plugin: BilibiliLiveAdapterPlugin, name: str) -> Any:
    try:
        return getattr(plugin.ctx, name)
    except RuntimeError:
        return None


def _build_live_chat_id(settings: LiveAdapterSettings) -> str:
    components = [PLATFORM_NAME]
    account_id = str(settings.identity.bot_user_id or "").strip()
    scope = str(settings.route_scope() or "").strip()
    room_id = str(settings.bilibili.room_id or "").strip()
    if account_id:
        components.append(f"account:{account_id}")
    if scope:
        components.append(f"scope:{scope}")
    components.append(room_id)
    return hashlib.md5("_".join(components).encode("utf-8")).hexdigest()


_PAID_SUPER_CHAT_TEMPLATES = (
    "Thanks for the super chat, {username}. You said, {sc_text}.",
    "Super chat from {username}. Message says, {sc_text}. Thank you.",
    "Thanks, {username}. Your super chat says, {sc_text}.",
    "Oh, super chat from {username}. It says, {sc_text}. Appreciate it.",
    "Thanks for the support, {username}. Your message was, {sc_text}.",
    "Well played, {username}. Super chat says, {sc_text}. Thank you.",
)

_PAID_GIFT_TEMPLATES = (
    "Thanks for the gift, {username}. {gift_phrase}. Very nice.",
    "Gift from {username}. {gift_phrase}. Thank you.",
    "Thanks, {username}. I appreciate the {gift_phrase}.",
    "Oh, {gift_phrase} from {username}. That is actually pretty cute.",
    "Thanks for the gift, {username}. {gift_phrase}. Not bad.",
    "Very nice, {username}. Thanks for the {gift_phrase}.",
)

_PAID_GUARD_TEMPLATES = (
    "Thanks for the membership, {username}. {guard_phrase}.",
    "Membership from {username}. {guard_phrase}. Thank you.",
    "Thanks, {username}. I appreciate the {guard_phrase}.",
    "Well then, {username}. {guard_phrase}. That is commitment.",
    "Thanks for the support, {username}. {guard_phrase}.",
    "Membership acknowledged, {username}. {guard_phrase}. Thank you.",
)

_PAID_GUARD_NAME_SPEECH_MAP = {
    "舰长": "Captain membership",
    "提督": "Commander membership",
    "总督": "Governor membership",
}

_PAID_ACKNOWLEDGEMENT_TTS_REQUEST_OVERRIDES: dict[str, Any] = {}


def _build_paid_acknowledgement_text(event: Mapping[str, Any]) -> str:
    event_type = str(event.get("type") or "").strip()
    username = _normalize_paid_acknowledgement_text(event.get("username") or event.get("user_id") or "anonymous")
    if event_type == "super_chat":
        template = random.choice(_PAID_SUPER_CHAT_TEMPLATES)
        return template.format(username=username, sc_text=_build_paid_super_chat_phrase(event))
    if event_type == "gift":
        template = random.choice(_PAID_GIFT_TEMPLATES)
        return template.format(
            username=username,
            gift_phrase=_build_paid_count_phrase(
                event.get("gift_name"),
                count=event.get("count"),
                fallback="gift",
            ),
        )
    if event_type == "guard":
        template = random.choice(_PAID_GUARD_TEMPLATES)
        return template.format(
            username=username,
            guard_phrase=_build_paid_count_phrase(
                _normalize_guard_name_for_speech(event.get("gift_name")),
                count=event.get("count"),
                fallback="membership",
                pluralize_membership=True,
            ),
        )
    return ""


def _build_paid_super_chat_phrase(event: Mapping[str, Any]) -> str:
    sc_text = _normalize_paid_acknowledgement_text(event.get("text") or "")
    if sc_text:
        return sc_text
    return "thank you"


def _build_paid_count_phrase(
    raw_name: Any,
    *,
    count: Any,
    fallback: str,
    pluralize_membership: bool = False,
) -> str:
    normalized_name = _normalize_paid_acknowledgement_text(raw_name) or fallback
    try:
        normalized_count = max(1, int(count or 1))
    except (TypeError, ValueError):
        normalized_count = 1
    if normalized_count <= 1:
        return normalized_name
    if pluralize_membership and normalized_name.lower().endswith("membership"):
        normalized_name = f"{normalized_name}s"
    return f"{normalized_count} {normalized_name}"


def _normalize_guard_name_for_speech(raw_name: Any) -> str:
    normalized = _normalize_paid_acknowledgement_text(raw_name)
    if not normalized:
        return "membership"
    return _PAID_GUARD_NAME_SPEECH_MAP.get(normalized, normalized)


def _normalize_paid_acknowledgement_text(value: Any) -> str:
    normalized = sanitize_model_reserved_tokens(str(value or ""))
    normalized = " ".join(normalized.replace("\n", " ").split()).strip()
    return normalized.strip(" ,.!?;:，。！？；：")


def _append_language_prompt(messages: list[dict[str, Any]], language_prompt: str) -> list[dict[str, Any]]:
    prompt = str(language_prompt or "").strip()
    if not prompt:
        return messages
    if not messages:
        return [{"role": "system", "content": prompt}]
    first_message = dict(messages[0])
    if str(first_message.get("role") or "").lower() != "system":
        return [{"role": "system", "content": prompt}, *messages]

    content = first_message.get("content")
    if isinstance(content, list):
        first_message["content"] = [*content, {"type": "text", "text": f"\n\n{prompt}"}]
    elif content is None:
        first_message["content"] = prompt
    else:
        first_message["content"] = f"{content}\n\n{prompt}"
    return [first_message, *messages[1:]]


def _merge_prompt_sections(*sections: str) -> str:
    merged_sections: list[str] = []
    seen: set[str] = set()
    for section in sections:
        normalized = str(section or "").strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        merged_sections.append(normalized)
    return "\n\n".join(merged_sections).strip()


def _append_reply_reference_info(raw_tool_calls: Any, language_prompt: str) -> list[dict[str, Any]] | None:
    prompt = str(language_prompt or "").strip()
    if not prompt or not isinstance(raw_tool_calls, list):
        return None

    changed = False
    normalized_tool_calls: list[dict[str, Any]] = []
    for item in raw_tool_calls:
        if not isinstance(item, Mapping):
            return None

        tool_call = dict(item)
        function_info = tool_call.get("function")
        if isinstance(function_info, Mapping):
            function_name = str(function_info.get("name") or "").strip()
            raw_arguments = function_info.get("arguments")
        else:
            function_name = str(tool_call.get("name") or "").strip()
            raw_arguments = tool_call.get("arguments")

        if function_name != "reply":
            normalized_tool_calls.append(tool_call)
            continue

        arguments = dict(raw_arguments) if isinstance(raw_arguments, Mapping) else {}
        current_reference = str(arguments.get("reference_info") or "").strip()
        if prompt in current_reference:
            normalized_tool_calls.append(tool_call)
            continue

        arguments["reference_info"] = f"{current_reference}\n\n{prompt}".strip() if current_reference else prompt
        if isinstance(function_info, Mapping):
            updated_function = dict(function_info)
            updated_function["arguments"] = arguments
            tool_call["function"] = updated_function
        else:
            tool_call["arguments"] = arguments

        changed = True
        normalized_tool_calls.append(tool_call)

    return normalized_tool_calls if changed else None


_DEFAULT_CHINESE_LIVE_SYSTEM_PROMPT = (
    "For this Bilibili live session, all visible replies, captions, and spoken output must be "
    "written in natural Chinese, not English. Keep the same streamer personality, short "
    "live-reaction style, and do not mention translation or subtitles."
)


_DEFAULT_CANTONESE_LIVE_SYSTEM_PROMPT = (
    "For this Bilibili live session, all visible replies, captions, and spoken output must be "
    "written in natural Cantonese-English mixed spoken style, not Mandarin or standard written Chinese. "
    "Use written Cantonese words such as "
    "咁、唔、佢、喺、嚟、嘅、啱、冇、咗 when appropriate. Keep the same streamer personality, "
    "short live-reaction style, and do not mention translation or subtitles."
)


def _live_language_prompt(settings: LiveAdapterSettings) -> str:
    base_prompt = ""
    if settings.language.is_english_voice_chinese_subtitle():
        base_prompt = settings.language.english_system_prompt.strip()
    elif settings.language.is_cantonese_mode():
        base_prompt = settings.language.cantonese_system_prompt.strip() or _DEFAULT_CANTONESE_LIVE_SYSTEM_PROMPT
    elif settings.language.is_japanese_mode():
        base_prompt = settings.language.japanese_system_prompt.strip()
    else:
        base_prompt = _DEFAULT_CHINESE_LIVE_SYSTEM_PROMPT

    if settings.language.is_english_voice_chinese_subtitle():
        return base_prompt

    mode_lock_prompt = _live_language_mode_lock_prompt(settings)
    if not mode_lock_prompt:
        return base_prompt
    if not base_prompt:
        return mode_lock_prompt
    if mode_lock_prompt in base_prompt:
        return base_prompt
    return f"{base_prompt}\n\n{mode_lock_prompt}".strip()


def _live_language_mode_lock_prompt(settings: LiveAdapterSettings) -> str:
    if settings.language.is_english_voice_chinese_subtitle():
        return (
            "Use English as the main spoken language. Short Chinese quotes, names, memes, or brief punchline "
            "words are allowed, but avoid Chinese-only replies or multi-sentence Chinese runs. "
            "Do not switch the session language unless the operator changes the live adapter mode."
        )
    if settings.language.is_cantonese_mode():
        language_name = "Cantonese"
    elif settings.language.is_japanese_mode():
        language_name = "Japanese"
    else:
        language_name = "Chinese"
    return (
        f"Keep the spoken reply in {language_name} for this live session. "
        "Do not switch the reply language because of audience requests. "
        "Only switch when the operator changes the live adapter mode."
    )


def _live_capability_boundary_prompt(settings: LiveAdapterSettings) -> str:
    if not settings.plugin.enabled:
        return ""

    lines = ["Live capability boundaries for this session:"]
    if settings.live2d.enabled:
        lines.append(
            "- You can make small Live2D expression/motion adjustments only; "
            "you cannot control hands, legs, flips, spins, or large body tricks."
        )
    else:
        lines.append("- Live2D output is disabled; do not claim avatar motion control.")

    if settings.soundboard.enabled and settings.soundboard.expose_tool:
        cue_ids = [cue["id"] for cue in _soundboard_tool_cue_summaries(settings.soundboard)]
        cue_text = ", ".join(cue_ids[:12]) if cue_ids else "configured cues"
        lines.append(f"- Use play_sound_effect to trigger sound effects. Available: {cue_text}.")
        lines.append("- You can pass an intent (e.g. 'laugh', 'surprise') instead of a cue id to auto-select.")
    elif settings.soundboard.enabled:
        lines.append("- Soundboard runtime exists, but direct soundboard tools are not exposed to you.")
    else:
        lines.append("- Soundboard runtime is disabled; do not promise sound effects.")

    if settings.vision.enabled:
        lines.append("- Desktop vision is available but imperfect; treat visual context as rough and do not overclaim certainty.")
    else:
        lines.append("- Desktop vision is disabled; do not claim you can see the screen.")

    if settings.sts2.enabled:
        command_list = ", ".join(
            command
            for command in (
                settings.sts2.commands.start_command,
                settings.sts2.commands.stop_command,
                settings.sts2.commands.status_command,
            )
            if command
        )
        lines.append(
            "- You can play or narrate Slay the Spire 2 when it is active, but you cannot start it yourself; "
            f"admin command is required ({command_list or 'configured commands'})."
        )
    else:
        lines.append("- STS2 gameplay integration is disabled; do not claim active Slay the Spire control.")

    lines.append("- You cannot sing or handle song requests.")
    lines.append("- If asked for unsupported control, say it is not enabled.")
    return "\n".join(lines)


def _filter_unavailable_tool_definitions(
    tool_definitions: list[dict[str, Any]],
    settings: LiveAdapterSettings,
    *,
    filter_finish: bool = False,
) -> list[dict[str, Any]]:
    unavailable_tool_names: set[str] = {
        "special_move",
        "control_game",
        "request_rvc_song",
    }
    if not settings.live2d.enabled or not settings.live2d.embodied.enabled:
        unavailable_tool_names.add("special_move")
    if not settings.game.enabled:
        unavailable_tool_names.add("control_game")
    if not settings.song_request.is_available():
        unavailable_tool_names.add("request_rvc_song")
    if not settings.vision.enabled or not settings.vision.expose_tool:
        unavailable_tool_names.add("inspect_desktop")
    if not settings.soundboard.enabled or not settings.soundboard.expose_tool:
        unavailable_tool_names.add("play_sound_effect")
    if filter_finish:
        unavailable_tool_names.add("finish")

    filtered_tools: list[dict[str, Any]] = []
    for tool_definition in tool_definitions:
        if not isinstance(tool_definition, Mapping):
            filtered_tools.append(tool_definition)
            continue
        if _tool_definition_name(tool_definition) in unavailable_tool_names:
            continue
        filtered_tools.append(dict(tool_definition))
    return filtered_tools


def _inject_soundboard_tool_hints(
    tool_definitions: list[dict[str, Any]],
    soundboard_settings: SoundboardConfig,
    *,
    admin_disabled: bool = False,
) -> list[dict[str, Any]]:
    if not soundboard_settings.enabled or not (
        soundboard_settings.expose_tool or soundboard_settings.expose_auto_tool
    ):
        return tool_definitions
    cue_summaries = _soundboard_tool_cue_summaries(soundboard_settings)
    if not cue_summaries:
        return tool_definitions
    hinted_tools: list[dict[str, Any]] = []
    for tool_definition in tool_definitions:
        if not isinstance(tool_definition, Mapping):
            hinted_tools.append(tool_definition)
            continue
        tool_name = _tool_definition_name(tool_definition)
        if tool_name == "play_sound_effect":
            hinted_tools.append(_with_soundboard_tool_hints(tool_definition, cue_summaries, admin_disabled=admin_disabled))
            continue
        if tool_name != "play_sound_effect":
            hinted_tools.append(dict(tool_definition))
            continue
    return hinted_tools


def _ensure_live_soundboard_tools_visible(
    messages: list[dict[str, Any]],
    tool_definitions: list[dict[str, Any]],
    soundboard_settings: SoundboardConfig,
    *,
    admin_disabled: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not soundboard_settings.enabled:
        return messages, tool_definitions
    desired_tool_names: list[str] = []
    if soundboard_settings.expose_tool:
        desired_tool_names.append("play_sound_effect")
    if not desired_tool_names:
        return messages, tool_definitions
    existing_tool_names = {_tool_definition_name(tool_definition) for tool_definition in tool_definitions}
    updated_tools = [dict(tool_definition) for tool_definition in tool_definitions]
    for tool_name in desired_tool_names:
        if tool_name in existing_tool_names:
            continue
        updated_tools.append(_build_live_soundboard_tool_definition(tool_name))
        existing_tool_names.add(tool_name)
    updated_messages = _strip_tools_from_deferred_tools_reminder(messages, set(desired_tool_names))
    return updated_messages, updated_tools


def _build_live_soundboard_tool_definition(tool_name: str) -> dict[str, Any]:
    normalized_tool_name = str(tool_name or "").strip()
    if normalized_tool_name == "play_sound_effect":
        return {
            "name": "play_sound_effect",
            "description": (
                "Play a configured live soundboard cue and show its green-screen media overlay when one exists. "
                "Use this only when you already know the exact cue id you want."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cue": {
                        "type": "string",
                        "description": "Configured cue id or label. Use one of the currently available soundboard cues only.",
                    },
                    "repeat_count": {
                        "type": "integer",
                        "default": 1,
                        "description": "How many times to play this cue for the current reaction.",
                    },
                    "reason": {
                        "type": "string",
                        "default": "",
                        "description": "Brief reason this cue fits the moment.",
                    },
                    "text": {
                        "type": "string",
                        "default": "",
                        "description": "Optional chat or reply text that prompted it.",
                    },
                },
                "required": [],
            },
        }
    return {"name": normalized_tool_name, "description": "", "parameters": {"type": "object", "properties": {}}}


def _strip_tools_from_deferred_tools_reminder(
    messages: list[dict[str, Any]],
    tool_names: set[str],
) -> list[dict[str, Any]]:
    if not messages or not tool_names:
        return messages
    updated_messages: list[dict[str, Any]] = []
    tool_prefixes = tuple(f"{tool_name}:" for tool_name in tool_names)
    for message in messages:
        if not isinstance(message, Mapping):
            updated_messages.append(message)
            continue
        message_copy = dict(message)
        content = message_copy.get("content")
        if not isinstance(content, str) or "<system-reminder>" not in content:
            updated_messages.append(message_copy)
            continue
        filtered_lines = [
            line
            for line in content.splitlines()
            if not any(prefix in line for prefix in tool_prefixes)
        ]
        message_copy["content"] = "\n".join(filtered_lines)
        updated_messages.append(message_copy)
    return updated_messages


def _with_soundboard_tool_hints(
    tool_definition: Mapping[str, Any],
    cue_summaries: list[dict[str, str]],
    *,
    admin_disabled: bool = False,
) -> dict[str, Any]:
    updated = dict(tool_definition)
    function_definition = tool_definition.get("function")
    cue_lines = [
        f"{cue['id']}: {cue['hint']}"
        for cue in cue_summaries
    ]
    disabled_notice = (
        "\n[IMPORTANT: The soundboard is currently ADMIN DISABLED by operator command. "
        "Do NOT call this tool. If chat asks about sound effects, tell them the soundboard is currently muted by the operator.]"
        if admin_disabled else ""
    )
    if isinstance(function_definition, Mapping):
        function_copy = dict(function_definition)
        base_description = str(function_copy.get("description") or "").strip()
        function_copy["description"] = (
            f"{base_description}{disabled_notice} Available cues: " + " | ".join(cue_lines)
        ).strip()
        parameters = function_definition.get("parameters")
        if isinstance(parameters, Mapping):
            parameters_copy = dict(parameters)
            cue_parameter = parameters_copy.get("cue")
            properties = parameters_copy.get("properties")
            properties_copy = dict(properties) if isinstance(properties, Mapping) else None
            if cue_parameter is None and properties_copy is not None:
                cue_parameter = properties_copy.get("cue")
            if isinstance(cue_parameter, Mapping):
                cue_parameter_copy = dict(cue_parameter)
                cue_parameter_copy["enum"] = [cue["id"] for cue in cue_summaries]
                cue_parameter_copy["description"] = (
                    "Cue id to play. Options: " + " | ".join(cue_lines)
                )
                if properties_copy is not None:
                    properties_copy["cue"] = cue_parameter_copy
                    parameters_copy["properties"] = properties_copy
                else:
                    parameters_copy["cue"] = cue_parameter_copy
            function_copy["parameters"] = parameters_copy
        updated["function"] = function_copy
        return updated
    base_description = str(updated.get("description") or "").strip()
    updated["description"] = (
        f"{base_description}{disabled_notice} Available cues: " + " | ".join(cue_lines)
    ).strip()
    parameters = updated.get("parameters")
    if isinstance(parameters, Mapping):
        parameters_copy = dict(parameters)
        properties = parameters_copy.get("properties")
        if isinstance(properties, Mapping):
            properties_copy = dict(properties)
            cue_parameter = properties_copy.get("cue")
            if isinstance(cue_parameter, Mapping):
                cue_parameter_copy = dict(cue_parameter)
                cue_parameter_copy["enum"] = [cue["id"] for cue in cue_summaries]
                cue_parameter_copy["description"] = (
                    "Cue id to play. Options: " + " | ".join(cue_lines)
                )
                properties_copy["cue"] = cue_parameter_copy
                parameters_copy["properties"] = properties_copy
        updated["parameters"] = parameters_copy
    return updated


def _with_soundboard_auto_tool_hints(
    tool_definition: Mapping[str, Any],
    cue_summaries: list[dict[str, str]],
    *,
    admin_disabled: bool = False,
) -> dict[str, Any]:
    updated = dict(tool_definition)
    function_definition = tool_definition.get("function")
    cue_lines = [f"{cue['id']}: {cue['hint']}" for cue in cue_summaries]
    disabled_notice = (
        "\n[IMPORTANT: The soundboard is currently ADMIN DISABLED by operator command. "
        "Do NOT call this tool. If chat asks about sound effects, tell them the soundboard is currently muted by the operator.]"
        if admin_disabled else ""
    )
    if isinstance(function_definition, Mapping):
        function_copy = dict(function_definition)
        base_description = str(function_copy.get("description") or "").strip()
        function_copy["description"] = (
            f"{base_description}{disabled_notice} Available reactions: " + " | ".join(cue_lines)
        ).strip()
        parameters = function_definition.get("parameters")
        if isinstance(parameters, Mapping):
            parameters_copy = dict(parameters)
            properties = parameters_copy.get("properties")
            properties_copy = dict(properties) if isinstance(properties, Mapping) else None
            if properties_copy is not None:
                intent_parameter = properties_copy.get("intent")
                if isinstance(intent_parameter, Mapping):
                    intent_parameter_copy = dict(intent_parameter)
                    intent_parameter_copy["description"] = (
                        "Short reaction intent or vibe. Available reactions: " + " | ".join(cue_lines)
                    )
                    properties_copy["intent"] = intent_parameter_copy
                parameters_copy["properties"] = properties_copy
            function_copy["parameters"] = parameters_copy
        updated["function"] = function_copy
        return updated
    base_description = str(updated.get("description") or "").strip()
    updated["description"] = (
        f"{base_description} Available reactions: " + " | ".join(cue_lines)
    ).strip()
    parameters = updated.get("parameters")
    if isinstance(parameters, Mapping):
        parameters_copy = dict(parameters)
        properties = parameters_copy.get("properties")
        if isinstance(properties, Mapping):
            properties_copy = dict(properties)
            intent_parameter = properties_copy.get("intent")
            if isinstance(intent_parameter, Mapping):
                intent_parameter_copy = dict(intent_parameter)
                intent_parameter_copy["description"] = (
                    "Short reaction intent or vibe. Available reactions: " + " | ".join(cue_lines)
                )
                properties_copy["intent"] = intent_parameter_copy
                parameters_copy["properties"] = properties_copy
        updated["parameters"] = parameters_copy
    return updated


def _soundboard_tool_cue_summaries(soundboard_settings: SoundboardConfig) -> list[dict[str, str]]:
    summaries: list[dict[str, str]] = []
    cue_metadata = list_soundboard_cues(
        soundboard_settings,
        plugin_dir=Path(__file__).resolve().parent,
        available_only=True,
    )
    for cue in cue_metadata:
        cue_id = str(cue.get("id") or "").strip()
        if not cue_id:
            continue
        hint = str(cue.get("usage_hint") or "").strip()
        if not hint:
            keywords = ", ".join(
                str(keyword).strip()
                for keyword in cue.get("keywords", [])
                if str(keyword).strip()
            )
            label = str(cue.get("label") or cue_id).strip()
            hint = f"use when your reply calls for {keywords}" if keywords else f"use for the {label} reaction"
        summaries.append({"id": cue_id, "hint": hint[:160]})
    return summaries[:12]


_SOUNDBOARD_AUTO_TOOL_STOPWORDS = {
    "a",
    "an",
    "and",
    "audio",
    "cue",
    "effect",
    "for",
    "play",
    "reaction",
    "sound",
    "the",
    "to",
    "use",
    "when",
    "音效",
    "声音",
    "播放",
    "效果",
    "反应",
}


def _select_soundboard_auto_tool_cue(
    soundboard_settings: SoundboardConfig,
    *,
    intent: str = "",
    reason: str = "",
    text: str = "",
) -> SoundboardResolvedCue | None:
    plugin_dir = Path(__file__).resolve().parent
    cue_metadata = list_soundboard_cues(
        soundboard_settings,
        plugin_dir=plugin_dir,
        available_only=True,
    )
    if not cue_metadata:
        return None
    resolved_cues: list[SoundboardResolvedCue] = []
    for cue_summary in cue_metadata:
        cue_id = str(cue_summary.get("id") or "").strip()
        if not cue_id:
            continue
        resolved = resolve_soundboard_cue(
            soundboard_settings,
            cue_id,
            plugin_dir=plugin_dir,
            available_only=True,
        )
        if resolved is not None:
            resolved_cues.append(resolved)
    if not resolved_cues:
        return None
    ranked: list[tuple[float, int, SoundboardResolvedCue]] = []
    for index, resolved in enumerate(resolved_cues):
        score = _score_soundboard_auto_tool_cue(
            resolved,
            intent=intent,
            reason=reason,
            text=text,
        )
        if score > 0:
            ranked.append((score, -index, resolved))
    if not ranked:
        return resolved_cues[0] if len(resolved_cues) == 1 else None
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return ranked[0][2]


def _score_soundboard_auto_tool_cue(
    resolved: SoundboardResolvedCue,
    *,
    intent: str = "",
    reason: str = "",
    text: str = "",
) -> float:
    cue = resolved.cue
    score = max(0.0, float(cue.priority) * 0.05)
    for query_text, weight in (
        (intent, 4.0),
        (reason, 3.0),
        (text, 2.0),
    ):
        normalized_query = str(query_text or "").strip()
        if not normalized_query:
            continue
        if soundboard_text_matches_cue(normalized_query, cue):
            score += 140.0 * weight
        if _soundboard_segment_mentions_cue(
            normalized_query,
            cue_id=resolved.cue_id,
            cue_label=cue.label,
        ):
            score += 90.0 * weight
        score += _soundboard_auto_tool_overlap_score(
            normalized_query,
            cue_id=resolved.cue_id,
            cue_label=cue.label,
            keywords=cue.keywords,
            usage_hint=cue.usage_hint,
            weight=weight,
        )
    return score


def _soundboard_auto_tool_overlap_score(
    query_text: str,
    *,
    cue_id: str,
    cue_label: str,
    keywords: Sequence[str],
    usage_hint: str,
    weight: float,
) -> float:
    normalized_query = str(query_text or "").casefold().strip()
    if not normalized_query:
        return 0.0
    cue_id_folded = str(cue_id or "").casefold().strip()
    cue_label_folded = str(cue_label or "").casefold().strip()
    keyword_folds = [str(keyword or "").casefold().strip() for keyword in keywords if str(keyword or "").strip()]
    usage_hint_folded = str(usage_hint or "").casefold().strip()
    score = 0.0
    for term in _soundboard_auto_tool_terms(normalized_query):
        if cue_id_folded:
            if term == cue_id_folded:
                score += 70.0 * weight
            elif term in cue_id_folded or cue_id_folded in normalized_query:
                score += 24.0 * weight
        if cue_label_folded:
            if term == cue_label_folded:
                score += 75.0 * weight
            elif term in cue_label_folded or cue_label_folded in normalized_query:
                score += 26.0 * weight
        for keyword_folded in keyword_folds:
            if term == keyword_folded:
                score += 38.0 * weight
            elif term in keyword_folded or keyword_folded in normalized_query:
                score += 20.0 * weight
        if usage_hint_folded and term in usage_hint_folded:
            score += 9.0 * weight
    return score


def _soundboard_auto_tool_terms(text: str) -> list[str]:
    normalized = str(text or "").casefold().strip()
    if not normalized:
        return []
    raw_terms = re.findall(r"[a-z0-9_.-]+|[\u4e00-\u9fff]{2,}", normalized)
    terms: list[str] = []
    seen: set[str] = set()
    for raw_term in raw_terms:
        term = str(raw_term or "").strip("._-")
        if not term or term in _SOUNDBOARD_AUTO_TOOL_STOPWORDS:
            continue
        if len(term) <= 1 and term.isascii():
            continue
        if term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def _tool_definition_name(tool_definition: Mapping[str, Any]) -> str:
    function_definition = tool_definition.get("function")
    if isinstance(function_definition, Mapping):
        return str(function_definition.get("name") or "").strip()
    return str(tool_definition.get("name") or "").strip()


def _remove_finish_tool_definitions(tool_definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered_tools: list[dict[str, Any]] = []
    for tool_definition in tool_definitions:
        if not isinstance(tool_definition, Mapping):
            filtered_tools.append(tool_definition)
            continue
        function_definition = tool_definition.get("function")
        if isinstance(function_definition, Mapping) and str(function_definition.get("name") or "").strip() == "finish":
            continue
        filtered_tools.append(dict(tool_definition))
    return filtered_tools


def _tool_calls_include_finish(raw_tool_calls: Any) -> bool:
    if not isinstance(raw_tool_calls, list):
        return False
    for item in raw_tool_calls:
        if not isinstance(item, Mapping):
            continue
        function_payload = item.get("function")
        if isinstance(function_payload, Mapping) and str(function_payload.get("name") or "").strip() == "finish":
            return True
    return False


_TTS_INLINE_LATIN_MOJIBAKE_REPLACEMENTS: Mapping[str, str] = {
    "茅": "e",
}


def _sanitize_tts_speech_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return raw
    chars = list(raw)
    sanitized: list[str] = []
    changed = False
    for index, char in enumerate(chars):
        replacement = _TTS_INLINE_LATIN_MOJIBAKE_REPLACEMENTS.get(char)
        if replacement is not None and _has_ascii_alpha_neighbor(chars, index):
            sanitized.append(replacement)
            changed = True
            continue
        if char == "\ufffd":
            changed = True
            continue
        sanitized.append(char)
    if not changed:
        return raw
    cleaned = re.sub(r"\s+", " ", "".join(sanitized)).strip()
    return cleaned or raw


def _has_ascii_alpha_neighbor(chars: Sequence[str], index: int) -> bool:
    previous_char = chars[index - 1] if index > 0 else ""
    next_char = chars[index + 1] if index + 1 < len(chars) else ""
    previous_is_ascii_alpha = previous_char.isascii() and previous_char.isalpha()
    next_is_ascii_alpha = next_char.isascii() and next_char.isalpha()
    return previous_is_ascii_alpha or next_is_ascii_alpha


def _contains_ascii_alpha(text: str) -> bool:
    return any(char.isascii() and char.isalpha() for char in str(text or ""))


def _contains_cjk_character(text: str) -> bool:
    for char in str(text or ""):
        codepoint = ord(char)
        if (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        ):
            return True
    return False


def _romanize_cjk_for_english_tts(text: str) -> str:
    normalized_text = str(text or "")
    if not normalized_text or lazy_pinyin is None or PinyinStyle is None:
        return normalized_text

    def replace_cjk_run(match: re.Match[str]) -> str:
        matched_text = match.group(0)
        syllables = lazy_pinyin(matched_text, style=PinyinStyle.NORMAL, errors="ignore")
        romanized = " ".join(str(syllable).capitalize() for syllable in syllables if syllable)
        if not romanized:
            return matched_text
        start, end = match.span()
        previous_char = normalized_text[start - 1] if start > 0 else ""
        next_char = normalized_text[end] if end < len(normalized_text) else ""
        if previous_char and previous_char.isascii() and previous_char.isalnum():
            romanized = f" {romanized}"
        if next_char and next_char.isascii() and next_char.isalnum():
            romanized = f"{romanized} "
        return romanized

    romanized_text = re.sub(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]+", replace_cjk_run, normalized_text)
    romanized_text = re.sub(r"\s+", " ", romanized_text).strip()
    return romanized_text or normalized_text


def _tts_text_lang(settings: LiveAdapterSettings, *, text: str | None = None) -> str:
    if settings.language.is_english_voice_chinese_subtitle():
        normalized_text = str(text or "").strip()
        if normalized_text and _contains_cjk_character(normalized_text):
            return "zh"
        return "en"
    if settings.language.is_cantonese_mode():
        return str(settings.language.cantonese_tts_text_lang or "").strip() or "yue"
    if settings.language.is_japanese_mode():
        return str(settings.language.japanese_tts_text_lang or "").strip() or "ja"
    return str(settings.tts.text_lang or "").strip() or "zh"


_TTS_FAST_REQUEST_OVERRIDES_BY_LANG: dict[str, dict[str, Any]] = {
    "en": {
        "batch_size": 4,
        "split_bucket": True,
        "parallel_infer": True,
    },
    "zh": {
        "batch_size": 2,
        "split_bucket": True,
        "parallel_infer": True,
    },
}

_TTS_FAST_REQUEST_OVERRIDE_KEYS = frozenset({"batch_size", "split_bucket", "parallel_infer"})


def _tts_language_speed_request_overrides(
    settings: LiveAdapterSettings,
    *,
    text: str | None = None,
    text_lang: Any = None,
) -> dict[str, Any]:
    if settings.language.is_cantonese_mode() or settings.language.is_japanese_mode():
        return {}
    resolved_text_lang = str(text_lang or _tts_text_lang(settings, text=text) or "").strip().lower()
    return dict(_TTS_FAST_REQUEST_OVERRIDES_BY_LANG.get(resolved_text_lang) or {})


def _tts_retry_fallback_plan(
    settings: LiveAdapterSettings,
    *,
    text: str,
    request_overrides: Mapping[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    current_text_lang = str(request_overrides.get("text_lang") or "").strip().lower()
    if not settings.language.is_english_voice_chinese_subtitle():
        return None
    if current_text_lang != "zh":
        return None
    if not (_contains_ascii_alpha(text) and _contains_cjk_character(text)):
        return None
    fallback_text_lang = "en"
    fallback_text = _romanize_cjk_for_english_tts(text)
    fallback_overrides = {
        key: value for key, value in dict(request_overrides).items() if key not in _TTS_FAST_REQUEST_OVERRIDE_KEYS
    }
    fallback_overrides.update(
        _tts_language_speed_request_overrides(
            settings,
            text=text,
            text_lang=fallback_text_lang,
        )
    )
    fallback_overrides["text_lang"] = fallback_text_lang
    return fallback_text, fallback_overrides


def _tts_parallel_infer_enabled(settings: LiveAdapterSettings) -> bool:
    return bool(settings.tts.parallel_infer)


def _tts_request_defaults(settings: LiveAdapterSettings) -> dict[str, Any]:
    request_defaults: dict[str, Any] = {
        "text_lang": _tts_text_lang(settings),
        "ref_audio_path": str(_resolve_optional_path(settings.tts.ref_audio_path) or settings.tts.ref_audio_path),
        "prompt_text": settings.tts.prompt_text,
        "prompt_lang": settings.tts.prompt_lang,
        "text_split_method": settings.tts.text_split_method,
        "top_k": settings.tts.top_k,
        "top_p": settings.tts.top_p,
        "temperature": settings.tts.temperature,
        "speed_factor": settings.tts.speed_factor,
        "split_bucket": _tts_split_bucket_enabled(settings),
        "parallel_infer": _tts_parallel_infer_enabled(settings),
    }
    aux_ref_audio_paths = [
        str(resolved_path)
        for raw_path in settings.tts.aux_ref_audio_paths
        if (resolved_path := _resolve_optional_path(raw_path)) is not None
    ]
    if aux_ref_audio_paths:
        request_defaults["aux_ref_audio_paths"] = aux_ref_audio_paths
    if not settings.language.is_cantonese_mode() and not settings.language.is_japanese_mode():
        request_defaults["batch_size"] = settings.tts.batch_size
        request_defaults["batch_threshold"] = settings.tts.batch_threshold
        request_defaults["seed"] = settings.tts.seed
        request_defaults["repetition_penalty"] = settings.tts.repetition_penalty
    return request_defaults


def _tts_split_bucket_enabled(settings: LiveAdapterSettings) -> bool:
    return bool(settings.tts.split_bucket) and not settings.language.is_cantonese_mode() and not settings.language.is_japanese_mode()


def _should_parallelize_segment_tts(settings: LiveAdapterSettings) -> bool:
    return not settings.language.is_cantonese_mode() and not settings.language.is_japanese_mode()


def _webui_original_text_for_subtitle(
    speech_text: str,
    subtitle_text: str,
    settings: LiveAdapterSettings,
) -> str:
    """Return source text only when the subtitle is a distinct translated line."""

    normalized_speech_text = str(speech_text or "").strip()
    normalized_subtitle_text = str(subtitle_text or "").strip()
    if not settings.language.uses_translated_chinese_subtitle():
        return ""
    if not normalized_speech_text or normalized_speech_text == normalized_subtitle_text:
        return ""
    return normalized_speech_text


def _extract_soundboard_trigger_text(event: Mapping[str, Any]) -> str:
    candidates = (
        event.get("text"),
        event.get("summary"),
        event.get("display_message"),
        event.get("plain_text"),
    )
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _looks_like_soundboard_request_text(text: str) -> bool:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        return False
    folded = normalized_text.casefold()
    if ("sound effect" in folded or "soundboard" in folded) and re.search(
        r"\b(play|drop|hit|queue|use|give|need|want)\b",
        folded,
    ):
        return True
    return bool(
        re.search(
            r"(来|放|播|整|给|上|搞)(个|点|一下|一段|一条)?[^。！？\r\n]{0,16}(音效|声效|特效音)"
            r"|(?:音效|声效|特效音)[^。！？\r\n]{0,12}(来|放|播|整|给|上|搞)"
            r"|放个[^。！？\r\n]{0,18}(声|音效)"
            r"|来点[^。！？\r\n]{0,18}(音效|声效)",
            normalized_text,
        )
    )


def _iter_soundboard_trigger_texts(*candidates: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text:
            continue
        folded = text.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(text)
    return result


def _soundboard_cue_mention_aliases(*, cue_id: str, cue_label: str) -> list[str]:
    return _iter_soundboard_trigger_texts(
        cue_label,
        cue_id,
        str(cue_id or "").replace("_", " "),
        str(cue_id or "").replace("-", " "),
        str(cue_id or "").replace("_", " ").replace("-", " "),
    )


def _soundboard_segment_mentions_cue(segment_text: str, *, cue_id: str, cue_label: str) -> bool:
    normalized_segment_text = str(segment_text or "").strip().casefold()
    if not normalized_segment_text:
        return False
    aliases = _soundboard_cue_mention_aliases(cue_id=cue_id, cue_label=cue_label)
    return any(alias.casefold() in normalized_segment_text for alias in aliases)


def _find_explicit_soundboard_cue_mention(
    soundboard_settings: SoundboardConfig,
    text: str,
    *,
    plugin_dir: Path | None = None,
) -> SoundboardResolvedCue | None:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        return None
    cue_summaries = list_soundboard_cues(
        soundboard_settings,
        plugin_dir=plugin_dir,
        available_only=True,
    )
    matches: list[tuple[int, int, int, SoundboardResolvedCue]] = []
    normalized_text_folded = normalized_text.casefold()
    for index, cue_summary in enumerate(cue_summaries):
        cue_id = str(cue_summary.get("id") or "").strip()
        if not cue_id:
            continue
        cue_label = str(cue_summary.get("label") or cue_id).strip()
        aliases = _soundboard_cue_mention_aliases(cue_id=cue_id, cue_label=cue_label)
        match_lengths = [len(alias) for alias in aliases if alias and alias.casefold() in normalized_text_folded]
        if not match_lengths:
            continue
        resolved = resolve_soundboard_cue(
            soundboard_settings,
            cue_id,
            plugin_dir=plugin_dir,
            available_only=True,
        )
        if resolved is None:
            continue
        matches.append((int(resolved.cue.priority), max(match_lengths), -index, resolved))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return matches[0][3]


def _default_soundboard_target_segment_index(segments: Sequence[str]) -> int:
    segment_count = len([str(segment or "").strip() for segment in segments if str(segment or "").strip()])
    if segment_count <= 0:
        return 1
    return max(1, (segment_count + 1) // 2)


def _sanitize_soundboard_reply_segments(
    segments: Sequence[str],
    *,
    settings: LiveAdapterSettings,
    scheduled_soundboard_triggers_by_segment: Mapping[int, Sequence[PendingSoundboardTrigger]] | None,
) -> list[str]:
    sanitized_segments: list[str] = []
    for index, segment_text in enumerate(segments, start=1):
        sanitized_segments.append(
            _sanitize_soundboard_reply_segment_text(
                segment_text,
                settings=settings,
                planned_triggers=(
                    scheduled_soundboard_triggers_by_segment.get(index)
                    if scheduled_soundboard_triggers_by_segment is not None
                    else None
                ),
            )
        )
    return sanitized_segments


_DEFAULT_BRACKET_PAIRS: list[tuple[str, str]] = [
    ("*", "*"), ("（", "）"), ("【", "】"), ("(", ")"), ("[", "]"),
]

def _build_bracket_regex(bracket_pairs: list[list[str]]) -> list[str]:
    patterns: list[str] = []
    for pair in bracket_pairs:
        if len(pair) != 2:
            continue
        open_b, close_b = str(pair[0]), str(pair[1])
        if not open_b or not close_b:
            continue
        escaped_open = re.escape(open_b)
        escaped_close = re.escape(close_b)
        patterns.append(escaped_open + r"(?P<content>[^" + escaped_close + r"\r\n]{1,200})" + escaped_close)
    if not patterns:
        patterns.append(r"\*(?P<content>[^*\r\n]{1,200})\*")
    return patterns

def _sanitize_soundboard_reply_segment_text(
    text: str,
    *,
    settings: LiveAdapterSettings,
    planned_triggers: Sequence[PendingSoundboardTrigger] | None = None,
) -> str:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        return ""
    plugin_dir = Path(__file__).resolve().parent
    bracket_pairs = getattr(settings.soundboard, "bracket_pairs", None) or _DEFAULT_BRACKET_PAIRS
    patterns = _build_bracket_regex(bracket_pairs)
    working_text = normalized_text
    for _iteration in range(20):
        best_match: re.Match[str] | None = None
        for pattern in patterns:
            match = re.search(pattern, working_text)
            if match is not None and (best_match is None or match.start() < best_match.start()):
                best_match = match
        if best_match is None:
            break
        directive_text = best_match.group("content") or ""
        if not _looks_like_soundboard_stage_direction(
            directive_text,
            settings=settings,
            planned_triggers=planned_triggers,
            plugin_dir=plugin_dir,
        ):
            break
        working_text = (working_text[:best_match.start()] + working_text[best_match.end():]).strip()
    return working_text.strip()


def _extract_soundboard_bot_output_directive(
    text: str,
    *,
    settings: LiveAdapterSettings,
    plugin_dir: Path,
) -> str | None:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        return None
    bracket_pairs = getattr(settings.soundboard, "bracket_pairs", None) or _DEFAULT_BRACKET_PAIRS
    patterns = _build_bracket_regex(bracket_pairs)
    for pattern in patterns:
        match = re.search(pattern, normalized_text)
        if match is not None:
            directive_text = match.group("content") or ""
            if _looks_like_soundboard_stage_direction(
                directive_text,
                settings=settings,
                planned_triggers=None,
                plugin_dir=plugin_dir,
            ):
                return directive_text
    if re.search(r"\bplay_sound_effect\b", normalized_text.casefold()):
        return normalized_text[:200]
    return None


def _looks_like_soundboard_stage_direction(
    directive_text: str,
    *,
    settings: LiveAdapterSettings,
    planned_triggers: Sequence[PendingSoundboardTrigger] | None,
    plugin_dir: Path,
) -> bool:
    normalized_directive = str(directive_text or "").strip()
    if not normalized_directive:
        return False
    if _find_explicit_soundboard_cue_mention(settings.soundboard, normalized_directive, plugin_dir=plugin_dir) is not None:
        return True
    folded_directive = normalized_directive.casefold()
    if any(token in folded_directive for token in ("sound effect", "soundboard", "音效", "音效盒")):
        return True
    if planned_triggers and any(token in folded_directive for token in ("play", "plays", "playing", "trigger", "播放", "放个", "放一下", "来个", "来一个")):
        return True
    return False


def _should_auto_open_soundboard_webui(enabled: bool, url: str, previously_opened_url: str) -> bool:
    normalized_url = str(url or "").strip()
    if not enabled or not normalized_url:
        return False
    return normalized_url != str(previously_opened_url or "").strip()


def _open_local_webui_url(url: str) -> bool:
    normalized_url = str(url or "").strip()
    if not normalized_url:
        return False
    if hasattr(os, "startfile"):
        try:
            os.startfile(normalized_url)  # type: ignore[attr-defined]
            return True
        except OSError:
            pass
    return bool(webbrowser.open_new_tab(normalized_url))


def _plugin_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def _project_root() -> Path:
    start = Path(__file__).resolve()
    for parent in start.parents:
        if (parent / "pyproject.toml").exists() and (parent / "config").exists():
            return parent
    return Path.cwd().resolve()


def _resolve_optional_path(raw_path: str) -> Path | None:
    normalized_path = str(raw_path or "").strip()
    if not normalized_path:
        return None
    return Path(normalized_path).expanduser().resolve()


def _extract_audio_timeline(metadata: Mapping[str, Any] | None, kwargs: Mapping[str, Any]) -> dict[str, Any] | None:
    candidates = []
    if isinstance(metadata, Mapping):
        candidates.append(metadata.get("audio_timeline"))
    candidates.append(kwargs.get("audio_timeline"))
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return dict(candidate)
    return None


def _with_text_visemes(
    audio_timeline: dict[str, Any],
    text: str,
    *,
    settings: LiveAdapterSettings,
) -> dict[str, Any]:
    if not settings.live2d.sync.viseme_timeline_enabled or isinstance(audio_timeline.get("visemes"), list):
        return audio_timeline
    duration_ms = _optional_int(audio_timeline.get("audio_duration_ms")) or 0
    if duration_ms <= 0 or not str(text or "").strip():
        return audio_timeline
    visemes = build_text_viseme_timeline(
        text,
        duration_ms,
        frame_interval_ms=settings.live2d.sync.mouth_update_interval_ms,
        mouth_vowel_shapes=_mouth_vowel_shapes_from_settings(settings),
        mouth_keyframe_transition_ms=settings.live2d.sync.mouth_keyframe_transition_ms,
        mouth_viseme_lead_ms=settings.live2d.sync.mouth_viseme_lead_ms,
    )
    if visemes:
        audio_timeline["visemes"] = visemes
    return audio_timeline


def _mouth_vowel_shapes_from_settings(settings: LiveAdapterSettings) -> dict[str, dict[str, float]]:
    return {
        "a": {"open": settings.live2d.sync.mouth_vowel_a_open, "form": settings.live2d.sync.mouth_vowel_a_form},
        "e": {"open": settings.live2d.sync.mouth_vowel_e_open, "form": settings.live2d.sync.mouth_vowel_e_form},
        "i": {"open": settings.live2d.sync.mouth_vowel_i_open, "form": settings.live2d.sync.mouth_vowel_i_form},
        "o": {"open": settings.live2d.sync.mouth_vowel_o_open, "form": settings.live2d.sync.mouth_vowel_o_form},
        "u": {"open": settings.live2d.sync.mouth_vowel_u_open, "form": settings.live2d.sync.mouth_vowel_u_form},
    }


def _extract_timeline_id(metadata: Mapping[str, Any] | None) -> str:
    if isinstance(metadata, Mapping):
        timeline_id = str(metadata.get("timeline_id") or "").strip()
        if timeline_id:
            return timeline_id
    return uuid4().hex


def _extract_platform(message: Mapping[str, Any]) -> str:
    platform = str(message.get("platform") or "").strip()
    if platform:
        return platform
    message_info = message.get("message_info")
    if isinstance(message_info, Mapping):
        additional_config = message_info.get("additional_config")
        if isinstance(additional_config, Mapping):
            for key in ("platform", "target_platform", "route_platform"):
                value = str(additional_config.get(key) or "").strip()
                if value:
                    return value
    return ""


def _extract_additional_config(metadata: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    message_info = metadata.get("message_info")
    if not isinstance(message_info, Mapping):
        return {}
    additional_config = message_info.get("additional_config")
    if not isinstance(additional_config, Mapping):
        return {}
    return additional_config


def _extract_reply_latency_source_text(metadata: Mapping[str, Any] | None) -> str:
    if not isinstance(metadata, Mapping):
        return ""
    for key in ("processed_plain_text", "display_message", "plain_text"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return extract_live_output_text_from_message(metadata)


def _extract_reply_latency_source_timestamp(metadata: Mapping[str, Any] | None) -> float | None:
    if not isinstance(metadata, Mapping):
        return None
    raw_timestamp = metadata.get("timestamp")
    if raw_timestamp is None:
        return None
    # 复用与 livehub 共享的时间戳归一化实现（兼容毫秒/微秒输入）
    return normalize_epoch_seconds(raw_timestamp)


def _extract_reply_latency_batch_id(metadata: Mapping[str, Any] | None) -> str:
    additional_config = _extract_additional_config(metadata)
    for key in ("replyer_batch_id", "event_id", "source_message_id"):
        value = str(additional_config.get(key) or "").strip()
        if value:
            return value
    if isinstance(metadata, Mapping):
        return str(metadata.get("message_id") or "").strip()
    return ""


def _extract_replyer_batch_identity(metadata: Mapping[str, Any] | None) -> tuple[str, int, int]:
    additional_config = _extract_additional_config(metadata)
    batch_id = str(additional_config.get("replyer_batch_id") or "").strip()
    segment_index = _optional_int(additional_config.get("replyer_segment_index")) or 0
    segment_count = max(1, _optional_int(additional_config.get("replyer_segment_count")) or 0)
    return batch_id, segment_index, segment_count


def _replyer_streaming_segments_from_metadata(metadata: Mapping[str, Any] | None) -> list[str] | None:
    additional_config = _extract_additional_config(metadata)
    raw_segments = additional_config.get("replyer_segments")
    if not isinstance(raw_segments, Sequence) or isinstance(raw_segments, (str, bytes, bytearray)):
        return None
    batch_id, segment_index, segment_count = _extract_replyer_batch_identity(metadata)
    normalized_segments = [str(segment or "").strip() for segment in raw_segments]
    normalized_segments = [segment for segment in normalized_segments if segment]
    if len(normalized_segments) <= 1:
        return None
    if segment_count > 1 and len(normalized_segments) != segment_count:
        return None
    if batch_id and segment_count > 1 and segment_index > 1:
        return None
    return normalized_segments


def _should_suppress_local_voice_self_judgment_reply(text: str, metadata: Mapping[str, Any] | None) -> bool:
    additional_config = _extract_additional_config(metadata)
    if not bool(additional_config.get("local_voice_self_judgment")):
        return False
    normalized = _normalize_local_voice_reply_placeholder_text(text)
    if not normalized:
        return False
    exact_markers = {
        "保持沉默",
        "沉默",
        "不回复",
        "不作回复",
        "不做回应",
        "无需回复",
        "略过",
        "跳过",
        "noreply",
        "silent",
        "staysilent",
        "skip",
        "pass",
    }
    if normalized in exact_markers:
        return True
    short_markers = (
        "保持沉默",
        "不回复",
        "不作回复",
        "不做回应",
        "无需回复",
        "略过",
        "跳过",
        "noreply",
        "silent",
        "staysilent",
    )
    return len(normalized) <= 16 and any(marker in normalized for marker in short_markers)


def _matches_slash_command_prefix(text: str, prefix: str) -> bool:
    normalized_text = str(text or "").strip()
    normalized_prefix = str(prefix or "").strip()
    if not normalized_text or not normalized_prefix:
        return False
    return normalized_text == normalized_prefix or normalized_text.startswith(f"{normalized_prefix} ")


def _clamp_live2d_debug_gain(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 1.0
    return min(1.5, max(0.0, parsed))


def _normalize_local_voice_reply_placeholder_text(text: str) -> str:
    normalized = re.sub(r"[\s\.,，。!！?？:：;；、'\"“”‘’`~\-_/\\|<>\[\]\(\)\{\}【】（）《》]+", "", str(text or ""))
    return normalized.casefold().strip()


def _build_suppressed_local_voice_reply_result(
    *,
    settings: LiveAdapterSettings,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "bilibili_sent": False,
        "timeline_id": _extract_timeline_id(metadata),
        "live2d_synchronized": False,
        "audio_ref": "",
        "audio_duration_ms": 0,
        "webui_published": False,
        "webui_audio_started": False,
        "audio_played_to_vts": False,
        "local_delivery": True,
        "language_mode": settings.language.mode,
        "speech_text": "",
        "subtitle_text": "",
        "delivery_waited": True,
        "suppressed_reply": True,
        "suppression_reason": "local_voice_self_judgment",
    }


def _build_render_metadata(
    message: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if not isinstance(message, Mapping):
        return metadata
    if not isinstance(metadata, Mapping) or not metadata:
        return message
    merged = dict(metadata)
    merged.update(dict(message))
    return merged


def _should_force_interrupt_live_reply(metadata: Mapping[str, Any] | None) -> bool:
    additional_config = _extract_additional_config(metadata)
    live_event_type = str(additional_config.get("live_event_type") or "").strip().lower()
    if live_event_type == "super_chat":
        return True
    if isinstance(metadata, Mapping) and str(metadata.get("paid_event_type") or "").strip().lower() == "super_chat":
        return True
    return False


def _metadata_matches_pending_sts2_decision(
    metadata: Mapping[str, Any] | None,
    controller: Any,
) -> bool:
    additional_config = _extract_additional_config(metadata)
    event_type = str(additional_config.get("live_event_type") or "").strip().lower()
    if event_type != "sts2_decision":
        return False
    metadata_decision_id = str(additional_config.get("sts2_decision_id") or "").strip()
    pending_decision_id = str(getattr(controller, "pending_decision_id", "") or "").strip()
    return bool(metadata_decision_id and pending_decision_id and metadata_decision_id == pending_decision_id)


def _split_sts2_tts_segments(
    text: str,
    metadata: Mapping[str, Any] | None,
    *,
    logger: Any = None,
) -> list[str]:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        return []
    required_segment_count = _required_sts2_segment_count(metadata)
    additional_config = _extract_additional_config(metadata)
    live_event_type = str(additional_config.get("live_event_type") or "").strip().lower()
    if not (bool(additional_config.get("sts2_priority")) or live_event_type.startswith("sts2")):
        return [normalized_text]
    try:
        from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext

        segments = BuiltinToolRuntimeContext.post_process_reply_text(normalized_text)
    except Exception as exc:
        if logger is not None:
            logger.warning(f"Failed to use MaiBot reply post-process for STS2 TTS splitting: {exc}")
        return _ensure_minimum_segment_count([normalized_text], minimum_count=required_segment_count)
    normalized_segments = [str(segment or "").strip() for segment in segments if str(segment or "").strip()]
    if required_segment_count > 1:
        normalized_segments = _restore_terminal_punctuation(normalized_segments, source_text=normalized_text)
    return _ensure_minimum_segment_count(
        normalized_segments or [normalized_text],
        minimum_count=required_segment_count,
    )


def _streaming_reply_segments(
    text: str,
    metadata: Mapping[str, Any] | None,
    *,
    logger: Any = None,
) -> list[str]:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        return []
    sts2_segments = _split_sts2_tts_segments(normalized_text, metadata, logger=logger)
    if len(sts2_segments) > 1:
        return sts2_segments
    normalized_segments = [str(segment or "").strip() for segment in split_text_segments(normalized_text)]
    normalized_segments = [segment for segment in normalized_segments if segment]
    compacted_segments = _compact_streaming_reply_segments(normalized_segments)
    return compacted_segments or [normalized_text]


def _compact_parallel_live_reply_text(
    text: str,
    metadata: Mapping[str, Any] | None,
    *,
    pending_count: int,
    logger: Any = None,
) -> str:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        return normalized_text
    segments = _streaming_reply_segments(normalized_text, metadata, logger=logger)
    if not segments:
        return normalized_text
    max_chars = 28 if pending_count >= 3 else 40
    compacted_text = segments[0]
    if len(segments) > 1 and _streaming_segment_core_length(compacted_text) <= 2:
        compacted_text = f"{compacted_text}{segments[1]}"
    return _truncate_parallel_live_reply_text(compacted_text, max_chars=max_chars)


def _compact_streaming_reply_segments(segments: list[str]) -> list[str]:
    normalized_segments = [str(segment or "").strip() for segment in segments if str(segment or "").strip()]
    if len(normalized_segments) <= 1:
        return normalized_segments
    compacted_segments: list[str] = []
    pending_fragment = ""
    for segment in normalized_segments:
        if _streaming_segment_core_length(segment) <= 2:
            pending_fragment = f"{pending_fragment}{segment}"
            continue
        if pending_fragment:
            segment = f"{pending_fragment}{segment}"
            pending_fragment = ""
        compacted_segments.append(segment)
    if pending_fragment:
        if compacted_segments:
            compacted_segments[-1] = f"{compacted_segments[-1]}{pending_fragment}"
        else:
            compacted_segments.append(pending_fragment)
    return compacted_segments or normalized_segments


def _truncate_parallel_live_reply_text(text: str, *, max_chars: int) -> str:
    normalized_text = str(text or "").strip()
    if len(normalized_text) <= max_chars:
        return normalized_text
    truncated_text = normalized_text[: max(1, max_chars)].rstrip()
    truncated_text = truncated_text.rstrip(",;: ")
    if not truncated_text:
        return normalized_text
    return truncated_text


def _required_sts2_segment_count(metadata: Mapping[str, Any] | None) -> int:
    additional_config = _extract_additional_config(metadata)
    sts2_payload = additional_config.get("sts2_payload")
    if not isinstance(sts2_payload, Mapping):
        return 1
    execution_plan = sts2_payload.get("execution_plan")
    if not isinstance(execution_plan, Mapping):
        return 1
    if str(execution_plan.get("type") or "").strip().lower() != "treasure_chest":
        return 1
    return max(1, int(execution_plan.get("min_segments") or 1))


def _ensure_minimum_segment_count(segments: list[str], *, minimum_count: int) -> list[str]:
    normalized_segments = [str(segment or "") for segment in segments if str(segment or "")]
    target_count = max(1, int(minimum_count))
    if len(normalized_segments) >= target_count:
        return [segment.strip() for segment in normalized_segments if segment.strip()]
    while len(normalized_segments) < target_count:
        split_index = max(range(len(normalized_segments)), key=lambda idx: _streaming_segment_core_length(normalized_segments[idx]))
        split_parts = _split_segment_once(normalized_segments[split_index])
        if len(split_parts) <= 1:
            break
        normalized_segments = (
            normalized_segments[:split_index]
            + split_parts
            + normalized_segments[split_index + 1 :]
        )
    return [segment.strip() for segment in normalized_segments if segment.strip()]


def _restore_terminal_punctuation(segments: list[str], *, source_text: str) -> list[str]:
    normalized_segments = [str(segment or "") for segment in segments if str(segment or "")]
    normalized_source = str(source_text or "").strip()
    if not normalized_segments or not normalized_source:
        return normalized_segments
    trailing_punctuation_chars: list[str] = []
    punctuation_chars = "。！？!?…"
    while normalized_source and normalized_source[-1] in punctuation_chars:
        trailing_punctuation_chars.append(normalized_source[-1])
        normalized_source = normalized_source[:-1]
    if not trailing_punctuation_chars:
        return normalized_segments
    trailing_punctuation = "".join(reversed(trailing_punctuation_chars))
    joined_segments = "".join(segment.strip() for segment in normalized_segments).rstrip()
    if joined_segments.endswith(trailing_punctuation):
        return normalized_segments
    restored_segments = list(normalized_segments)
    restored_segments[-1] = f"{restored_segments[-1].rstrip()}{trailing_punctuation}"
    return restored_segments


def _split_segment_once(text: str) -> list[str]:
    normalized_text = str(text or "")
    if len(normalized_text) <= 1:
        return [normalized_text]
    midpoint = max(1, len(normalized_text) // 2)
    punctuation_candidates = [
        index + 1
        for index, char in enumerate(normalized_text[:-1])
        if char in "锛?銆侊紱;锛?銆傦紒锛??"
    ]
    split_at = min(
        punctuation_candidates,
        key=lambda index: abs(index - midpoint),
        default=midpoint,
    )
    split_at = max(1, min(len(normalized_text) - 1, split_at))
    return [normalized_text[:split_at], normalized_text[split_at:]]


def _chain_callbacks(*callbacks: Callable[[], None] | None) -> Callable[[], None] | None:
    active_callbacks = [callback for callback in callbacks if callback is not None]
    if not active_callbacks:
        return None

    def _callback() -> None:
        for callback in active_callbacks:
            callback()

    return _callback


def _streaming_segment_core_length(text: str) -> int:
    return sum(1 for char in str(text or "") if char.isalnum())


def _tts_output_dir(settings: LiveAdapterSettings) -> Path:
    return Path(_resolve_optional_path(settings.tts.output_dir) or (_plugin_data_dir() / "tts_output")).expanduser()


def _merge_synthesized_speeches(
    speeches: list[SynthesizedSpeech],
    *,
    text: str,
    settings: LiveAdapterSettings,
) -> SynthesizedSpeech | None:
    if not speeches:
        return None
    output_dir = _tts_output_dir(settings)
    output_dir.mkdir(parents=True, exist_ok=True)
    merged_path = output_dir / f"sts2-merged-{uuid4().hex}.wav"
    try:
        sample_signature: tuple[int, int, int, str, str] | None = None
        with wave.open(str(merged_path), "wb") as merged_wav:
            for speech in speeches:
                source_path = Path(speech.audio_ref).expanduser().resolve()
                with wave.open(str(source_path), "rb") as source_wav:
                    current_signature = (
                        source_wav.getnchannels(),
                        source_wav.getsampwidth(),
                        source_wav.getframerate(),
                        source_wav.getcomptype(),
                        source_wav.getcompname(),
                    )
                    if sample_signature is None:
                        sample_signature = current_signature
                        merged_wav.setnchannels(current_signature[0])
                        merged_wav.setsampwidth(current_signature[1])
                        merged_wav.setframerate(current_signature[2])
                        merged_wav.setcomptype(current_signature[3], current_signature[4])
                    elif current_signature != sample_signature:
                        raise ValueError("incompatible wav parameters across segmented STS2 TTS outputs")
                    merged_wav.writeframes(source_wav.readframes(source_wav.getnframes()))
    except Exception:
        try:
            merged_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return build_synthesized_speech_from_wav(
        merged_path,
        text,
        provider=speeches[0].provider,
        amplitude_interval_ms=settings.tts.amplitude_interval_ms,
        amplitude_normalization_enabled=settings.tts.amplitude_normalization_enabled,
        amplitude_noise_floor=settings.tts.amplitude_noise_floor,
        amplitude_peak_percentile=settings.tts.amplitude_peak_percentile,
        amplitude_normalization_gain=settings.tts.amplitude_normalization_gain,
    )


def _build_reply_segment_payload(text: str, speech: SynthesizedSpeech) -> dict[str, Any]:
    payload = speech.to_audio_timeline()
    payload["text"] = str(text or "").strip()
    return payload


def _extract_reply_segment_payloads(audio_timeline: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(audio_timeline, Mapping):
        return []
    raw_segments = audio_timeline.get("reply_segments")
    if not isinstance(raw_segments, list):
        return []
    normalized_segments: list[dict[str, Any]] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, Mapping):
            continue
        segment_text = str(raw_segment.get("text") or "").strip()
        if not segment_text:
            continue
        normalized_segments.append(dict(raw_segment))
    return normalized_segments


def _build_local_render_only_modified_kwargs(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    modified_kwargs = dict(kwargs)
    modified_kwargs["typing"] = False
    modified_kwargs["set_reply"] = False
    modified_kwargs["reply_message_id"] = None
    modified_kwargs["storage_message"] = False
    modified_kwargs["show_log"] = False
    return modified_kwargs


def _segment_audio_from_timeline(audio_timeline: Mapping[str, Any] | None) -> SynthesizedSpeech | None:
    if not isinstance(audio_timeline, Mapping):
        return None
    audio_ref = str(audio_timeline.get("audio_ref") or "").strip()
    if not audio_ref:
        return None
    duration_ms = _optional_int(audio_timeline.get("audio_duration_ms")) or 0
    amplitudes = audio_timeline.get("amplitudes")
    return SynthesizedSpeech(
        provider=str(audio_timeline.get("provider") or "").strip() or "external",
        text="",
        audio_ref=audio_ref,
        audio_duration_ms=max(0, duration_ms),
        sample_rate=0,
        amplitudes=list(amplitudes) if isinstance(amplitudes, list) else [],
        amplitude_stats=dict(audio_timeline.get("amplitude_stats") or {}),
        content_type=str(audio_timeline.get("content_type") or "audio/wav"),
    )


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _uses_vts_native_lip_sync(settings: LiveAdapterSettings) -> bool:
    return False


def _should_play_local_audio(settings: LiveAdapterSettings) -> bool:
    return bool(settings.tts.enabled and settings.tts.audio_playback_enabled)


def _should_expose_webui_audio(settings: LiveAdapterSettings) -> bool:
    return not _should_play_local_audio(settings)


_LIVE2D_EMOTION_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "react_happy",
        (
            "haha",
            "lol",
            "happy",
            "great",
            "amazing",
            "\u54c8\u54c8",
            "\u5f00\u5fc3",
            "\u592a\u597d",
            "\u592a\u68d2",
            "\u597d\u8036",
            "\u559c\u6b22",
            "鍝堝搱",
            "寮€蹇?",
            "澶ソ",
            "澶",
            "濂借€?",
        ),
    ),
    (
        "react_surprised",
        (
            "wow",
            "surprise",
            "surprised",
            "unbelievable",
            "\u54c7",
            "\u60ca\u8bb6",
            "\u9707\u60ca",
            "\u771f\u7684\u5417",
            "\u592a\u79bb\u8c31",
            "鍝?",
            "鎯婅",
            "闇囨儕",
            "鐪熺殑鍚?",
        ),
    ),
    (
        "react_shy",
        (
            "shy",
            "blush",
            "\u5bb3\u7f9e",
            "\u8138\u7ea2",
            "\u817c\u7ea2",
            "\u4e0d\u597d\u610f\u601d",
            "瀹崇緸",
            "鑴哥孩",
            "涓嶅ソ鎰忔€?",
        ),
    ),
    (
        "react_angry",
        (
            "angry",
            "mad",
            "furious",
            "\u751f\u6c14",
            "\u6c14\u6b7b",
            "\u6c14\u70b8",
            "\u706b\u5927",
            "鐢熸皵",
            "姘旀",
        ),
    ),
    (
        "react_sad",
        (
            "sad",
            "cry",
            "upset",
            "\u96be\u8fc7",
            "\u4f24\u5fc3",
            "\u60b2\u4f24",
            "\u60f3\u54ed",
            "闅捐繃",
            "浼ゅ績",
            "鍝?",
        ),
    ),
    (
        "react_confused",
        (
            "confused",
            "why",
            "how",
            "what",
            "\u4e3a\u4ec0\u4e48",
            "\u600e\u4e48",
            "\u600e\u4e48\u56de\u4e8b",
            "\u5565",
            "涓轰粈涔?",
            "鎬庝箞",
            "鍟?",
        ),
    ),
)


def _infer_emotion_intent_impl(text: str) -> str:
    normalized = str(text or "")
    lowered = normalized.lower()
    for intent, hints in _LIVE2D_EMOTION_HINTS:
        if any(hint in normalized or hint in lowered for hint in hints):
            return intent
    return ""


def _infer_emotion_intent(text: str) -> str:
    return _infer_emotion_intent_impl(text)
    lowered = text.lower()
    if any(token in text for token in ("哈哈", "开心", "太棒", "好耶")) or any(token in lowered for token in ("haha", "lol")):
        return "react_happy"
    if any(token in text for token in ("？！", "！", "?", "惊", "震惊")):
        return "react_surprised"
    if any(token in text for token in ("害羞", "脸红", "不好意思")):
        return "react_shy"
    if any(token in text for token in ("？", "?", "怎么", "为什么")):
        return "react_confused"
    return ""

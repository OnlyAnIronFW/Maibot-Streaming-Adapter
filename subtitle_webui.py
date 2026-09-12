"""原生桌面字幕 UI 服务入口。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import asyncio
import time

from .live2d_control_state import DEFAULT_LIVE2D_CONTROL_STATE, Live2DControlState
from .subtitle_native_runtime import (
    ControlStateChangedCallback,
    ShellActionRequestedCallback,
    SubtitleNativeUIRuntime,
)
from .subtitle_native_state import (
    DEFAULT_SUBTITLE_UI_SETTINGS,
    SubtitleUISettingsStore,
    normalize_subtitle_ui_settings,
    subtitle_defaults_to_settings,
)


@dataclass(frozen=True)
class SubtitleSegment:
    """单条字幕分段。"""

    index: int
    text: str
    duration_ms: int
    audio_ref: str = ""
    audio_url: str = ""
    provider: str = ""
    speech_text: str = ""


def estimate_subtitle_duration_ms(text: str, *, chars_per_second: float = 7.5) -> int:
    """在没有真实音频时，估算一条字幕的可读时长。"""

    normalized_text = str(text or "").strip()
    if not normalized_text:
        return 500
    base_duration_ms = int(max(500.0, len(normalized_text) / max(1.0, float(chars_per_second)) * 1000.0))
    punctuation_pause_ms = sum(_punctuation_pause_ms(char) for char in normalized_text)
    return base_duration_ms + punctuation_pause_ms


def build_subtitle_reply_payload(
    *,
    reply_id: str,
    text: str,
    segments: list[SubtitleSegment],
    source_platform: str = "",
) -> dict[str, Any]:
    """构建供原生字幕运行时消费的 reply payload。"""

    return {
        "type": "subtitle.reply",
        "reply_id": reply_id,
        "text": str(text or ""),
        "source_platform": str(source_platform or ""),
        "created_at_ms": int(time.time() * 1000),
        "retention_policy": "trim_oldest",
        "segments": [asdict(segment) for segment in segments],
    }


def build_shell_control_payload(
    *,
    enabled: bool,
    click_through: bool = True,
    interactive: bool = False,
    actions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build a normalized secondary-control payload for the subtitle control window."""

    normalized_actions = [
        str(action).strip()
        for action in (actions or ())
        if str(action).strip()
    ]
    return {
        "enabled": bool(enabled),
        "click_through": bool(click_through),
        "interactive": bool(interactive),
        "actions": normalized_actions,
    }


class SubtitleWebUIService:
    """保持插件侧 API 稳定的原生字幕 UI 服务。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        subtitle_defaults: Mapping[str, Any] | None = None,
        control_state: Live2DControlState | Mapping[str, Any] | None = None,
        on_control_state_changed: ControlStateChangedCallback | None = None,
        shell_controls: Mapping[str, Any] | None = None,
        on_shell_action_requested: ShellActionRequestedCallback | None = None,
        logger: Any = None,
    ) -> None:
        self.host = str(host or "").strip() or "127.0.0.1"
        self.port = max(1, int(port or 18182))
        self.subtitle_defaults = normalize_subtitle_ui_settings(
            subtitle_defaults_to_settings(subtitle_defaults),
            defaults=DEFAULT_SUBTITLE_UI_SETTINGS,
        )
        self.logger = logger
        self.control_state = self._normalize_control_state(control_state)
        self.on_control_state_changed = self._normalize_control_state_callback(on_control_state_changed)
        self.shell_controls = self._normalize_shell_controls(shell_controls)
        self.on_shell_action_requested = self._normalize_shell_action_callback(on_shell_action_requested)
        self._runtime: SubtitleNativeUIRuntime | None = None
        self._audio_start_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._audio_start_events: dict[str, dict[str, Any]] = {}
        self._event_loop: asyncio.AbstractEventLoop | None = None

    @property
    def has_clients(self) -> bool:
        """原生 UI 运行中时视为已连接。"""

        return bool(self._runtime is not None and self._runtime.is_running)

    async def start(self) -> None:
        """启动原生字幕窗口运行时。"""

        if self._runtime is not None and self._runtime.is_running:
            return
        self._event_loop = asyncio.get_running_loop()
        settings_store = SubtitleUISettingsStore(_plugin_data_dir() / "subtitle_ui_settings.json", defaults=self.subtitle_defaults)
        runtime = SubtitleNativeUIRuntime(
            settings_store=settings_store,
            logger=self.logger,
            on_audio_started=self.handle_audio_started,
            control_state=self.control_state,
            on_control_state_changed=self.on_control_state_changed,
            shell_controls=self.shell_controls,
            on_shell_action_requested=self.on_shell_action_requested,
        )
        runtime.start()
        self._runtime = runtime
        self._log_info("Subtitle native UI started")

    async def stop(self) -> None:
        """停止原生字幕运行时，并清理等待中的 ACK。"""

        runtime = self._runtime
        self._runtime = None
        if runtime is not None:
            runtime.stop()
        for waiter in self._audio_start_waiters.values():
            if not waiter.done():
                waiter.cancel()
        self._audio_start_waiters.clear()
        self._audio_start_events.clear()

    def update_shell_controls(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        """Update the secondary shell-control payload pushed into the native control window."""

        normalized = self._normalize_shell_controls(payload)
        self.shell_controls = normalized
        runtime = self._runtime
        if runtime is not None:
            runtime.update_shell_controls(normalized)
        return dict(normalized)

    def show_windows(self) -> None:
        """Bring the subtitle control and overlay windows to the foreground."""

        runtime = self._runtime
        if runtime is not None:
            runtime.show_windows()

    def register_audio_asset(self, path: Path) -> str:
        """注册本地音频资源，原生 UI 直接返回绝对路径。"""

        resolved_path = Path(path).expanduser().resolve()
        if not resolved_path.exists():
            raise FileNotFoundError(f"Subtitle audio asset does not exist: {resolved_path}")
        return str(resolved_path)

    async def publish_reply(
        self,
        *,
        reply_id: str,
        text: str,
        segments: list[SubtitleSegment],
        source_platform: str = "",
    ) -> None:
        """将一条 reply 投递给原生 UI。"""

        if not segments:
            return
        runtime = self._runtime
        if runtime is None:
            return
        payload = build_subtitle_reply_payload(
            reply_id=reply_id,
            text=text,
            segments=segments,
            source_platform=source_platform,
        )
        runtime.enqueue_reply(payload)

    async def wait_for_audio_start(self, reply_id: str, *, timeout_sec: float = 2.5) -> dict[str, Any] | None:
        """等待字幕音频开始事件。"""

        normalized_reply_id = str(reply_id or "").strip()
        if not normalized_reply_id:
            return None
        cached_event = self._audio_start_events.get(normalized_reply_id)
        if cached_event is not None:
            return dict(cached_event)
        waiter = asyncio.get_running_loop().create_future()
        self._audio_start_waiters[normalized_reply_id] = waiter
        try:
            return await asyncio.wait_for(waiter, timeout=max(0.05, float(timeout_sec)))
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None
        finally:
            if self._audio_start_waiters.get(normalized_reply_id) is waiter:
                self._audio_start_waiters.pop(normalized_reply_id, None)

    def handle_audio_started(self, reply_id: str, *, segment_index: int = 0, started_at_ms: int | None = None) -> None:
        """供原生运行时回调，表示某段音频已经开始。"""

        event = {
            "reply_id": str(reply_id or "").strip(),
            "segment_index": int(segment_index),
            "started_at_ms": int(started_at_ms or time.time() * 1000),
            "server_received_at_ms": int(time.time() * 1000),
        }
        if not event["reply_id"]:
            return
        self._audio_start_events[event["reply_id"]] = event
        waiter = self._audio_start_waiters.pop(event["reply_id"], None)
        if waiter is not None and not waiter.done():
            loop = self._event_loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(waiter.set_result, dict(event))
            else:
                waiter.set_result(dict(event))

    def _log_info(self, message: str) -> None:
        if self.logger is not None:
            self.logger.info(message)

    def _log_warning(self, message: str) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(message)

    def _normalize_control_state(
        self,
        control_state: Live2DControlState | Mapping[str, Any] | None,
    ) -> Live2DControlState:
        if isinstance(control_state, Live2DControlState):
            return control_state
        if control_state is None:
            return DEFAULT_LIVE2D_CONTROL_STATE
        normalized = Live2DControlState.from_dict(control_state)
        if normalized is not None:
            return normalized
        self._log_warning("Invalid control_state provided to SubtitleWebUIService; falling back to defaults.")
        return DEFAULT_LIVE2D_CONTROL_STATE

    def _normalize_control_state_callback(
        self,
        callback: ControlStateChangedCallback | None,
    ) -> ControlStateChangedCallback | None:
        if callback is None:
            return None
        if callable(callback):
            return callback
        self._log_warning("Invalid on_control_state_changed callback provided to SubtitleWebUIService; ignoring callback.")
        return None

    def _normalize_shell_controls(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            return build_shell_control_payload(enabled=False)
        return build_shell_control_payload(
            enabled=bool(payload.get("enabled")),
            click_through=bool(payload.get("click_through", True)),
            interactive=bool(payload.get("interactive", False)),
            actions=payload.get("actions") if isinstance(payload.get("actions"), Sequence) else (),
        )

    def _normalize_shell_action_callback(
        self,
        callback: ShellActionRequestedCallback | None,
    ) -> ShellActionRequestedCallback | None:
        if callback is None:
            return None
        if callable(callback):
            def _dispatch(action: str) -> None:
                loop = self._event_loop
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(callback, str(action))
                    return
                callback(str(action))

            return _dispatch
        self._log_warning("Invalid on_shell_action_requested callback provided to SubtitleWebUIService; ignoring callback.")
        return None


def _plugin_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def _punctuation_pause_ms(char: str) -> int:
    if char in ",，、":
        return 120
    if char in ".。!?！？；:：":
        return 250
    return 0

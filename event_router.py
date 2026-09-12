"""Inbound Bilibili live event router."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Coroutine, Mapping, Protocol

import asyncio
import contextlib
import re
import time
from uuid import uuid4

from .config import LiveAdapterSettings
from .constants import GATEWAY_NAME
from .interaction_planner import LiveInteractionPlanner
from .message_codec import build_message_dict, sanitize_model_reserved_tokens
from .topic_extension_client import TopicExtensionClient
from .topic_state import (
    LiveTopicSnapshot,
    LiveTopicStateStore,
    LiveTopicStatus,
    TopicExpansionResult,
    classify_live_topic_status,
)


class _GatewayProtocol(Protocol):
    async def route_message(
        self,
        gateway_name: str,
        message: dict[str, Any],
        *,
        route_metadata: dict[str, Any] | None = None,
        external_message_id: str = "",
        dedupe_key: str = "",
    ) -> bool:
        ...


class _JsonBridgeProtocol(Protocol):
    async def send(self, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        ...


class _Live2DControllerProtocol(Protocol):
    @property
    def is_speaking(self) -> bool:
        ...

    @property
    def bridge(self) -> Any:
        ...


class _STS2ControllerProtocol(Protocol):
    @property
    def is_active(self) -> bool:
        ...

    @property
    def has_pending_decision(self) -> bool:
        ...

    def record_live_event_context(self, event: dict[str, Any]) -> None:
        ...

    async def start_from_command(self, event: dict[str, Any]) -> bool:
        ...

    async def stop_from_command(self, event: dict[str, Any]) -> bool:
        ...

    async def status_from_command(self, event: dict[str, Any]) -> bool:
        ...


class _Live2DDebugCommandHandlerProtocol(Protocol):
    async def __call__(self, event: dict[str, Any]) -> bool:
        ...


class _VisualContextCommandHandlerProtocol(Protocol):
    async def __call__(self, event: dict[str, Any]) -> bool:
        ...


class _VideoWatchEventHandlerProtocol(Protocol):
    async def __call__(self, event: dict[str, Any]) -> bool:
        ...


class _VideoWatchIdleOfferHandlerProtocol(Protocol):
    async def __call__(self) -> bool:
        ...


class _VideoWatchIdleBlockerProtocol(Protocol):
    def __call__(self) -> bool:
        ...


class _Live2DWinkHandlerProtocol(Protocol):
    async def __call__(self, event: dict[str, Any]) -> bool:
        ...


class _Live2DSpecialMoveHandlerProtocol(Protocol):
    async def __call__(self, event: dict[str, Any]) -> bool:
        ...


class _PaidEventHandlerProtocol(Protocol):
    async def __call__(self, event: dict[str, Any]) -> bool:
        ...


class _SoundboardKeywordHandlerProtocol(Protocol):
    async def __call__(self, event: dict[str, Any]) -> bool:
        ...


class _SoundboardCommandHandlerProtocol(Protocol):
    async def __call__(self, event: dict[str, Any]) -> bool:
        ...


@dataclass(frozen=True)
class _IdleTopicPlan:
    prompt: str
    status: LiveTopicStatus
    snapshot: LiveTopicSnapshot
    expansion_result: TopicExpansionResult | None = None


class LiveEventRouter:
    """Filter, plan, and route Bilibili live events."""

    def __init__(
        self,
        *,
        gateway: _GatewayProtocol,
        settings: LiveAdapterSettings,
        planner: LiveInteractionPlanner,
        live2d_controller: _Live2DControllerProtocol | None = None,
        game_bridge: _JsonBridgeProtocol | None = None,
        sts2_controller: _STS2ControllerProtocol | None = None,
        live2d_debug_command_handler: _Live2DDebugCommandHandlerProtocol | None = None,
        visual_context_command_handler: _VisualContextCommandHandlerProtocol | None = None,
        video_watch_event_handler: _VideoWatchEventHandlerProtocol | None = None,
        video_watch_idle_offer_handler: _VideoWatchIdleOfferHandlerProtocol | None = None,
        video_watch_idle_blocker: _VideoWatchIdleBlockerProtocol | None = None,
        live2d_wink_handler: _Live2DWinkHandlerProtocol | None = None,
        live2d_special_move_handler: _Live2DSpecialMoveHandlerProtocol | None = None,
        paid_event_handler: _PaidEventHandlerProtocol | None = None,
        soundboard_keyword_handler: _SoundboardKeywordHandlerProtocol | None = None,
        soundboard_command_handler: _SoundboardCommandHandlerProtocol | None = None,
        topic_extension_client: TopicExtensionClient | None = None,
        topic_state_store: LiveTopicStateStore | None = None,
        logger: Any = None,
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.planner = planner
        self.live2d_controller = live2d_controller
        self.game_bridge = game_bridge
        self.sts2_controller = sts2_controller
        self.live2d_debug_command_handler = live2d_debug_command_handler
        self.visual_context_command_handler = visual_context_command_handler
        self.video_watch_event_handler = video_watch_event_handler
        self.video_watch_idle_offer_handler = video_watch_idle_offer_handler
        self.video_watch_idle_blocker = video_watch_idle_blocker
        self.live2d_wink_handler = live2d_wink_handler
        self.live2d_special_move_handler = live2d_special_move_handler
        self.paid_event_handler = paid_event_handler
        self.soundboard_keyword_handler = soundboard_keyword_handler
        self.soundboard_command_handler = soundboard_command_handler
        self.topic_extension_client = topic_extension_client or TopicExtensionClient(
            self.settings.interaction.topic_extension,
            logger=logger,
        )
        default_state_path = Path(__file__).resolve().parent / "data" / "live_topic_state.json"
        self.topic_state_store = topic_state_store or LiveTopicStateStore(default_state_path)
        self.logger = logger
        self._buffer: list[dict[str, Any]] = []
        self._seen_ids: dict[str, float] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._inflight_route_tasks: dict[asyncio.Task[dict[str, Any] | None], int] = {}
        self._route_task_sequence = 0
        self._flush_task: asyncio.Task[None] | None = None
        self._idle_topic_task: asyncio.Task[None] | None = None
        self._live_reply_busy_timeout_task: asyncio.Task[None] | None = None
        self._recent_live_records: list[dict[str, Any]] = []
        self._recent_bot_outputs: list[str] = []
        self._recent_topic_timeline: list[dict[str, Any]] = []
        self._topic_context_sequence = 0
        self._last_bot_output_at = 0.0
        self._last_routeable_live_event_at = 0.0
        self._last_idle_topic_injected_at = 0.0
        self._live_reply_busy_count = 0
        self._idle_topic_cooldown_active = False
        self._topic_snapshot = self._restore_topic_snapshot()
        if self._topic_snapshot is not None:
            self._last_routeable_live_event_at = max(self._last_routeable_live_event_at, self._topic_snapshot.updated_at)
            if self._topic_snapshot.recent_bot_outputs:
                self._last_bot_output_at = max(self._last_bot_output_at, self._topic_snapshot.updated_at)

    async def handle_event(self, event: Mapping[str, Any]) -> None:
        """Handle one normalized Bilibili live event."""

        normalized_event = self._sanitize_inbound_event(dict(event))
        self._log_inbound_event(normalized_event)
        if self._is_duplicate_event(normalized_event):
            self._log_planner_event_status(
                normalized_event,
                selected=False,
                routed=False,
                accepted=False,
                hold_reason="duplicate_event",
                event_index=-1,
            )
            return
        if _allows_command_side_effects(normalized_event):
            if await self._handle_video_watch_event(normalized_event):
                return
            if await self._handle_sts2_command(normalized_event):
                return
            if await self._handle_live2d_debug_command(normalized_event):
                return
            if await self._handle_visual_context_command(normalized_event):
                return
            if await self._handle_soundboard_command(normalized_event):
                return
            await self._handle_live2d_special_move_request(normalized_event)
            await self._handle_live2d_wink_request(normalized_event)
        soundboard_requested = await self._handle_soundboard_keyword_trigger(normalized_event)
        if not self._has_direct_injection_payload(normalized_event):
            self._log_planner_event_status(
                normalized_event,
                selected=False,
                routed=False,
                accepted=False,
                hold_reason="empty_after_sanitization",
                event_index=-1,
            )
            return
        if self._counts_as_live_activity(normalized_event):
            self._record_live_activity()
        self._record_recent_live_event(normalized_event)
        self._record_sts2_live_context(normalized_event)
        self._start_background_task(
            self._forward_environment_event(normalized_event),
            name="bilibili_live.forward_environment",
        )
        if _is_hard_paid_priority_event(normalized_event):
            self._start_route_task(
                normalized_event,
                selection_reason="paid_priority",
                selection_score=9999.0,
            )
            await asyncio.sleep(0)
            return
        if soundboard_requested and _should_force_route_soundboard_request(normalized_event, settings=self.settings):
            self._start_route_task(
                normalized_event,
                selection_reason="soundboard_request",
                selection_score=9000.0,
            )
            await asyncio.sleep(0)
            return
        if self._should_collect_for_sts2_decision(normalized_event):
            self._log_planner_event_status(
                normalized_event,
                selected=False,
                routed=False,
                accepted=False,
                hold_reason="sts2_collected",
                event_index=-1,
                selection_reason="sts2_collect",
                selection_score=9999.0,
            )
            return
        self._buffer.append(normalized_event)
        self._buffer = self._buffer[-self.settings.interaction.max_batch_size :]
        if await self.planner.should_flush_immediately(list(self._buffer)):
            if self._flush_task is not None and not self._flush_task.done():
                self._flush_task.cancel()
            self._flush_task = None
            await self.flush_window()
            return
        self._schedule_flush()

    async def flush_window(self) -> list[dict[str, Any]]:
        """Flush the current selection window and wait for selected routes to finish."""

        events = self._buffer
        self._buffer = []
        selected_count = 0
        if events:
            selected = await self.planner.select(events, ai_speaking=self._is_ai_speaking())
            selected_count = len(selected)
            for item in selected:
                event_dict = dict(item.event)
                context_danmaku = self._extract_context_danmaku(event_dict, events)
                if context_danmaku:
                    event_dict["context_danmaku"] = context_danmaku
                self._start_route_task(
                    event_dict,
                    selection_reason=item.reason,
                    selection_score=item.score,
                )

        tasks = tuple(
            task
            for task, _sequence in sorted(self._inflight_route_tasks.items(), key=lambda item: item[1])
            if not task.done()
        )
        if not tasks:
            self._log_planner_flush(buffered_count=len(events), selected_count=selected_count, routed_count=0)
            return []
        results = await asyncio.gather(*tasks, return_exceptions=False)
        routed = [message for message in results if message is not None]
        self._log_planner_flush(
            buffered_count=len(events),
            selected_count=selected_count,
            routed_count=len(routed),
        )
        return routed

    @staticmethod
    def _extract_context_danmaku(
        event: Mapping[str, Any],
        window_events: list[dict[str, Any]],
        *,
        max_context: int = 2,
    ) -> list[dict[str, Any]]:
        """Extract preceding danmaku from the same window as context for referent resolution.

        Returns up to max_context events that appeared immediately before the target event
        in the flush window, so the LLM can understand what the viewer is responding to.
        """
        event_id = str(event.get("event_id") or "").strip()
        if not event_id or not window_events:
            return []
        target_index = -1
        for idx, ev in enumerate(window_events):
            if str(ev.get("event_id") or "").strip() == event_id:
                target_index = idx
                break
        if target_index <= 0:
            return []
        start = max(0, target_index - max_context)
        context = []
        for ev in window_events[start:target_index]:
            ev_type = str(ev.get("type") or "")
            ev_text = str(ev.get("text") or "").strip()
            if not ev_text:
                continue
            if ev_type in ("super_chat", "gift", "guard"):
                continue
            context.append({
                "username": str(ev.get("username") or "anonymous").strip(),
                "text": ev_text,
            })
        return context

    def start_idle_topic_watch(self) -> None:
        """Start the idle-topic timer for a connected live room."""

        self._schedule_idle_topic(restart=True)

    def stop_idle_topic_watch(self) -> None:
        """Stop the idle-topic timer without clearing other route state."""

        if self._idle_topic_task is not None:
            self._idle_topic_task.cancel()
        self._idle_topic_task = None

    def reset(self) -> None:
        """Clear route buffers and cancel pending timers."""

        self._buffer.clear()
        self._seen_ids.clear()
        self._recent_live_records.clear()
        self._recent_bot_outputs.clear()
        self._recent_topic_timeline.clear()
        self._topic_context_sequence = 0
        self._route_task_sequence = 0
        self._last_bot_output_at = 0.0
        self._last_routeable_live_event_at = 0.0
        self._last_idle_topic_injected_at = 0.0
        self._idle_topic_cooldown_active = False
        self._live_reply_busy_count = 0
        if self._flush_task is not None:
            self._flush_task.cancel()
        self._flush_task = None
        self._cancel_tracked_tasks(self._background_tasks)
        self._cancel_tracked_tasks(self._inflight_route_tasks)
        self._cancel_live_reply_busy_timeout()
        self.stop_idle_topic_watch()

    def record_bot_output_history(self, text: str) -> None:
        """Remember recent bot output so the next topic prompt can avoid repetition."""
        normalized_text = _normalize_context_text(text, max_length=180)
        if not normalized_text:
            return
        if not self._recent_bot_outputs or self._recent_bot_outputs[-1] != normalized_text:
            self._recent_bot_outputs.append(normalized_text)
            limit = max(1, int(self.settings.interaction.idle_topic_history_limit))
            self._recent_bot_outputs = self._recent_bot_outputs[-limit:]
            self._append_topic_timeline({"role": "bot", "text": normalized_text})
        self._sync_topic_snapshot(
            current_topic=self._current_topic_hint(),
            recent_bot_outputs=self._recent_bot_outputs,
        )
        current_task: asyncio.Task[Any] | None = None
        with contextlib.suppress(RuntimeError):
            current_task = asyncio.current_task()
        if self._idle_topic_task is not None and self._idle_topic_task is not current_task:
            self._schedule_idle_topic(restart=True)

    def record_live_reply_output_started(self, reason: str = "output_started") -> None:
        """Mark the live reply pipeline as busy until playback/rendering fully ends."""

        del reason
        self._live_reply_busy_count += 1
        self._cancel_live_reply_busy_timeout()
        self._schedule_live_reply_busy_timeout()
        self.stop_idle_topic_watch()

    def record_idle_topic_reply(self, text: str) -> None:
        """Backward-compatible alias for older callers."""

        self.record_bot_output_history(text)

    def record_live_reply_finished_without_output(self, reason: str = "no_output") -> None:
        """Release the live reply busy marker when no more local delivery work remains."""

        completion_reason = str(reason or "").strip()
        if completion_reason and completion_reason not in {
            "no_output",
            "suppressed_reply",
            "delivery_error",
            "delivery_cancelled",
            "delivery_wait_failed",
            "delivery_wait_cancelled",
        }:
            self._last_bot_output_at = time.time()
            self._idle_topic_cooldown_active = False
            advance_pending_anchor = getattr(self.planner, "advance_pending_anchor", None)
            if callable(advance_pending_anchor):
                with contextlib.suppress(Exception):
                    advance_pending_anchor(when=self._last_bot_output_at)
        if self._live_reply_busy_count > 0:
            self._live_reply_busy_count -= 1
        if self._live_reply_busy_count <= 0:
            self._live_reply_busy_count = 0
            self._cancel_live_reply_busy_timeout()
            self._schedule_idle_topic(restart=True)

    def _is_duplicate_event(self, event: Mapping[str, Any]) -> bool:
        event_id = str(event.get("event_id") or "").strip()
        if event_id:
            now = time.time()
            self._seen_ids = {key: stamp for key, stamp in self._seen_ids.items() if now - stamp < 120.0}
            if event_id in self._seen_ids:
                return True
            self._seen_ids[event_id] = now
        return False

    @staticmethod
    def _has_direct_injection_payload(event: Mapping[str, Any]) -> bool:
        text = _strip_markup_only_text(str(event.get("text") or event.get("summary") or "").strip())
        return bool(text)

    @staticmethod
    def _counts_as_live_activity(event: Mapping[str, Any]) -> bool:
        event_type = str(event.get("type") or "").strip()
        if event_type not in {"danmaku", "super_chat", "gift", "guard", "hub_local_input", "hub_bot_reply"}:
            return False
        if event_type in {"gift", "guard"}:
            return True
        text = str(event.get("text") or event.get("summary") or "").strip()
        return bool(text)

    def _record_recent_live_event(self, event: Mapping[str, Any]) -> None:
        event_type = str(event.get("type") or "").strip()
        if event_type not in {"danmaku", "super_chat", "gift", "guard", "hub_local_input", "hub_bot_reply"}:
            return
        text = _normalize_context_text(str(event.get("text") or event.get("summary") or ""), max_length=140)
        if not text:
            return
        username = _normalize_context_text(str(event.get("username") or event.get("user_id") or "anonymous"), max_length=32)
        self._recent_live_records.append(
            {
                "type": event_type,
                "username": username or "anonymous",
                "text": text,
            }
        )
        limit = max(1, int(self.settings.interaction.idle_topic_context_limit))
        self._recent_live_records = self._recent_live_records[-limit:]
        self._append_topic_timeline(
            {
                "role": "live",
                "type": event_type,
                "username": username or "anonymous",
                "text": text,
            }
        )
        self._sync_topic_snapshot(
            current_topic=self._current_topic_hint(),
            recent_viewer_messages=self._recent_viewer_message_texts(),
        )

    def _append_topic_timeline(self, record: dict[str, Any]) -> None:
        self._topic_context_sequence += 1
        record["sequence"] = self._topic_context_sequence
        self._recent_topic_timeline.append(record)
        live_limit = max(1, int(self.settings.interaction.idle_topic_context_limit))
        bot_limit = max(1, int(self.settings.interaction.idle_topic_history_limit))
        self._recent_topic_timeline = self._recent_topic_timeline[-(live_limit + bot_limit) :]

    @staticmethod
    def _sanitize_inbound_event(event: dict[str, Any]) -> dict[str, Any]:
        for key in ("text", "summary"):
            if key in event:
                event[key] = sanitize_model_reserved_tokens(str(event.get(key) or ""))
        return event

    def _record_sts2_live_context(self, event: dict[str, Any]) -> None:
        if not self.settings.sts2.enabled or self.sts2_controller is None:
            return
        if not self.sts2_controller.is_active:
            return
        recorder = getattr(self.sts2_controller, "record_live_event_context", None)
        if not callable(recorder):
            return
        with contextlib.suppress(Exception):
            recorder(dict(event))

    async def _forward_environment_event(self, event: Mapping[str, Any]) -> None:
        if self.settings.live2d.enabled and self.settings.live2d.forward_inbound_danmaku and self.live2d_controller:
            with contextlib.suppress(Exception):
                await self.live2d_controller.bridge.send_event(
                    {
                        "type": "live.danmaku",
                        "event": dict(event),
                    }
                )
        if self.settings.game.enabled and self.settings.game.forward_inbound_danmaku and self.game_bridge:
            with contextlib.suppress(Exception):
                await self.game_bridge.send("danmaku", {"event": dict(event)})

    async def _handle_live2d_wink_request(self, event: Mapping[str, Any]) -> bool:
        if self.live2d_wink_handler is None:
            return False
        text = str(event.get("text") or event.get("summary") or "").strip()
        if not _is_explicit_wink_request(text):
            return False
        wink_event = dict(event)
        wink_event["wink_side"] = _extract_wink_side(text)
        self._start_background_task(
            self.live2d_wink_handler(wink_event),
            name="bilibili_live.live2d_wink",
        )
        return True

    async def _handle_live2d_special_move_request(self, event: Mapping[str, Any]) -> bool:
        text = str(event.get("text") or event.get("summary") or "").strip()
        move = _extract_special_live2d_move(text)
        if move is None:
            return False
        special_move_event = dict(event)
        special_move_event["live2d_action"] = {
            "action": "Special_move",
            "move": move,
            "duration_sec": 10.0,
        }
        if isinstance(event, dict):
            event["live2d_action"] = dict(special_move_event["live2d_action"])
        if self.live2d_special_move_handler is None:
            return False
        self._start_background_task(
            self.live2d_special_move_handler(special_move_event),
            name="bilibili_live.live2d_special_move",
        )
        return True

    def _start_background_task(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _schedule_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = asyncio.create_task(self._delayed_flush(), name="bilibili_live.router_flush")

    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(max(0.05, float(self.settings.interaction.window_seconds)))
            await self.flush_window()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self.logger is not None:
                self.logger.warning(f"Bilibili live event routing failed: {exc}")
        finally:
            if self._flush_task is asyncio.current_task():
                self._flush_task = None

    def _start_route_task(
        self,
        event: Mapping[str, Any],
        *,
        selection_reason: str = "direct_injection",
        selection_score: float = 1.0,
    ) -> None:
        task = asyncio.create_task(
            self._route_live_event(
                dict(event),
                selection_reason=selection_reason,
                selection_score=selection_score,
            ),
            name="bilibili_live.route_event",
        )
        self._route_task_sequence += 1
        self._inflight_route_tasks[task] = self._route_task_sequence
        task.add_done_callback(self._forget_inflight_route_task)

    def _forget_inflight_route_task(self, task: asyncio.Task[dict[str, Any] | None]) -> None:
        self._inflight_route_tasks.pop(task, None)
        if not self._inflight_route_tasks:
            self._schedule_idle_topic(restart=True)

    @staticmethod
    def _cancel_tracked_tasks(tasks: set[asyncio.Task[Any]] | dict[asyncio.Task[Any], Any]) -> None:
        current_task: asyncio.Task[Any] | None = None
        with contextlib.suppress(RuntimeError):
            current_task = asyncio.current_task()
        tracked_tasks = tuple(tasks.keys()) if isinstance(tasks, dict) else tuple(tasks)
        for task in tracked_tasks:
            if task is current_task or task.done():
                continue
            task.cancel()
        tasks.clear()

    async def _route_live_event(
        self,
        event: dict[str, Any],
        *,
        selection_reason: str = "direct_injection",
        selection_score: float = 1.0,
    ) -> dict[str, Any] | None:
        try:
            event_type = str(event.get("type") or "").strip()
            if event_type in {"super_chat", "gift", "guard"}:
                paid_acknowledged = await self._handle_paid_event(event)
                if paid_acknowledged and event_type in {"gift", "guard"}:
                    message = build_message_dict(event, self.settings, reason=selection_reason)
                    self._log_planner_event_status(
                        event,
                        selected=True,
                        routed=True,
                        accepted=True,
                        hold_reason="",
                        event_index=-1,
                        selection_reason=selection_reason,
                        selection_score=selection_score,
                    )
                    return message
            message = build_message_dict(event, self.settings, reason=selection_reason)
            route_metadata = {
                "source": "bilibili_live",
                "room_id": self.settings.bilibili.room_id,
                "selection_reason": selection_reason,
                "selection_score": selection_score,
            }
            accepted = await self.gateway.route_message(
                GATEWAY_NAME,
                message,
                route_metadata=route_metadata,
                external_message_id=str(event.get("event_id") or ""),
                dedupe_key=str(event.get("event_id") or ""),
            )
        except asyncio.CancelledError:
            return None
        except Exception as exc:
            if self.logger is not None:
                event_id = str(event.get("event_id") or "").strip() or "-"
                self.logger.warning(f"Bilibili live event routing failed: event_id={event_id} error={exc}")
            self._log_planner_event_status(
                event,
                selected=True,
                routed=False,
                accepted=False,
                hold_reason="route_error",
                event_index=-1,
                selection_reason=selection_reason,
                selection_score=selection_score,
            )
            return None
        routed = bool(accepted)
        self._log_planner_event_status(
            event,
            selected=True,
            routed=routed,
            accepted=routed,
            hold_reason="" if routed else "gateway_rejected",
            event_index=-1,
            selection_reason=selection_reason,
            selection_score=selection_score,
        )
        return message if routed else None

    async def _handle_paid_event(self, event: dict[str, Any]) -> bool:
        if self.paid_event_handler is None:
            return False
        try:
            return bool(await self.paid_event_handler(dict(event)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.logger is not None:
                event_id = str(event.get("event_id") or "").strip() or "-"
                self.logger.warning(f"Paid live event acknowledgement failed: event_id={event_id} error={exc}")
            return False

    def _schedule_idle_topic(self, *, restart: bool = False, delay_sec: float | None = None) -> None:
        if not self._idle_topic_enabled():
            self.stop_idle_topic_watch()
            return
        if restart:
            self.stop_idle_topic_watch()
        elif self._idle_topic_task is not None and not self._idle_topic_task.done():
            return
        self._idle_topic_task = asyncio.create_task(
            self._idle_topic_wait_once(delay_sec=delay_sec),
            name="bilibili_live.idle_topic",
        )

    async def _idle_topic_wait_once(self, *, delay_sec: float | None = None) -> None:
        normalized_delay_sec = max(
            0.05,
            float(delay_sec if delay_sec is not None else self.settings.interaction.idle_topic_after_sec),
        )
        retry_delay_sec: float | None = None
        try:
            await asyncio.sleep(normalized_delay_sec)
            try:
                routed = await self._route_idle_topic()
                if not routed:
                    retry_delay_sec = await self._compute_idle_topic_retry_delay()
                    if retry_delay_sec is not None and self.logger is not None:
                        self.logger.info(
                            "Bilibili idle topic retry scheduled: "
                            f"room_id={self.settings.bilibili.room_id} delay_sec={retry_delay_sec:.2f}"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.logger is not None:
                    self.logger.warning(f"Bilibili idle topic routing failed: {exc}")
        finally:
            if self._idle_topic_task is asyncio.current_task():
                self._idle_topic_task = None
            if retry_delay_sec is not None and self._idle_topic_enabled():
                self._schedule_idle_topic(restart=True, delay_sec=retry_delay_sec)

    async def _route_idle_topic(self, *, respect_pending_live_reply: bool = False) -> bool:
        del respect_pending_live_reply
        eligible, blockers, diagnostics = await self._evaluate_idle_topic_eligibility()
        if not eligible:
            self._log_idle_topic_skip(blockers=blockers, diagnostics=diagnostics)
            return False
        if await self._maybe_offer_idle_video_watch():
            return True
        plan = await self._build_idle_topic_plan()
        if plan is None:
            self._log_idle_topic_skip(
                blockers=["no_plan"],
                diagnostics={
                    "recent_viewer_messages": len(self._recent_viewer_message_texts()),
                    "recent_bot_outputs": len(self._recent_bot_outputs),
                    "timeline_records": len(self._recent_topic_timeline),
                },
            )
            return False
        prompt = plan.prompt
        event_id = f"bilibili-idle-topic-{uuid4().hex}"
        event = {
            "event_id": event_id,
            "type": "idle_topic",
            "text": prompt,
            "summary": prompt,
            "user_id": "bilibili-live-idle",
            "username": "\u76f4\u64ad\u95f4",
            "timestamp": time.time(),
        }
        message = build_message_dict(event, self.settings, reason="idle_topic")
        route_metadata = {
            "source": "bilibili_live",
            "room_id": self.settings.bilibili.room_id,
            "selection_reason": "idle_topic",
            "selection_score": 0.0,
        }
        accepted = await self.gateway.route_message(
            GATEWAY_NAME,
            message,
            route_metadata=route_metadata,
            external_message_id=event_id,
            dedupe_key=event_id,
        )
        if accepted:
            self._idle_topic_cooldown_active = True
            self._last_idle_topic_injected_at = time.time()
            self._topic_snapshot = plan.snapshot
            self._persist_topic_snapshot()
            record_injection = getattr(self.planner, "record_external_injection", None)
            if callable(record_injection):
                with contextlib.suppress(Exception):
                    record_injection()
            if self.logger is not None:
                self.logger.info(
                    "Bilibili idle topic injected: "
                    f"room_id={self.settings.bilibili.room_id} event_id={event_id} status={plan.status.value}"
                )
            return True
        if self.logger is not None:
            self.logger.info(
                "Bilibili idle topic gateway rejected: "
                f"room_id={self.settings.bilibili.room_id} event_id={event_id} status={plan.status.value}"
            )
        return False

    async def _is_idle_topic_eligible(self) -> bool:
        eligible, _blockers, _diagnostics = await self._evaluate_idle_topic_eligibility()
        return eligible

    async def _evaluate_idle_topic_eligibility(self) -> tuple[bool, list[str], dict[str, Any]]:
        blockers: list[str] = []
        if self._should_defer_for_sts2():
            blockers.append("sts2_defer")
        if self._video_watch_should_block_idle_topic():
            blockers.append("video_watch_active")
        if self._idle_topic_cooldown_active:
            blockers.append("cooldown_active")
        if self._is_ai_speaking():
            blockers.append("ai_speaking")
        if self._live_reply_busy_count > 0:
            blockers.append("live_reply_busy")
        if self._inflight_route_tasks:
            blockers.append("inflight_routes")
        quiet_seconds = max(0.05, float(self.settings.interaction.idle_topic_after_sec))
        now = time.time()
        pending_count = await self.planner.get_pending_message_count()
        last_live_activity_at = self._last_routeable_live_event_at
        if last_live_activity_at <= 0 and self._topic_snapshot is not None:
            last_live_activity_at = float(self._topic_snapshot.updated_at or 0.0)
        if last_live_activity_at <= 0:
            blockers.append("no_live_activity")
        last_live_age_sec: float | None = None
        if last_live_activity_at > 0:
            last_live_age_sec = max(0.0, now - last_live_activity_at)
            if last_live_age_sec < quiet_seconds:
                blockers.append("quiet_window_live")
        last_bot_age_sec: float | None = None
        if self._last_bot_output_at > 0:
            last_bot_age_sec = max(0.0, now - self._last_bot_output_at)
            if last_bot_age_sec < quiet_seconds:
                blockers.append("quiet_window_bot")
        if self._sts2_has_pending_decision():
            blockers.append("sts2_pending")
        pending_requires_clear = False
        if pending_count is not None and int(pending_count) > 0:
            pending_requires_clear = last_live_activity_at > max(
                self._last_bot_output_at,
                self._last_idle_topic_injected_at,
            )
            if pending_requires_clear:
                blockers.append(f"pending_messages:{int(pending_count)}")
        diagnostics = {
            "pending_count": pending_count,
            "pending_requires_clear": pending_requires_clear,
            "last_live_age_sec": last_live_age_sec,
            "last_bot_age_sec": last_bot_age_sec,
            "live_reply_busy_count": self._live_reply_busy_count,
            "inflight_route_count": len(self._inflight_route_tasks),
        }
        return not blockers, blockers, diagnostics

    async def _compute_idle_topic_retry_delay(self) -> float | None:
        if not self._idle_topic_enabled():
            return None
        eligible, blockers, _diagnostics = await self._evaluate_idle_topic_eligibility()
        if eligible:
            return None
        quiet_seconds = max(0.05, float(self.settings.interaction.idle_topic_after_sec))
        retry_delays: list[float] = []
        diagnostics = _diagnostics if isinstance(_diagnostics, Mapping) else {}
        last_live_age_sec = diagnostics.get("last_live_age_sec")
        last_bot_age_sec = diagnostics.get("last_bot_age_sec")
        if "quiet_window_live" in blockers and isinstance(last_live_age_sec, (int, float)):
            retry_delays.append(max(0.05, quiet_seconds - float(last_live_age_sec) + 0.05))
        if "quiet_window_bot" in blockers and isinstance(last_bot_age_sec, (int, float)):
            retry_delays.append(max(0.05, quiet_seconds - float(last_bot_age_sec) + 0.05))
        if retry_delays:
            return min(retry_delays)
        retryable_prefixes = {
            "cooldown_active",
            "sts2_defer",
            "video_watch_active",
            "ai_speaking",
            "live_reply_busy",
            "inflight_routes",
            "sts2_pending",
            "pending_messages",
        }
        for blocker in blockers:
            blocker_prefix = blocker.split(":", 1)[0]
            if blocker_prefix in retryable_prefixes:
                return min(1.0, quiet_seconds)
        return None

    def _log_idle_topic_skip(self, *, blockers: list[str], diagnostics: Mapping[str, Any] | None = None) -> None:
        if self.logger is None:
            return
        diagnostic_parts: list[str] = []
        for key in (
            "pending_count",
            "pending_requires_clear",
            "last_live_age_sec",
            "last_bot_age_sec",
            "live_reply_busy_count",
            "inflight_route_count",
            "recent_viewer_messages",
            "recent_bot_outputs",
            "timeline_records",
        ):
            value = diagnostics.get(key) if isinstance(diagnostics, Mapping) else None
            if value is None:
                continue
            if isinstance(value, float):
                diagnostic_parts.append(f"{key}={value:.2f}")
            else:
                diagnostic_parts.append(f"{key}={value}")
        detail_suffix = f" diagnostics={' '.join(diagnostic_parts)}" if diagnostic_parts else ""
        self.logger.info(
            "Bilibili idle topic skipped: "
            f"room_id={self.settings.bilibili.room_id} blockers={','.join(blockers) or 'unknown'}{detail_suffix}"
        )

    async def _build_idle_topic_prompt(self) -> str:
        plan = await self._build_idle_topic_plan()
        return plan.prompt if plan is not None else ""

    async def _build_idle_topic_plan(self) -> _IdleTopicPlan | None:
        base_prompt = str(self.settings.interaction.idle_topic_prompt or "").strip()
        if not base_prompt:
            return None
        snapshot = self._topic_snapshot or self._restore_topic_snapshot()
        recent_viewer_messages = self._recent_viewer_message_texts()
        recent_bot_outputs = list(self._recent_bot_outputs)
        status = classify_live_topic_status(
            snapshot=snapshot,
            recent_timeline=list(self._recent_topic_timeline),
            recent_viewer_messages=recent_viewer_messages,
            recent_bot_outputs=recent_bot_outputs,
        )
        if status is LiveTopicStatus.EMPTY:
            return await self._build_empty_idle_topic_plan(base_prompt=base_prompt, snapshot=snapshot)
        if snapshot is None:
            return None
        if status is LiveTopicStatus.ACTIVE:
            prompt = self._compose_active_topic_prompt(base_prompt=base_prompt, snapshot=snapshot)
            return _IdleTopicPlan(prompt=prompt, status=status, snapshot=self._touch_snapshot(snapshot))
        return await self._build_tailing_idle_topic_plan(base_prompt=base_prompt, snapshot=snapshot)

    async def _build_tailing_idle_topic_plan(
        self,
        *,
        base_prompt: str,
        snapshot: LiveTopicSnapshot,
    ) -> _IdleTopicPlan | None:
        current_topic = self._topic_from_snapshot(snapshot)
        if not current_topic:
            return await self._build_empty_idle_topic_plan(base_prompt=base_prompt, snapshot=snapshot)
        expansion = await self._expand_topic_from_tailing_context(current_topic=current_topic)
        if expansion is None:
            prompt = self._compose_active_topic_prompt(base_prompt=base_prompt, snapshot=snapshot)
            return _IdleTopicPlan(
                prompt=prompt,
                status=LiveTopicStatus.ACTIVE,
                snapshot=self._touch_snapshot(snapshot),
            )
        updated_snapshot = LiveTopicSnapshot(
            room_id=self._room_id(),
            live_chat_id=self._live_chat_id(),
            current_topic=expansion.related_topic,
            previous_topic=current_topic,
            recent_viewer_messages=self._recent_viewer_message_texts(),
            recent_bot_outputs=list(self._recent_bot_outputs),
            last_expansion=expansion,
            updated_at=time.time(),
        )
        prompt = self._compose_tailing_topic_prompt(
            base_prompt=base_prompt,
            current_topic=current_topic,
            expansion=expansion,
        )
        return _IdleTopicPlan(
            prompt=prompt,
            status=LiveTopicStatus.TAILING,
            snapshot=updated_snapshot,
            expansion_result=expansion,
        )

    async def _build_empty_idle_topic_plan(
        self,
        *,
        base_prompt: str,
        snapshot: LiveTopicSnapshot | None,
    ) -> _IdleTopicPlan | None:
        recent_viewer_messages = self._recent_viewer_message_texts()
        if not recent_viewer_messages:
            if snapshot is None or not snapshot.recent_viewer_messages:
                return None
            recent_viewer_messages = list(snapshot.recent_viewer_messages)
        seed_result = await self._extract_seed_topic()
        if seed_result is None:
            return None
        updated_snapshot = LiveTopicSnapshot(
            room_id=self._room_id(),
            live_chat_id=self._live_chat_id(),
            current_topic=seed_result.related_topic,
            previous_topic=self._topic_from_snapshot(snapshot),
            recent_viewer_messages=recent_viewer_messages,
            recent_bot_outputs=list(self._recent_bot_outputs or (snapshot.recent_bot_outputs if snapshot else [])),
            last_expansion=seed_result,
            updated_at=time.time(),
        )
        prompt = self._compose_empty_topic_prompt(base_prompt=base_prompt, seed_result=seed_result)
        return _IdleTopicPlan(
            prompt=prompt,
            status=LiveTopicStatus.EMPTY,
            snapshot=updated_snapshot,
            expansion_result=seed_result,
        )

    async def _expand_topic_from_tailing_context(self, *, current_topic: str) -> TopicExpansionResult | None:
        client = self.topic_extension_client
        if client is None:
            return None
        expand_topic = getattr(client, "expand_topic", None)
        if not callable(expand_topic):
            return None
        return await expand_topic(
            current_topic=current_topic,
            recent_timeline=list(self._recent_topic_timeline),
            recent_viewer_messages=self._recent_viewer_message_texts(),
            recent_bot_outputs=list(self._recent_bot_outputs),
        )

    async def _extract_seed_topic(self) -> TopicExpansionResult | None:
        client = self.topic_extension_client
        if client is None:
            return None
        extract_seed_topic = getattr(client, "extract_seed_topic", None)
        if not callable(extract_seed_topic):
            return None
        return await extract_seed_topic(
            recent_timeline=list(self._recent_topic_timeline),
            recent_viewer_messages=self._recent_viewer_message_texts(),
            recent_bot_outputs=list(self._recent_bot_outputs),
        )

    def _compose_active_topic_prompt(self, *, base_prompt: str, snapshot: LiveTopicSnapshot) -> str:
        sections = [base_prompt]
        current_topic = self._topic_from_snapshot(snapshot)
        if current_topic:
            sections.append(f"上一轮刚聊到的话题摘要：{current_topic}")
        viewer_seed = self._format_recent_live_records()
        if viewer_seed:
            sections.append(f"最近观众切入点：\n{viewer_seed}")
        timeline = self._format_recent_topic_timeline()
        if timeline:
            sections.append(f"最近直播时间线：\n{timeline}")
        sections.append(self._idle_topic_style_guidance())
        sections.append("请沿着上一个话题自然续聊，可以顺着观众的切入点补充、追问或展开，不要把天聊死。")
        return "\n\n".join(section for section in sections if section)

    def _compose_tailing_topic_prompt(
        self,
        *,
        base_prompt: str,
        current_topic: str,
        expansion: TopicExpansionResult,
    ) -> str:
        sections = [
            base_prompt,
            f"上一轮刚聊到的话题：{current_topic}",
            f"相关拓展话题：{expansion.related_topic}",
            f"拓展角度：{expansion.expansion_angle}",
            f"为什么相关：{expansion.why_related}",
            f"建议转场方式：{expansion.handoff_prompt}",
        ]
        viewer_seed = self._format_recent_live_records()
        if viewer_seed:
            sections.append(f"最近观众切入点：\n{viewer_seed}")
        timeline = self._format_recent_topic_timeline()
        if timeline:
            sections.append(f"最近直播时间线：\n{timeline}")
        sections.append(self._idle_topic_style_guidance())
        sections.append("请先承接原话题，再自然转到相关拓展话题，保持开放式表达，不要把天聊死。")
        return "\n\n".join(section for section in sections if section)

    def _compose_empty_topic_prompt(self, *, base_prompt: str, seed_result: TopicExpansionResult) -> str:
        sections = [
            base_prompt,
            f"最近弹幕里提炼出的种子话题：{seed_result.related_topic}",
            f"提炼角度：{seed_result.expansion_angle}",
            f"提炼依据：{seed_result.why_related}",
            f"建议起聊方式：{seed_result.handoff_prompt}",
        ]
        viewer_seed = self._format_recent_live_records()
        if viewer_seed:
            sections.append(f"最近观众切入点：\n{viewer_seed}")
        timeline = self._format_recent_topic_timeline()
        if timeline:
            sections.append(f"最近直播时间线：\n{timeline}")
        sections.append(self._idle_topic_style_guidance())
        sections.append("请围绕这个种子话题自然开聊，保持话题可继续，不要泛泛而谈，也不要把天聊死。")
        return "\n\n".join(section for section in sections if section)

    def _idle_topic_style_guidance(self) -> str:
        return (
            "风格要求：\n"
            "- 优先强关联地玩梗、接梗、抖包袱、搞抽象，必要时可以短暂假装故障、卡壳、系统提示异常来开场。\n"
            "- 先抓一个具体的小点、怪点或反差点，不要把话题写成播客标题、论文标题、鸡汤分析题。\n"
            "- 多用口语化吐槽、站队题、离谱假设、反问或点名互动，让观众马上能接话。\n"
            "- 如果原话题偏正经，先把它拧成更有直播味、更有梗、更接地气的切口再聊。\n"
            "- 保持轻松、短句、互动感，不要长篇讲大道理，不要把天聊死。"
        )

    def _format_recent_topic_timeline(self) -> str:
        if not self._recent_topic_timeline:
            return ""
        live_limit = max(1, int(self.settings.interaction.idle_topic_context_limit))
        bot_limit = max(1, int(self.settings.interaction.idle_topic_history_limit))
        records = self._recent_topic_timeline[-(live_limit + bot_limit) :]
        lines = []
        for record in records:
            text = str(record.get("text") or "")
            if not text:
                continue
            if str(record.get("role") or "") == "bot":
                lines.append(f"- bot: {text}")
                continue
            username = str(record.get("username") or "anonymous")
            lines.append(f"- \u89c2\u4f17 {username}: {text}")
        return "\n".join(lines)

    def _format_recent_live_records(self) -> str:
        if not self._recent_live_records:
            return ""
        limit = max(1, int(self.settings.interaction.idle_topic_context_limit))
        lines = []
        for record in self._recent_live_records[-limit:]:
            username = str(record.get("username") or "anonymous")
            text = str(record.get("text") or "")
            if text:
                lines.append(f"- {username}: {text}")
        return "\n".join(lines)

    def _format_recent_bot_outputs(self) -> str:
        if not self._recent_bot_outputs:
            return ""
        limit = max(1, int(self.settings.interaction.idle_topic_history_limit))
        return "\n".join(f"- {topic}" for topic in self._recent_bot_outputs[-limit:])

    def _record_live_activity(self) -> None:
        self._last_routeable_live_event_at = time.time()
        self._idle_topic_cooldown_active = False
        self._schedule_idle_topic(restart=True)

    def _schedule_live_reply_busy_timeout(self) -> None:
        timeout_sec = max(0.05, float(self.settings.interaction.serial_reply_timeout_sec))
        self._live_reply_busy_timeout_task = asyncio.create_task(
            self._live_reply_busy_timeout_loop(timeout_sec),
            name="bilibili_live.reply_busy_timeout",
        )

    def _cancel_live_reply_busy_timeout(self) -> None:
        task = self._live_reply_busy_timeout_task
        if task is None:
            return
        current_task: asyncio.Task[Any] | None = None
        with contextlib.suppress(RuntimeError):
            current_task = asyncio.current_task()
        if task is not current_task and not task.done():
            task.cancel()
        if task is not current_task:
            self._live_reply_busy_timeout_task = None

    async def _live_reply_busy_timeout_loop(self, timeout_sec: float) -> None:
        try:
            await asyncio.sleep(timeout_sec)
            if self._live_reply_busy_count > 0:
                self._live_reply_busy_count = 0
                self.record_live_reply_finished_without_output(reason="timeout")
        except asyncio.CancelledError:
            return
        finally:
            if self._live_reply_busy_timeout_task is asyncio.current_task():
                self._live_reply_busy_timeout_task = None

    def _restore_topic_snapshot(self) -> LiveTopicSnapshot | None:
        try:
            return self.topic_state_store.load(
                room_id=self._room_id(),
                live_chat_id=self._live_chat_id(),
                max_age_sec=7200.0,
            )
        except Exception:
            return None

    def _persist_topic_snapshot(self) -> None:
        if self._topic_snapshot is None:
            return
        with contextlib.suppress(Exception):
            self.topic_state_store.save(self._topic_snapshot)

    def _sync_topic_snapshot(
        self,
        *,
        current_topic: str | None = None,
        recent_viewer_messages: list[str] | None = None,
        recent_bot_outputs: list[str] | None = None,
        last_expansion: TopicExpansionResult | None | object = None,
    ) -> None:
        snapshot = self._topic_snapshot or self._restore_topic_snapshot()
        previous_topic = self._topic_from_snapshot(snapshot)
        updated_snapshot = LiveTopicSnapshot(
            room_id=self._room_id(),
            live_chat_id=self._live_chat_id(),
            current_topic=(current_topic if current_topic is not None else previous_topic),
            previous_topic=str(snapshot.previous_topic if snapshot is not None else "").strip(),
            recent_viewer_messages=list(
                recent_viewer_messages
                if recent_viewer_messages is not None
                else (snapshot.recent_viewer_messages if snapshot is not None else [])
            ),
            recent_bot_outputs=list(
                recent_bot_outputs
                if recent_bot_outputs is not None
                else (snapshot.recent_bot_outputs if snapshot is not None else [])
            ),
            last_expansion=(
                snapshot.last_expansion if snapshot is not None else None
                if last_expansion is None
                else (last_expansion if isinstance(last_expansion, TopicExpansionResult) else None)
            ),
            updated_at=time.time(),
        )
        self._topic_snapshot = updated_snapshot
        self._persist_topic_snapshot()

    def _touch_snapshot(self, snapshot: LiveTopicSnapshot) -> LiveTopicSnapshot:
        return LiveTopicSnapshot(
            room_id=snapshot.room_id,
            live_chat_id=snapshot.live_chat_id,
            current_topic=snapshot.current_topic,
            previous_topic=snapshot.previous_topic,
            recent_viewer_messages=self._recent_viewer_message_texts() or list(snapshot.recent_viewer_messages),
            recent_bot_outputs=list(self._recent_bot_outputs or snapshot.recent_bot_outputs),
            last_expansion=snapshot.last_expansion,
            updated_at=time.time(),
        )

    def _topic_from_snapshot(self, snapshot: LiveTopicSnapshot | None) -> str:
        if snapshot is None:
            return ""
        return str(snapshot.current_topic or snapshot.previous_topic or "").strip()

    def _current_topic_hint(self) -> str:
        return self._topic_from_snapshot(self._topic_snapshot)

    def _recent_viewer_message_texts(self) -> list[str]:
        limit = max(1, int(self.settings.interaction.idle_topic_context_limit))
        messages: list[str] = []
        for record in self._recent_live_records[-limit:]:
            text = str(record.get("text") or "").strip()
            if text:
                messages.append(text)
        return messages

    def _room_id(self) -> str:
        return str(self.settings.bilibili.room_id or "").strip()

    def _live_chat_id(self) -> str:
        chat_id = str(getattr(self.planner, "chat_id", "") or "").strip()
        if chat_id:
            return chat_id
        room_id = self._room_id() or "unknown"
        return f"bilibili-live:{room_id}"

    async def _handle_sts2_command(self, event: dict[str, Any]) -> bool:
        if not self.settings.sts2.enabled:
            return False
        text = str(event.get("text") or event.get("summary") or "").strip()
        if not text.startswith("/"):
            return False
        start_command = self.settings.sts2.commands.start_command
        stop_command = self.settings.sts2.commands.stop_command
        status_command = self.settings.sts2.commands.status_command
        user_id = str(event.get("user_id") or "").strip()
        is_admin = bool(user_id and user_id in self.settings.sts2.commands.admin_user_ids)
        sc_can_start = text == start_command and _is_super_chat_start_authorized(
            event,
            min_price=self.settings.sts2.commands.super_chat_start_min_price,
        )
        if not (is_admin or sc_can_start):
            return False
        controller = self.sts2_controller
        if controller is None:
            if self.logger is not None:
                self.logger.warning("STS2 command ignored because controller is unavailable.")
            return text in {
                start_command,
                stop_command,
                status_command,
            }
        if text == start_command:
            await controller.start_from_command(event)
            return True
        if not is_admin:
            return False
        if text == stop_command:
            await controller.stop_from_command(event)
            return True
        if text == status_command:
            await controller.status_from_command(event)
            return True
        return False

    async def _handle_live2d_debug_command(self, event: dict[str, Any]) -> bool:
        handler = self.live2d_debug_command_handler
        if handler is None:
            return False
        try:
            return bool(await handler(event))
        except Exception as exc:
            if self.logger is not None and hasattr(self.logger, "warning"):
                self.logger.warning(f"Live2D debug command handler failed: {exc}")
            return False

    async def _handle_visual_context_command(self, event: dict[str, Any]) -> bool:
        handler = self.visual_context_command_handler
        if handler is None:
            return False
        try:
            return bool(await handler(event))
        except Exception as exc:
            if self.logger is not None and hasattr(self.logger, "warning"):
                self.logger.warning(f"Visual context command handler failed: {exc}")
            return False

    async def _handle_soundboard_command(self, event: dict[str, Any]) -> bool:
        handler = self.soundboard_command_handler
        if handler is None:
            return False
        try:
            return bool(await handler(event))
        except Exception as exc:
            if self.logger is not None and hasattr(self.logger, "warning"):
                self.logger.warning(f"Soundboard command handler failed: {exc}")
            return False

    async def _handle_video_watch_event(self, event: dict[str, Any]) -> bool:
        handler = self.video_watch_event_handler
        if handler is None:
            return False
        try:
            return bool(await handler(event))
        except Exception as exc:
            if self.logger is not None and hasattr(self.logger, "warning"):
                self.logger.warning(f"Video watch event handler failed: {exc}")
            return False

    async def _maybe_offer_idle_video_watch(self) -> bool:
        handler = self.video_watch_idle_offer_handler
        if handler is None:
            return False
        try:
            return bool(await handler())
        except Exception as exc:
            if self.logger is not None and hasattr(self.logger, "warning"):
                self.logger.warning(f"Video watch idle offer handler failed: {exc}")
            return False

    def _video_watch_should_block_idle_topic(self) -> bool:
        blocker = self.video_watch_idle_blocker
        if blocker is None:
            return False
        try:
            return bool(blocker())
        except Exception as exc:
            if self.logger is not None and hasattr(self.logger, "warning"):
                self.logger.warning(f"Video watch idle blocker failed: {exc}")
            return False

    async def _handle_soundboard_keyword_trigger(self, event: dict[str, Any]) -> bool:
        handler = self.soundboard_keyword_handler
        if handler is None:
            return False
        try:
            return bool(await handler(event))
        except Exception as exc:
            if self.logger is not None and hasattr(self.logger, "warning"):
                self.logger.warning(f"Soundboard keyword trigger failed: {exc}")
            return False

    def _idle_topic_enabled(self) -> bool:
        return bool(
            self.settings.interaction.enabled
            and self.settings.interaction.idle_topic_enabled
            and str(self.settings.interaction.idle_topic_prompt or "").strip()
        )

    def has_live_reply_busy(self) -> bool:
        return self._live_reply_busy_count > 0

    def _is_ai_speaking(self) -> bool:
        return bool(self.live2d_controller is not None and self.live2d_controller.is_speaking)

    def _should_defer_for_sts2(self) -> bool:
        return bool(
            self.settings.sts2.enabled
            and self.settings.sts2.narration.priority_over_danmaku
            and self.sts2_controller is not None
            and self.sts2_controller.is_active
            and self._sts2_has_pending_decision()
        )

    def _should_collect_for_sts2_decision(self, event: Mapping[str, Any]) -> bool:
        return bool(
            self.settings.sts2.enabled
            and self.settings.sts2.narration.priority_over_danmaku
            and self.sts2_controller is not None
            and self.sts2_controller.is_active
            and _is_live_reply_event(event)
        )

    def _sts2_has_pending_decision(self) -> bool:
        if self.sts2_controller is None:
            return False
        return bool(getattr(self.sts2_controller, "has_pending_decision", False))

    def _log_inbound_event(self, event: Mapping[str, Any]) -> None:
        if self.logger is None:
            return
        event_type = str(event.get("type") or "").strip()
        if event_type not in {"danmaku", "super_chat", "gift", "guard"}:
            return
        username = str(event.get("username") or "anonymous").strip() or "anonymous"
        user_id = str(event.get("user_id") or "").strip()
        text = self._sanitize_log_text(str(event.get("text") or event.get("summary") or "").strip())
        event_id = str(event.get("event_id") or "").strip()
        sender = f"{username}({user_id})" if user_id else username
        if event_type == "danmaku":
            self.logger.info(
                "Bilibili inbound danmaku: "
                f"room_id={self.settings.bilibili.room_id} user={sender} text={text!r} event_id={event_id or '-'}"
            )
            return
        extra_parts: list[str] = []
        if event_type == "super_chat":
            price = event.get("price")
            if isinstance(price, (int, float)):
                extra_parts.append(f"price={float(price):g}")
        if event_type in {"gift", "guard"}:
            gift_name = str(event.get("gift_name") or "").strip()
            count = event.get("count")
            if gift_name:
                extra_parts.append(f"gift_name={gift_name!r}")
            if isinstance(count, (int, float)):
                extra_parts.append(f"count={int(count)}")
        extra = (" " + " ".join(extra_parts)) if extra_parts else ""
        self.logger.info(
            f"Bilibili inbound {event_type}: "
            f"room_id={self.settings.bilibili.room_id} user={sender}{extra} text={text!r} event_id={event_id or '-'}"
        )

    def _log_planner_flush(self, *, buffered_count: int, selected_count: int, routed_count: int) -> None:
        if self.logger is None:
            return
        self.logger.info(
            "Bilibili planner flush: "
            f"buffered={buffered_count} selected={selected_count} routed={routed_count}"
        )

    @staticmethod
    def _planner_status_key(event: Mapping[str, Any], event_index: int) -> str:
        event_id = str(event.get("event_id") or "").strip()
        if event_id:
            return event_id
        event_type = str(event.get("type") or "event").strip() or "event"
        timestamp = str(event.get("timestamp") or "").strip() or "-"
        return f"{event_type}:{timestamp}:{event_index}"

    def _log_planner_event_status(
        self,
        event: Mapping[str, Any],
        *,
        selected: bool,
        routed: bool,
        accepted: bool,
        hold_reason: str,
        event_index: int,
        selection_reason: str = "",
        selection_score: float | None = None,
    ) -> None:
        if self.logger is None:
            return
        event_type = str(event.get("type") or "").strip() or "unknown"
        event_id = str(event.get("event_id") or "").strip() or f"index-{event_index}"
        text = self._sanitize_log_text(str(event.get("text") or event.get("summary") or "").strip())
        reason = str(hold_reason or "").strip() or "-"
        selection_reason_text = str(selection_reason or "").strip() or "-"
        selection_score_text = "-" if selection_score is None else f"{float(selection_score):.3f}"
        self.logger.info(
            "Bilibili planner event: "
            f"room_id={self.settings.bilibili.room_id} "
            f"event_id={event_id} type={event_type} text={text!r} "
            f"selected={int(bool(selected))} routed={int(bool(routed))} accepted={int(bool(accepted))} "
            f"hold_reason={reason} selection_reason={selection_reason_text} "
            f"selection_score={selection_score_text}"
        )

    @staticmethod
    def _sanitize_log_text(text: str, *, max_length: int = 160) -> str:
        normalized = " ".join(str(text).split())
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max_length - 3] + "..."


def _normalize_context_text(text: str, *, max_length: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max(0, max_length - 3)] + "..."


def _compact_live_command_text(text: str) -> str:
    return re.sub(r"[\s\.,，。!！?？:：;；、'\"“”‘’`~\-_/\\|<>\[\]\(\)\{\}【】（）《》]+", "", str(text or "")).lower()


def _is_explicit_wink_request(text: str) -> bool:
    compact = _compact_live_command_text(text)
    if not compact:
        return False
    explicit_tokens = (
        "wink",
        "winkplease",
        "leftwink",
        "rightwink",
        "wink一下",
        "眨眼",
        "眨一下",
        "单眼",
        "左眼wink",
        "右眼wink",
        "wink左眼",
        "wink右眼",
        "左wink",
        "右wink",
    )
    return any(token in compact for token in explicit_tokens)


def _extract_wink_side(text: str) -> str:
    compact = _compact_live_command_text(text)
    if any(token in compact for token in ("leftwink", "lefteye", "左眼", "wink左眼", "左wink")):
        return "left"
    if any(token in compact for token in ("rightwink", "righteye", "右眼", "wink右眼", "右wink")):
        return "right"
    return ""


def _extract_special_live2d_move(text: str) -> str | None:
    compact = _compact_live_command_text(text)
    if not compact:
        return None
    if any(
        token in compact
        for token in (
            "\u8f6c\u5446\u6bdb",
            "\u52a8\u4e00\u52a8\u5446\u6bdb",
            "\u5446\u6bdb\u8f6c\u8d77\u6765",
            "\u5446\u6bdb\u76f4\u5347\u673a",
            "ahogespin",
        )
    ):
        return "ahoge_spin"
    return None


def _is_live_reply_event(event: Mapping[str, Any]) -> bool:
    return str(event.get("type") or "").strip() in {
        "danmaku",
        "super_chat",
        "gift",
        "guard",
        "hub_local_input",
        "hub_bot_reply",
    }


def _is_hard_paid_priority_event(event: Mapping[str, Any]) -> bool:
    return str(event.get("type") or "").strip() in {"super_chat", "gift", "guard"}


def _should_force_route_soundboard_request(
    event: Mapping[str, Any],
    *,
    settings: LiveAdapterSettings,
) -> bool:
    if not settings.soundboard.enabled or not settings.soundboard.force_route_inbound_requests:
        return False
    if not bool(event.get("_soundboard_request_detected")):
        return False
    event_type = str(event.get("type") or "").strip().lower()
    return event_type in {"danmaku", "super_chat", "hub_local_input"}


def _allows_command_side_effects(event: Mapping[str, Any]) -> bool:
    return str(event.get("type") or "").strip() != "hub_bot_reply"


def _is_super_chat_start_authorized(event: Mapping[str, Any], *, min_price: float) -> bool:
    if str(event.get("type") or "").strip() != "super_chat":
        return False
    try:
        price = float(event.get("price") or 0.0)
    except (TypeError, ValueError):
        return False
    return price >= float(min_price)


_ANGLE_BRACKET_TOKEN_RE = re.compile(r"<[^<>\r\n]{0,128}(?:>|(?=<)|$)")


def _strip_markup_only_text(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    normalized = _ANGLE_BRACKET_TOKEN_RE.sub(" ", normalized)
    normalized = re.sub(r"[ \t\f\v]+", " ", normalized)
    return normalized.strip()

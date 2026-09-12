"""LiveHub 核心：事件汇聚、参与者管理与语音互斥协调。

与插件侧客户端（hub_input_client.py / plugin.py hub 逻辑）的协议精确对齐：
- 事件 record 结构：seq / event_id / type / origin / text / summary / user_id / username / timestamp / raw
- 事件类型：danmaku / super_chat / gift / guard（origin="bilibili"）、local_inject、bot_reply
- speaking 状态：{"current": {"request_id", "client_id", "bot_name", ...} | None}
- participants 状态：[{"client_id", "bot_name", "forward_user_id", "forward_username", ...}]
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from collections import deque
from typing import Any, Awaitable, Callable, Mapping

from ._utils import normalize_duration_ms, normalize_epoch_seconds, normalize_text
from .config import LiveHubConfig

BroadcastHandler = Callable[[Mapping[str, Any]], Awaitable[None]]

DEFAULT_ORIGIN = "hub"

# 待处理广播任务上限：慢客户端阻塞时丢弃事件广播（事件仍可经 HTTP 轮询获取），避免任务无界堆积
MAX_PENDING_BROADCASTS = 32
# 语音等待队列上限：防止异常客户端无限排入 speak-request
MAX_PENDING_SPEAK_REQUESTS = 128
# 参与者上限：防止任意 client_id 无限注册 presence
MAX_PARTICIPANTS = 128


class LiveHub:
    """livehub 服务端核心状态机。

    线程模型：所有方法均在 asyncio 事件循环内调用（HTTP handler / WS handler /
    后台任务），因此无需额外加锁。
    """

    def __init__(self, config: LiveHubConfig, *, logger: Any = None) -> None:
        self._config = config
        self._logger = logger
        self._events: deque[dict[str, Any]] = deque(maxlen=max(64, config.history_limit * 2))
        self._seq = 0
        self._participants: dict[str, dict[str, Any]] = {}
        self._speaking_current: dict[str, Any] | None = None
        self._speaking_pending: deque[dict[str, Any]] = deque()
        self._broadcast_handler: BroadcastHandler | None = None
        self._background_task: asyncio.Task[None] | None = None
        self._pending_broadcasts = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def set_broadcast_handler(self, handler: BroadcastHandler) -> None:
        """设置 WS 广播回调（由 server 层注入，用于向所有客户端推送消息）。"""
        self._broadcast_handler = handler

    def start(self) -> None:
        """启动后台维护任务（参与者超时清理、语音超时释放）。"""
        if self._background_task is not None:
            return
        self._background_task = asyncio.create_task(self._maintenance_loop(), name="livehub.maintenance")

    async def stop(self) -> None:
        """停止后台维护任务。"""
        task = self._background_task
        self._background_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _maintenance_loop(self) -> None:
        """每秒执行一次参与者清理与语音超时检查；单轮异常不中断循环。"""
        while True:
            try:
                await asyncio.sleep(1.0)
                changed = self._prune_presence()
                speech_changed = self._release_expired_speech()
                if changed or speech_changed:
                    await self._broadcast_health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_warning(f"livehub 维护循环异常: {exc}")

    # ------------------------------------------------------------------ #
    # 事件汇聚
    # ------------------------------------------------------------------ #
    def publish_event(self, record: Mapping[str, Any]) -> int:
        """为外部事件分配 seq 并存入历史，然后广播给所有客户端。

        返回分配的 seq。
        """
        self._seq += 1
        normalized: dict[str, Any] = {
            "seq": self._seq,
            "event_id": normalize_text(record.get("event_id")) or f"hub-event-{self._seq}",
            "type": normalize_text(record.get("type")) or "danmaku",
            "origin": normalize_text(record.get("origin")) or DEFAULT_ORIGIN,
            "text": (text := normalize_text(record.get("text"))),
            "summary": normalize_text(record.get("summary")) or text,
            "user_id": normalize_text(record.get("user_id")) or "anonymous",
            "username": normalize_text(record.get("username")) or "anonymous",
            "timestamp": normalize_epoch_seconds(record.get("timestamp")),
            "raw": dict(record.get("raw")) if isinstance(record.get("raw"), Mapping) else {},
        }
        self._events.append(normalized)
        self._schedule_broadcast_event(normalized)
        return self._seq

    def _schedule_broadcast(self, coro_factory: Callable[[], Awaitable[None]], *, name: str) -> None:
        """调度一次异步广播：维护待处理任务计数，积压超限时丢弃本次广播。"""
        handler = self._broadcast_handler
        if handler is None or self._pending_broadcasts >= MAX_PENDING_BROADCASTS:
            return
        self._pending_broadcasts += 1

        async def _deliver() -> None:
            try:
                await coro_factory()
            finally:
                self._pending_broadcasts -= 1

        asyncio.get_running_loop().create_task(_deliver(), name=name)

    def _schedule_broadcast_event(self, record: Mapping[str, Any]) -> None:
        """异步广播单条事件（不阻塞事件汇聚路径）。"""
        self._schedule_broadcast(
            lambda: self._broadcast_event(record),
            name="livehub.broadcast_event",
        )

    async def _broadcast_event(self, record: Mapping[str, Any]) -> None:
        handler = self._broadcast_handler
        if handler is None:
            return
        with contextlib.suppress(Exception):
            await handler({"kind": "event", "event": dict(record)})

    def recent_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        """返回最近的事件列表（按 seq 升序）；limit 缺省使用配置的 history_limit。"""
        effective_limit = limit if limit and limit > 0 else self._config.history_limit
        count = max(1, min(int(effective_limit), len(self._events)))
        return list(self._events)[-count:]

    # ------------------------------------------------------------------ #
    # 状态快照
    # ------------------------------------------------------------------ #
    def snapshot(self, limit: int | None = None) -> dict[str, Any]:
        """返回客户端连接时的完整快照（events + participants + speaking + health）。

        limit 为 None 时使用配置的 history_limit。
        """
        return {
            "events": self.recent_events(limit if limit and limit > 0 else self._config.history_limit),
            "participants": self.participants(),
            "speaking": self.speaking_state(),
            "health": self.health_state(),
        }

    def health_state(self) -> dict[str, Any]:
        """返回健康状态（供 /api/events 与 WS health 消息使用）。"""
        return {
            "room_id": self._config.room_id,
            "event_count": len(self._events),
            "last_seq": self._seq,
            "participant_count": len(self._participants),
            "server_time": time.time(),
        }

    def participants(self) -> list[dict[str, Any]]:
        """返回参与者列表（按 client_id 排序，供客户端展示与身份识别）。"""
        return [
            {
                "client_id": client_id,
                "bot_name": str(item.get("bot_name") or client_id),
                "forward_user_id": str(item.get("forward_user_id") or ""),
                "forward_username": str(item.get("forward_username") or ""),
                "last_seen_at": float(item.get("last_seen_at") or 0.0),
                "connected": bool(item.get("connected")),
            }
            for client_id, item in sorted(self._participants.items())
        ]

    def speaking_state(self) -> dict[str, Any]:
        """返回当前语音互斥状态。"""
        return {"current": dict(self._speaking_current) if self._speaking_current is not None else None}

    # ------------------------------------------------------------------ #
    # 参与者管理（presence）
    # ------------------------------------------------------------------ #
    def register_presence(
        self,
        *,
        client_id: str,
        bot_name: str,
        forward_user_id: str = "",
        forward_username: str = "",
    ) -> dict[str, Any]:
        """注册 / 刷新参与者心跳。

        返回精简状态快照（participants + speaking + health，不含事件历史）。
        """
        normalized_client_id = normalize_text(client_id)
        if not normalized_client_id:
            return self.snapshot()
        if normalized_client_id not in self._participants and len(self._participants) >= MAX_PARTICIPANTS:
            self._log_warning(f"Hub 参与者数量达到上限，拒绝注册: client_id={normalized_client_id}")
            return {
                "participants": self.participants(),
                "speaking": self.speaking_state(),
                "health": self.health_state(),
            }
        previous = self._participants.get(normalized_client_id) or {}
        self._participants[normalized_client_id] = {
            "client_id": normalized_client_id,
            "bot_name": normalize_text(bot_name) or normalized_client_id,
            "forward_user_id": normalize_text(forward_user_id) or str(previous.get("forward_user_id") or ""),
            "forward_username": normalize_text(forward_username) or str(previous.get("forward_username") or ""),
            "last_seen_at": time.time(),
            "connected": True,
        }
        self._schedule_broadcast_health()
        # presence 心跳响应只需参与者 / 语音 / 健康状态（不含事件历史，客户端不消费）
        return {
            "participants": self.participants(),
            "speaking": self.speaking_state(),
            "health": self.health_state(),
        }

    def _prune_presence(self) -> bool:
        """移除超过 presence_timeout_sec 未心跳的参与者；返回是否有变化。"""
        if not self._participants:
            return False
        deadline = time.time() - max(1.0, self._config.presence_timeout_sec)
        expired_ids = [
            client_id
            for client_id, item in self._participants.items()
            if item.get("last_seen_at", 0) < deadline
        ]
        if not expired_ids:
            return False
        for client_id in expired_ids:
            self._participants.pop(client_id, None)
        self._log_info(f"Hub 参与者心跳超时移除: {', '.join(expired_ids)}")
        return True

    # ------------------------------------------------------------------ #
    # 语音互斥协调
    # ------------------------------------------------------------------ #
    def request_speak(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """请求说话权。

        当前无人说话 -> 立即授予；否则进入等待队列。
        返回 {"granted": bool, "speaking": {...}}。
        """
        request: dict[str, Any] = {
            "request_id": normalize_text(payload.get("request_id")),
            "client_id": normalize_text(payload.get("client_id")),
            "bot_name": normalize_text(payload.get("bot_name")) or normalize_text(payload.get("client_id")),
            "text": normalize_text(payload.get("text")),
            "expected_duration_ms": normalize_duration_ms(payload.get("expected_duration_ms")),
            "room_id": normalize_text(payload.get("room_id")),
            "live_event_type": normalize_text(payload.get("live_event_type")),
            "granted_at": time.time(),
        }
        if not request["request_id"] or not request["client_id"]:
            return {"granted": False, "speaking": self.speaking_state()}
        if self._speaking_current is None:
            self._speaking_current = request
            self._schedule_broadcast_health()
            self._log_info(
                f"Hub 语音授予: client_id={request['client_id']} request_id={request['request_id']}"
            )
            return {"granted": True, "speaking": self.speaking_state()}
        if len(self._speaking_pending) >= MAX_PENDING_SPEAK_REQUESTS:
            self._log_warning(
                f"Hub 语音等待队列已满，拒绝请求: client_id={request['client_id']} request_id={request['request_id']}"
            )
            return {"granted": False, "speaking": self.speaking_state()}
        self._speaking_pending.append(request)
        self._log_info(
            f"Hub 语音排队: client_id={request['client_id']} request_id={request['request_id']}"
        )
        return {"granted": False, "speaking": self.speaking_state()}

    def complete_speak(self, *, request_id: str, client_id: str, status: str) -> dict[str, Any]:
        """释放说话权；若等待队列非空则把说话权交给队首请求。

        返回 {"speaking": {...}}。
        """
        normalized_request_id = normalize_text(request_id)
        normalized_client_id = normalize_text(client_id)
        current = self._speaking_current
        if current is not None and current.get("request_id") == normalized_request_id:
            self._speaking_current = None
            self._grant_next_speaker()
            self._schedule_broadcast_health()
            self._log_info(
                f"Hub 语音释放: client_id={normalized_client_id} request_id={normalized_request_id} status={status}"
            )
        else:
            self._speaking_pending = deque(
                item
                for item in self._speaking_pending
                if not (
                    item.get("request_id") == normalized_request_id and item.get("client_id") == normalized_client_id
                )
            )
            # 取消排队只移除队列项；仅当当前无人说话时才把说话权交给队首，
            # 避免顶掉正在说话的客户端。
            if status == "cancelled" and self._speaking_current is None:
                self._grant_next_speaker()
                self._schedule_broadcast_health()
        return {"speaking": self.speaking_state()}

    def _grant_next_speaker(self) -> None:
        """把说话权交给等待队列队首请求（若有）。"""
        if not self._speaking_pending:
            return
        next_request = self._speaking_pending.popleft()
        next_request["granted_at"] = time.time()
        self._speaking_current = next_request

    def _release_expired_speech(self) -> bool:
        """释放超时的当前说话者（expected_duration_ms + 宽限秒数）。"""
        current = self._speaking_current
        if current is None:
            return False
        expected_sec = normalize_duration_ms(current.get("expected_duration_ms")) / 1000.0
        granted_at = float(current.get("granted_at") or 0.0)
        timeout_sec = expected_sec + max(0.0, self._config.speech_timeout_grace_sec)
        if time.time() - granted_at < timeout_sec:
            return False
        self._speaking_current = None
        self._grant_next_speaker()
        self._log_info(
            f"Hub 语音超时释放: client_id={current.get('client_id')} "
            f"request_id={current.get('request_id')} after {timeout_sec:.1f}s"
        )
        return True

    # ------------------------------------------------------------------ #
    # WS 广播
    # ------------------------------------------------------------------ #
    def _schedule_broadcast_health(self) -> None:
        """异步广播 health 消息（participants / speaking 变化时调用）。"""
        self._schedule_broadcast(self._broadcast_health, name="livehub.broadcast_health")

    async def _broadcast_health(self) -> None:
        handler = self._broadcast_handler
        if handler is None:
            return
        with contextlib.suppress(Exception):
            await handler(
                {
                    "kind": "health",
                    "participants": self.participants(),
                    "speaking": self.speaking_state(),
                    "health": self.health_state(),
                }
            )

    def _log_info(self, message: str) -> None:
        if self._logger is not None:
            self._logger.info(message)

    def _log_warning(self, message: str) -> None:
        if self._logger is not None:
            self._logger.warning(message)

"""Hub input subscriber that mirrors shared hub events into live-adapter events."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

import asyncio
import contextlib
import json
import math
import time

from uuid import uuid4

try:
    from aiohttp import ClientSession, ClientTimeout, WSMsgType

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False


HubRecordHandler = Callable[[Mapping[str, Any]], Awaitable[None]]
ConnectionCallback = Callable[[], Any]
HubStateHandler = Callable[[Mapping[str, Any]], Any]


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_epoch_seconds(value: Any) -> float:
    """将时间戳规范化为秒级浮点数（兼容毫秒/微秒输入）。

    与 livehub/_utils.normalize_epoch_seconds 保持一致的阈值与循环除法语义；
    两侧修改需同步。
    """
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return time.time()
    if not math.isfinite(timestamp) or timestamp <= 0:
        return time.time()
    while timestamp > 32_503_680_000:
        timestamp /= 1000.0
    return timestamp


def _normalize_record_seq(record: Mapping[str, Any]) -> int:
    try:
        return int(record.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        normalized[str(key)] = _normalize_json_value(item)
    return normalized


def _normalize_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return _normalize_mapping(value)
    if isinstance(value, (list, tuple, set)):
        return [_normalize_json_value(item) for item in value]
    return str(value)


def _finalize_translated_event(
    event: dict[str, Any],
    *,
    record: Mapping[str, Any],
    fallback_type: str,
) -> dict[str, Any]:
    event.setdefault("event_id", _normalize_text(record.get("event_id")) or f"hub-event-{uuid4().hex}")
    event.setdefault("type", fallback_type)
    event.setdefault("text", _normalize_text(record.get("text") or record.get("summary")))
    event.setdefault("summary", _normalize_text(record.get("summary") or event.get("text")))
    event.setdefault(
        "username",
        _normalize_text(record.get("username")) or _normalize_text(record.get("user_id")) or "anonymous",
    )
    event.setdefault(
        "user_id",
        _normalize_text(record.get("user_id")) or _normalize_text(record.get("username")) or "anonymous",
    )
    event.setdefault("timestamp", _normalize_epoch_seconds(record.get("timestamp")))
    event["hub_origin"] = _normalize_text(record.get("origin"))
    event["hub_seq"] = _normalize_record_seq(record)
    event["hub_record_type"] = _normalize_text(record.get("type")) or fallback_type
    return event


def _is_self_hub_bot_reply(
    record: Mapping[str, Any],
    *,
    self_client_ids: set[str],
    self_bot_names: set[str],
) -> bool:
    user_id = _normalize_text(record.get("user_id"))
    username = _normalize_text(record.get("username")).casefold()
    if user_id and user_id in self_client_ids:
        return True
    if username and username in self_bot_names:
        return True
    raw = record.get("raw")
    if isinstance(raw, Mapping):
        raw_client_id = _normalize_text(raw.get("client_id") or raw.get("user_id"))
        raw_bot_name = _normalize_text(raw.get("bot_name") or raw.get("username")).casefold()
        if raw_client_id and raw_client_id in self_client_ids:
            return True
        if raw_bot_name and raw_bot_name in self_bot_names:
            return True
    return False


def normalize_hub_record_to_live_event(
    record: Mapping[str, Any],
    *,
    self_client_ids: set[str],
    self_bot_names: set[str],
    inject_local_messages: bool = True,
    inject_other_bot_replies: bool = True,
    ignore_self_bot_replies: bool = True,
) -> dict[str, Any] | None:
    record_type = _normalize_text(record.get("type"))
    origin = _normalize_text(record.get("origin"))
    if record_type in {"danmaku", "super_chat", "gift", "guard"} or origin == "bilibili":
        raw_event = _normalize_mapping(record.get("raw"))
        event = raw_event if raw_event else {}
        return _finalize_translated_event(event, record=record, fallback_type=record_type or "danmaku")
    if record_type == "local_inject":
        if not inject_local_messages:
            return None
        return _finalize_translated_event({}, record=record, fallback_type="hub_local_input")
    if record_type == "bot_reply":
        if not inject_other_bot_replies:
            return None
        normalized_client_ids = {value for value in self_client_ids if value}
        normalized_bot_names = {value.casefold() for value in self_bot_names if value}
        if ignore_self_bot_replies and _is_self_hub_bot_reply(
            record,
            self_client_ids=normalized_client_ids,
            self_bot_names=normalized_bot_names,
        ):
            return None
        return _finalize_translated_event({}, record=record, fallback_type="hub_bot_reply")
    return None


class HubInputClient:
    """Best-effort subscriber for the standalone live hub."""

    def __init__(
        self,
        *,
        http_url: str = "",
        websocket_url: str = "",
        presence_url: str = "",
        speak_request_url: str = "",
        speak_complete_url: str = "",
        auth_token: str = "",
        connect_timeout_sec: float = 10.0,
        history_limit: int = 80,
        presence_heartbeat_sec: float = 10.0,
        client_id: str = "",
        bot_name: str = "",
        reconnect_delay_sec: float = 2.0,
        poll_interval_sec: float = 2.0,
        on_event: HubRecordHandler,
        on_state: HubStateHandler | None = None,
        on_connection_opened: ConnectionCallback | None = None,
        on_connection_closed: ConnectionCallback | None = None,
        logger: Any = None,
    ) -> None:
        self.http_url = _normalize_text(http_url)
        self.websocket_url = _normalize_text(websocket_url)
        self.presence_url = _normalize_text(presence_url)
        self.speak_request_url = _normalize_text(speak_request_url)
        self.speak_complete_url = _normalize_text(speak_complete_url)
        self.auth_token = _normalize_text(auth_token)
        self.connect_timeout_sec = max(1.0, float(connect_timeout_sec or 10.0))
        self.history_limit = max(1, int(history_limit or 80))
        self.presence_heartbeat_sec = max(3.0, float(presence_heartbeat_sec or 10.0))
        self.client_id = _normalize_text(client_id)
        self.bot_name = _normalize_text(bot_name) or self.client_id
        self.reconnect_delay_sec = max(0.5, float(reconnect_delay_sec or 2.0))
        self.poll_interval_sec = max(0.5, float(poll_interval_sec or 2.0))
        self.on_event = on_event
        self.on_state = on_state
        self.on_connection_opened = on_connection_opened
        self.on_connection_closed = on_connection_closed
        self.logger = logger
        self._session: Any = None
        self._ws: Any = None
        self._runner_task: asyncio.Task[None] | None = None
        self._presence_task: asyncio.Task[None] | None = None
        self._connected = False
        self._bootstrapped = False
        self._last_seq = 0

    async def start(self) -> None:
        if not AIOHTTP_AVAILABLE:
            self._log_warning("Hub input client disabled because aiohttp is unavailable")
            return
        if self._runner_task is not None:
            return
        self._runner_task = asyncio.create_task(self._run(), name="maibot_bilibili_live_adapter.hub_input")

    async def stop(self) -> None:
        task = self._runner_task
        self._runner_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._close_ws()
        await self._close_session()
        await self._notify_connection_closed()

    async def _run(self) -> None:
        while True:
            try:
                await self._ensure_session()
                self._start_presence_task()
                await self._sync_recent_events(deliver=self._bootstrapped)
                if not self.websocket_url:
                    self._bootstrapped = True
                    await self._notify_connection_opened()
                    await self._poll_loop()
                else:
                    await self._listen_ws()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_warning(f"Hub input connection loop failed: {exc}")
            finally:
                await self._stop_presence_task()
                await self._close_ws()
                await self._notify_connection_closed()
            if self._runner_task is None:
                return
            await asyncio.sleep(self.reconnect_delay_sec)

    async def _poll_loop(self) -> None:
        while self._runner_task is not None:
            await asyncio.sleep(self.poll_interval_sec)
            await self._sync_recent_events(deliver=True)

    async def _listen_ws(self) -> None:
        if self._session is None or not self.websocket_url:
            return
        self._ws = await self._session.ws_connect(self.websocket_url, heartbeat=20.0)
        await self._notify_connection_opened()
        async for message in self._ws:
            if message.type == WSMsgType.TEXT:
                payload = json.loads(str(message.data))
                kind = _normalize_text(payload.get("kind"))
                if kind == "snapshot":
                    await self._notify_state(payload)
                    await self._consume_records(payload.get("events"), deliver=self._bootstrapped)
                    self._bootstrapped = True
                    continue
                if kind == "event":
                    await self._notify_state(payload)
                    await self._consume_record(payload.get("event"), deliver=True)
                    self._bootstrapped = True
                    continue
                if kind == "health":
                    await self._notify_state(payload)
                    continue
            elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                break

    async def _sync_recent_events(self, *, deliver: bool) -> None:
        if not self.http_url or self._session is None:
            return
        events_url = self.http_url
        separator = "&" if "?" in events_url else "?"
        events_url = f"{events_url}{separator}limit={self.history_limit}"
        async with self._session.get(events_url) as response:
            response.raise_for_status()
            payload = await response.json()
        if isinstance(payload, Mapping):
            await self._notify_state(payload)
        await self._consume_records(payload.get("events"), deliver=deliver)

    async def _consume_records(self, events: Any, *, deliver: bool) -> None:
        if not isinstance(events, list):
            return
        normalized_records = [
            event
            for event in events
            if isinstance(event, Mapping) and _normalize_record_seq(event) > 0
        ]
        normalized_records.sort(key=_normalize_record_seq)
        for record in normalized_records:
            await self._consume_record(record, deliver=deliver)

    async def _consume_record(self, record: Any, *, deliver: bool) -> None:
        if not isinstance(record, Mapping):
            return
        seq = _normalize_record_seq(record)
        if seq <= 0 or seq <= self._last_seq:
            return
        self._last_seq = seq
        if deliver:
            await self.on_event(record)

    async def _ensure_session(self) -> None:
        if self._session is not None:
            return
        timeout = ClientTimeout(total=None, connect=self.connect_timeout_sec)
        self._session = ClientSession(headers=self._headers(), timeout=timeout)

    async def _close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _close_session(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    def _start_presence_task(self) -> None:
        if self._presence_task is not None or not self.presence_url or not self.client_id:
            return
        self._presence_task = asyncio.create_task(
            self._presence_loop(),
            name="maibot_bilibili_live_adapter.hub_presence",
        )

    async def _stop_presence_task(self) -> None:
        task = self._presence_task
        self._presence_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _presence_loop(self) -> None:
        while self._runner_task is not None and self._session is not None:
            try:
                await self._post_presence()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_warning(f"Hub input presence heartbeat failed: {exc}")
            await asyncio.sleep(self.presence_heartbeat_sec)

    async def _post_presence(self) -> None:
        if self._session is None or not self.presence_url or not self.client_id:
            return
        async with self._session.post(
            self.presence_url,
            json={
                "client_id": self.client_id,
                "bot_name": self.bot_name or self.client_id,
            },
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        if isinstance(payload, Mapping):
            await self._notify_state(payload)

    async def request_speak_turn(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._session is None:
            await self._ensure_session()
        if self._session is None or not self.speak_request_url:
            return {}
        async with self._session.post(self.speak_request_url, json=dict(payload)) as response:
            response.raise_for_status()
            result = await response.json()
        if isinstance(result, Mapping):
            await self._notify_state(result)
        return dict(result) if isinstance(result, Mapping) else {}

    async def complete_speak_turn(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._session is None:
            await self._ensure_session()
        if self._session is None or not self.speak_complete_url:
            return {}
        async with self._session.post(self.speak_complete_url, json=dict(payload)) as response:
            response.raise_for_status()
            result = await response.json()
        if isinstance(result, Mapping):
            await self._notify_state(result)
        return dict(result) if isinstance(result, Mapping) else {}

    async def _notify_connection_opened(self) -> None:
        if self._connected:
            return
        self._connected = True
        callback = self.on_connection_opened
        if callback is None:
            return
        result = callback()
        if asyncio.iscoroutine(result):
            await result

    async def _notify_connection_closed(self) -> None:
        if not self._connected:
            return
        self._connected = False
        callback = self.on_connection_closed
        if callback is None:
            return
        result = callback()
        if asyncio.iscoroutine(result):
            await result

    async def _notify_state(self, payload: Mapping[str, Any]) -> None:
        callback = self.on_state
        if callback is None:
            return
        state = {
            "participants": payload.get("participants"),
            "health": payload.get("health"),
            "speaking": payload.get("speaking"),
        }
        result = callback(state)
        if asyncio.iscoroutine(result):
            await result

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "MaiBot-Bilibili-Live-Adapter/0.1"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)

"""Bilibili live danmaku WebSocket transport."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

import asyncio
import contextlib
import hashlib
import json
import time

try:  # pragma: no cover - availability is environment dependent.
    import aiohttp
except Exception:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

from .bilibili_codec import build_auth_packet, build_heartbeat_packet, normalize_event, parse_packets
from .config import BilibiliConfig
from .constants import DEFAULT_BILIBILI_WS_URL
from livehub.bilibili_protocol import select_ws_urls


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
LifecycleCallback = Callable[[], Awaitable[None]]
HISTORY_POLL_INTERVAL_SEC = 2.0
MAX_HISTORY_EVENT_IDS = 4096
MAX_EVENT_DEDUP_KEYS = 4096
MAX_RECENT_SOURCE_EVENTS = 256
CROSS_SOURCE_DEDUP_WINDOW_SEC = 5.0
MIN_PARALLEL_WS_CONNECTIONS_WITH_BACKUPS = 3
IDLE_RECONNECT_STAGGER_RATIO = 0.2
IDLE_RECONNECT_STAGGER_MIN_SEC = 1.0
IDLE_RECONNECT_STAGGER_MAX_SEC = 15.0


def collect_history_event_ids(payload: Mapping[str, Any]) -> set[str]:
    """Collect normalized history event ids from a Bilibili gethistory payload."""

    event_ids: set[str] = set()
    for event in extract_history_events(payload):
        event_ids.add(str(event["event_id"]))
    return event_ids


def build_history_baseline(payload: Mapping[str, Any] | None) -> set[str] | None:
    """Build the startup baseline used to avoid replaying pre-start history."""

    if payload is None:
        return None
    return collect_history_event_ids(payload)


def extract_history_events(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract normalized history danmaku events from admin/room buckets."""

    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    deduped: dict[str, dict[str, Any]] = {}
    for bucket in ("admin", "room"):
        items = data.get(bucket)
        if not isinstance(items, list):
            continue
        for item in items:
            event = _history_item_to_event(item)
            if event is None:
                continue
            deduped.setdefault(str(event["event_id"]), event)
    return list(deduped.values())


def _history_item_to_event(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    text = str(item.get("text") or "").strip()
    if not text:
        return None
    user_id = str(item.get("uid") or "anonymous")
    username = str(item.get("nickname") or item.get("uname") or user_id)
    timeline = str(item.get("timeline") or "").strip()
    timestamp = _parse_history_timeline(timeline)
    raw_event = dict(item)
    return {
        "event_id": _history_event_id(raw_event),
        "type": "danmaku",
        "text": text,
        "summary": text,
        "user_id": user_id,
        "username": username,
        "timestamp": timestamp,
        "raw": raw_event,
    }


def _history_event_id(item: Mapping[str, Any]) -> str:
    id_str = str(item.get("id_str") or "").strip()
    if id_str:
        return f"bilibili-history-{id_str}"
    digest = hashlib.md5(
        json.dumps(dict(item), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"bilibili-history-{digest}"


def _parse_history_timeline(timeline: str) -> float:
    normalized = str(timeline or "").strip()
    if not normalized:
        return datetime.now().timestamp()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(normalized, fmt).timestamp()
        except ValueError:
            continue
    return datetime.now().timestamp()


def _event_dedup_key(event: Mapping[str, Any]) -> str:
    event_type = str(event.get("type") or "").strip()
    event_id = str(event.get("event_id") or "").strip()
    user_id = str(event.get("user_id") or "").strip()
    text = str(event.get("text") or event.get("summary") or "").strip()
    timestamp = _normalize_timestamp_bucket(event.get("timestamp"))
    if event_type and user_id and text:
        return f"{event_type}|{user_id}|{text}|{timestamp}"
    if event_id:
        return f"event_id|{event_id}"
    raw = event.get("raw")
    digest = hashlib.md5(
        json.dumps(raw if isinstance(raw, Mapping) else dict(event), ensure_ascii=False, sort_keys=True, default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"raw|{digest}"


def _normalize_timestamp_bucket(value: Any) -> int:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return 0
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    return int(timestamp + 0.5)


def _normalize_timestamp_seconds(value: Any) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return 0.0
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    return timestamp


def _event_source(event: Mapping[str, Any]) -> str:
    event_id = str(event.get("event_id") or "").strip()
    return "history" if event_id.startswith("bilibili-history-") else "ws"


def _is_anonymous_user_id(value: Any) -> bool:
    normalized = str(value or "").strip().casefold()
    return normalized in {"", "0", "anonymous", "unknown"}


def _normalize_username(value: Any) -> str:
    return "".join(ch for ch in str(value or "").strip().casefold() if not ch.isspace())


def _username_looks_masked(value: Any) -> bool:
    normalized = _normalize_username(value)
    return bool(normalized and "*" in normalized)


def _usernames_look_compatible(left: Any, right: Any) -> bool:
    left_name = _normalize_username(left)
    right_name = _normalize_username(right)
    if not left_name or not right_name:
        return False
    if left_name == right_name:
        return True
    if "*" in left_name:
        prefix = left_name.split("*", 1)[0]
        if prefix and right_name.startswith(prefix):
            return True
    if "*" in right_name:
        prefix = right_name.split("*", 1)[0]
        if prefix and left_name.startswith(prefix):
            return True
    return False


class BilibiliDanmakuTransport:
    """Minimal input-only Bilibili live WebSocket client."""

    def __init__(
        self,
        *,
        on_event: EventCallback,
        on_connection_opened: LifecycleCallback | None = None,
        on_connection_closed: LifecycleCallback | None = None,
        logger: Any = None,
    ) -> None:
        self._on_event = on_event
        self._on_connection_opened = on_connection_opened
        self._on_connection_closed = on_connection_closed
        self._logger = logger
        self._config = BilibiliConfig()
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._history_task: asyncio.Task[None] | None = None
        self._session: Any = None
        self._ws: Any = None
        self._active_ws_connections: list[Any] = []
        self._parallel_connection_tasks: set[asyncio.Task[None]] = set()
        self._connection_ready_reported = False
        self._history_fallback_enabled = False
        self._cached_history_payload: Mapping[str, Any] | None = None
        self._cached_history_payload_at = 0.0
        self._ws_url_candidates: list[str] = []
        self._ws_url_cursor = 0
        self._last_danmaku_monotonic: float | None = None
        self._history_seen_event_ids: set[str] = set()
        self._history_seen_order: deque[str] = deque()
        self._event_dedup_keys: set[str] = set()
        self._event_dedup_order: deque[str] = deque()
        self._recent_source_events: deque[dict[str, Any]] = deque()
        self._logged_unsupported_paid_commands: set[str] = set()

    def configure(self, config: BilibiliConfig) -> None:
        """Apply transport config."""

        self._config = config

    def is_available(self) -> bool:
        """Return whether aiohttp is importable."""

        return aiohttp is not None

    async def start(self) -> None:
        """Start the reconnecting receive loop."""

        if not self.is_available():
            self._log_error("aiohttp is required for Bilibili live transport")
            return
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._connection_ready_reported = False
        self._history_fallback_enabled = False
        self._ws_url_candidates.clear()
        self._ws_url_cursor = 0
        self._last_danmaku_monotonic = None
        self._history_seen_event_ids.clear()
        self._history_seen_order.clear()
        self._event_dedup_keys.clear()
        self._event_dedup_order.clear()
        self._recent_source_events.clear()
        await self._ensure_session()
        await self._prepare_history_fallback()
        self._task = asyncio.create_task(self._run_loop(), name="bilibili_live.transport")

    async def stop(self) -> None:
        """Stop the receive loop and close network resources."""

        self._running = False
        if self._history_task is not None:
            self._history_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._history_task
        self._history_task = None

        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
        self._heartbeat_task = None

        await self._cancel_parallel_connection_tasks()
        await self._close_active_ws_connections()

        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

        await self._reset_session()
        if self._on_connection_closed is not None:
            await self._on_connection_closed()
        self._connection_ready_reported = False
        self._history_fallback_enabled = False
        self._ws_url_candidates.clear()
        self._ws_url_cursor = 0
        self._last_danmaku_monotonic = None
        self._history_seen_event_ids.clear()
        self._history_seen_order.clear()
        self._event_dedup_keys.clear()
        self._event_dedup_order.clear()
        self._recent_source_events.clear()

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_warning(f"Bilibili live transport disconnected: {exc}")
            finally:
                if self._history_task is not None:
                    self._history_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._history_task
                    self._history_task = None
                if self._heartbeat_task is not None:
                    self._heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._heartbeat_task
                    self._heartbeat_task = None
                await self._cancel_parallel_connection_tasks()
                await self._close_active_ws_connections()
                await self._reset_session()
                if self._on_connection_closed is not None:
                    await self._on_connection_closed()
                self._connection_ready_reported = False

            if self._running:
                await asyncio.sleep(max(0.1, float(self._config.reconnect_delay_sec)))

    async def _cancel_parallel_connection_tasks(self) -> None:
        tasks = list(self._parallel_connection_tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._parallel_connection_tasks.difference_update(tasks)

    async def _close_active_ws_connections(self) -> None:
        for ws in list(self._active_ws_connections):
            with contextlib.suppress(Exception):
                await ws.close()
        self._active_ws_connections.clear()
        self._ws = None

    async def _connect_once(self) -> None:
        assert aiohttp is not None
        await self._ensure_session()
        self._connection_ready_reported = False

        parallel_limit = self._get_parallel_ws_connections()
        if parallel_limit <= 1:
            ws_url, auth_token = await self._resolve_connection_target()
            await self._connect_single_url_once(ws_url, auth_token, set_primary=True, connection_slot=0, parallel_pool_size=1)
            return

        ws_urls, auth_token = await self._resolve_connection_targets()
        ws_urls = ws_urls[:parallel_limit]
        if len(ws_urls) <= 1:
            await self._connect_single_url_once(
                ws_urls[0],
                auth_token,
                set_primary=True,
                connection_slot=0,
                parallel_pool_size=1,
            )
            return

        await self._connect_many_once(ws_urls, auth_token)

    async def _connect_many_once(self, ws_urls: list[str], auth_token: str) -> None:
        parallel_pool_size = len(ws_urls)
        task_by_url: dict[asyncio.Task[None], str] = {
            asyncio.create_task(
                self._connect_single_url_once(
                    ws_url,
                    auth_token,
                    set_primary=False,
                    connection_slot=index,
                    parallel_pool_size=parallel_pool_size,
                ),
                name=f"bilibili_live.ws.{index}",
            ): ws_url
            for index, ws_url in enumerate(ws_urls)
        }
        pending = set(task_by_url)
        self._parallel_connection_tasks.update(pending)
        last_error: BaseException | None = None
        try:
            while self._running and pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    ws_url = task_by_url[task]
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc is None:
                        continue
                    last_error = exc
                    self._log_warning(
                        self._append_transport_health_snapshot(
                            f"Bilibili live websocket candidate disconnected: url={ws_url} reason={exc}"
                        )
                    )

            if self._running:
                if last_error is not None:
                    raise ConnectionError(
                        f"all Bilibili live websocket candidates disconnected; last error: {last_error}"
                    ) from last_error
                raise ConnectionError("all Bilibili live websocket candidates disconnected")
        finally:
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._parallel_connection_tasks.difference_update(task_by_url)

    async def _connect_single_url_once(
        self,
        ws_url: str,
        auth_token: str,
        *,
        set_primary: bool,
        connection_slot: int,
        parallel_pool_size: int,
    ) -> None:
        if not ws_url:
            raise ConnectionError("Bilibili live WebSocket URL is empty")
        ws = await self._session.ws_connect(ws_url, heartbeat=None)
        if set_primary:
            self._ws = ws
        self._active_ws_connections.append(ws)
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop_for_ws(ws),
            name="bilibili_live.heartbeat",
        )
        if set_primary:
            self._heartbeat_task = heartbeat_task

        auth_ready = False
        ws_ready_reported = False
        last_danmaku_at = time.monotonic()
        try:
            await ws.send_bytes(build_auth_packet(self._config.room_id, uid=self._config.uid, token=auth_token))
            while self._running and not ws.closed:
                receive_timeout = self._get_receive_timeout_sec(auth_ready=auth_ready)
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=receive_timeout)
                except asyncio.TimeoutError as exc:
                    if not auth_ready:
                        raise ConnectionError(
                            f"Bilibili live auth timeout after {receive_timeout:.1f}s on {ws_url}"
                        ) from exc
                    raise ConnectionError(
                        f"Bilibili live WebSocket receive timeout after {receive_timeout:.1f}s on {ws_url}"
                    ) from exc

                if not self._running:
                    break
                if message.type == aiohttp.WSMsgType.BINARY:
                    auth_ready = auth_ready or self._payload_has_successful_auth_reply(message.data)
                    if await self._handle_binary(message.data):
                        last_danmaku_at = time.monotonic()
                elif message.type == aiohttp.WSMsgType.TEXT:
                    payload = message.data.encode("utf-8")
                    auth_ready = auth_ready or self._payload_has_successful_auth_reply(payload)
                    if await self._handle_binary(payload):
                        last_danmaku_at = time.monotonic()
                elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                    break
                if auth_ready and not ws_ready_reported:
                    self._log_info(
                        self._append_transport_health_snapshot(
                            f"Bilibili transport health: reason=ws_ready url={ws_url} slot={connection_slot}"
                        )
                    )
                    ws_ready_reported = True
                if auth_ready and self._should_reconnect_for_danmaku_idle(
                    last_danmaku_at=last_danmaku_at,
                    connection_slot=connection_slot,
                    parallel_pool_size=parallel_pool_size,
                    other_open_ws_connections=self._count_open_ws_connections(exclude_ws=ws),
                ):
                    threshold = self._get_danmaku_idle_reconnect_threshold_sec(
                        connection_slot=connection_slot,
                        parallel_pool_size=parallel_pool_size,
                    )
                    raise ConnectionError(f"Bilibili live danmaku idle for {threshold:.1f}s on {ws_url}")
        except Exception as exc:
            if parallel_pool_size <= 1:
                self._log_warning(
                    self._append_transport_health_snapshot(
                        f"Bilibili live websocket candidate disconnected: url={ws_url} reason={exc}",
                        active_hosts=self._count_open_ws_connections(),
                    )
                )
            raise
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            if set_primary and self._heartbeat_task is heartbeat_task:
                self._heartbeat_task = None
            with contextlib.suppress(ValueError):
                self._active_ws_connections.remove(ws)
            with contextlib.suppress(Exception):
                await ws.close()
            if set_primary and self._ws is ws:
                self._ws = None

    def _get_receive_timeout_sec(self, *, auth_ready: bool | None = None) -> float:
        if not (self._connection_ready_reported if auth_ready is None else auth_ready):
            return max(0.001, float(self._config.connect_timeout_sec))
        heartbeat_timeout = float(self._config.heartbeat_interval_sec) * 2.0 + float(self._config.connect_timeout_sec)
        return max(1.0, heartbeat_timeout)

    def _get_parallel_ws_connections(self) -> int:
        try:
            configured = int(getattr(self._config, "parallel_ws_connections", 1) or 1)
        except (TypeError, ValueError):
            configured = 1
        normalized = min(8, max(1, configured))
        if normalized <= 1:
            return 1
        return min(8, max(MIN_PARALLEL_WS_CONNECTIONS_WITH_BACKUPS, normalized))

    def _get_danmaku_idle_reconnect_sec(self) -> float:
        return max(0.0, float(getattr(self._config, "danmaku_idle_reconnect_sec", 0.0) or 0.0))

    def _get_danmaku_idle_reconnect_threshold_sec(self, *, connection_slot: int = 0, parallel_pool_size: int = 1) -> float:
        threshold = self._get_danmaku_idle_reconnect_sec()
        if threshold <= 0:
            return 0.0
        if parallel_pool_size <= 1:
            return threshold
        stagger_step = min(
            IDLE_RECONNECT_STAGGER_MAX_SEC,
            max(IDLE_RECONNECT_STAGGER_MIN_SEC, threshold * IDLE_RECONNECT_STAGGER_RATIO),
        )
        return threshold + max(0, int(connection_slot)) * stagger_step

    def _get_required_other_ws_connections_before_idle_reconnect(self, *, parallel_pool_size: int) -> int:
        # Candidate tasks are rebuilt as a pool, so a degraded idle pool must be allowed to drain.
        return 0

    def _count_open_ws_connections(self, *, exclude_ws: Any | None = None) -> int:
        count = 0
        for candidate in self._active_ws_connections:
            if candidate is exclude_ws:
                continue
            if bool(getattr(candidate, "closed", False)):
                continue
            count += 1
        return count

    def _get_last_danmaku_age_sec(self, *, now: float | None = None) -> float | None:
        if self._last_danmaku_monotonic is None:
            return None
        current_time = time.monotonic() if now is None else float(now)
        return max(0.0, current_time - float(self._last_danmaku_monotonic))

    def _is_single_host_degraded(self, *, active_hosts: int | None = None) -> bool:
        host_count = self._count_open_ws_connections() if active_hosts is None else max(0, int(active_hosts))
        return self._get_parallel_ws_connections() > 1 and host_count == 1

    def _build_transport_health_snapshot(self, *, now: float | None = None, active_hosts: int | None = None) -> str:
        host_count = self._count_open_ws_connections() if active_hosts is None else max(0, int(active_hosts))
        danmaku_age = self._get_last_danmaku_age_sec(now=now)
        danmaku_age_text = "never" if danmaku_age is None else f"{danmaku_age:.1f}"
        single_host_degraded = "true" if self._is_single_host_degraded(active_hosts=host_count) else "false"
        return (
            f"active_hosts={host_count} "
            f"last_danmaku_age_sec={danmaku_age_text} "
            f"single_host_degraded={single_host_degraded}"
        )

    def _append_transport_health_snapshot(
        self,
        message: str,
        *,
        now: float | None = None,
        active_hosts: int | None = None,
    ) -> str:
        return f"{message} {self._build_transport_health_snapshot(now=now, active_hosts=active_hosts)}"

    def _should_reconnect_for_danmaku_idle(
        self,
        *,
        last_danmaku_at: float,
        now: float | None = None,
        connection_slot: int = 0,
        parallel_pool_size: int = 1,
        other_open_ws_connections: int | None = None,
    ) -> bool:
        threshold = self._get_danmaku_idle_reconnect_threshold_sec(
            connection_slot=connection_slot,
            parallel_pool_size=parallel_pool_size,
        )
        if threshold <= 0:
            return False
        required_other_connections = self._get_required_other_ws_connections_before_idle_reconnect(
            parallel_pool_size=parallel_pool_size
        )
        if (
            other_open_ws_connections is not None
            and other_open_ws_connections < required_other_connections
        ):
            return False
        current_time = time.monotonic() if now is None else float(now)
        return current_time - float(last_danmaku_at) >= threshold

    async def _heartbeat_loop(self) -> None:
        await self._heartbeat_loop_for_ws(self._ws)

    async def _heartbeat_loop_for_ws(self, ws: Any) -> None:
        while self._running and ws is not None:
            await asyncio.sleep(max(1.0, float(self._config.heartbeat_interval_sec)))
            if ws is None or ws.closed:
                return
            await ws.send_bytes(build_heartbeat_packet())

    def _payload_has_successful_auth_reply(self, payload: bytes) -> bool:
        return any(self._is_successful_auth_reply(packet) for packet in parse_packets(payload))

    async def _handle_binary(self, payload: bytes) -> bool:
        routed_danmaku = False
        for packet in parse_packets(payload):
            if self._is_successful_auth_reply(packet):
                await self._report_connection_opened_once()
                await self._start_history_polling_if_needed()
                continue
            if str(packet.get("type") or "") == "auth_reply":
                self._log_warning(f"Bilibili live auth failed: {packet.get('raw') or {}}")
                continue
            event = normalize_event(packet)
            if event is not None:
                await self._report_connection_opened_once()
                await self._start_history_polling_if_needed()
                event = await self._enrich_event_identity(event)
                if self._mark_event_seen(event):
                    await self._on_event(event)
                    if str(event.get("type") or "") == "danmaku":
                        received_at = time.monotonic()
                        self._last_danmaku_monotonic = received_at
                        self._log_info(
                            self._append_transport_health_snapshot(
                                "Bilibili transport health: reason=danmaku_received",
                                now=received_at,
                            )
                        )
                        routed_danmaku = True
                continue
            self._log_unsupported_paid_command(packet)
        return routed_danmaku

    async def _resolve_connection_target(self) -> tuple[str, str]:
        fallback_url = self._config.ws_url or DEFAULT_BILIBILI_WS_URL
        conf = await self._fetch_danmaku_conf()
        if not isinstance(conf, Mapping):
            return self._next_ws_url([fallback_url]), ""
        token = str(conf.get("token") or "").strip()
        resolved_url = self._next_ws_url(self._select_ws_urls(conf) or [fallback_url])
        return resolved_url, token

    async def _resolve_connection_targets(self) -> tuple[list[str], str]:
        fallback_url = self._config.ws_url or DEFAULT_BILIBILI_WS_URL
        conf = await self._fetch_danmaku_conf()
        if not isinstance(conf, Mapping):
            return self._ordered_ws_urls([fallback_url]), ""
        token = str(conf.get("token") or "").strip()
        urls = self._select_ws_urls(conf)
        if fallback_url:
            urls.append(fallback_url)
        return self._ordered_ws_urls(urls or [fallback_url]), token

    async def _fetch_danmaku_conf(self) -> Mapping[str, Any] | None:
        if self._session is None:
            return None
        url = "https://api.live.bilibili.com/room/v1/Danmu/getConf"
        params = {
            "room_id": str(self._config.room_id),
            "platform": "pc",
            "player": "web",
        }
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://live.bilibili.com/{self._config.room_id}/",
        }
        try:
            async with self._session.get(url, params=params, headers=headers) as response:
                payload = await response.json(content_type=None)
        except Exception as exc:
            self._log_warning(f"Bilibili danmaku config request failed: {exc}")
            return None
        if not isinstance(payload, Mapping) or payload.get("code") != 0:
            self._log_warning(f"Bilibili danmaku config request returned: {payload}")
            return None
        data = payload.get("data")
        return data if isinstance(data, Mapping) else None

    async def _fetch_history_payload(self) -> Mapping[str, Any] | None:
        if self._session is None:
            return None
        url = "https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://live.bilibili.com/{self._config.room_id}/",
        }
        try:
            async with self._session.get(url, params={"roomid": str(self._config.room_id)}, headers=headers) as response:
                payload = await response.json(content_type=None)
        except Exception:
            return None
        if not isinstance(payload, Mapping) or payload.get("code") != 0:
            return None
        self._cached_history_payload = payload
        self._cached_history_payload_at = time.monotonic()
        return payload

    async def _fetch_cached_history_payload(self, *, max_age_sec: float = 1.5) -> Mapping[str, Any] | None:
        now = time.monotonic()
        if self._cached_history_payload is not None and now - self._cached_history_payload_at <= max_age_sec:
            return self._cached_history_payload
        return await self._fetch_history_payload()

    async def _prepare_history_fallback(self) -> None:
        if not self._config.history_fallback_enabled:
            self._history_fallback_enabled = False
            return
        payload = await self._fetch_history_payload()
        baseline = build_history_baseline(payload)
        if baseline is None:
            self._log_warning("Bilibili history fallback disabled because the startup baseline snapshot could not be fetched.")
            return
        self._history_fallback_enabled = True
        for event_id in baseline:
            self._remember_history_event_id(event_id)

    async def _history_poll_loop(self) -> None:
        while self._running:
            await asyncio.sleep(HISTORY_POLL_INTERVAL_SEC)
            payload = await self._fetch_history_payload()
            if payload is None:
                continue
            await self._handle_history_payload(payload)

    async def _handle_history_payload(self, payload: Mapping[str, Any]) -> None:
        self._cached_history_payload = payload
        self._cached_history_payload_at = time.monotonic()
        for event in extract_history_events(payload):
            event_id = str(event.get("event_id") or "").strip()
            if not event_id or event_id in self._history_seen_event_ids:
                continue
            self._remember_history_event_id(event_id)
            if self._mark_event_seen(event):
                await self._on_event(event)

    async def _start_history_polling_if_needed(self) -> None:
        if not self._config.history_fallback_enabled:
            return
        if not self._history_fallback_enabled:
            return
        if self._history_task is not None and not self._history_task.done():
            return
        self._history_task = asyncio.create_task(self._history_poll_loop(), name="bilibili_live.history_poll")

    async def _ensure_session(self) -> None:
        assert aiohttp is not None
        timeout = aiohttp.ClientTimeout(total=max(1.0, float(self._config.connect_timeout_sec)))
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": self._config.user_agent},
            )

    async def _reset_session(self) -> None:
        if self._session is None:
            return
        with contextlib.suppress(Exception):
            await self._session.close()
        self._session = None

    def _remember_history_event_id(self, event_id: str) -> None:
        if event_id in self._history_seen_event_ids:
            return
        if len(self._history_seen_order) >= MAX_HISTORY_EVENT_IDS:
            oldest = self._history_seen_order.popleft()
            self._history_seen_event_ids.discard(oldest)
        self._history_seen_order.append(event_id)
        self._history_seen_event_ids.add(event_id)

    def _mark_event_seen(self, event: Mapping[str, Any]) -> bool:
        if self._is_cross_source_duplicate(event):
            return False
        dedup_key = _event_dedup_key(event)
        if dedup_key in self._event_dedup_keys:
            return False
        if len(self._event_dedup_order) >= MAX_EVENT_DEDUP_KEYS:
            oldest = self._event_dedup_order.popleft()
            self._event_dedup_keys.discard(oldest)
        self._event_dedup_order.append(dedup_key)
        self._event_dedup_keys.add(dedup_key)
        self._remember_source_event(event)
        return True

    async def _enrich_event_identity(self, event: Mapping[str, Any]) -> dict[str, Any]:
        normalized_event = dict(event)
        if not self._should_enrich_event_identity(normalized_event):
            return normalized_event
        payload = await self._fetch_history_payload()
        if payload is None:
            return normalized_event
        self._refresh_recent_source_event_identities(payload)
        resolved = self._resolve_history_identity(normalized_event, payload)
        if resolved is None:
            return normalized_event
        normalized_event.update(resolved)
        return normalized_event

    def _should_enrich_event_identity(self, event: Mapping[str, Any]) -> bool:
        return bool(
            self._config.history_fallback_enabled
            and str(event.get("type") or "").strip() in {"danmaku", "super_chat", "gift", "guard"}
        )

    def _refresh_recent_source_event_identities(self, payload: Mapping[str, Any]) -> None:
        if not self._recent_source_events:
            return
        refreshed: deque[dict[str, Any]] = deque()
        for item in self._recent_source_events:
            updated = dict(item)
            if _is_anonymous_user_id(updated.get("user_id")) or _username_looks_masked(updated.get("username")):
                resolved = self._resolve_history_identity(updated, payload)
                if resolved is not None:
                    updated.update(resolved)
            refreshed.append(updated)
        self._recent_source_events = refreshed

    def _resolve_history_identity(
        self,
        event: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        text = str(event.get("text") or event.get("summary") or "").strip()
        if not text:
            return None
        timestamp = _normalize_timestamp_seconds(event.get("timestamp"))
        username = event.get("username")
        for candidate in extract_history_events(payload):
            if str(candidate.get("type") or "").strip() != "danmaku":
                continue
            if str(candidate.get("text") or "").strip() != text:
                continue
            if abs(_normalize_timestamp_seconds(candidate.get("timestamp")) - timestamp) > CROSS_SOURCE_DEDUP_WINDOW_SEC:
                continue
            candidate_user_id = str(candidate.get("user_id") or "").strip()
            candidate_username = candidate.get("username")
            if _is_anonymous_user_id(candidate_user_id):
                continue
            if not candidate_username or _username_looks_masked(candidate_username):
                continue
            if _usernames_look_compatible(candidate_username, username):
                return {
                    "user_id": candidate_user_id,
                    "username": str(candidate_username).strip(),
                }
        return None

    def _is_cross_source_duplicate(self, event: Mapping[str, Any]) -> bool:
        event_type = str(event.get("type") or "").strip()
        if event_type != "danmaku":
            return False
        text = str(event.get("text") or event.get("summary") or "").strip()
        if not text:
            return False
        timestamp = _normalize_timestamp_seconds(event.get("timestamp"))
        source = _event_source(event)
        user_id = str(event.get("user_id") or "").strip()
        username = event.get("username")

        for candidate in reversed(self._recent_source_events):
            if candidate["source"] == source:
                continue
            if candidate["type"] != event_type:
                continue
            if candidate["text"] != text:
                continue
            if abs(float(candidate["timestamp"]) - timestamp) > CROSS_SOURCE_DEDUP_WINDOW_SEC:
                continue
            candidate_user_id = str(candidate["user_id"] or "").strip()
            if candidate_user_id == user_id:
                return True
            if not (_is_anonymous_user_id(candidate_user_id) or _is_anonymous_user_id(user_id)):
                continue
            if _usernames_look_compatible(candidate["username"], username):
                return True
        return False

    def _remember_source_event(self, event: Mapping[str, Any]) -> None:
        source_event = {
            "source": _event_source(event),
            "type": str(event.get("type") or "").strip(),
            "text": str(event.get("text") or event.get("summary") or "").strip(),
            "timestamp": _normalize_timestamp_seconds(event.get("timestamp")),
            "user_id": str(event.get("user_id") or "").strip(),
            "username": str(event.get("username") or "").strip(),
        }
        if len(self._recent_source_events) >= MAX_RECENT_SOURCE_EVENTS:
            self._recent_source_events.popleft()
        self._recent_source_events.append(source_event)

    @staticmethod
    def _select_ws_url(conf: Mapping[str, Any]) -> str:
        urls = select_ws_urls(conf)
        return urls[0] if urls else ""

    @staticmethod
    def _select_ws_urls(conf: Mapping[str, Any]) -> list[str]:
        # 与 livehub/bilibili_protocol.select_ws_urls 共用同一实现（含 host/端口校验）
        return select_ws_urls(conf)

    def _next_ws_url(self, urls: list[str]) -> str:
        ordered = self._ordered_ws_urls(urls)
        return ordered[0] if ordered else ""

    def _ordered_ws_urls(self, urls: list[str]) -> list[str]:
        if not urls:
            return []
        normalized = [url for url in urls if url]
        if not normalized:
            return []
        if normalized != self._ws_url_candidates:
            self._ws_url_candidates = normalized
            self._ws_url_cursor %= len(normalized)
        index = self._ws_url_cursor % len(normalized)
        self._ws_url_cursor += 1
        return normalized[index:] + normalized[:index]

    async def _report_connection_opened_once(self) -> None:
        if self._connection_ready_reported:
            return
        if self._on_connection_opened is not None:
            await self._on_connection_opened()
        self._connection_ready_reported = True

    def _log_info(self, message: str) -> None:
        if self._logger is not None:
            self._logger.info(message)

    @staticmethod
    def _is_successful_auth_reply(packet: Mapping[str, Any]) -> bool:
        if str(packet.get("type") or "") != "auth_reply":
            return False
        raw = packet.get("raw")
        if not isinstance(raw, Mapping):
            return True
        code = raw.get("code")
        return code in {None, 0, "0"}

    def _log_warning(self, message: str) -> None:
        if self._logger is not None:
            self._logger.warning(message)

    def _log_error(self, message: str) -> None:
        if self._logger is not None:
            self._logger.error(message)

    def _log_unsupported_paid_command(self, packet: Mapping[str, Any]) -> None:
        command = str(packet.get("cmd") or "").split(":", 1)[0].strip()
        if not command:
            return
        if not any(keyword in command for keyword in ("SUPER_CHAT", "GIFT", "GUARD")):
            return
        if command in self._logged_unsupported_paid_commands:
            return
        self._logged_unsupported_paid_commands.add(command)
        self._log_warning(f"Bilibili live dropped unsupported paid command: cmd={command}")

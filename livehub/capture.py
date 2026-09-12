"""B 站直播间弹幕采集器。

独立实现 B 站直播 WebSocket 协议（不依赖插件包的 bilibili_codec / bilibili_transport）：
- 通过 getConf 接口获取弹幕服务器地址与认证 token
- 并行建立多条 WebSocket 连接，发送认证包与心跳包
- 解析二进制包（含 zlib 压缩），将业务消息规范化为统一事件字典
- 断线自动重连（指数退避）

**协议同步说明**：本模块的包编解码（build_packet / parse_packets / normalize_event
等）与插件侧 ``bilibili_codec.py`` 为独立进程内的同步实现，任何协议级改动必须
同时在两处应用；一致性由 ``tests/test_livehub_codec.py`` 的交叉用例保证。

事件字典结构（供 LiveHub 汇聚后广播）：
{
    "event_id": str,
    "type": "danmaku" | "super_chat" | "gift" | "guard",
    "text": str,
    "summary": str,
    "user_id": str,
    "username": str,
    "timestamp": float,
    "raw": dict,
}
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from collections import deque
from typing import Any, Awaitable, Callable, Mapping

try:
    from aiohttp import ClientSession, ClientTimeout, WSMsgType

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from .bilibili_protocol import (
    DANMU_CONF_URL,
    build_auth_packet,
    build_heartbeat_packet,
    normalize_event,
    parse_packets,
    select_ws_urls,
)
# 事件去重窗口秒数：多条并行 WS 连接会收到相同弹幕，按 event_id 在此窗口内去重
DEDUP_WINDOW_SEC = 10.0

# 重连指数退避上限
MAX_RECONNECT_DELAY_SEC = 60.0

CaptureEventHandler = Callable[[Mapping[str, Any]], Awaitable[None]]


# --------------------------------------------------------------------------- #
class BilibiliCapture:
    """B 站直播间弹幕采集器。

    并行连接多条弹幕服务器，auth 成功后持续接收弹幕事件并交给 on_event 回调。
    任一连接断开都会触发整体重连。
    """

    def __init__(
        self,
        *,
        room_id: int,
        on_event: CaptureEventHandler,
        parallel_ws_connections: int = 3,
        heartbeat_interval_sec: float = 30.0,
        reconnect_delay_sec: float = 5.0,
        connect_timeout_sec: float = 10.0,
        logger: Any = None,
    ) -> None:
        self.room_id = max(0, int(room_id))
        self.on_event = on_event
        self.parallel_ws_connections = max(1, int(parallel_ws_connections or 3))
        self.heartbeat_interval_sec = max(5.0, float(heartbeat_interval_sec or 30.0))
        self.reconnect_delay_sec = max(0.5, float(reconnect_delay_sec or 5.0))
        self.connect_timeout_sec = max(1.0, float(connect_timeout_sec or 10.0))
        self.logger = logger
        self._session: ClientSession | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._recent_event_ids: deque[tuple[str, float]] = deque(maxlen=1000)
        self._reconnect_attempts = 0

    async def start(self) -> None:
        """启动采集循环（后台任务）。"""
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="livehub.bilibili_capture")

    async def stop(self) -> None:
        """停止采集并关闭连接。"""
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._close_session()

    async def _run(self) -> None:
        while self._running:
            try:
                await self._ensure_session()
                targets, token = await self._resolve_connection_targets()
                if not targets:
                    self._log_warning("B 站弹幕配置未返回服务器地址，稍后重试")
                else:
                    connected_at = time.monotonic()
                    await self._connect_many_once(targets, token)
                    # 连接存活超过 5s 说明曾成功建立，重置重连退避计数
                    if time.monotonic() - connected_at > 5.0:
                        if self._reconnect_attempts > 0:
                            self._log_info("B 站弹幕连接恢复")
                        self._reconnect_attempts = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_warning(f"B 站采集循环失败: {exc}")
            finally:
                await self._close_session()
            if not self._running:
                return
            await asyncio.sleep(self._next_reconnect_delay())

    def _next_reconnect_delay(self) -> float:
        """指数退避重连间隔：base * 2^(n-1)，封顶 MAX_RECONNECT_DELAY_SEC。"""
        self._reconnect_attempts += 1
        exponent = max(0, self._reconnect_attempts - 1)
        return min(self.reconnect_delay_sec * (2**exponent), MAX_RECONNECT_DELAY_SEC)

    async def _ensure_session(self) -> None:
        if self._session is not None:
            return
        timeout = ClientTimeout(total=None, connect=self.connect_timeout_sec)
        self._session = ClientSession(headers={"User-Agent": "MaiBot-LiveHub/1.0"}, timeout=timeout)

    async def _close_session(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    async def _resolve_connection_targets(self) -> tuple[list[str], str]:
        """通过 getConf 接口获取弹幕服务器地址列表与认证 token。"""
        fallback_url = "wss://broadcastlv.chat.bilibili.com/sub"
        conf = await self._fetch_danmaku_conf()
        if not isinstance(conf, Mapping):
            return [fallback_url], ""
        token = str(conf.get("token") or "").strip()
        urls = select_ws_urls(conf)
        return (urls or [fallback_url]), token

    async def _fetch_danmaku_conf(self) -> Mapping[str, Any] | None:
        session = self._session
        if session is None or not self.room_id:
            return None
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://live.bilibili.com/{self.room_id}/",
        }
        try:
            async with session.get(
                DANMU_CONF_URL,
                params={"room_id": str(self.room_id), "platform": "pc", "player": "web"},
                headers=headers,
            ) as response:
                payload = await response.json(content_type=None)
        except Exception as exc:
            self._log_warning(f"B 站弹幕配置请求失败: {exc}")
            return None
        if not isinstance(payload, Mapping) or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, Mapping) else payload
            message = payload.get("message") if isinstance(payload, Mapping) else ""
            self._log_warning(f"B 站弹幕配置请求失败: code={code} message={message}")
            return None
        data = payload.get("data")
        return data if isinstance(data, Mapping) else None


    async def _connect_many_once(self, urls: list[str], token: str) -> None:
        """并行建立多条连接；任一连接异常退出则整体重连。"""
        slots = min(self.parallel_ws_connections, len(urls))
        if slots <= 0:
            raise ConnectionError("B 站弹幕 WebSocket 地址列表为空")
        rotated = urls[1:] + urls[:1] if len(urls) > 1 else urls
        connection_urls = [urls[0]] + [rotated[index % len(rotated)] for index in range(slots - 1)]
        tasks = [
            asyncio.create_task(
                self._connect_single_url_once(url, token),
                name=f"livehub.bilibili_capture.{index}",
            )
            for index, url in enumerate(connection_urls)
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _connect_single_url_once(self, ws_url: str, token: str) -> None:
        """单条连接的完整生命周期：连接 -> 认证 -> 心跳 -> 接收事件。"""
        session = self._session
        if session is None or not ws_url:
            raise ConnectionError("B 站弹幕 WebSocket 地址为空")
        try:
            ws = await session.ws_connect(ws_url, heartbeat=None)
        except Exception as exc:
            raise ConnectionError(f"B 站弹幕 WebSocket 连接失败: {ws_url} ({exc})") from exc
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws), name="livehub.bilibili_heartbeat")
        auth_ready = False
        try:
            await ws.send_bytes(build_auth_packet(self.room_id, token=token))
            # 接收超时必须大于心跳间隔（服务器仅在收到心跳后回包），避免冷清直播间误判断线
            receive_timeout = max(60.0, self.heartbeat_interval_sec * 2 + 5.0)
            while self._running and not ws.closed:
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=receive_timeout)
                except asyncio.TimeoutError as exc:
                    raise ConnectionError(f"B 站弹幕接收超时: {ws_url}") from exc
                if not self._running:
                    break
                if message.type in {WSMsgType.BINARY, WSMsgType.TEXT}:
                    payload = message.data if message.type == WSMsgType.BINARY else message.data.encode("utf-8")
                    auth_ok, _ = await self._handle_binary(payload)
                    if auth_ok and not auth_ready:
                        auth_ready = True
                        self._log_info(f"B 站弹幕连接认证成功: url={ws_url}")
                    elif not auth_ready:
                        raise ConnectionError(f"B 站弹幕连接认证未完成: {ws_url}")
                elif message.type in {WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            with contextlib.suppress(Exception):
                await ws.close()

    async def _heartbeat_loop(self, ws: Any) -> None:
        """按固定间隔发送心跳包。"""
        while self._running and not ws.closed:
            await asyncio.sleep(self.heartbeat_interval_sec)
            with contextlib.suppress(Exception):
                await ws.send_bytes(build_heartbeat_packet())

    async def _handle_binary(self, payload: bytes) -> tuple[bool, int]:
        """解析一帧数据；返回 (是否含成功 auth_reply, 交付事件数)。单次解析完成认证检查与事件交付。"""
        auth_ok = False
        delivered = 0
        for packet in parse_packets(payload):
            packet_type = packet.get("type")
            if packet_type == "auth_reply":
                raw = packet.get("raw")
                if isinstance(raw, Mapping) and raw.get("code") == 0:
                    auth_ok = True
                continue
            if packet_type == "heartbeat_reply":
                continue
            if packet.get("operation") is not None:
                # 未知 operation 包（如 popularity 广播）无业务意义
                continue
            raw_event = packet if isinstance(packet, Mapping) else {}
            normalized = normalize_event(raw_event)
            if normalized is not None and not self._is_duplicate_event(normalized):
                await self.on_event(normalized)
                delivered += 1
        return auth_ok, delivered

    def _is_duplicate_event(self, event: Mapping[str, Any], *, now: float | None = None) -> bool:
        """判断事件是否在去重窗口内已出现过（多路 WS 会收到相同弹幕流）。

        返回 True 表示该 event_id 在 DEDUP_WINDOW_SEC 内已上报过，应丢弃。
        """
        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            return False
        current_time = time.time() if now is None else now
        while self._recent_event_ids and self._recent_event_ids[0][1] < current_time - DEDUP_WINDOW_SEC:
            self._recent_event_ids.popleft()
        for seen_id, _ in self._recent_event_ids:
            if seen_id == event_id:
                return True
        self._recent_event_ids.append((event_id, current_time))
        return False

    def _log_info(self, message: str) -> None:
        if self.logger is not None:
            self.logger.info(message)

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)
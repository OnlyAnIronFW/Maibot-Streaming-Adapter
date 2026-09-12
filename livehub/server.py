"""livehub aiohttp 服务端：HTTP API + WebSocket 广播 + B 站采集装配。

端点一览（与插件 hub_input_client.py / plugin.py 的调用完全对齐）：
- GET  /api/events?limit=N       事件历史轮询，返回 {events, participants, speaking, health}
- POST /api/client/presence      presence 心跳 {client_id, bot_name} -> 精简状态 {participants, speaking, health}
- POST /api/client/speak-request 语音请求 {request_id, client_id, ...} -> {granted, speaking}
- POST /api/client/speak-complete 语音释放 {request_id, client_id, status} -> {speaking}
- POST /api/client/reply         转发 bot 回复 {client_id, bot_name, text, ...} -> {success}
- POST /api/inject               手动注入本地事件（调试/外部输入源）
- GET  /ws                       WebSocket 事件流（snapshot / event / health）

错误响应统一为 {"success": false, "error": "..."} + 4xx 状态码。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time

from typing import Any, Mapping
from uuid import uuid4

try:
    from aiohttp import WSMsgType, web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    WSMsgType = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from .capture import BilibiliCapture
from .config import LiveHubConfig
from .hub import LiveHub

_REPLY_MAX_TEXT_LENGTH = 4000


class LiveHubServer:
    """livehub 服务进程：HTTP/WS 服务 + B 站采集 + 核心状态机。"""

    def __init__(self, config: LiveHubConfig, *, logger: logging.Logger | None = None) -> None:
        self._config = config
        self._logger = logger or logging.getLogger("livehub")
        self._hub = LiveHub(config, logger=self._logger)
        self._capture: BilibiliCapture | None = None
        self._ws_clients: set[Any] = set()
        self._runner: Any = None
        self._site: Any = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """启动 HTTP/WS 服务与 B 站采集；启动失败时回滚已启动的组件。"""
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError("aiohttp is required to run livehub")
        self._hub.set_broadcast_handler(self._broadcast)
        self._hub.start()
        app = self._build_app()
        self._runner = web.AppRunner(app)
        try:
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._config.host, self._config.port)
            await self._site.start()
        except Exception:
            await self.stop()
            raise
        self._logger.info(
            f"livehub listening on http://{self._config.host}:{self._config.port} "
            f"(room_id={self._config.room_id or '未配置，仅汇聚转发'})"
        )
        if self._config.room_id:
            self._capture = BilibiliCapture(
                room_id=self._config.room_id,
                on_event=self._on_capture_event,
                parallel_ws_connections=self._config.parallel_ws_connections,
                heartbeat_interval_sec=self._config.heartbeat_interval_sec,
                reconnect_delay_sec=self._config.reconnect_delay_sec,
                connect_timeout_sec=self._config.connect_timeout_sec,
                logger=self._logger,
            )
            await self._capture.start()
        else:
            self._logger.warning("room_id 未配置：livehub 仅作为事件汇聚/转发枢纽运行，不采集 B 站弹幕")

    async def stop(self) -> None:
        """停止服务与采集。

        先断开所有 WebSocket 客户端，再关闭站点与 Runner，避免 aiohttp shutdown
        等待活跃 handler（_handle_ws 的 async for）导致最长阻塞 shutdown_timeout。
        """
        capture = self._capture
        self._capture = None
        if capture is not None:
            await capture.stop()
        for ws in tuple(self._ws_clients):
            with contextlib.suppress(Exception):
                await ws.close()
        self._ws_clients.clear()
        site = self._site
        self._site = None
        if site is not None:
            with contextlib.suppress(Exception):
                await site.stop()
        runner = self._runner
        self._runner = None
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()
        await self._hub.stop()

    async def _on_capture_event(self, event: Mapping[str, Any]) -> None:
        """B 站采集回调：打上 origin 后汇入事件流。"""
        self._hub.publish_event({**dict(event), "origin": "bilibili"})

    # ------------------------------------------------------------------ #
    # aiohttp 应用
    # ------------------------------------------------------------------ #
    def _build_app(self) -> Any:
        # 显式限制请求体大小（默认 1MB），避免超大 body 拖垮解析
        app = web.Application(middlewares=[self._auth_middleware], client_max_size=1024 * 1024)
        app.router.add_get("/api/events", self._handle_events)
        app.router.add_post("/api/client/presence", self._handle_presence)
        app.router.add_post("/api/client/speak-request", self._handle_speak_request)
        app.router.add_post("/api/client/speak-complete", self._handle_speak_complete)
        app.router.add_post("/api/client/reply", self._handle_reply)
        app.router.add_post("/api/inject", self._handle_inject)
        app.router.add_get("/api/health", self._handle_health)
        app.router.add_get("/ws", self._handle_ws)
        return app

    @web.middleware
    async def _auth_middleware(self, request: Any, *, handler: Any) -> Any:
        """Bearer 令牌校验（auth_token 为空时不启用）。

        aiohttp 3.13 新式中间件：``@web.middleware`` 标记版本 1，
        调用时以 ``partial(m, handler=handler)`` 绑定 handler，签名 ``(request, *, handler)``。
        比较使用 hmac.compare_digest 避免时序侧信道。
        """

        token = self._config.auth_token
        if token:
            authorization = str(request.headers.get("Authorization") or "").strip()
            expected = f"Bearer {token}"
            if not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
                self._logger.warning(f"livehub 认证拒绝: 来源 {request.remote}")
                return web.json_response({"success": False, "error": "unauthorized"}, status=401)
        return await handler(request)

    # ------------------------------------------------------------------ #
    # HTTP 端点
    # ------------------------------------------------------------------ #
    async def _handle_events(self, request: Any) -> Any:
        limit_text = str(request.query.get("limit") or "").strip()
        try:
            limit = int(limit_text) if limit_text else 0
        except ValueError:
            limit = 0
        return web.json_response(self._hub.snapshot(limit=limit if limit > 0 else None))

    async def _handle_presence(self, request: Any) -> Any:
        payload = await self._read_json_body(request)
        if payload is None:
            return self._json_error("invalid JSON body")
        client_id = str(payload.get("client_id") or "").strip()
        if not client_id:
            return self._json_error("client_id is required")
        snapshot = self._hub.register_presence(
            client_id=client_id,
            bot_name=str(payload.get("bot_name") or ""),
            forward_user_id=str(payload.get("forward_user_id") or ""),
            forward_username=str(payload.get("forward_username") or ""),
        )
        return web.json_response(snapshot)

    async def _handle_speak_request(self, request: Any) -> Any:
        payload = await self._read_json_body(request)
        if payload is None:
            return self._json_error("invalid JSON body")
        if not str(payload.get("request_id") or "").strip() or not str(payload.get("client_id") or "").strip():
            return self._json_error("request_id and client_id are required")
        result = self._hub.request_speak(payload)
        return web.json_response(result)

    async def _handle_speak_complete(self, request: Any) -> Any:
        payload = await self._read_json_body(request)
        if payload is None:
            return self._json_error("invalid JSON body")
        if not str(payload.get("request_id") or "").strip() or not str(payload.get("client_id") or "").strip():
            return self._json_error("request_id and client_id are required")
        result = self._hub.complete_speak(
            request_id=str(payload.get("request_id") or ""),
            client_id=str(payload.get("client_id") or ""),
            status=str(payload.get("status") or "completed"),
        )
        return web.json_response(result)

    async def _handle_reply(self, request: Any) -> Any:
        """接收 bot 回复并广播为 bot_reply 事件，供其他接入的 bot 感知。

        兼容两种请求体形态：
        - 插件 JsonBridgeClient.send 的包装形态：{"id", "type", "timestamp", "source", "payload": {...}}
        - 直接形态：{"client_id", "bot_name", "text", ...}
        """
        payload = await self._read_json_body(request)
        if payload is None:
            return self._json_error("invalid JSON body")
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        # 兼容包装形态：内层缺失时回退外层取值
        client_id = str(inner.get("client_id") or payload.get("client_id") or "").strip()
        bot_name = str(inner.get("bot_name") or payload.get("bot_name") or client_id or "unknown").strip()
        text = str(inner.get("text") or payload.get("text") or "").strip()
        if not text:
            return self._json_error("empty reply text")
        if len(text) > _REPLY_MAX_TEXT_LENGTH:
            text = text[:_REPLY_MAX_TEXT_LENGTH]
        seq = self._hub.publish_event(
            {
                "event_id": f"bot-reply-{uuid4().hex}",
                "type": "bot_reply",
                "origin": "hub",
                "text": text,
                "summary": text,
                "user_id": client_id,
                "username": bot_name,
                "timestamp": time.time(),
                "raw": dict(inner),
            }
        )
        return web.json_response({"success": True, "seq": seq})

    async def _handle_inject(self, request: Any) -> Any:
        """手动注入 local_inject 事件（调试 / 外部输入源）。"""
        payload = await self._read_json_body(request)
        if payload is None:
            return self._json_error("invalid JSON body")
        text = str(payload.get("text") or "").strip()
        if not text:
            return self._json_error("empty inject text")
        seq = self._hub.publish_event(
            {
                "event_id": str(payload.get("event_id") or f"hub-local-{uuid4().hex}"),
                "type": "local_inject",
                "origin": str(payload.get("origin") or "hub"),
                "text": text,
                "summary": str(payload.get("summary") or text),
                "user_id": str(payload.get("user_id") or "hub-operator"),
                "username": str(payload.get("username") or "hub-operator"),
                "timestamp": time.time(),
                "raw": dict(payload),
            }
        )
        return web.json_response({"success": True, "seq": seq})

    async def _handle_health(self, request: Any) -> Any:
        return web.json_response(
            {
                "success": True,
                "config": self._config.to_dict(),
                "hub": self._hub.health_state(),
            }
        )

    # ------------------------------------------------------------------ #
    # WebSocket
    # ------------------------------------------------------------------ #
    async def _handle_ws(self, request: Any) -> Any:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        self._logger.info(f"Hub WebSocket 客户端连接: {request.remote}")
        try:
            # snapshot 发送失败（客户端过早断开）不向 aiohttp 冒泡，统一走 finally 清理
            with contextlib.suppress(Exception):
                await ws.send_json({"kind": "snapshot", **self._hub.snapshot()})
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    # 客户端当前不向服务端发送业务消息；忽略（保留兼容扩展空间）
                    continue
                if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
        finally:
            self._ws_clients.discard(ws)
            self._logger.info(f"Hub WebSocket 客户端断开: {request.remote}")
        return ws

    async def _broadcast(self, payload: Mapping[str, Any]) -> None:
        """向所有 WebSocket 客户端并行推送消息；单个客户端超时/失败不影响其他客户端。"""
        clients = tuple(self._ws_clients)
        if not clients:
            return
        results = await asyncio.gather(
            *(self._send_to_client(ws, payload) for ws in clients),
            return_exceptions=True,
        )
        for ws, result in zip(clients, results, strict=True):
            if isinstance(result, BaseException):
                self._logger.warning(f"Hub WebSocket 广播失败，移除客户端: {result}")
                with contextlib.suppress(Exception):
                    await ws.close()
                self._ws_clients.discard(ws)

    async def _send_to_client(self, ws: Any, payload: Mapping[str, Any]) -> None:
        """向单个客户端发送消息；5 秒超时视为慢客户端。"""
        await asyncio.wait_for(ws.send_json(dict(payload)), timeout=5.0)
    
    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _json_error(message: str, *, status: int = 400) -> Any:
        """构造统一的 JSON 错误响应。"""
        return web.json_response({"success": False, "error": message}, status=status)

    @staticmethod
    async def _read_json_body(request: Any) -> dict[str, Any] | None:
        """读取并解析 JSON 请求体；空体或非法 JSON 返回 None（调用方返回 400）。"""
        try:
            raw = await request.read()
        except Exception:
            return None
        if not raw:
            return None
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return None
        return dict(decoded) if isinstance(decoded, dict) else None

"""Live2D JSON bridge clients."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

import asyncio
import contextlib
import json
import time
from uuid import uuid4

try:
    from aiohttp import ClientSession, ClientTimeout, WSMsgType

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in stripped environments.
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from ..constants import LIVE2D_BRIDGE_SOURCE


@runtime_checkable
class Live2DBridgeProtocol(Protocol):
    """Small protocol used by the adaptive controller."""

    async def start(self) -> None:
        """Start the bridge if needed."""
        ...

    async def stop(self) -> None:
        """Stop the bridge."""
        ...

    async def send_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Send a bridge event."""
        ...

    async def request_capabilities(self) -> dict[str, Any] | None:
        """Request model capabilities if supported."""
        ...

    async def request_vts_parameters(self) -> dict[str, Any] | None:
        """Request VTube Studio Live2D parameters if supported."""
        ...


class InMemoryLive2DBridge:
    """Test bridge that records events in memory."""

    def __init__(self, capabilities: dict[str, Any] | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.capabilities = capabilities
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(event)
        self.events.append(payload)
        return {"success": True, "event_id": payload.get("id")}

    async def request_capabilities(self) -> dict[str, Any] | None:
        return self.capabilities

    async def request_vts_parameters(self) -> dict[str, Any] | None:
        return None


class JsonLive2DBridge:
    """HTTP/WebSocket JSON bridge for Live2D runtimes."""

    def __init__(
        self,
        *,
        http_url: str = "",
        websocket_url: str = "",
        auth_token: str = "",
        connect_timeout_sec: float = 10.0,
        logger: Any = None,
    ) -> None:
        self.http_url = str(http_url or "").strip()
        self.websocket_url = str(websocket_url or "").strip()
        self.auth_token = str(auth_token or "").strip()
        self.connect_timeout_sec = max(1.0, float(connect_timeout_sec or 10.0))
        self.logger = logger
        self._session: Any = None
        self._ws: Any = None
        self._send_lock = asyncio.Lock()

    async def start(self) -> None:
        if not AIOHTTP_AVAILABLE:
            self._log_warning("aiohttp is unavailable; Live2D bridge disabled")
            return
        if self._session is None:
            timeout = ClientTimeout(total=None, connect=self.connect_timeout_sec)
            self._session = ClientSession(headers=self._headers(), timeout=timeout)
        if self.websocket_url and self._ws is None:
            try:
                self._ws = await self._session.ws_connect(self.websocket_url)
            except Exception as exc:
                self._log_warning(f"Live2D bridge WebSocket connection failed: {_describe_exception(exc)}")
                self._ws = None

    async def stop(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        session = self._session
        self._session = None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    async def send_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._envelope(event)
        sent_ws = await self._send_ws(payload)
        sent_http = await self._send_http(payload)
        if sent_ws or sent_http:
            return {"success": True, "event_id": payload["id"]}
        return {"success": False, "error": "no Live2D bridge delivery succeeded", "event_id": payload["id"]}

    async def request_capabilities(self) -> dict[str, Any] | None:
        request = self._envelope({"type": "live2d.capabilities.request"})
        if self._ws is not None:
            try:
                async with self._send_lock:
                    await self._ws.send_str(json.dumps(request, ensure_ascii=False))
                    response = await asyncio.wait_for(
                        self._ws.receive(),
                        timeout=max(1.0, float(self.connect_timeout_sec)),
                    )
                if WSMsgType is not None and response.type == WSMsgType.TEXT:
                    payload = json.loads(response.data)
                    return payload if isinstance(payload, dict) else None
            except Exception as exc:
                self._log_warning(f"Live2D capabilities request failed over WebSocket: {_describe_exception(exc)}")
        if self.http_url and self._session is not None:
            try:
                async with self._session.post(self.http_url, json=request) as response:
                    payload = await response.json(content_type=None)
                    return payload if isinstance(payload, dict) else None
            except Exception as exc:
                self._log_warning(f"Live2D capabilities request failed over HTTP: {_describe_exception(exc)}")
        return None

    async def request_vts_parameters(self) -> dict[str, Any] | None:
        """Request the currently loaded model parameters through the VTS public API."""

        if not self.websocket_url:
            return None
        if self._ws is None:
            await self.start()
        if self._ws is None:
            return None
        request_id = uuid4().hex
        request = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": request_id,
            "messageType": "Live2DParameterListRequest",
            "data": {},
        }
        try:
            async with self._send_lock:
                await self._ws.send_str(json.dumps(request, ensure_ascii=False))
                for _ in range(3):
                    response = await asyncio.wait_for(self._ws.receive(), timeout=self.connect_timeout_sec)
                    if WSMsgType is None or response.type != WSMsgType.TEXT:
                        continue
                    payload = json.loads(response.data)
                    if not isinstance(payload, Mapping):
                        continue
                    if payload.get("requestID") not in {"", request_id, None}:
                        continue
                    if payload.get("messageType") == "Live2DParameterListResponse":
                        data = payload.get("data")
                        if not isinstance(data, Mapping):
                            return None
                        return {
                            "type": "vts.live2d_parameter_list.response",
                            "model_id": data.get("modelID") or data.get("modelId") or "",
                            "model_name": data.get("modelName") or "",
                            "parameters": data.get("parameters") if isinstance(data.get("parameters"), list) else [],
                            "data": dict(data),
                        }
        except Exception as exc:
            self._log_warning(f"VTube Studio parameter request failed: {_describe_exception(exc)}")
        return None

    async def _send_ws(self, payload: Mapping[str, Any]) -> bool:
        if self.websocket_url and self._ws is None:
            await self.start()
        if self._ws is None:
            return False
        try:
            async with self._send_lock:
                await self._ws.send_str(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception as exc:
            self._log_warning(f"Live2D bridge WebSocket send failed: {_describe_exception(exc)}")
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
            return False

    async def _send_http(self, payload: Mapping[str, Any]) -> bool:
        if not self.http_url:
            return False
        if self._session is None:
            await self.start()
        if self._session is None:
            return False
        try:
            async with self._session.post(self.http_url, json=payload) as response:
                if response.status < 400:
                    return True
                self._log_warning(f"Live2D bridge HTTP returned status {response.status}")
        except Exception as exc:
            self._log_warning(f"Live2D bridge HTTP send failed: {_describe_exception(exc)}")
        return False

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "MaiBot-Bilibili-Live-Adapter/0.1"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def _envelope(self, event: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(event)
        payload.setdefault("id", uuid4().hex)
        payload.setdefault("timestamp", time.time())
        payload.setdefault("source", LIVE2D_BRIDGE_SOURCE)
        return payload

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)


def _describe_exception(exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__

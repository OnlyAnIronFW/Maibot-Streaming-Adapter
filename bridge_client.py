"""Generic JSON bridge used for game/display integrations."""

from __future__ import annotations

from typing import Any, Mapping

import contextlib
import json
import time
from uuid import uuid4

try:
    from aiohttp import ClientSession, ClientTimeout

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False


class JsonBridgeClient:
    """Best-effort HTTP/WebSocket JSON bridge."""

    def __init__(
        self,
        *,
        name: str,
        http_url: str = "",
        websocket_url: str = "",
        auth_token: str = "",
        connect_timeout_sec: float = 10.0,
        logger: Any = None,
    ) -> None:
        self.name = name
        self.http_url = str(http_url or "").strip()
        self.websocket_url = str(websocket_url or "").strip()
        self.auth_token = str(auth_token or "").strip()
        self.connect_timeout_sec = max(1.0, float(connect_timeout_sec or 10.0))
        self.logger = logger
        self._session: Any = None
        self._ws: Any = None

    async def start(self) -> None:
        if not AIOHTTP_AVAILABLE:
            self._log_warning(f"{self.name} bridge disabled because aiohttp is unavailable")
            return
        if self._session is None:
            timeout = ClientTimeout(total=None, connect=self.connect_timeout_sec)
            self._session = ClientSession(headers=self._headers(), timeout=timeout)
        if self.websocket_url and self._ws is None:
            try:
                self._ws = await self._session.ws_connect(self.websocket_url)
            except Exception as exc:
                self._log_warning(f"{self.name} bridge WebSocket connection failed: {exc}")
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

    async def send(self, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        event = {
            "id": uuid4().hex,
            "type": event_type,
            "timestamp": time.time(),
            "source": "maibot_bilibili_live_adapter",
            "payload": dict(payload),
        }
        sent_ws = await self._send_ws(event)
        sent_http = await self._send_http(event)
        if sent_ws or sent_http:
            return {"success": True, "event_id": event["id"]}
        return {"success": False, "error": f"{self.name} bridge delivery failed", "event_id": event["id"]}

    async def _send_ws(self, event: Mapping[str, Any]) -> bool:
        if self.websocket_url and self._ws is None:
            await self.start()
        if self._ws is None:
            return False
        try:
            await self._ws.send_str(json.dumps(event, ensure_ascii=False))
            return True
        except Exception as exc:
            self._log_warning(f"{self.name} bridge WebSocket send failed: {exc}")
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
            return False

    async def _send_http(self, event: Mapping[str, Any]) -> bool:
        if not self.http_url:
            return False
        if self._session is None:
            await self.start()
        if self._session is None:
            return False
        try:
            async with self._session.post(self.http_url, json=event) as response:
                if response.status < 400:
                    return True
                self._log_warning(f"{self.name} bridge HTTP returned {response.status}")
        except Exception as exc:
            self._log_warning(f"{self.name} bridge HTTP send failed: {exc}")
        return False

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "MaiBot-Bilibili-Live-Adapter/0.1"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)

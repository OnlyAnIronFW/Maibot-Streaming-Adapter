"""Message gateway runtime-state helper."""

from __future__ import annotations

import asyncio

from typing import Any, Protocol

from .constants import GATEWAY_NAME, PLATFORM_NAME, PROTOCOL_NAME
from .config import LiveAdapterSettings


class _GatewayCapabilityProtocol(Protocol):
    async def update_state(
        self,
        gateway_name: str,
        *,
        ready: bool,
        platform: str = "",
        account_id: str = "",
        scope: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        ...


class LiveAdapterRuntimeState:
    """Report gateway readiness to MaiBot Host."""

    def __init__(self, gateway_capability: _GatewayCapabilityProtocol, logger: Any) -> None:
        self._gateway_capability = gateway_capability
        self._logger = logger
        self._ready = False
        self._ready_event = asyncio.Event()

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def wait_until_ready(self) -> None:
        if self._ready:
            return
        await self._ready_event.wait()

    async def report_ready(self, settings: LiveAdapterSettings) -> bool:
        account_id = settings.identity.bot_user_id
        scope = settings.route_scope()
        try:
            accepted = await self._gateway_capability.update_state(
                gateway_name=GATEWAY_NAME,
                ready=True,
                platform=PLATFORM_NAME,
                account_id=account_id,
                scope=scope,
                metadata={
                    "protocol": PROTOCOL_NAME,
                    "room_id": settings.bilibili.room_id,
                    "live2d_enabled": settings.live2d.enabled,
                    "game_enabled": settings.game.enabled,
                },
            )
        except Exception as exc:
            self._logger.warning(f"Bilibili live gateway readiness report failed: {exc}")
            return False
        self._ready = bool(accepted)
        if self._ready:
            self._ready_event.set()
        else:
            self._ready_event.clear()
        return self._ready

    async def report_disconnected(self) -> None:
        if not self._ready:
            self._ready_event.clear()
            return
        try:
            await self._gateway_capability.update_state(
                gateway_name=GATEWAY_NAME,
                ready=False,
                platform=PLATFORM_NAME,
            )
        except Exception as exc:
            self._logger.warning(f"Bilibili live gateway disconnect report failed: {exc}")
        finally:
            self._ready = False
            self._ready_event.clear()

    def mark_not_ready_locally(self) -> None:
        self._ready = False
        self._ready_event.clear()

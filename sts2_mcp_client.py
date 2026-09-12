"""MCP client wrapper for STS2-Agent."""

from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping, TextIO

import json
import os
import sys

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .config import STS2MCPConfig


class STS2MCPClient:
    """Connect to an STS2-Agent MCP server and expose guided gameplay calls."""

    def __init__(self, config: STS2MCPConfig, *, logger: Any = None, stderr: TextIO | None = None) -> None:
        self.config = config
        self.logger = logger
        self.stderr = stderr
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def start(self) -> None:
        if self._session is not None:
            return
        stack = AsyncExitStack()
        try:
            self._log_info("Starting STS2 MCP transport")
            read_stream, write_stream = await self._open_transport(stack)
            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=max(0.1, self.config.connect_timeout_sec)),
                )
            )
            await session.initialize()
            self._log_info("STS2 MCP session initialized")
        except Exception:
            self._log_exception("Failed to start STS2 MCP session")
            await stack.aclose()
            raise
        self._exit_stack = stack
        self._session = session

    async def stop(self) -> None:
        self._log_info("Stopping STS2 MCP session")
        stack = self._exit_stack
        self._session = None
        self._exit_stack = None
        if stack is not None:
            await stack.aclose()

    async def health_check(self) -> dict[str, Any]:
        result = await self.call_tool("health_check")
        return _ensure_dict(result)

    async def get_game_state(self) -> dict[str, Any]:
        result = await self.call_tool("get_game_state")
        return _ensure_dict(result)

    async def get_available_actions(self) -> list[dict[str, Any]]:
        result = await self.call_tool("get_available_actions")
        return _ensure_action_list(result)

    async def wait_until_actionable(self, *, timeout_seconds: float) -> dict[str, Any]:
        result = await self.call_tool(
            "wait_until_actionable",
            {"timeout_seconds": max(0.1, float(timeout_seconds))},
            timeout_seconds=max(float(timeout_seconds) + 1.0, self.config.action_timeout_sec),
        )
        return _ensure_dict(result)

    async def act(
        self,
        *,
        action: str,
        card_index: int | None = None,
        target_index: int | None = None,
        option_index: int | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"action": str(action or "").strip()}
        if card_index is not None:
            arguments["card_index"] = int(card_index)
        if target_index is not None:
            arguments["target_index"] = int(target_index)
        if option_index is not None:
            arguments["option_index"] = int(option_index)
        return _ensure_dict(
            await self.call_tool(
                "act",
                arguments,
                timeout_seconds=self.config.action_timeout_sec,
            )
        )

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        if self._session is None:
            await self.start()
        if self._session is None:
            raise RuntimeError("STS2 MCP session is not available.")
        call_arguments = dict(arguments or {})
        self._log_debug(f"Calling STS2 MCP tool: name={name} arguments={_safe_json(call_arguments)}")
        try:
            result = await self._session.call_tool(
                name,
                arguments=call_arguments,
                read_timeout_seconds=timedelta(
                    seconds=max(0.1, float(timeout_seconds or self.config.action_timeout_sec))
                ),
            )
            normalized = _normalize_tool_result(result)
            self._log_debug(f"STS2 MCP tool returned: name={name} result={_safe_json(_summarize_result(normalized))}")
            return normalized
        except Exception:
            self._log_exception(f"STS2 MCP tool failed: name={name} arguments={_safe_json(call_arguments)}")
            raise

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        transport = self.config.transport.strip().lower()
        if transport in {"", "stdio"}:
            cwd = _optional_path(self.config.server_cwd)
            self._log_info(
                "Opening STS2 MCP stdio server: "
                f"command={self.config.server_command!r} args={list(self.config.server_args)!r} cwd={str(cwd or '')!r} "
                f"api_base_url={self.config.api_base_url!r} tool_profile={self.config.tool_profile!r}"
            )
            params = StdioServerParameters(
                command=self.config.server_command,
                args=list(self.config.server_args),
                cwd=cwd,
                env=self._build_stdio_env(),
            )
            return await stack.enter_async_context(stdio_client(params, errlog=self.stderr or sys.stderr))
        if transport in {"streamable_http", "http", "mcp_http"}:
            if not self.config.streamable_http_url:
                raise RuntimeError("sts2.mcp.streamable_http_url is required for streamable_http transport.")
            self._log_info(f"Opening STS2 MCP streamable HTTP transport: {self.config.streamable_http_url}")
            read_stream, write_stream, _session_id_getter = await stack.enter_async_context(
                streamablehttp_client(
                    self.config.streamable_http_url,
                    timeout=self.config.connect_timeout_sec,
                    sse_read_timeout=max(30.0, self.config.wait_actionable_timeout_sec),
                )
            )
            return read_stream, write_stream
        if transport == "sse":
            if not self.config.sse_url:
                raise RuntimeError("sts2.mcp.sse_url is required for sse transport.")
            self._log_info(f"Opening STS2 MCP SSE transport: {self.config.sse_url}")
            return await stack.enter_async_context(
                sse_client(
                    self.config.sse_url,
                    timeout=self.config.connect_timeout_sec,
                    sse_read_timeout=max(30.0, self.config.wait_actionable_timeout_sec),
                )
            )
        raise RuntimeError(f"Unsupported STS2 MCP transport: {self.config.transport}")

    def _build_stdio_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.config.api_base_url:
            env["STS2_API_BASE_URL"] = self.config.api_base_url
        env["STS2_API_TIMEOUT_SECONDS"] = str(max(0.1, self.config.action_timeout_sec))
        env["STS2_MCP_TOOL_PROFILE"] = self.config.tool_profile or "guided"
        return env

    def _log_debug(self, message: str) -> None:
        _log(self.logger, "debug", message)

    def _log_info(self, message: str) -> None:
        _log(self.logger, "info", message)

    def _log_exception(self, message: str) -> None:
        _log(self.logger, "exception", message)


def _normalize_tool_result(result: Any) -> Any:
    for attr in ("structured_content", "structuredContent"):
        value = getattr(result, attr, None)
        if value not in (None, {}):
            return value
    if isinstance(result, (dict, list)):
        return result
    content = getattr(result, "content", None)
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if text is None and isinstance(item, Mapping):
                text = item.get("text")
            if text is not None:
                text_parts.append(str(text))
        text = "\n".join(text_parts).strip()
        if text:
            with_json = _try_json_loads(text)
            return with_json if with_json is not None else {"text": text}
    return {}


def _ensure_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {"value": value}


def _ensure_action_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        for key in ("available_actions", "actions"):
            actions = value.get(key)
            if isinstance(actions, list):
                return [dict(item) for item in actions if isinstance(item, Mapping)]
    return []


def _optional_path(raw_path: str) -> Path | None:
    normalized = str(raw_path or "").strip()
    return Path(normalized).expanduser().resolve() if normalized else None


def _try_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:4000]
    except Exception:
        return repr(value)[:4000]


def _summarize_result(value: Any) -> Any:
    if isinstance(value, Mapping):
        summary = dict(value)
        for key in list(summary):
            if key.lower() in {"state", "available_actions", "actions", "cards", "monsters"}:
                item = summary[key]
                if isinstance(item, list):
                    summary[key] = f"<list len={len(item)}>"
                elif isinstance(item, Mapping):
                    summary[key] = f"<dict keys={len(item)}>"
        return summary
    if isinstance(value, list):
        return f"<list len={len(value)}>"
    return value


def _log(logger: Any, method: str, message: str) -> None:
    if logger is None:
        return
    try:
        getattr(logger, method)(message)
    except AttributeError:
        try:
            logger.info(message)
        except Exception:
            return

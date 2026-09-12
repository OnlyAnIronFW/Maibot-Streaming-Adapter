"""Interactively inject simulated Bilibili danmaku into a running MaiBot instance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time
import tomllib
from uuid import uuid4


SCRIPT_PATH = Path(__file__).resolve()
# 当前插件目录（tools/ 的上一级）；宿主为 MaiBot 主仓根（供 src.* 导入）
PLUGIN_DIR = SCRIPT_PATH.parents[1]
MAIBOT_ROOT = PLUGIN_DIR.parent
DEFAULT_BOT_CONFIG_PATH = MAIBOT_ROOT / "config" / "bot_config.toml"
DEFAULT_PLUGIN_CONFIG_PATH = PLUGIN_DIR / "config.toml"
DEFAULT_URL_PATH = "/ws"
DEFAULT_PLATFORM = "bilibili_live"
DEFAULT_ROOM_ID = 4538234
DEFAULT_USER_ID = "10001"
DEFAULT_USERNAME = "danmaku_tester"
DEFAULT_REASON = "manual_test"
DEFAULT_API_KEY = "maibot-bilibili-injector"

for import_path in (str(MAIBOT_ROOT), str(PLUGIN_DIR)):
    if import_path in sys.path:
        sys.path.remove(import_path)
    sys.path.insert(0, import_path)

from maim_message import MessageConverter  # noqa: E402
from maim_message.client_factory import create_client_config  # noqa: E402
from maim_message.client_ws_api import WebSocketClient  # noqa: E402
from maim_message.message_base import BaseMessageInfo, GroupInfo, MessageBase, Seg, UserInfo  # noqa: E402

from src.common.utils.utils_session import SessionUtils  # noqa: E402


@dataclass(slots=True)
class ConnectionSettings:
    """Connection settings for the Additional API server."""

    url: str
    api_key: str


@dataclass(slots=True)
class InjectorState:
    """Mutable REPL state for repeated danmaku injections."""

    room_id: int
    user_id: str
    username: str
    qq_account: str
    bot_user_id: str
    route_scope: str
    platform: str
    reason: str
    sent_count: int = 0

    def scope(self) -> str:
        return str(self.route_scope or self.room_id)


@dataclass(slots=True)
class ReplAction:
    """Result of interpreting one REPL line."""

    kind: str
    message: str = ""
    text: str = ""


class TerminalLogger:
    """Small logger for the WebSocket client."""

    def info(self, message: str) -> None:
        print(f"[info] {message}", flush=True)

    def warning(self, message: str) -> None:
        print(f"[warn] {message}", file=sys.stderr, flush=True)

    def error(self, message: str) -> None:
        print(f"[error] {message}", file=sys.stderr, flush=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Type simulated Bilibili danmaku and inject them into a running MaiBot reply chain.",
    )
    parser.add_argument(
        "--bot-config",
        type=Path,
        default=DEFAULT_BOT_CONFIG_PATH,
        help=f"Path to bot_config.toml. Default: {DEFAULT_BOT_CONFIG_PATH}",
    )
    parser.add_argument(
        "--plugin-config",
        type=Path,
        default=DEFAULT_PLUGIN_CONFIG_PATH,
        help=f"Path to the Bilibili live adapter config.toml. Default: {DEFAULT_PLUGIN_CONFIG_PATH}",
    )
    parser.add_argument("--url", default="", help="Override Additional API server URL, e.g. ws://127.0.0.1:18040/ws")
    parser.add_argument("--api-key", default="", help="Override Additional API server API key.")
    parser.add_argument("--room-id", type=int, default=0, help="Override the default Bilibili room id.")
    parser.add_argument("--user-id", default=DEFAULT_USER_ID, help="Default simulated Bilibili uid.")
    parser.add_argument("--username", default=DEFAULT_USERNAME, help="Default simulated Bilibili username.")
    parser.add_argument("--route-scope", default="", help="Override platform_io_scope in injected messages.")
    parser.add_argument("--reason", default=DEFAULT_REASON, help="Injected live_selection_reason marker.")
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=5.0,
        help="Seconds to wait for the Additional API client to become connected.",
    )
    return parser.parse_args(argv)


def load_defaults(*, bot_config_path: Path, plugin_config_path: Path) -> tuple[ConnectionSettings, InjectorState]:
    bot_config = _load_toml_file(bot_config_path)
    plugin_config = _load_toml_file(plugin_config_path)

    bot_section = bot_config.get("bot", {})
    maim_section = bot_config.get("maim_message", {})
    bilibili_section = plugin_config.get("bilibili", {})
    identity_section = plugin_config.get("identity", {})

    use_wss = bool(maim_section.get("api_server_use_wss", False))
    host = _normalize_client_host(str(maim_section.get("api_server_host") or "127.0.0.1").strip())
    port = int(maim_section.get("api_server_port") or 8090)
    api_keys = maim_section.get("api_server_allowed_api_keys") or []
    api_key = str(api_keys[0]).strip() if isinstance(api_keys, list) and api_keys else DEFAULT_API_KEY

    connection = ConnectionSettings(
        url=_build_server_url(host=host, port=port, use_wss=use_wss),
        api_key=api_key,
    )
    state = InjectorState(
        room_id=int(bilibili_section.get("room_id") or DEFAULT_ROOM_ID),
        user_id=DEFAULT_USER_ID,
        username=DEFAULT_USERNAME,
        qq_account=str(bot_section.get("qq_account") or "").strip(),
        bot_user_id=str(identity_section.get("bot_user_id") or "maibot-live").strip() or "maibot-live",
        route_scope=str(identity_section.get("route_scope") or "").strip(),
        platform=DEFAULT_PLATFORM,
        reason=DEFAULT_REASON,
    )
    return connection, state


def build_api_message(text: str, *, state: InjectorState, api_key: str, message_id: str = "") -> object:
    """Build an APIMessageBase payload that mimics the live adapter's danmaku injection."""

    room_id = str(state.room_id)
    user_id = str(state.user_id).strip()
    username = str(state.username).strip() or user_id or DEFAULT_USERNAME
    normalized_text = str(text).strip()
    normalized_message_id = str(message_id).strip() or f"bilibili-manual-{uuid4().hex}"
    timestamp = time.time()
    qq_account = state.qq_account if state.qq_account not in {"", "0"} else None
    memory_chat_id = SessionUtils.calculate_session_id("qq", group_id=room_id, account_id=qq_account)

    legacy_message = MessageBase(
        message_info=BaseMessageInfo(
            platform=state.platform,
            message_id=normalized_message_id,
            time=timestamp,
            group_info=GroupInfo(
                platform=state.platform,
                group_id=room_id,
                group_name=f"bilibili_live_{room_id}",
            ),
            user_info=UserInfo(
                platform=state.platform,
                user_id=user_id,
                user_nickname=username,
                user_cardname=None,
            ),
            additional_config={
                "platform_io_account_id": state.bot_user_id,
                "platform_io_scope": state.scope(),
                "live_event_type": "danmaku",
                "live_selection_reason": state.reason,
                "maibot_memory_platform": "qq",
                "maibot_memory_user_id": user_id,
                "maibot_memory_group_id": room_id,
                "maibot_memory_chat_id": memory_chat_id,
                "maibot_local_render_only": True,
            },
        ),
        message_segment=Seg(type="text", data=normalized_text),
        raw_message=normalized_text,
    )
    return MessageConverter.to_api_receive(legacy_message, api_key=api_key, platform=state.platform)


def interpret_repl_input(line: str, state: InjectorState) -> ReplAction:
    """Interpret one REPL line and update state for command inputs."""

    normalized = _normalize_repl_line(line)
    if not normalized:
        return ReplAction(kind="noop")
    if not normalized.startswith("/"):
        return ReplAction(kind="send", text=normalized)

    command, _, remainder = normalized.partition(" ")
    payload = remainder.strip()
    if command in {"/quit", "/exit"}:
        return ReplAction(kind="quit", message="bye")
    if command in {"/state", "/show"}:
        return ReplAction(kind="print", message=format_state(state))
    if command == "/help":
        return ReplAction(kind="print", message=_help_text())
    if command == "/room":
        if not payload.isdigit():
            return ReplAction(kind="print", message="usage: /room <room_id>")
        state.room_id = int(payload)
        return ReplAction(kind="print", message=f"room_id -> {state.room_id}")
    if command == "/user":
        user_id, user_name = _split_user_payload(payload)
        if not user_id:
            return ReplAction(kind="print", message="usage: /user <uid> [username]")
        state.user_id = user_id
        state.username = user_name or f"user_{user_id}"
        return ReplAction(kind="print", message=f"user -> {state.username}({state.user_id})")
    if command == "/name":
        if not payload:
            return ReplAction(kind="print", message="usage: /name <username>")
        state.username = payload
        return ReplAction(kind="print", message=f"username -> {state.username}")
    return ReplAction(kind="print", message=f"unknown command: {command}\n{_help_text()}")


def format_state(state: InjectorState) -> str:
    """Format the current REPL state for terminal diagnostics."""

    return (
        f"room_id={state.room_id} "
        f"user={state.username}({state.user_id}) "
        f"scope={state.scope()} "
        f"sent={state.sent_count}"
    )


async def main_async(argv: Sequence[str] | None = None) -> int:
    _configure_library_logging()
    args = parse_args(argv)
    connection, state = load_defaults(
        bot_config_path=args.bot_config,
        plugin_config_path=args.plugin_config,
    )
    if args.url:
        connection.url = str(args.url).strip()
    if args.api_key:
        connection.api_key = str(args.api_key).strip()
    if args.room_id > 0:
        state.room_id = int(args.room_id)
    state.user_id = str(args.user_id).strip() or state.user_id
    state.username = str(args.username).strip() or state.username
    state.reason = str(args.reason).strip() or state.reason
    if args.route_scope:
        state.route_scope = str(args.route_scope).strip()

    client_config = create_client_config(
        connection.url,
        connection.api_key,
        platform=state.platform,
    )
    client = WebSocketClient(client_config)

    await client.start()
    connect_requested = await client.connect()
    if not connect_requested:
        print("[error] failed to request Additional API connection", file=sys.stderr, flush=True)
        return 1
    connected = await _wait_for_client_connected(client, timeout_sec=max(0.5, float(args.connect_timeout)))
    if not connected:
        last_error = client.get_last_error() or "connection timeout"
        print(f"[error] failed to connect to {connection.url}: {last_error}", file=sys.stderr, flush=True)
        return 1

    print(f"[ready] connected to {connection.url}", flush=True)
    print(f"[state] {format_state(state)}", flush=True)
    print(_help_text(), flush=True)

    while True:
        try:
            line = await asyncio.to_thread(input, "danmaku> ")
        except EOFError:
            print("[exit] stdin closed", flush=True)
            break
        except KeyboardInterrupt:
            print("\n[exit] interrupted", flush=True)
            break

        action = interpret_repl_input(line, state)
        if action.kind == "noop":
            continue
        if action.kind == "quit":
            print("[exit] user requested shutdown", flush=True)
            break
        if action.kind == "print":
            print(action.message, flush=True)
            continue
        if action.kind != "send":
            print(f"[warn] unsupported action: {action.kind}", flush=True)
            continue

        api_message = build_api_message(action.text, state=state, api_key=connection.api_key)
        success = await client.send_message(api_message)
        if not success:
            last_error = client.get_last_error() or "send_message returned false"
            print(f"[error] failed to send danmaku: {last_error}", file=sys.stderr, flush=True)
            continue
        state.sent_count += 1
        print(
            "[sent] "
            f"#{state.sent_count} room={state.room_id} "
            f"user={state.username}({state.user_id}) "
            f"msg_id={api_message.get_message_id()} "
            f"text={action.text!r}",
            flush=True,
        )

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(_cli_exception_handler)
        return int(loop.run_until_complete(main_async(argv)))
    finally:
        with contextlib.suppress(Exception):
            loop.run_until_complete(loop.shutdown_asyncgens())
        asyncio.set_event_loop(None)
        loop.close()


def _load_toml_file(path: Path) -> dict:
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _normalize_client_host(host: str) -> str:
    normalized = str(host or "").strip()
    if normalized in {"", "0.0.0.0", "::"}:
        return "127.0.0.1"
    return normalized


def _build_server_url(*, host: str, port: int, use_wss: bool) -> str:
    scheme = "wss" if use_wss else "ws"
    return f"{scheme}://{host}:{int(port)}{DEFAULT_URL_PATH}"


def _split_user_payload(payload: str) -> tuple[str, str]:
    normalized = str(payload).strip()
    if not normalized:
        return "", ""
    parts = normalized.split(maxsplit=1)
    user_id = parts[0].strip()
    username = parts[1].strip() if len(parts) > 1 else ""
    return user_id, username


def _help_text() -> str:
    return (
        "Commands: /room <room_id>, /user <uid> [username], /name <username>, "
        "/state, /help, /quit. Any other line is sent as one simulated danmaku."
    )


async def _wait_for_client_connected(client: WebSocketClient, *, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if client.is_connected():
            return True
        await asyncio.sleep(0.1)
    return client.is_connected()


def _configure_library_logging() -> None:
    for logger_name in (
        "maim_message",
        "maim_message.ws_config",
        "maim_message.client_ws_connection",
        "maim_message.log_queue",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def _normalize_repl_line(line: str) -> str:
    normalized = str(line).strip().lstrip("\ufeff")
    if normalized.startswith("/"):
        return normalized
    slash_index = normalized.find("/")
    if 0 < slash_index <= 3:
        prefix = normalized[:slash_index]
        if all((not char.isascii()) or (not char.isalnum()) for char in prefix):
            return normalized[slash_index:]
    return normalized


def _cli_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    exception = context.get("exception")
    handle = context.get("handle")
    if isinstance(exception, asyncio.CancelledError) and "task_done_callback" in repr(handle):
        return
    loop.default_exception_handler(context)


if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(int(exit_code))

"""Watch a Bilibili live room with the plugin transport and print new danmaku."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import argparse
import asyncio
import json
import struct
import sys
import time
import tomllib
from collections import Counter


SCRIPT_PATH = Path(__file__).resolve()
# 当前插件目录（tools/ 的上一级）；宿主为 MaiBot 主仓根（供 src.* 与插件包导入）
PLUGIN_DIR = SCRIPT_PATH.parents[1]
MAIBOT_ROOT = PLUGIN_DIR.parent
DEFAULT_CONFIG_PATH = PLUGIN_DIR / "config.toml"
HEADER_STRUCT = struct.Struct(">IHHII")

for import_path in (str(MAIBOT_ROOT), str(PLUGIN_DIR)):
    if import_path in sys.path:
        sys.path.remove(import_path)
    sys.path.insert(0, import_path)

from maibot_bilibili_live_adapter_copy.bilibili_transport import BilibiliDanmakuTransport  # noqa: E402
from maibot_bilibili_live_adapter_copy.bilibili_codec import (  # noqa: E402
    build_auth_packet,
    build_heartbeat_packet,
    normalize_event,
    parse_packets,
)
from maibot_bilibili_live_adapter_copy.config import BilibiliConfig, LiveAdapterSettings  # noqa: E402


class TerminalLogger:
    """Small logger compatible with the plugin transport."""

    def info(self, message: str) -> None:
        print(f"[info] {message}", flush=True)

    def warning(self, message: str) -> None:
        print(f"[warn] {message}", file=sys.stderr, flush=True)

    def error(self, message: str) -> None:
        print(f"[error] {message}", file=sys.stderr, flush=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch Bilibili danmaku using maibot_bilibili_live_adapter's transport.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Plugin config.toml path. Default: {DEFAULT_CONFIG_PATH}",
    )
    parser.add_argument(
        "--room-id",
        type=int,
        default=0,
        help="Override bilibili.room_id from config.toml.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Stop after this many seconds. 0 means run until Ctrl+C.",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=0,
        help="Stop after printing this many events. 0 means unlimited.",
    )
    parser.add_argument(
        "--all-events",
        action="store_true",
        help="Also print normalized gift, guard, and Super Chat events.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print normalized event JSON instead of a compact line.",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Check token, auth reply, heartbeat, and raw Bilibili packet counters.",
    )
    return parser.parse_args(argv)


def load_settings(config_path: Path) -> LiveAdapterSettings:
    config_text = config_path.read_text(encoding="utf-8")
    raw_config = tomllib.loads(config_text)
    return LiveAdapterSettings.model_validate(raw_config)


def build_bilibili_config(settings: LiveAdapterSettings, room_id: int = 0) -> BilibiliConfig:
    if room_id > 0:
        return settings.bilibili.model_copy(update={"room_id": room_id})
    return settings.bilibili


def should_print_event(event: Mapping[str, Any], *, include_non_danmaku: bool) -> bool:
    event_type = str(event.get("type") or "").strip()
    return include_non_danmaku or event_type == "danmaku"


def iter_packet_headers(data: bytes) -> list[tuple[int, int]]:
    headers: list[tuple[int, int]] = []
    offset = 0
    data_length = len(data)
    while offset + HEADER_STRUCT.size <= data_length:
        packet_length, header_length, protocol_version, operation, _sequence = HEADER_STRUCT.unpack_from(data, offset)
        if packet_length < header_length or header_length < HEADER_STRUCT.size:
            break
        if offset + packet_length > data_length:
            break
        headers.append((protocol_version, operation))
        offset += packet_length
    return headers


def format_counter(counter: Mapping[Any, int]) -> str:
    if not counter:
        return "<none>"
    items = sorted((str(key), int(value)) for key, value in counter.items())
    return ", ".join(f"{key}={value}" for key, value in items)


def format_event(event: Mapping[str, Any], *, raw: bool = False) -> str:
    if raw:
        return json.dumps(dict(event), ensure_ascii=False, sort_keys=True, default=str)

    event_type = str(event.get("type") or "event").strip() or "event"
    username = str(event.get("username") or "anonymous")
    user_id = str(event.get("user_id") or "").strip()
    text = str(event.get("summary") or event.get("text") or "").strip()
    timestamp = _format_timestamp(event.get("timestamp"))
    sender = f"{username}({user_id})" if user_id and user_id != username else username
    return f"[{timestamp}] [{event_type}] {sender}: {text}"


async def diagnose(args: argparse.Namespace) -> int:
    try:
        import aiohttp
    except Exception as exc:
        print(f"[error] aiohttp is required for diagnostics: {exc}", file=sys.stderr, flush=True)
        return 2

    settings = load_settings(args.config)
    bilibili_config = build_bilibili_config(settings, room_id=args.room_id)
    duration = float(args.duration) if args.duration > 0 else 20.0
    timeout = aiohttp.ClientTimeout(total=max(1.0, float(bilibili_config.connect_timeout_sec)))

    print(f"[diagnose] room_id={bilibili_config.room_id} duration={duration:g}s", flush=True)
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers={"User-Agent": bilibili_config.user_agent},
    ) as session:
        await _print_room_info(session, bilibili_config)
        conf = await _fetch_danmaku_conf(session, bilibili_config)
        token = str(conf.get("token") or "").strip() if isinstance(conf, Mapping) else ""
        hosts = conf.get("host_server_list") if isinstance(conf, Mapping) else []
        ws_url = BilibiliDanmakuTransport._select_ws_url(conf) if isinstance(conf, Mapping) else bilibili_config.ws_url

        print(f"[diagnose] token_len={len(token)}", flush=True)
        print(f"[diagnose] host_count={len(hosts) if isinstance(hosts, list) else 0}", flush=True)
        print(f"[diagnose] ws_url={ws_url}", flush=True)
        if not token:
            print("[diagnose] token is empty; auth may fall back to anonymous direct WS.", file=sys.stderr, flush=True)

        if not ws_url:
            print("[error] no Bilibili WebSocket URL resolved.", file=sys.stderr, flush=True)
            return 2

        return await _diagnose_ws(session, bilibili_config, ws_url=ws_url, token=token, duration=duration)


async def _print_room_info(session: Any, config: BilibiliConfig) -> None:
    url = "https://api.live.bilibili.com/room/v1/Room/get_info"
    headers = {"Referer": f"https://live.bilibili.com/{config.room_id}/"}
    try:
        async with session.get(url, params={"room_id": str(config.room_id)}, headers=headers) as response:
            payload = await response.json(content_type=None)
    except Exception as exc:
        print(f"[diagnose] room_info failed: {exc}", file=sys.stderr, flush=True)
        return
    data = payload.get("data") if isinstance(payload, Mapping) else {}
    if not isinstance(data, Mapping):
        print(f"[diagnose] room_info returned: {payload}", flush=True)
        return
    print(
        "[diagnose] room_info "
        f"live_status={data.get('live_status')} online={data.get('online')} title={data.get('title')}",
        flush=True,
    )


async def _fetch_danmaku_conf(session: Any, config: BilibiliConfig) -> Mapping[str, Any] | None:
    url = "https://api.live.bilibili.com/room/v1/Danmu/getConf"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://live.bilibili.com/{config.room_id}/",
    }
    params = {
        "room_id": str(config.room_id),
        "platform": "pc",
        "player": "web",
    }
    try:
        async with session.get(url, params=params, headers=headers) as response:
            payload = await response.json(content_type=None)
    except Exception as exc:
        print(f"[diagnose] getConf failed: {exc}", file=sys.stderr, flush=True)
        return None
    print(
        f"[diagnose] getConf http_status={response.status} code={payload.get('code') if isinstance(payload, Mapping) else None}",
        flush=True,
    )
    if not isinstance(payload, Mapping) or payload.get("code") != 0:
        print(f"[diagnose] getConf payload={payload}", file=sys.stderr, flush=True)
        return None
    data = payload.get("data")
    return data if isinstance(data, Mapping) else None


async def _diagnose_ws(
    session: Any,
    config: BilibiliConfig,
    *,
    ws_url: str,
    token: str,
    duration: float,
) -> int:
    try:
        import aiohttp
    except Exception:
        return 2

    header_counts: Counter[str] = Counter()
    parsed_counts: Counter[str] = Counter()
    command_counts: Counter[str] = Counter()
    normalized_counts: Counter[str] = Counter()
    unparsed_frames = 0
    samples: list[str] = []
    auth_ok = False

    async with session.ws_connect(ws_url, heartbeat=None) as ws:
        await ws.send_bytes(build_auth_packet(config.room_id, uid=config.uid, token=token))
        started = time.monotonic()
        last_heartbeat = started
        while time.monotonic() - started < duration:
            if time.monotonic() - last_heartbeat >= min(10.0, max(1.0, float(config.heartbeat_interval_sec))):
                await ws.send_bytes(build_heartbeat_packet())
                last_heartbeat = time.monotonic()
            try:
                message = await ws.receive(timeout=2)
            except asyncio.TimeoutError:
                continue
            if message.type == aiohttp.WSMsgType.BINARY:
                raw = message.data
            elif message.type == aiohttp.WSMsgType.TEXT:
                raw = message.data.encode("utf-8")
            elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                print(f"[diagnose] websocket closed type={message.type}", file=sys.stderr, flush=True)
                break
            else:
                continue

            for protocol_version, operation in iter_packet_headers(raw):
                header_counts[f"proto{protocol_version}/op{operation}"] += 1
            packets = parse_packets(raw)
            if not packets:
                unparsed_frames += 1
                if len(samples) < 5:
                    samples.append(f"unparsed frame len={len(raw)} first16={raw[:16].hex()}")
            for packet in packets:
                packet_type = str(packet.get("type") or "")
                if packet_type:
                    parsed_counts[packet_type] += 1
                if packet_type == "auth_reply":
                    raw_reply = packet.get("raw")
                    auth_ok = not isinstance(raw_reply, Mapping) or raw_reply.get("code") in {None, 0, "0"}
                command = str(packet.get("cmd") or "").split(":", 1)[0]
                if command:
                    command_counts[command] += 1
                event = normalize_event(packet)
                if event is not None:
                    normalized_counts[str(event.get("type") or "")] += 1
                    if len(samples) < 5:
                        samples.append(format_event(event))
                elif len(samples) < 5 and (packet_type or command):
                    samples.append(json.dumps({"type": packet_type, "cmd": command}, ensure_ascii=False))

    print(f"[diagnose] auth_ok={auth_ok}", flush=True)
    print(f"[diagnose] header_counts={format_counter(header_counts)}", flush=True)
    print(f"[diagnose] parsed_counts={format_counter(parsed_counts)}", flush=True)
    print(f"[diagnose] command_counts={format_counter(command_counts)}", flush=True)
    print(f"[diagnose] normalized_counts={format_counter(normalized_counts)}", flush=True)
    print(f"[diagnose] unparsed_frames={unparsed_frames}", flush=True)
    if samples:
        print("[diagnose] samples:", flush=True)
        for sample in samples:
            print(f"  {sample}", flush=True)
    if auth_ok and not normalized_counts:
        print(
            "[diagnose] token and auth look OK; no normalized danmaku/gift/SC events arrived during this window.",
            flush=True,
        )
    return 0 if auth_ok else 1


async def watch(args: argparse.Namespace) -> int:
    try:
        __import__("aiohttp")
    except Exception as exc:
        print(f"[error] aiohttp is required for watcher mode: {exc}", file=sys.stderr, flush=True)
        return 2

    settings = load_settings(args.config)
    bilibili_config = build_bilibili_config(settings, room_id=args.room_id)
    logger = TerminalLogger()
    printed_events = 0
    stop_event = asyncio.Event()

    async def on_event(event: dict[str, Any]) -> None:
        nonlocal printed_events
        if not should_print_event(event, include_non_danmaku=bool(args.all_events)):
            return
        printed_events += 1
        print(format_event(event, raw=bool(args.raw)), flush=True)
        if args.max_events > 0 and printed_events >= args.max_events:
            stop_event.set()

    async def on_connection_opened() -> None:
        print(f"[ready] connected to Bilibili live room {bilibili_config.room_id}", flush=True)

    async def on_connection_closed() -> None:
        print("[closed] Bilibili live connection closed", file=sys.stderr, flush=True)

    transport = BilibiliDanmakuTransport(
        on_event=on_event,
        on_connection_opened=on_connection_opened,
        on_connection_closed=on_connection_closed,
        logger=logger,
    )
    transport.configure(bilibili_config)

    print(
        f"[start] room_id={bilibili_config.room_id} config={args.config} "
        "waiting for new danmaku; press Ctrl+C to stop.",
        flush=True,
    )
    await transport.start()
    try:
        if args.duration > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=float(args.duration))
            except asyncio.TimeoutError:
                pass
        else:
            await stop_event.wait()
    finally:
        await transport.stop()
    print(f"[done] printed_events={printed_events}", flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdout()
    args = parse_args(argv)
    try:
        if args.diagnose:
            return asyncio.run(diagnose(args))
        return asyncio.run(watch(args))
    except KeyboardInterrupt:
        print("\n[done] interrupted", flush=True)
        return 130


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _format_timestamp(value: Any) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        timestamp = datetime.now().timestamp()
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")


if __name__ == "__main__":
    raise SystemExit(main())

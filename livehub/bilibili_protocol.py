"""B 站直播 WebSocket 协议编解码、事件规范化与服务器地址解析（共享实现，单一事实来源）。

本模块不依赖任何插件宿主模块，供以下使用方复用：
- ``livehub/capture.py``：livehub 的 B 站弹幕采集器（本模块的原始实现方）
- ``bilibili_codec.py``（插件侧）：以 re-export 方式暴露相同 API，保证插件行为一致
- ``bilibili_transport.py``（插件侧）：服务器地址解析复用 ``select_ws_urls``

**维护约定**：协议级改动只需修改本文件一处，所有使用方自动同步；
两侧 API 兼容性由 ``tests/test_livehub_codec.py`` 的交叉用例锁定。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import time
import zlib

from typing import Any, Mapping

# B 站直播协议常量
BILIBILI_OPERATION_HEARTBEAT = 2
BILIBILI_OPERATION_HEARTBEAT_REPLY = 3
BILIBILI_OPERATION_MESSAGE = 5
BILIBILI_OPERATION_AUTH = 7
BILIBILI_OPERATION_AUTH_REPLY = 8
BILIBILI_PROTOCOL_ZLIB = 2

HEADER_STRUCT = struct.Struct(">IHHII")
HEADER_LENGTH = 16

DANMU_CONF_URL = "https://api.live.bilibili.com/room/v1/Danmu/getConf"

# 弹幕服务器 host 合法字符集（域名 / IPv4），用于收窄 getConf 响应注入面
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9.\-]+$")

# 毫秒级时间戳阈值：大于该值的数值视为毫秒而非秒
_MILLISECONDS_THRESHOLD = 32_503_680_000


# --------------------------------------------------------------------------- #
# 包编解码
# --------------------------------------------------------------------------- #
def build_packet(
    operation: int,
    payload: bytes | str | Mapping[str, Any] | None = None,
    *,
    protocol_version: int = 1,
    sequence: int = 1,
) -> bytes:
    """构建一个 B 站直播 WebSocket 数据包。"""
    payload_bytes = _to_payload_bytes(payload)
    packet_length = HEADER_LENGTH + len(payload_bytes)
    header = HEADER_STRUCT.pack(packet_length, HEADER_LENGTH, int(protocol_version), int(operation), int(sequence))
    return header + payload_bytes


def build_auth_packet(room_id: int, *, uid: int = 0, token: str = "") -> bytes:
    """构建匿名认证包。"""
    payload: dict[str, Any] = {
        "uid": max(0, int(uid)),
        "roomid": max(0, int(room_id)),
        "protover": BILIBILI_PROTOCOL_ZLIB,
        "platform": "web",
        "type": 2,
    }
    if token:
        payload["key"] = token
    return build_packet(BILIBILI_OPERATION_AUTH, payload, protocol_version=1)


def build_heartbeat_packet() -> bytes:
    """构建心跳包。"""
    return build_packet(BILIBILI_OPERATION_HEARTBEAT, b"", protocol_version=1)


def parse_packets(data: bytes) -> list[dict[str, Any]]:
    """解析一帧二进制数据中可能包含的多个 B 站数据包。"""
    packets: list[dict[str, Any]] = []
    offset = 0
    data_length = len(data)
    while offset + HEADER_LENGTH <= data_length:
        packet_length, header_length, protocol_version, operation, sequence = HEADER_STRUCT.unpack_from(data, offset)
        if packet_length < header_length or header_length < HEADER_LENGTH:
            break
        packet_end = offset + packet_length
        if packet_end > data_length:
            break
        payload = data[offset + header_length : packet_end]
        packets.extend(
            _parse_payload(payload, operation=operation, protocol_version=protocol_version, sequence=sequence)
        )
        offset = packet_end
    return packets


def _parse_payload(payload: bytes, *, operation: int, protocol_version: int, sequence: int) -> list[dict[str, Any]]:
    if operation == BILIBILI_OPERATION_MESSAGE and protocol_version == BILIBILI_PROTOCOL_ZLIB:
        try:
            return parse_packets(zlib.decompress(payload))
        except zlib.error:
            return []

    if operation == BILIBILI_OPERATION_HEARTBEAT_REPLY:
        popularity = 0
        if len(payload) >= 4:
            popularity = struct.unpack(">I", payload[:4])[0]
        return [
            {
                "operation": operation,
                "protocol_version": protocol_version,
                "sequence": sequence,
                "type": "heartbeat_reply",
                "popularity": popularity,
            }
        ]

    if operation == BILIBILI_OPERATION_AUTH_REPLY:
        decoded = _decode_json(payload)
        return [
            {
                "operation": operation,
                "protocol_version": protocol_version,
                "sequence": sequence,
                "type": "auth_reply",
                "raw": decoded if isinstance(decoded, Mapping) else {},
            }
        ]

    if operation != BILIBILI_OPERATION_MESSAGE:
        return [
            {
                "operation": operation,
                "protocol_version": protocol_version,
                "sequence": sequence,
                "payload": payload,
            }
        ]

    decoded = _decode_json(payload)
    if isinstance(decoded, list):
        return [dict(item) for item in decoded if isinstance(item, Mapping)]
    if isinstance(decoded, Mapping):
        return [dict(decoded)]
    return []


def _decode_json(payload: bytes) -> Any:
    if not payload:
        return {}
    text = payload.decode("utf-8", errors="ignore").strip("\x00\r\n\t ")
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _to_payload_bytes(payload: bytes | str | Mapping[str, Any] | None) -> bytes:
    if payload is None:
        return b""
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------------- #
# 事件规范化
# --------------------------------------------------------------------------- #
def normalize_event(raw_event: Mapping[str, Any]) -> dict[str, Any] | None:
    """将 B 站业务消息规范化为统一事件字典；无法识别的命令返回 None。"""
    command = str(raw_event.get("cmd") or "").split(":", 1)[0]
    if command == "DANMU_MSG":
        return _normalize_danmaku(raw_event)
    if command in {"SUPER_CHAT_MESSAGE", "SUPER_CHAT_MESSAGE_JPN"}:
        return _normalize_super_chat(raw_event)
    if command == "SEND_GIFT":
        return _normalize_gift(raw_event)
    if command == "GUARD_BUY":
        return _normalize_guard(raw_event)
    return None


def _normalize_danmaku(raw_event: Mapping[str, Any]) -> dict[str, Any] | None:
    info = raw_event.get("info")
    if not isinstance(info, list) or len(info) < 3:
        return None
    text = str(info[1] or "").strip()
    if not text:
        return None
    user = info[2] if isinstance(info[2], list) else []
    user_id = str(user[0] if len(user) > 0 else "anonymous")
    username = str(user[1] if len(user) > 1 else user_id)
    mentions = _extract_at_mentions(info)
    if mentions:
        at_prefix = " ".join(f"@{name}" for name in mentions)
        text = f"{at_prefix} {text}"
    return _base_event(raw_event, event_type="danmaku", text=text, user_id=user_id, username=username)


def _extract_at_mentions(info: list) -> list[str]:
    """从 DANMU_MSG info 数组的补充元素中提取 @提及的用户名。"""
    mentions: list[str] = []
    seen: set[str] = set()
    for item in info[3:]:
        if not isinstance(item, list) or len(item) < 2:
            continue
        candidate_uid = item[0]
        candidate_name = item[1]
        uid_int = _try_parse_uid(candidate_uid)
        if uid_int is None or uid_int < 1000:
            continue
        if not isinstance(candidate_name, str) or not candidate_name.strip():
            continue
        normalized_name = candidate_name.strip()
        if normalized_name.startswith(("§", "＼")):
            continue
        if normalized_name not in seen:
            seen.add(normalized_name)
            mentions.append(normalized_name)
    return mentions


def _try_parse_uid(value: Any) -> int | None:
    """尝试将值解析为数字 UID，失败返回 None。"""
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit() and len(stripped) >= 4:
            return int(stripped)
    return None


def _normalize_super_chat(raw_event: Mapping[str, Any]) -> dict[str, Any] | None:
    data = raw_event.get("data")
    if not isinstance(data, Mapping):
        return None
    text = str(data.get("message") or "").strip()
    user_info = data.get("user_info") if isinstance(data.get("user_info"), Mapping) else {}
    user_id = str(data.get("uid") or user_info.get("uid") or "anonymous")
    username = str(user_info.get("uname") or data.get("uname") or user_id)
    price = as_float(data.get("price"))
    event = _base_event(raw_event, event_type="super_chat", text=text, user_id=user_id, username=username)
    event["price"] = price
    event["summary"] = f"SC {price:g}: {text}" if price else text
    return event


def _normalize_gift(raw_event: Mapping[str, Any]) -> dict[str, Any] | None:
    data = raw_event.get("data")
    if not isinstance(data, Mapping):
        return None
    gift_name = str(data.get("giftName") or data.get("gift_name") or "gift").strip()
    count = max(1, int(as_float(data.get("num"), default=1.0)))
    username = str(data.get("uname") or data.get("username") or data.get("uid") or "anonymous")
    user_id = str(data.get("uid") or "anonymous")
    summary = f"{username} sent {gift_name} x{count}"
    event = _base_event(raw_event, event_type="gift", text=summary, user_id=user_id, username=username)
    event.update({"gift_name": gift_name, "count": count, "price": as_float(data.get("price"))})
    return event


def _normalize_guard(raw_event: Mapping[str, Any]) -> dict[str, Any] | None:
    data = raw_event.get("data")
    if not isinstance(data, Mapping):
        return None
    username = str(data.get("username") or data.get("uname") or data.get("uid") or "anonymous")
    user_id = str(data.get("uid") or "anonymous")
    gift_name = str(data.get("gift_name") or data.get("giftName") or "guard").strip()
    count = max(1, int(as_float(data.get("num"), default=1.0)))
    summary = f"{username} bought {gift_name} x{count}"
    event = _base_event(raw_event, event_type="guard", text=summary, user_id=user_id, username=username)
    event.update({"gift_name": gift_name, "count": count})
    return event


def _base_event(
    raw_event: Mapping[str, Any], *, event_type: str, text: str, user_id: str, username: str
) -> dict[str, Any]:
    return {
        "event_id": _event_id(raw_event),
        "type": event_type,
        "text": text,
        "summary": text,
        "user_id": user_id,
        "username": username,
        "timestamp": _extract_timestamp(raw_event),
        "raw": dict(raw_event),
    }


def _extract_timestamp(raw_event: Mapping[str, Any]) -> float:
    data = raw_event.get("data")
    if isinstance(data, Mapping):
        for key in ("ts", "timestamp", "start_time"):
            value = data.get(key)
            if value:
                return normalize_epoch_seconds(value)
    info = raw_event.get("info")
    if isinstance(info, list) and info:
        meta = info[0]
        if isinstance(meta, list) and len(meta) > 4:
            return normalize_epoch_seconds(meta[4])
    return time.time()


def _event_id(raw_event: Mapping[str, Any]) -> str:
    data = raw_event.get("data")
    if isinstance(data, Mapping):
        for key in ("id", "message_id", "msg_id"):
            value = str(data.get(key) or "").strip()
            if value:
                return f"bilibili-{value}"
    digest = hashlib.md5(
        json.dumps(raw_event, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"bilibili-{digest}"


# --------------------------------------------------------------------------- #
# 归一化工具
# --------------------------------------------------------------------------- #
def as_float(value: Any, *, default: float = 0.0) -> float:
    """将任意值解析为浮点数；非法值回退 default。"""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_epoch_seconds(value: Any) -> float:
    """将时间戳规范化为秒级浮点数（兼容毫秒/微秒输入）。

    大于 ``_MILLISECONDS_THRESHOLD`` 的数值会被连续除以 1000，直到落入秒级范围；
    非法或非正输入回退当前时间。
    """
    timestamp = as_float(value, default=time.time())
    if not math.isfinite(timestamp) or timestamp <= 0:
        return time.time()
    while timestamp > _MILLISECONDS_THRESHOLD:
        timestamp /= 1000.0
    return timestamp


# --------------------------------------------------------------------------- #
# 弹幕服务器地址解析
# --------------------------------------------------------------------------- #
def select_ws_urls(conf: Mapping[str, Any]) -> list[str]:
    """从 getConf 返回的 host_server_list 提取 wss 地址列表。

    host 仅接受域名 / IPv4 字符集，端口限制在 1-65535，避免畸形响应构造非法 URL。
    """
    urls: list[str] = []
    seen: set[str] = set()
    host_server_list = conf.get("host_server_list")
    if isinstance(host_server_list, list):
        for item in host_server_list:
            if not isinstance(item, Mapping):
                continue
            host = str(item.get("host") or "").strip()
            if not host or not _HOST_PATTERN.fullmatch(host):
                continue
            try:
                wss_port = int(item.get("wss_port") or 443)
            except (TypeError, ValueError):
                continue
            if not 1 <= wss_port <= 65535:
                continue
            url = f"wss://{host}:{wss_port}/sub"
            if url not in seen:
                urls.append(url)
                seen.add(url)
    host = str(conf.get("host") or "").strip()
    if host and _HOST_PATTERN.fullmatch(host):
        url = f"wss://{host}:443/sub"
        if url not in seen:
            urls.append(url)
    return urls

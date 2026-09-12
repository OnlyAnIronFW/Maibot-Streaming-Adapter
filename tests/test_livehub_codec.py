"""livehub B 站协议编解码与事件规范化单元测试。"""

from __future__ import annotations

import json
import struct
import zlib

from maibot_bilibili_live_adapter_copy.bilibili_codec import (  # noqa: E402 - 交叉一致性对照实现
    build_auth_packet as plugin_build_auth_packet,
    build_heartbeat_packet as plugin_build_heartbeat_packet,
    build_packet as plugin_build_packet,
    normalize_event as plugin_normalize_event,
    parse_packets as plugin_parse_packets,
)

from livehub.bilibili_protocol import (
    build_auth_packet,
    build_heartbeat_packet,
    build_packet,
    normalize_event,
    parse_packets,
)


def test_auth_packet_operation() -> None:
    packet = build_auth_packet(4538234, uid=0, token="test-token")
    parsed = parse_packets(packet)
    assert len(parsed) == 1
    assert parsed[0]["operation"] == 7


def test_heartbeat_packet_operation() -> None:
    parsed = parse_packets(build_heartbeat_packet())
    assert parsed[0]["operation"] == 2


def test_zlib_nested_packet_parsing() -> None:
    raw = {"cmd": "DANMU_MSG", "info": [[0, 1, 25, 16777215, 1700000000], "hello", [123, "tester"], [], [], [], [], []]}
    inner = build_packet(5, json.dumps(raw, ensure_ascii=False).encode(), protocol_version=1)
    outer = build_packet(5, zlib.compress(inner), protocol_version=2)
    parsed = parse_packets(outer)
    assert any(packet.get("cmd") == "DANMU_MSG" for packet in parsed)


def test_heartbeat_reply_popularity() -> None:
    payload = struct.pack(">I", 12345)
    packet = build_packet(3, payload, protocol_version=1)
    parsed = parse_packets(packet)
    assert parsed[0]["type"] == "heartbeat_reply"
    assert parsed[0]["popularity"] == 12345


def test_normalize_danmaku() -> None:
    raw = {
        "cmd": "DANMU_MSG",
        "info": [[0, 1, 25, 16777215, 1700000000], "hello world", [123, "tester"], [], [], [], [], []],
    }
    event = normalize_event(raw)
    assert event is not None
    assert event["type"] == "danmaku"
    assert event["text"] == "hello world"
    assert event["username"] == "tester"
    assert event["user_id"] == "123"


def test_normalize_danmaku_at_mention() -> None:
    raw = {
        "cmd": "DANMU_MSG",
        "info": [[0, 1, 25, 16777215, 1700000000], "你好", [1, "sender"], [], [], [20001, "target"], [], []],
    }
    event = normalize_event(raw)
    assert event is not None
    assert event["text"] == "@target 你好"


def test_normalize_super_chat() -> None:
    raw = {
        "cmd": "SUPER_CHAT_MESSAGE",
        "data": {"message": "sc内容", "uid": 5, "user_info": {"uname": "scuser"}, "price": 30},
    }
    event = normalize_event(raw)
    assert event is not None
    assert event["type"] == "super_chat"
    assert event["summary"] == "SC 30: sc内容"
    assert event["price"] == 30.0


def test_normalize_gift() -> None:
    raw = {"cmd": "SEND_GIFT", "data": {"giftName": "小心心", "num": 3, "uid": 9, "uname": "giver", "price": 100}}
    event = normalize_event(raw)
    assert event is not None
    assert event["type"] == "gift"
    assert event["summary"] == "giver sent 小心心 x3"
    assert event["count"] == 3


def test_normalize_guard() -> None:
    raw = {"cmd": "GUARD_BUY", "data": {"username": "guarduser", "uid": 7, "gift_name": "舰长", "num": 1}}
    event = normalize_event(raw)
    assert event is not None
    assert event["type"] == "guard"
    assert event["summary"] == "guarduser bought 舰长 x1"


def test_normalize_unknown_command_returns_none() -> None:
    assert normalize_event({"cmd": "UNKNOWN_CMD", "data": {}}) is None


def test_event_id_stable_for_same_raw() -> None:
    raw = {"cmd": "DANMU_MSG", "info": [[0, 1, 25, 16777215, 1700000000], "dup", [1, "u"], [], [], [], [], []]}
    first = normalize_event(raw)
    second = normalize_event(raw)
    assert first is not None and second is not None
    assert first["event_id"] == second["event_id"]


# --------------------------------------------------------------------------- #
# 交叉一致性：bilibili_codec（插件侧 re-export）与 livehub.bilibili_protocol（源实现）
# 必须保持 API 与行为一致（协议实现已统一为单一事实来源）
# --------------------------------------------------------------------------- #
_CROSS_EVENT_FIXTURES: list[tuple[dict, list[str]]] = [
    (
        {
            "cmd": "DANMU_MSG",
            "info": [
                [0, 1, 25, 16777215, 1700000000], "跨端弹幕", [42, "cross_user"], [], [], [20001, "target"], [], []
            ],
        },
        ["type", "text", "summary", "user_id", "username", "event_id"],
    ),
    (
        {
            "cmd": "SUPER_CHAT_MESSAGE",
            "data": {"message": "跨端SC", "uid": 5, "user_info": {"uname": "scuser"}, "price": 30},
        },
        ["type", "summary", "user_id", "username"],
    ),
    (
        {"cmd": "SEND_GIFT", "data": {"giftName": "小心心", "num": 2, "uid": 9, "uname": "giver", "price": 100}},
        ["type", "summary", "user_id", "username", "gift_name", "count"],
    ),
    (
        {"cmd": "GUARD_BUY", "data": {"username": "guarduser", "uid": 7, "gift_name": "舰长", "num": 1}},
        ["type", "summary", "user_id", "username"],
    ),
]


def test_cross_impl_auth_and_heartbeat_packets_identical() -> None:
    assert build_auth_packet(4538234, uid=0, token="t") == plugin_build_auth_packet(4538234, uid=0, token="t")
    assert build_heartbeat_packet() == plugin_build_heartbeat_packet()


def test_cross_impl_zlib_packet_parsing_identical() -> None:
    raw = {"cmd": "DANMU_MSG", "info": [[0, 1, 25, 16777215, 1700000000], "hi", [1, "u"], [], [], [], [], []]}
    inner = build_packet(5, json.dumps(raw, ensure_ascii=False).encode(), protocol_version=1)
    outer = build_packet(5, zlib.compress(inner), protocol_version=2)
    assert parse_packets(outer) == plugin_parse_packets(outer)
    plugin_outer = plugin_build_packet(
        5,
        zlib.compress(plugin_build_packet(5, json.dumps(raw, ensure_ascii=False).encode(), protocol_version=1)),
        protocol_version=2,
    )
    assert parse_packets(plugin_outer) == plugin_parse_packets(plugin_outer)


def test_cross_impl_event_normalization_identical() -> None:
    for raw_event, keys in _CROSS_EVENT_FIXTURES:
        ours = normalize_event(raw_event)
        theirs = plugin_normalize_event(raw_event)
        assert ours is not None and theirs is not None, f"两侧均需识别: {raw_event['cmd']}"
        for key in keys:
            assert ours[key] == theirs[key], f"{raw_event['cmd']} 字段 {key} 不一致: {ours[key]!r} vs {theirs[key]!r}"

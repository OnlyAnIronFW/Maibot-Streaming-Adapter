"""LiveHub 核心状态机单元测试（事件环 / 参与者 / 语音互斥 / 超时）。"""

from __future__ import annotations

import asyncio
import logging

import pytest

from livehub.config import LiveHubConfig
from livehub.hub import LiveHub


def _make_hub(**overrides) -> LiveHub:
    config = LiveHubConfig(host="127.0.0.1", port=18190, room_id=0, history_limit=50)
    if overrides:
        config = LiveHubConfig(**{**config.__dict__, **overrides})
    return LiveHub(config, logger=logging.getLogger("test_livehub_hub"))


def test_publish_event_assigns_increasing_seq() -> None:
    hub = _make_hub()
    assert hub.publish_event({"type": "danmaku", "text": "a"}) == 1
    assert hub.publish_event({"type": "danmaku", "text": "b"}) == 2
    assert [event["seq"] for event in hub.recent_events(10)] == [1, 2]


def test_publish_event_fills_required_fields() -> None:
    hub = _make_hub()
    hub.publish_event({"type": "danmaku", "text": "hi", "username": "u", "user_id": "1"})
    record = hub.recent_events(1)[0]
    assert record["event_id"]
    assert record["origin"] == "hub"
    assert record["summary"] == "hi"
    assert record["timestamp"] > 0
    assert record["raw"] == {}


def test_recent_events_respects_limit() -> None:
    hub = _make_hub(history_limit=5)
    for index in range(20):
        hub.publish_event({"type": "danmaku", "text": str(index)})
    events = hub.recent_events(5)
    assert len(events) == 5
    assert events[-1]["seq"] == 20


def test_register_presence_and_state() -> None:
    hub = _make_hub()
    state = hub.register_presence(client_id="bot-a", bot_name="BotA")
    assert any(p["client_id"] == "bot-a" and p["bot_name"] == "BotA" for p in state["participants"])
    assert "speaking" in state
    assert "health" in state
    # presence 心跳响应为精简状态（不含事件历史）
    assert "events" not in state


@pytest.mark.asyncio
async def test_presence_timeout_prunes() -> None:
    hub = _make_hub(presence_timeout_sec=2.0)
    hub.start()
    try:
        hub.register_presence(client_id="bot-a", bot_name="BotA")
        assert len(hub.participants()) == 1
        await asyncio.sleep(3.5)
        assert len(hub.participants()) == 0
    finally:
        await hub.stop()


def test_speak_immediate_grant() -> None:
    hub = _make_hub()
    result = hub.request_speak({"request_id": "r1", "client_id": "bot-a", "expected_duration_ms": 5000})
    assert result["granted"] is True
    assert result["speaking"]["current"]["request_id"] == "r1"


def test_speak_queued_when_busy() -> None:
    hub = _make_hub()
    hub.request_speak({"request_id": "r1", "client_id": "bot-a", "expected_duration_ms": 5000})
    result = hub.request_speak({"request_id": "r2", "client_id": "bot-b", "expected_duration_ms": 5000})
    assert result["granted"] is False
    assert result["speaking"]["current"]["request_id"] == "r1"


def test_speak_complete_hands_over_to_queue_head() -> None:
    hub = _make_hub()
    hub.request_speak({"request_id": "r1", "client_id": "bot-a", "expected_duration_ms": 5000})
    hub.request_speak({"request_id": "r2", "client_id": "bot-b", "expected_duration_ms": 5000})
    result = hub.complete_speak(request_id="r1", client_id="bot-a", status="completed")
    current = result["speaking"]["current"]
    assert current["request_id"] == "r2"
    assert current["client_id"] == "bot-b"


def test_speak_complete_release_all() -> None:
    hub = _make_hub()
    hub.request_speak({"request_id": "r1", "client_id": "bot-a"})
    result = hub.complete_speak(request_id="r1", client_id="bot-a", status="completed")
    assert result["speaking"]["current"] is None


def test_cancel_queued_request_does_not_steal_speaking_rights() -> None:
    """回归：排队中的请求被取消时，不得顶掉当前说话者。"""
    hub = _make_hub()
    hub.request_speak({"request_id": "r1", "client_id": "bot-a", "expected_duration_ms": 10000})
    hub.request_speak({"request_id": "r2", "client_id": "bot-b", "expected_duration_ms": 10000})
    # bot-b 取消自己的排队请求
    result = hub.complete_speak(request_id="r2", client_id="bot-b", status="cancelled")
    assert result["speaking"]["current"]["request_id"] == "r1"
    # bot-a 正常完成后，队列已空，无人说话
    result = hub.complete_speak(request_id="r1", client_id="bot-a", status="completed")
    assert result["speaking"]["current"] is None


@pytest.mark.asyncio
async def test_speech_timeout_auto_release() -> None:
    hub = _make_hub(speech_timeout_grace_sec=0.0)
    hub.start()
    try:
        hub.request_speak({"request_id": "r1", "client_id": "bot-a", "expected_duration_ms": 0})
        assert hub.speaking_state()["current"]["request_id"] == "r1"
        await asyncio.sleep(2.0)
        assert hub.speaking_state()["current"] is None
    finally:
        await hub.stop()


def test_snapshot_shape() -> None:
    hub = _make_hub()
    snapshot = hub.snapshot()
    assert set(snapshot) == {"events", "participants", "speaking", "health"}
    assert set(snapshot["health"]) == {"room_id", "event_count", "last_seq", "participant_count", "server_time"}

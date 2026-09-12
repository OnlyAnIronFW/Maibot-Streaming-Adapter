"""livehub HTTP/WS 服务端到端测试（真实 aiohttp 服务）。"""

from __future__ import annotations

import asyncio
import logging
import time

from uuid import uuid4

import pytest
import pytest_asyncio

from aiohttp import ClientSession

from livehub.config import LiveHubConfig
from livehub.server import LiveHubServer

# 测试端口：避开默认 18190，避免与真实实例冲突
TEST_PORT = 18191
AUTH_TEST_PORT = 18192


async def _base_url() -> str:
    return f"http://127.0.0.1:{TEST_PORT}"


@pytest_asyncio.fixture
async def server():
    logging.basicConfig(level=logging.CRITICAL)
    instance = LiveHubServer(
        LiveHubConfig(host="127.0.0.1", port=TEST_PORT, room_id=0, history_limit=20),
        logger=logging.getLogger("test_livehub_server"),
    )
    await instance.start()
    await asyncio.sleep(0.2)
    yield instance
    await instance.stop()


@pytest.mark.asyncio
async def test_health_endpoint(server) -> None:
    async with ClientSession() as session:
        async with session.get(f"{await _base_url()}/api/health") as resp:
            payload = await resp.json()
            assert resp.status == 200
            assert payload["success"] is True
            assert payload["config"]["port"] == TEST_PORT


@pytest.mark.asyncio
async def test_inject_then_events(server) -> None:
    async with ClientSession() as session:
        async with session.post(
            f"{await _base_url()}/api/inject", json={"text": "你好", "username": "u", "user_id": "1"}
        ) as resp:
            injected = await resp.json()
            assert injected["success"] is True
            assert injected["seq"] == 1
        async with session.get(f"{await _base_url()}/api/events?limit=5") as resp:
            events = (await resp.json())["events"]
            assert events[-1]["seq"] == 1
            assert events[-1]["type"] == "local_inject"


@pytest.mark.asyncio
async def test_presence_registers_participant(server) -> None:
    async with ClientSession() as session:
        async with session.post(
            f"{await _base_url()}/api/client/presence", json={"client_id": "bot-a", "bot_name": "BotA"}
        ) as resp:
            payload = await resp.json()
            assert any(p["client_id"] == "bot-a" for p in payload["participants"])


@pytest.mark.asyncio
async def test_speak_flow(server) -> None:
    async with ClientSession() as session:
        url = await _base_url()
        async with session.post(
            f"{url}/api/client/speak-request",
            json={"request_id": "r1", "client_id": "bot-a", "text": "hi", "expected_duration_ms": 5000},
        ) as resp:
            assert (await resp.json())["granted"] is True
        async with session.post(
            f"{url}/api/client/speak-request",
            json={"request_id": "r2", "client_id": "bot-b", "text": "hi2", "expected_duration_ms": 5000},
        ) as resp:
            assert (await resp.json())["granted"] is False
        async with session.post(
            f"{url}/api/client/speak-complete",
            json={"request_id": "r1", "client_id": "bot-a", "status": "completed"},
        ) as resp:
            current = (await resp.json())["speaking"]["current"]
            assert current["request_id"] == "r2"


@pytest.mark.asyncio
async def test_reply_direct_and_wrapped(server) -> None:
    async with ClientSession() as session:
        url = await _base_url()
        # 直接形态
        async with session.post(
            f"{url}/api/client/reply", json={"client_id": "b1", "bot_name": "B1", "text": "直接"}
        ) as resp:
            assert (await resp.json())["success"] is True
        # 插件 JsonBridgeClient 包装形态
        wrapped = {
            "id": uuid4().hex,
            "type": "client_reply",
            "timestamp": time.time(),
            "source": "maibot_bilibili_live_adapter",
            "payload": {"client_id": "b2", "bot_name": "B2", "text": "包装", "room_id": "1", "route_scope": "live"},
        }
        async with session.post(f"{url}/api/client/reply", json=wrapped) as resp:
            assert (await resp.json())["success"] is True
        async with session.get(f"{url}/api/events?limit=5") as resp:
            events = (await resp.json())["events"]
            assert [e["type"] for e in events].count("bot_reply") == 2


@pytest.mark.asyncio
async def test_ws_snapshot_and_event_push(server) -> None:
    async with ClientSession() as session:
        url = (await _base_url()).replace("http", "ws")
        ws = await session.ws_connect(f"{url}/ws")
        snapshot = await ws.receive_json(timeout=5)
        assert snapshot["kind"] == "snapshot"
        assert "events" in snapshot and "participants" in snapshot
        async with session.post(f"{await _base_url()}/api/inject", json={"text": "ws测试"}) as resp:
            await resp.json()
        pushed = await ws.receive_json(timeout=5)
        assert pushed["kind"] == "event"
        assert pushed["event"]["text"] == "ws测试"
        await ws.close()


@pytest.mark.asyncio
async def test_invalid_json_body_returns_400(server) -> None:
    async with ClientSession() as session:
        url = await _base_url()
        # 空 body
        async with session.post(f"{url}/api/client/presence", data=b"") as resp:
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid JSON body"
        # 非法 JSON
        async with session.post(f"{url}/api/client/speak-request", data=b"not-json") as resp:
            assert resp.status == 400
        # 非对象 JSON
        async with session.post(f"{url}/api/inject", data=b"[1,2,3]") as resp:
            assert resp.status == 400


@pytest.mark.asyncio
async def test_presence_requires_client_id(server) -> None:
    async with ClientSession() as session:
        async with session.post(
            f"{await _base_url()}/api/client/presence", json={"bot_name": "NoId"}
        ) as resp:
            assert resp.status == 400
            assert (await resp.json())["error"] == "client_id is required"


@pytest.mark.asyncio
async def test_speak_requires_request_and_client_id(server) -> None:
    async with ClientSession() as session:
        url = await _base_url()
        async with session.post(f"{url}/api/client/speak-request", json={"client_id": "b"}) as resp:
            assert resp.status == 400
        async with session.post(f"{url}/api/client/speak-complete", json={"request_id": "r"}) as resp:
            assert resp.status == 400


@pytest.mark.asyncio
async def test_speak_queue_has_upper_bound() -> None:
    """语音等待队列达到上限后拒绝新请求（不无限增长）。"""
    from livehub.hub import MAX_PENDING_SPEAK_REQUESTS, LiveHub

    hub = LiveHub(LiveHubConfig(host="127.0.0.1", port=TEST_PORT, room_id=0), logger=logging.getLogger("t"))
    hub.request_speak({"request_id": "owner", "client_id": "owner", "expected_duration_ms": 99999999})
    for index in range(MAX_PENDING_SPEAK_REQUESTS + 10):
        hub.request_speak({"request_id": f"r-{index}", "client_id": f"b-{index}"})
    # 队列已满时最后一个请求被拒绝且 granted=False
    assert len(hub._speaking_pending) <= MAX_PENDING_SPEAK_REQUESTS


@pytest.mark.asyncio
async def test_invalid_limit_falls_back(server) -> None:
    async with ClientSession() as session:
        async with session.get(f"{await _base_url()}/api/events?limit=not-a-number") as resp:
            payload = await resp.json()
            assert resp.status == 200
            assert "events" in payload
    logging.basicConfig(level=logging.CRITICAL)
    instance = LiveHubServer(
        LiveHubConfig(host="127.0.0.1", port=AUTH_TEST_PORT, room_id=0, auth_token="secret"),
        logger=logging.getLogger("test_livehub_server_auth"),
    )
    await instance.start()
    await asyncio.sleep(0.2)
    try:
        async with ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{AUTH_TEST_PORT}/api/health") as resp:
                assert resp.status == 401
            async with session.get(
                f"http://127.0.0.1:{AUTH_TEST_PORT}/api/health", headers={"Authorization": "Bearer secret"}
            ) as resp:
                assert resp.status == 200
    finally:
        await instance.stop()

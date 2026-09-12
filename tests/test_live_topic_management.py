import asyncio
import tempfile
import time
import unittest

from pathlib import Path
from typing import Any

from . import _host_bootstrap  # noqa: F401

from maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from maibot_bilibili_live_adapter_copy.event_router import LiveEventRouter
from maibot_bilibili_live_adapter_copy.interaction_planner import LiveInteractionPlanner
from maibot_bilibili_live_adapter_copy.plugin import BilibiliLiveAdapterPlugin, PLATFORM_NAME
from maibot_bilibili_live_adapter_copy.topic_extension_client import (
    build_seed_topic_prompt,
    build_topic_expansion_prompt,
    parse_topic_expansion_result,
)
from maibot_bilibili_live_adapter_copy.topic_state import (
    LiveTopicSnapshot,
    LiveTopicStateStore,
    LiveTopicStatus,
    TopicExpansionResult,
    classify_live_topic_status,
)


class _GatewayRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def route_message(
        self,
        gateway_name: str,
        message: dict[str, Any],
        *,
        route_metadata: dict[str, Any] | None = None,
        external_message_id: str = "",
        dedupe_key: str = "",
    ) -> bool:
        self.calls.append(
            {
                "gateway_name": gateway_name,
                "message": dict(message),
                "route_metadata": dict(route_metadata or {}),
                "external_message_id": external_message_id,
                "dedupe_key": dedupe_key,
            }
        )
        return True


class _BlockingGatewayRecorder(_GatewayRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def route_message(
        self,
        gateway_name: str,
        message: dict[str, Any],
        *,
        route_metadata: dict[str, Any] | None = None,
        external_message_id: str = "",
        dedupe_key: str = "",
    ) -> bool:
        accepted = await super().route_message(
            gateway_name,
            message,
            route_metadata=route_metadata,
            external_message_id=external_message_id,
            dedupe_key=dedupe_key,
        )
        self.started.set()
        await self.release.wait()
        return accepted


class _MessageCapability:
    def __init__(self, count: int = 0) -> None:
        self.count = count

    async def count_new(self, chat_id: str, since: str) -> int:
        del chat_id, since
        return self.count


class _TimelineMessageCapability:
    def __init__(self, timestamps: list[float] | None = None) -> None:
        self.timestamps = list(timestamps or [])

    async def count_new(self, chat_id: str, since: str) -> int:
        del chat_id
        anchor = float(since)
        return sum(1 for stamp in self.timestamps if stamp > anchor)


class _SpeakingControllerStub:
    def __init__(self, *, is_speaking: bool) -> None:
        self.is_speaking = is_speaking


class _TopicExtensionClientStub:
    def __init__(
        self,
        *,
        expansion_result: TopicExpansionResult | None = None,
        seed_result: TopicExpansionResult | None = None,
    ) -> None:
        self.expansion_result = expansion_result
        self.seed_result = seed_result
        self.expand_calls: list[dict[str, Any]] = []
        self.seed_calls: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return True

    async def expand_topic(
        self,
        *,
        current_topic: str,
        recent_timeline: list[dict[str, Any]],
        recent_viewer_messages: list[str],
        recent_bot_outputs: list[str],
    ) -> TopicExpansionResult | None:
        self.expand_calls.append(
            {
                "current_topic": current_topic,
                "recent_timeline": list(recent_timeline),
                "recent_viewer_messages": list(recent_viewer_messages),
                "recent_bot_outputs": list(recent_bot_outputs),
            }
        )
        return self.expansion_result

    async def extract_seed_topic(
        self,
        *,
        recent_timeline: list[dict[str, Any]],
        recent_viewer_messages: list[str],
        recent_bot_outputs: list[str],
    ) -> TopicExpansionResult | None:
        self.seed_calls.append(
            {
                "recent_timeline": list(recent_timeline),
                "recent_viewer_messages": list(recent_viewer_messages),
                "recent_bot_outputs": list(recent_bot_outputs),
            }
        )
        return self.seed_result


def _make_settings() -> LiveAdapterSettings:
    settings = LiveAdapterSettings()
    settings.plugin.enabled = True
    settings.bilibili.room_id = 12345
    settings.interaction.enabled = True
    settings.interaction.idle_topic_enabled = True
    settings.interaction.idle_topic_after_sec = 0.05
    settings.interaction.idle_topic_prompt = "直播间安静下来时，请自然续聊。"
    settings.interaction.idle_topic_context_enabled = True
    settings.interaction.topic_extension.enabled = False
    return settings


def _make_snapshot(*, topic: str, updated_at: float | None = None) -> LiveTopicSnapshot:
    now = time.time() if updated_at is None else updated_at
    return LiveTopicSnapshot(
        room_id="12345",
        live_chat_id="bilibili-live-test",
        current_topic=topic,
        previous_topic="",
        recent_viewer_messages=["观众说这个话题挺有意思"],
        recent_bot_outputs=["MaiBot 还在继续聊这个话题"],
        last_expansion=None,
        updated_at=now,
    )


def _make_router(
    *,
    gateway: _GatewayRecorder | None = None,
    pending_count: int = 0,
    message_capability: Any | None = None,
    topic_extension_client: _TopicExtensionClientStub | None = None,
    topic_state_store: LiveTopicStateStore | None = None,
) -> LiveEventRouter:
    settings = _make_settings()
    if topic_extension_client is not None:
        settings.interaction.topic_extension.enabled = True
    if topic_state_store is None:
        topic_state_store = LiveTopicStateStore(Path(tempfile.mkdtemp()) / "live_topic_state.json")
    planner = LiveInteractionPlanner(
        settings.interaction,
        message_capability=message_capability or _MessageCapability(count=pending_count),
        chat_id="bilibili-live-test",
    )
    return LiveEventRouter(
        gateway=gateway or _GatewayRecorder(),
        settings=settings,
        planner=planner,
        topic_extension_client=topic_extension_client,
        topic_state_store=topic_state_store,
    )


def _live_event(event_id: str, text: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "type": "danmaku",
        "text": text,
        "summary": text,
        "username": f"user-{event_id}",
        "user_id": f"uid-{event_id}",
    }


class TopicStateStoreTest(unittest.TestCase):
    def test_store_restores_matching_recent_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LiveTopicStateStore(Path(temp_dir) / "live_topic_state.json")
            snapshot = _make_snapshot(topic="大学宿舍夜聊", updated_at=time.time())

            store.save(snapshot)
            restored = store.load(room_id="12345", live_chat_id="bilibili-live-test", max_age_sec=7200.0)

            self.assertIsNotNone(restored)
            self.assertEqual(restored.current_topic, "大学宿舍夜聊")

    def test_store_rejects_room_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LiveTopicStateStore(Path(temp_dir) / "live_topic_state.json")
            store.save(_make_snapshot(topic="旅行见闻"))

            restored = store.load(room_id="999", live_chat_id="bilibili-live-test", max_age_sec=7200.0)

            self.assertIsNone(restored)

    def test_store_rejects_expired_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LiveTopicStateStore(Path(temp_dir) / "live_topic_state.json")
            store.save(_make_snapshot(topic="旅行见闻", updated_at=time.time() - 7201.0))

            restored = store.load(room_id="12345", live_chat_id="bilibili-live-test", max_age_sec=7200.0)

            self.assertIsNone(restored)


class TopicStatusTest(unittest.TestCase):
    def test_classify_active_topic(self) -> None:
        status = classify_live_topic_status(
            snapshot=_make_snapshot(topic="校园食堂"),
            recent_timeline=[
                {"role": "live", "text": "那你大学食堂最好吃的是哪家？", "username": "A"},
                {"role": "bot", "text": "你们也可以说说自己学校的王牌窗口。"},
            ],
            recent_viewer_messages=["那你大学食堂最好吃的是哪家？"],
            recent_bot_outputs=["你们也可以说说自己学校的王牌窗口。"],
        )

        self.assertEqual(status, LiveTopicStatus.ACTIVE)

    def test_classify_tailing_topic(self) -> None:
        status = classify_live_topic_status(
            snapshot=_make_snapshot(topic="旅游攻略"),
            recent_timeline=[
                {"role": "live", "text": "确实哈哈", "username": "A"},
                {"role": "bot", "text": "差不多就是这些，回头我再补图。"},
            ],
            recent_viewer_messages=["确实哈哈"],
            recent_bot_outputs=["差不多就是这些，回头我再补图。"],
        )

        self.assertEqual(status, LiveTopicStatus.TAILING)

    def test_classify_empty_topic(self) -> None:
        status = classify_live_topic_status(
            snapshot=None,
            recent_timeline=[],
            recent_viewer_messages=[],
            recent_bot_outputs=[],
        )

        self.assertEqual(status, LiveTopicStatus.EMPTY)


class TopicExpansionParseTest(unittest.TestCase):
    def test_parse_topic_expansion_result_from_plain_json(self) -> None:
        result = parse_topic_expansion_result(
            '{"current_topic":"校园生活","related_topic":"宿舍夜聊","expansion_angle":"从食堂延伸到舍友作息","handoff_prompt":"先接一句食堂，再聊宿舍夜聊","why_related":"都属于大学生活"}'
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.related_topic, "宿舍夜聊")

    def test_parse_topic_expansion_result_from_fenced_json(self) -> None:
        result = parse_topic_expansion_result(
            '```json\n{"current_topic":"旅行经历","related_topic":"出发前准备","expansion_angle":"从旅途见闻延伸到打包技巧","handoff_prompt":"先收束旅途，再转到准备阶段","why_related":"同属旅行线"}\n```'
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.expansion_angle, "从旅途见闻延伸到打包技巧")

    def test_parse_topic_expansion_result_returns_none_for_invalid_json(self) -> None:
        result = parse_topic_expansion_result("not-json")

        self.assertIsNone(result)


class TopicExtensionPromptStyleTest(unittest.TestCase):
    def test_topic_expansion_prompt_prefers_streamer_style_hooks(self) -> None:
        prompt = build_topic_expansion_prompt(
            current_topic="AI主播到底算不算打工",
            recent_timeline=[{"role": "live", "text": "那Vedal算老板吗", "username": "A"}],
            recent_viewer_messages=["那Vedal算老板吗"],
            recent_bot_outputs=["那我岂不是在给人类打工？"],
        )

        self.assertIn("fake malfunction bits", prompt)
        self.assertIn("podcast episode name", prompt)
        self.assertIn("more streamer-style hook", prompt)

    def test_seed_topic_prompt_prefers_memeable_hooks(self) -> None:
        prompt = build_seed_topic_prompt(
            recent_timeline=[{"role": "live", "text": "你是不是又系统崩了", "username": "A"}],
            recent_viewer_messages=["你是不是又系统崩了"],
            recent_bot_outputs=["我没有崩，我只是加载得很有个性。"],
        )

        self.assertIn("easy to joke about", prompt)
        self.assertIn("fake-break", prompt)
        self.assertIn("streamer-style chat hook", prompt)


class LiveTopicRouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_idle_topic_blocked_when_route_task_inflight(self) -> None:
        gateway = _BlockingGatewayRecorder()
        router = _make_router(gateway=gateway)
        router._topic_snapshot = _make_snapshot(topic="校园生活")
        router._last_bot_output_at = time.time() - 60.0

        await router.handle_event(_live_event("evt-1", "第一条弹幕"))
        await asyncio.wait_for(gateway.started.wait(), timeout=1.0)

        injected = await router._route_idle_topic()

        self.assertFalse(injected)
        self.assertEqual(len(gateway.calls), 1)
        gateway.release.set()
        await router.flush_window()

    async def test_idle_topic_blocked_when_pending_message_count_is_non_zero(self) -> None:
        router = _make_router(pending_count=2)
        router._topic_snapshot = _make_snapshot(topic="校园生活", updated_at=time.time() - 60.0)
        router._last_routeable_live_event_at = time.time() - 1.0
        router._last_bot_output_at = time.time() - 60.0

        injected = await router._route_idle_topic()

        self.assertFalse(injected)

    async def test_idle_topic_ignores_stale_pending_message_count_after_bot_output(self) -> None:
        gateway = _GatewayRecorder()
        router = _make_router(gateway=gateway, pending_count=2)
        router._topic_snapshot = _make_snapshot(topic="校园生活", updated_at=time.time() - 60.0)
        router._last_routeable_live_event_at = time.time() - 60.0
        router._last_bot_output_at = time.time() - 1.0

        injected = await router._route_idle_topic()

        self.assertTrue(injected)
        self.assertEqual(len(gateway.calls), 1)

    async def test_idle_topic_blocked_when_live_reply_is_busy(self) -> None:
        router = _make_router()
        router._topic_snapshot = _make_snapshot(topic="校园生活")
        router._last_bot_output_at = time.time() - 60.0
        router.record_live_reply_output_started(reason="reply_started")

        injected = await router._route_idle_topic()

        self.assertFalse(injected)

    async def test_new_live_event_restarts_idle_timer(self) -> None:
        gateway = _GatewayRecorder()
        router = _make_router(gateway=gateway)
        router._topic_snapshot = _make_snapshot(topic="校园生活")
        router._last_bot_output_at = time.time() - 60.0

        router.start_idle_topic_watch()
        await asyncio.sleep(0.03)
        await router.handle_event(_live_event("evt-2", "新的弹幕来了"))
        call_count_after_new_event = len(gateway.calls)
        await asyncio.sleep(0.03)

        self.assertEqual(call_count_after_new_event, 1)
        self.assertEqual(len(gateway.calls), 1)

    async def test_idle_topic_triggers_once_after_quiet_window_and_release(self) -> None:
        gateway = _GatewayRecorder()
        router = _make_router(gateway=gateway)
        router._topic_snapshot = _make_snapshot(topic="校园生活")
        router._last_bot_output_at = time.time() - 60.0
        router.record_live_reply_output_started(reason="reply_started")

        router.start_idle_topic_watch()
        await asyncio.sleep(0.08)
        self.assertEqual(len(gateway.calls), 0)

        router.record_live_reply_finished_without_output(reason="finish")
        await asyncio.sleep(0.08)
        first_idle_count = len(gateway.calls)
        await asyncio.sleep(0.08)

        self.assertEqual(first_idle_count, 1)
        self.assertEqual(len(gateway.calls), 1)

    async def test_idle_topic_retries_after_transient_ai_speaking_block_clears(self) -> None:
        gateway = _GatewayRecorder()
        router = _make_router(gateway=gateway)
        router._topic_snapshot = _make_snapshot(topic="校园生活")
        router._last_bot_output_at = time.time() - 60.0
        router.live2d_controller = _SpeakingControllerStub(is_speaking=True)

        router.start_idle_topic_watch()
        await asyncio.sleep(0.08)
        self.assertEqual(len(gateway.calls), 0)

        router.live2d_controller.is_speaking = False
        await asyncio.sleep(0.08)

        self.assertEqual(len(gateway.calls), 1)

    async def test_idle_topic_retries_after_bot_quiet_window_boundary(self) -> None:
        gateway = _GatewayRecorder()
        router = _make_router(gateway=gateway)
        router._topic_snapshot = _make_snapshot(topic="校园生活", updated_at=time.time() - 60.0)
        router._last_routeable_live_event_at = time.time() - 60.0
        router._last_bot_output_at = time.time() - 0.02

        router.start_idle_topic_watch()
        await asyncio.sleep(0.18)

        self.assertEqual(len(gateway.calls), 1)

    async def test_idle_topic_retries_after_cooldown_clears(self) -> None:
        gateway = _GatewayRecorder()
        router = _make_router(gateway=gateway)
        router._topic_snapshot = _make_snapshot(topic="校园生活", updated_at=time.time() - 60.0)
        router._last_routeable_live_event_at = time.time() - 60.0
        router._last_bot_output_at = time.time() - 60.0
        router._idle_topic_cooldown_active = True

        router.start_idle_topic_watch()
        await asyncio.sleep(0.08)
        self.assertEqual(len(gateway.calls), 0)

        router._idle_topic_cooldown_active = False
        await asyncio.sleep(0.08)

        self.assertEqual(len(gateway.calls), 1)

    async def test_idle_topic_retry_delay_tracks_remaining_bot_quiet_window(self) -> None:
        router = _make_router()
        router._topic_snapshot = _make_snapshot(topic="校园生活", updated_at=time.time() - 60.0)
        router._last_routeable_live_event_at = time.time() - 60.0
        router._last_bot_output_at = time.time() - 0.02

        retry_delay_sec = await router._compute_idle_topic_retry_delay()

        self.assertIsNotNone(retry_delay_sec)
        assert retry_delay_sec is not None
        self.assertGreaterEqual(retry_delay_sec, 0.05)
        self.assertLess(retry_delay_sec, 0.2)

    async def test_active_branch_prompt_continues_previous_topic(self) -> None:
        router = _make_router()
        router._topic_snapshot = _make_snapshot(topic="大学社团故事")
        router._last_bot_output_at = time.time() - 60.0
        router._recent_live_records = [{"username": "A", "text": "你后来还参加过别的社团吗？"}]
        router._recent_topic_timeline = [
            {"role": "live", "text": "你后来还参加过别的社团吗？", "username": "A"},
            {"role": "bot", "text": "你们也可以讲讲自己最离谱的社团经历。"},
        ]
        router._recent_bot_outputs = ["你们也可以讲讲自己最离谱的社团经历。"]

        prompt = await router._build_idle_topic_prompt()

        self.assertIn("大学社团故事", prompt)
        self.assertIn("不要把天聊死", prompt)
        self.assertIn("你后来还参加过别的社团吗", prompt)
        self.assertIn("假装故障", prompt)
        self.assertIn("不要把话题写成播客标题", prompt)

    async def test_tailing_branch_prompt_contains_related_expansion_topic(self) -> None:
        client = _TopicExtensionClientStub(
            expansion_result=TopicExpansionResult(
                current_topic="旅游见闻",
                related_topic="出发前准备",
                expansion_angle="从旅途中遇到的问题延伸到准备技巧",
                handoff_prompt="先收一下旅途见闻，再自然转到出发前准备",
                why_related="都属于同一次旅行链路",
            )
        )
        router = _make_router(topic_extension_client=client)
        router._topic_snapshot = _make_snapshot(topic="旅游见闻")
        router._last_bot_output_at = time.time() - 60.0
        router._recent_live_records = [{"username": "A", "text": "确实哈哈"}]
        router._recent_topic_timeline = [
            {"role": "live", "text": "确实哈哈", "username": "A"},
            {"role": "bot", "text": "差不多就是这些，回头我再补图。"},
        ]
        router._recent_bot_outputs = ["差不多就是这些，回头我再补图。"]

        prompt = await router._build_idle_topic_prompt()

        self.assertIn("旅游见闻", prompt)
        self.assertIn("出发前准备", prompt)
        self.assertEqual(len(client.expand_calls), 1)
        self.assertIn("搞抽象", prompt)
        self.assertIn("更有直播味", prompt)

    async def test_empty_branch_extracts_seed_topic_from_recent_live_messages(self) -> None:
        client = _TopicExtensionClientStub(
            seed_result=TopicExpansionResult(
                current_topic="",
                related_topic="五一出游计划",
                expansion_angle="从最近弹幕里提炼出的直播种子",
                handoff_prompt="围绕五一出游计划继续聊，并留出观众接话空间",
                why_related="多条弹幕都在问假期安排",
            )
        )
        router = _make_router(topic_extension_client=client)
        router._topic_snapshot = None
        router._last_bot_output_at = time.time() - 60.0
        router._recent_live_records = [
            {"username": "A", "text": "五一准备去哪玩"},
            {"username": "B", "text": "有没有出游计划"},
        ]
        router._recent_topic_timeline = [
            {"role": "live", "text": "五一准备去哪玩", "username": "A"},
            {"role": "live", "text": "有没有出游计划", "username": "B"},
        ]

        prompt = await router._build_idle_topic_prompt()

        self.assertIn("五一出游计划", prompt)
        self.assertEqual(len(client.seed_calls), 1)
        self.assertIn("玩梗", prompt)
        self.assertIn("点名互动", prompt)

    async def test_router_restores_snapshot_from_store_for_same_room(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LiveTopicStateStore(Path(temp_dir) / "live_topic_state.json")
            store.save(_make_snapshot(topic="大学社团故事"))
            router = _make_router(topic_state_store=store)
            router._last_bot_output_at = time.time() - 60.0
            router._recent_live_records = [{"username": "A", "text": "后来还去比赛了吗"}]
            router._recent_topic_timeline = [{"role": "live", "text": "后来还去比赛了吗", "username": "A"}]

            prompt = await router._build_idle_topic_prompt()

            self.assertIn("大学社团故事", prompt)

    async def test_bot_output_advances_pending_anchor_so_self_reply_does_not_block_idle_topic(self) -> None:
        gateway = _GatewayRecorder()
        capability = _TimelineMessageCapability()
        router = _make_router(gateway=gateway, message_capability=capability)
        router._topic_snapshot = _make_snapshot(topic="鏍″洯鐢熸椿", updated_at=time.time() - 60.0)
        router._last_routeable_live_event_at = time.time() - 60.0
        router.planner.record_external_injection(when=time.time() - 2.0)
        capability.timestamps.append(time.time() - 1.0)

        self.assertEqual(await router.planner.get_pending_message_count(), 1)

        router.record_live_reply_output_started(reason="reply_started")
        router.record_bot_output_history("MaiBot 杩樺彲浠ョ户缁亰杩欎釜璇濋")
        self.assertEqual(await router.planner.get_pending_message_count(), 1)

        router.record_live_reply_finished_without_output(reason="playback_finished")
        self.assertFalse(await router._route_idle_topic())
        await asyncio.sleep(0.08)

        self.assertEqual(await router.planner.get_pending_message_count(), 0)
        self.assertEqual(len(gateway.calls), 1)


class PluginBusyTrackingTest(unittest.TestCase):
    def test_plugin_only_tracks_router_busy_for_live_platform(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()

        self.assertTrue(plugin._should_track_router_live_reply(source_platform=PLATFORM_NAME, metadata=None))
        self.assertFalse(plugin._should_track_router_live_reply(source_platform="onebot", metadata=None))


if __name__ == "__main__":
    unittest.main()

import asyncio
import time
import unittest

from pathlib import Path
from unittest.mock import AsyncMock, patch

import _host_bootstrap  # noqa: F401

from maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from maibot_bilibili_live_adapter_copy.event_router import LiveEventRouter
from maibot_bilibili_live_adapter_copy.interaction_planner import LiveInteractionPlanner
from maibot_bilibili_live_adapter_copy.plugin import BilibiliLiveAdapterPlugin, _build_live_chat_id
from maibot_bilibili_live_adapter_copy.video_watch import (
    ActiveVideoWatchSession,
    BrowserPlaybackHandle,
    VideoAnalysisCue,
    VideoAnalysisResult,
    VideoMemoryEntry,
    VideoWatchController,
    VideoWatchSourceSpec,
    parse_video_watch_command,
)


def _settings() -> LiveAdapterSettings:
    return LiveAdapterSettings.model_validate(
        {
            "plugin": {"enabled": True},
            "bilibili": {"room_id": 4538234},
            "interaction": {
                "enabled": True,
                "idle_topic_enabled": True,
                "idle_topic_after_sec": 0.05,
                "idle_topic_prompt": "直播间安静时正常续聊。",
            },
            "video_watch": {
                "enabled": True,
                "auto_offer_enabled": True,
                "offer_prompt": "要不要一起看个视频？",
                "command": {
                    "enabled": True,
                    "prefix": "/watchvideo",
                    "authorized_identities": ["Vedal"],
                    "allow_hub_local_input": True,
                    "drop_non_admin_commands": True,
                    "stop_aliases": ["stop", "off"],
                    "status_aliases": ["status", "state"],
                },
            },
        }
    )


class _GatewayRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def route_message(
        self,
        gateway_name: str,
        message: dict,
        *,
        route_metadata: dict | None = None,
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


class _VideoWatchEventRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, event: dict) -> bool:
        self.calls.append(dict(event))
        return str(event.get("text") or "").startswith("/watchvideo")


class _VideoWatchControllerStub:
    def __init__(
        self,
        *,
        waiting: bool = False,
        blocking: bool = False,
        consent_result: bool = False,
        manual_result: bool = False,
        prompt: str = "",
    ) -> None:
        self.enabled = True
        self.waiting = waiting
        self.blocking = blocking
        self.consent_result = consent_result
        self.manual_result = manual_result
        self.prompt = prompt
        self.consent_calls: list[dict] = []
        self.manual_calls: list[dict] = []

    def is_waiting_for_consent(self) -> bool:
        return self.waiting

    def should_block_idle_topic(self) -> bool:
        return self.blocking

    def build_prompt_context(self) -> str:
        return self.prompt

    async def handle_consent_reply(self, *, text: str, requested_by: str) -> bool:
        self.consent_calls.append({"text": text, "requested_by": requested_by})
        return self.consent_result

    async def handle_manual_command(self, *, command_text: str, requested_by: str, trigger: str) -> bool:
        self.manual_calls.append(
            {"command_text": command_text, "requested_by": requested_by, "trigger": trigger}
        )
        return self.manual_result

    async def maybe_offer(self) -> bool:
        return True


class VideoWatchCommandParsingTest(unittest.TestCase):
    def test_parse_watchvideo_commands(self) -> None:
        parsed = parse_video_watch_command(
            command_text="/watchvideo stop",
            prefix="/watchvideo",
            stop_aliases=["stop", "off"],
            status_aliases=["status", "state"],
        )
        self.assertEqual(parsed["action"], "stop")

        parsed = parse_video_watch_command(
            command_text="/watchvideo https://www.bilibili.com/video/BV1xx411c7mD",
            prefix="/watchvideo",
            stop_aliases=["stop", "off"],
            status_aliases=["status", "state"],
        )
        self.assertEqual(parsed["source_type"], "url")

        parsed = parse_video_watch_command(
            command_text="/watchvideo up 12345",
            prefix="/watchvideo",
            stop_aliases=["stop", "off"],
            status_aliases=["status", "state"],
        )
        self.assertEqual(parsed["source_type"], "up")
        self.assertEqual(parsed["value"], "12345")


class VideoWatchControllerTest(unittest.IsolatedAsyncioTestCase):
    async def test_offer_is_sent_once_and_then_waits_for_reply(self) -> None:
        settings = _settings()
        routed: list[dict] = []

        async def route_reply(text: str, reason: str, metadata: dict | None) -> bool:
            routed.append({"text": text, "reason": reason, "metadata": dict(metadata or {})})
            return True

        controller = VideoWatchController(
            settings.video_watch,
            route_reply=route_reply,
            memory_ingest=AsyncMock(),
            commentary_lane_available=lambda: False,
        )

        self.assertTrue(await controller.maybe_offer())
        self.assertTrue(controller.is_waiting_for_consent())
        self.assertTrue(await controller.maybe_offer())
        self.assertEqual(len(routed), 1)
        self.assertEqual(routed[0]["reason"], "video_watch_offer")

    async def test_commentary_cue_is_skipped_when_live_lane_is_busy(self) -> None:
        settings = _settings()
        route_reply = AsyncMock(return_value=True)
        controller = VideoWatchController(
            settings.video_watch,
            route_reply=route_reply,
            memory_ingest=AsyncMock(),
            commentary_lane_available=lambda: True,
        )
        source = VideoWatchSourceSpec(
            source_type="url",
            request_value="https://www.bilibili.com/video/BV1xx411c7mD",
            page_url="https://www.bilibili.com/video/BV1xx411c7mD",
            video_url="https://www.bilibili.com/video/BV1xx411c7mD",
            title="测试视频",
            canonical_id="BV1xx411c7mD",
            up_name="Vedal",
            metadata={"duration_sec": 0.0},
        )
        analysis = VideoAnalysisResult(
            summary="视频摘要",
            conversation_hooks=("钩子",),
            memory_entries=(),
            timeline_cues=(
                VideoAnalysisCue(
                    start_sec=0.0,
                    end_sec=0.1,
                    title="开场",
                    description="开场画面",
                    commentary_text="这里应该吐槽一下。",
                ),
            ),
            raw_payload={},
            model_label="qwen3.6-flash",
        )
        session = ActiveVideoWatchSession(
            session_id="video-session",
            source=source,
            local_video_path=Path("F:/tmp/test.mp4"),
            analysis=analysis,
            playback=BrowserPlaybackHandle(
                page_url=source.video_url,
                started_at=time.monotonic() - 0.01,
                close_callback=AsyncMock(),
            ),
        )
        controller._active_session = session

        await controller._run_commentary_schedule(session)

        route_reply.assert_not_awaited()
        self.assertIn(0, session.spoken_cue_indexes)
        self.assertIsNone(controller._active_session)


class RouterVideoWatchIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_router_intercepts_watchvideo_commands_before_gateway_routing(self) -> None:
        settings = _settings()
        settings.interaction.enabled = False
        gateway = _GatewayRecorder()
        planner = LiveInteractionPlanner(settings.interaction)
        handler = _VideoWatchEventRecorder()
        router = LiveEventRouter(
            gateway=gateway,
            settings=settings,
            planner=planner,
            video_watch_event_handler=handler,
        )

        await router.handle_event(
            {
                "event_id": "evt-watch",
                "type": "danmaku",
                "text": "/watchvideo status",
                "summary": "/watchvideo status",
                "user_id": "27853192",
                "username": "IDKWhatID2Use",
            }
        )

        self.assertEqual(len(gateway.calls), 0)
        self.assertEqual(len(handler.calls), 1)

    async def test_idle_video_offer_runs_before_normal_idle_topic_injection(self) -> None:
        settings = _settings()
        gateway = _GatewayRecorder()
        planner = LiveInteractionPlanner(settings.interaction)
        offer = AsyncMock(return_value=True)
        router = LiveEventRouter(
            gateway=gateway,
            settings=settings,
            planner=planner,
            video_watch_idle_offer_handler=offer,
        )
        router._topic_snapshot = None
        router._last_routeable_live_event_at = time.time() - 60.0
        router._last_bot_output_at = time.time() - 60.0

        handled = await router._route_idle_topic()

        self.assertTrue(handled)
        offer.assert_awaited_once()
        self.assertEqual(len(gateway.calls), 0)

    async def test_video_watch_blocker_suppresses_normal_idle_topic(self) -> None:
        settings = _settings()
        gateway = _GatewayRecorder()
        planner = LiveInteractionPlanner(settings.interaction)
        router = LiveEventRouter(
            gateway=gateway,
            settings=settings,
            planner=planner,
            video_watch_idle_blocker=lambda: True,
        )
        router._last_routeable_live_event_at = time.time() - 60.0
        router._last_bot_output_at = time.time() - 60.0

        handled = await router._route_idle_topic()

        self.assertFalse(handled)
        self.assertEqual(len(gateway.calls), 0)


class PluginVideoWatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_video_watch_command_is_forwarded_to_controller(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = _settings()
        controller = _VideoWatchControllerStub(manual_result=True)
        plugin._video_watch_controller = controller  # type: ignore[assignment]

        with patch.object(plugin, "_load_settings", return_value=settings):
            handled = await plugin.handle_video_watch_event(
                {
                    "type": "danmaku",
                    "text": "/watchvideo status",
                    "summary": "/watchvideo status",
                    "user_id": "27853192",
                    "username": "IDKWhatID2Use",
                }
            )

        self.assertTrue(handled)
        self.assertEqual(len(controller.manual_calls), 1)
        self.assertEqual(controller.manual_calls[0]["requested_by"], "Vedal")

    async def test_non_admin_video_watch_command_is_consumed(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = _settings()
        controller = _VideoWatchControllerStub(manual_result=True)
        plugin._video_watch_controller = controller  # type: ignore[assignment]

        with patch.object(plugin, "_load_settings", return_value=settings):
            handled = await plugin.handle_video_watch_event(
                {
                    "type": "danmaku",
                    "text": "/watchvideo",
                    "summary": "/watchvideo",
                    "user_id": "guest-user",
                    "username": "guest-user",
                }
            )

        self.assertTrue(handled)
        self.assertEqual(controller.manual_calls, [])

    async def test_waiting_consent_replies_are_handled_before_manual_commands(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = _settings()
        controller = _VideoWatchControllerStub(waiting=True, consent_result=True, manual_result=True)
        plugin._video_watch_controller = controller  # type: ignore[assignment]

        with patch.object(plugin, "_load_settings", return_value=settings):
            handled = await plugin.handle_video_watch_event(
                {
                    "type": "danmaku",
                    "text": "看",
                    "summary": "看",
                    "user_id": "27853192",
                    "username": "IDKWhatID2Use",
                }
            )

        self.assertTrue(handled)
        self.assertEqual(len(controller.consent_calls), 1)
        self.assertEqual(controller.manual_calls, [])

    async def test_video_watch_context_is_injected_into_live_prompts(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = _settings()
        live_session_id = _build_live_chat_id(settings)
        plugin._video_watch_controller = _VideoWatchControllerStub(prompt="额外视频上下文：当前正在看测试视频。")  # type: ignore[assignment]

        with patch.object(plugin, "_load_settings", return_value=settings):
            planner_request = await plugin.enforce_live_language_prompt(
                messages=[{"role": "user", "content": "继续聊"}],
                tool_definitions=[],
                session_id=live_session_id,
            )
            self.assertIn("额外视频上下文", str(planner_request["messages"][0]["content"]))

            planner_response = await plugin.enforce_live_language_prompt_for_replyer(
                tool_calls=[
                    {
                        "function": {
                            "name": "reply",
                            "arguments": {"text": "继续说", "reference_info": ""},
                        }
                    }
                ],
                session_id=live_session_id,
            )
            reply_reference = planner_response["modified_kwargs"]["tool_calls"][0]["function"]["arguments"][
                "reference_info"
            ]
            self.assertIn("额外视频上下文", reply_reference)

    async def test_memory_ingest_uses_video_scoped_chat_id_and_multiple_entries(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        source = VideoWatchSourceSpec(
            source_type="url",
            request_value="https://www.bilibili.com/video/BV1xx411c7mD",
            page_url="https://www.bilibili.com/video/BV1xx411c7mD",
            video_url="https://www.bilibili.com/video/BV1xx411c7mD",
            title="测试视频",
            canonical_id="BV1xx411c7mD",
            up_name="Vedal",
            metadata={"bvid": "BV1xx411c7mD"},
        )
        analysis = VideoAnalysisResult(
            summary="摘要",
            conversation_hooks=("钩子1", "钩子2"),
            memory_entries=(
                VideoMemoryEntry(title="梗", text="这是第一个梗。", tags=("meme",), start_sec=10.0, end_sec=12.0),
                VideoMemoryEntry(title="知识点", text="这是第二个知识点。", tags=("fact",)),
            ),
            timeline_cues=(),
            raw_payload={},
            model_label="qwen3.6-flash",
        )

        with patch("maibot_bilibili_live_adapter_copy.plugin.a_memorix_host_service.invoke", AsyncMock()) as invoke:
            await plugin._ingest_video_watch_memories(source, analysis)

        self.assertEqual(invoke.await_count, 2)
        for call in invoke.await_args_list:
            self.assertEqual(call.args[0], "ingest_summary")
            self.assertEqual(call.kwargs["args"]["chat_id"], source.memory_chat_id)
            self.assertFalse(call.kwargs["args"]["respect_filter"])
            self.assertEqual(call.kwargs["args"]["metadata"]["video_url"], source.video_url)


if __name__ == "__main__":
    unittest.main()

import asyncio
import sys
import unittest

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from maibot_bilibili_live_adapter_copy.event_router import LiveEventRouter
from maibot_bilibili_live_adapter_copy.interaction_planner import LiveInteractionPlanner
from maibot_bilibili_live_adapter_copy.plugin import BilibiliLiveAdapterPlugin


def live_event(event_id: str, text: str, *, event_type: str = "danmaku") -> dict:
    return {
        "event_id": event_id,
        "type": event_type,
        "text": text,
        "summary": text,
        "username": f"user-{event_id}",
        "user_id": f"uid-{event_id}",
    }


class _GatewayRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.called = asyncio.Event()

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
        self.called.set()
        return True


class _Live2DSpecialMoveRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.called = asyncio.Event()

    async def __call__(self, event: dict) -> bool:
        self.calls.append(dict(event))
        self.called.set()
        return True


class _SpecialMoveRuntimeStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def request_special_move(self, *, action: str, move: str, duration_sec: float) -> bool:
        self.calls.append(
            {
                "action": str(action),
                "move": str(move),
                "duration_sec": float(duration_sec),
            }
        )
        return True


class Live2DSpecialMoveRequestTest(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_special_move_tool_dispatches_to_runtime(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        runtime = _SpecialMoveRuntimeStub()
        plugin._embodied_live2d_runtime = runtime  # type: ignore[assignment]

        result = await plugin.special_move(move="ahoge_spin", duration_sec=10.0)

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "Special_move")
        self.assertEqual(result["move"], "ahoge_spin")
        self.assertEqual(runtime.calls, [{"action": "Special_move", "move": "ahoge_spin", "duration_sec": 10.0}])

    async def test_plugin_special_move_tool_normalizes_ahoge_spin_alias(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        runtime = _SpecialMoveRuntimeStub()
        plugin._embodied_live2d_runtime = runtime  # type: ignore[assignment]

        result = await plugin.special_move(move="ahoge spin", duration_sec=5.0)

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "Special_move")
        self.assertEqual(result["move"], "ahoge_spin")
        self.assertEqual(runtime.calls, [{"action": "Special_move", "move": "ahoge_spin", "duration_sec": 5.0}])

    async def test_router_attaches_special_move_for_ahoge_spin_request(self) -> None:
        settings = LiveAdapterSettings()
        settings.interaction.enabled = False

        gateway = _GatewayRecorder()
        planner = LiveInteractionPlanner(settings.interaction)
        special_move_handler = _Live2DSpecialMoveRecorder()
        router = LiveEventRouter(
            gateway=gateway,
            settings=settings,
            planner=planner,
            live2d_special_move_handler=special_move_handler,
        )

        await router.handle_event(live_event("evt-ahoge", "\u8f6c\u5446\u6bdb"))
        await asyncio.wait_for(gateway.called.wait(), timeout=1.0)
        await asyncio.wait_for(special_move_handler.called.wait(), timeout=1.0)

        routed_message = gateway.calls[0]["message"]
        self.assertEqual(routed_message["live2d_action"]["action"], "Special_move")
        self.assertEqual(routed_message["live2d_action"]["move"], "ahoge_spin")
        self.assertEqual(routed_message["live2d_action"]["duration_sec"], 10.0)
        self.assertEqual(special_move_handler.calls[0]["live2d_action"]["action"], "Special_move")
        self.assertEqual(special_move_handler.calls[0]["live2d_action"]["move"], "ahoge_spin")
        self.assertEqual(special_move_handler.calls[0]["live2d_action"]["duration_sec"], 10.0)

    async def test_router_leaves_normal_chat_without_special_move(self) -> None:
        settings = LiveAdapterSettings()
        settings.interaction.enabled = False

        gateway = _GatewayRecorder()
        planner = LiveInteractionPlanner(settings.interaction)
        special_move_handler = _Live2DSpecialMoveRecorder()
        router = LiveEventRouter(
            gateway=gateway,
            settings=settings,
            planner=planner,
            live2d_special_move_handler=special_move_handler,
        )

        await router.handle_event(live_event("evt-normal", "\u4eca\u5929\u804a\u70b9\u522b\u7684"))
        await asyncio.wait_for(gateway.called.wait(), timeout=1.0)
        await asyncio.sleep(0)

        routed_message = gateway.calls[0]["message"]
        self.assertNotIn("live2d_action", routed_message)
        self.assertFalse(special_move_handler.called.is_set())
        self.assertEqual(len(special_move_handler.calls), 0)


if __name__ == "__main__":
    unittest.main()

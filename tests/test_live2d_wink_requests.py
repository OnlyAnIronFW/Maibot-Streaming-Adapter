import asyncio
import sys
import unittest

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from maibot_bilibili_live_adapter_copy.event_router import LiveEventRouter
from maibot_bilibili_live_adapter_copy.interaction_planner import LiveInteractionPlanner


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


class _Live2DWinkRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.called = asyncio.Event()

    async def __call__(self, event: dict) -> bool:
        self.calls.append(dict(event))
        self.called.set()
        return True


class Live2DWinkRequestTest(unittest.IsolatedAsyncioTestCase):
    async def test_router_triggers_wink_handler_and_still_routes_event(self) -> None:
        settings = LiveAdapterSettings()
        settings.interaction.enabled = False

        gateway = _GatewayRecorder()
        planner = LiveInteractionPlanner(settings.interaction)
        wink_handler = _Live2DWinkRecorder()
        router = LiveEventRouter(
            gateway=gateway,
            settings=settings,
            planner=planner,
            live2d_wink_handler=wink_handler,
        )

        await router.handle_event(live_event("evt-wink", "左眼wink一下"))
        await asyncio.wait_for(gateway.called.wait(), timeout=1.0)
        await asyncio.wait_for(wink_handler.called.wait(), timeout=1.0)

        self.assertEqual(len(wink_handler.calls), 1)
        self.assertEqual(wink_handler.calls[0]["wink_side"], "left")
        self.assertEqual([call["external_message_id"] for call in gateway.calls], ["evt-wink"])
        self.assertEqual([call["message"]["message_id"] for call in gateway.calls], ["evt-wink"])

    async def test_router_ignores_normal_text_without_wink_request(self) -> None:
        settings = LiveAdapterSettings()
        settings.interaction.enabled = False

        gateway = _GatewayRecorder()
        planner = LiveInteractionPlanner(settings.interaction)
        wink_handler = _Live2DWinkRecorder()
        router = LiveEventRouter(
            gateway=gateway,
            settings=settings,
            planner=planner,
            live2d_wink_handler=wink_handler,
        )

        await router.handle_event(live_event("evt-normal", "今天聊天气氛真不错"))
        await asyncio.wait_for(gateway.called.wait(), timeout=1.0)
        await asyncio.sleep(0)

        self.assertFalse(wink_handler.called.is_set())
        self.assertEqual(len(wink_handler.calls), 0)
        self.assertEqual([call["external_message_id"] for call in gateway.calls], ["evt-normal"])
        self.assertEqual([call["message"]["message_id"] for call in gateway.calls], ["evt-normal"])


if __name__ == "__main__":
    unittest.main()

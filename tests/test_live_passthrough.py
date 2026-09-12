import asyncio
import unittest

import _host_bootstrap  # noqa: F401

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


class _BlockingGatewayRecorder(_GatewayRecorder):
    def __init__(self, *, expected_calls: int) -> None:
        super().__init__()
        self.expected_calls = max(1, int(expected_calls))
        self._started_calls = 0
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()

    async def route_message(
        self,
        gateway_name: str,
        message: dict,
        *,
        route_metadata: dict | None = None,
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
        self._started_calls += 1
        if self._started_calls >= self.expected_calls:
            self.all_started.set()
        await self.release.wait()
        return accepted


class _Live2DDebugCommandRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, event: dict) -> bool:
        self.calls.append(dict(event))
        text = str(event.get("text") or "")
        return text.startswith("/l2d")


class _VisualContextCommandRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, event: dict) -> bool:
        self.calls.append(dict(event))
        text = str(event.get("text") or "")
        return text.startswith("/vision")


class LivePassthroughTest(unittest.IsolatedAsyncioTestCase):
    async def test_planner_routes_all_events_when_interaction_selection_disabled(self) -> None:
        settings = LiveAdapterSettings()
        settings.interaction.enabled = False

        planner = LiveInteractionPlanner(settings.interaction)
        events = [
            live_event("evt-1", "第一条弹幕"),
            live_event("evt-2", "第二条弹幕"),
        ]

        selected = await planner.select(events)
        should_flush_immediately = await planner.should_flush_immediately(events)

        self.assertEqual([item.event["event_id"] for item in selected], ["evt-1", "evt-2"])
        self.assertTrue(should_flush_immediately)

    async def test_router_flushes_inflight_events_in_arrival_order_without_serial_reply_gate(self) -> None:
        settings = LiveAdapterSettings()
        settings.interaction.enabled = False

        gateway = _BlockingGatewayRecorder(expected_calls=2)
        planner = LiveInteractionPlanner(settings.interaction)
        router = LiveEventRouter(
            gateway=gateway,
            settings=settings,
            planner=planner,
        )
        await router.handle_event(live_event("evt-1", "第一条弹幕"))
        await router.handle_event(live_event("evt-2", "第二条弹幕"))

        flush_task = asyncio.create_task(router.flush_window())
        await asyncio.wait_for(gateway.all_started.wait(), timeout=1.0)
        gateway.release.set()
        routed = await asyncio.wait_for(flush_task, timeout=1.0)
        await asyncio.sleep(0)

        self.assertEqual([item["message_id"] for item in routed], ["evt-1", "evt-2"])
        self.assertEqual([call["external_message_id"] for call in gateway.calls], ["evt-1", "evt-2"])
        self.assertFalse(router._inflight_route_tasks)

    async def test_router_intercepts_live2d_debug_commands_before_gateway_routing(self) -> None:
        settings = LiveAdapterSettings()
        settings.interaction.enabled = False

        gateway = _GatewayRecorder()
        planner = LiveInteractionPlanner(settings.interaction)
        debug_handler = _Live2DDebugCommandRecorder()
        router = LiveEventRouter(
            gateway=gateway,
            settings=settings,
            planner=planner,
            live2d_debug_command_handler=debug_handler,
        )

        await router.handle_event(live_event("evt-debug", "/l2d status"))

        self.assertEqual(len(gateway.calls), 0)
        self.assertEqual(len(debug_handler.calls), 1)
        self.assertEqual(debug_handler.calls[0]["text"], "/l2d status")

    async def test_router_intercepts_visual_context_commands_before_gateway_routing(self) -> None:
        settings = LiveAdapterSettings()
        settings.interaction.enabled = False

        gateway = _GatewayRecorder()
        planner = LiveInteractionPlanner(settings.interaction)
        visual_handler = _VisualContextCommandRecorder()
        router = LiveEventRouter(
            gateway=gateway,
            settings=settings,
            planner=planner,
            visual_context_command_handler=visual_handler,
        )

        await router.handle_event(live_event("evt-vision", "/vision 现在是什么画面"))

        self.assertEqual(len(gateway.calls), 0)
        self.assertEqual(len(visual_handler.calls), 1)
        self.assertEqual(visual_handler.calls[0]["text"], "/vision 现在是什么画面")

    async def test_router_force_routes_soundboard_request_events(self) -> None:
        settings = LiveAdapterSettings()
        settings.soundboard.enabled = True
        settings.soundboard.force_route_inbound_requests = True

        gateway = _GatewayRecorder()
        planner = LiveInteractionPlanner(settings.interaction)

        async def soundboard_handler(event: dict) -> bool:
            event["_soundboard_request_detected"] = True
            event["_soundboard_request_mode"] = "generic"
            event["_soundboard_request_text"] = str(event.get("text") or "")
            return True

        router = LiveEventRouter(
            gateway=gateway,
            settings=settings,
            planner=planner,
            soundboard_keyword_handler=soundboard_handler,
        )

        await router.handle_event(live_event("evt-soundboard", "来个音效"))
        await asyncio.sleep(0)

        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(gateway.calls[0]["route_metadata"]["selection_reason"], "soundboard_request")
        additional_config = gateway.calls[0]["message"]["message_info"]["additional_config"]
        self.assertTrue(additional_config["soundboard_request_detected"])
        self.assertEqual(additional_config["soundboard_request_mode"], "generic")
        self.assertEqual(additional_config["soundboard_request_text"], "来个音效")


if __name__ == "__main__":
    unittest.main()

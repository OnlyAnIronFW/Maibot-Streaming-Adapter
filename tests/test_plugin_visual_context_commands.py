import asyncio
import unittest

from unittest.mock import AsyncMock, MagicMock, patch

import _host_bootstrap  # noqa: F401

from maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from maibot_bilibili_live_adapter_copy.plugin import BilibiliLiveAdapterPlugin, _build_live_chat_id


def _settings() -> LiveAdapterSettings:
    return LiveAdapterSettings.model_validate(
        {
            "plugin": {"enabled": True},
            "bilibili": {"room_id": 1},
            "vision": {
                "enabled": True,
                "expose_tool": False,
                "model_identifier": "qwen3-vl-flash",
                "command": {
                    "enabled": True,
                    "prefix": "/vision",
                    "authorized_identities": ["Vedal"],
                    "allow_hub_local_input": True,
                    "drop_non_admin_commands": True,
                    "model_name": "qwen3.5-flash",
                    "poll_interval_sec": 60.0,
                    "stop_aliases": ["stop", "off"],
                },
            },
        }
    )


class PluginVisualContextCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_visual_command_starts_polling_and_routes_followup(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = _settings()

        with patch.object(plugin, "_load_settings", return_value=settings):
            with patch.object(plugin, "_route_visual_command_followup_message", AsyncMock(return_value=True)) as followup:
                with patch.object(plugin, "_start_visual_context_polling", AsyncMock(return_value=True)) as starter:
                    handled = await plugin.handle_visual_context_command(
                        {
                            "type": "danmaku",
                            "text": "/vision 现在是什么情况",
                            "summary": "/vision 现在是什么情况",
                            "user_id": "27853192",
                            "username": "IDKWhatID2Use",
                        }
                    )

        self.assertTrue(handled)
        starter.assert_awaited_once()
        self.assertEqual(starter.await_args.kwargs["focus_question"], "现在是什么情况")
        followup.assert_awaited_once()
        routed_event = followup.await_args.kwargs["event"]
        self.assertEqual(routed_event["text"], "现在是什么情况")
        self.assertEqual(routed_event["summary"], "现在是什么情况")

    async def test_hub_local_visual_command_is_allowed_without_bilibili_identity(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = _settings()

        with patch.object(plugin, "_load_settings", return_value=settings):
            with patch.object(plugin, "_route_visual_command_followup_message", AsyncMock(return_value=True)) as followup:
                with patch.object(plugin, "_start_visual_context_polling", AsyncMock(return_value=True)) as starter:
                    handled = await plugin.handle_visual_context_command(
                        {
                            "type": "hub_local_input",
                            "text": "/vision 看下现在在做什么",
                            "summary": "/vision 看下现在在做什么",
                            "user_id": "",
                            "username": "",
                        }
                    )

        self.assertTrue(handled)
        starter.assert_awaited_once()
        followup.assert_awaited_once()

    async def test_non_admin_visual_command_is_consumed_without_starting_polling(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = _settings()

        with patch.object(plugin, "_load_settings", return_value=settings):
            with patch.object(plugin, "_route_visual_command_followup_message", AsyncMock(return_value=True)) as followup:
                with patch.object(plugin, "_start_visual_context_polling", AsyncMock(return_value=True)) as starter:
                    handled = await plugin.handle_visual_context_command(
                        {
                            "type": "danmaku",
                            "text": "/vision 现在是什么情况",
                            "summary": "/vision 现在是什么情况",
                            "user_id": "guest-user",
                            "username": "guest-user",
                        }
                    )

        self.assertTrue(handled)
        starter.assert_not_awaited()
        followup.assert_not_awaited()

    async def test_non_admin_visual_command_emits_rejection_log(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = _settings()
        logger = MagicMock()

        with patch.object(plugin, "_load_settings", return_value=settings):
            with patch.object(plugin, "_logger", return_value=logger):
                handled = await plugin.handle_visual_context_command(
                    {
                        "type": "danmaku",
                        "text": "/vision",
                        "summary": "/vision",
                        "user_id": "0",
                        "username": "I***",
                    }
                )

        self.assertTrue(handled)
        logger.warning.assert_called_once()
        warning_text = str(logger.warning.call_args.args[0])
        self.assertIn("Visual context command rejected", warning_text)

    async def test_visual_command_uses_dedicated_model_override_without_changing_tool_model(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = _settings()

        command_vision = plugin._resolve_visual_context_vision_config(settings)

        self.assertEqual(command_vision.model_name, "qwen3.5-flash")
        self.assertEqual(command_vision.model_identifier, "qwen3-vl-flash")
        self.assertEqual(settings.vision.model_name, "")
        self.assertEqual(settings.vision.model_identifier, "qwen3-vl-flash")

    async def test_active_visual_poll_context_persists_until_stop_command(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = _settings()
        live_session_id = _build_live_chat_id(settings)
        first_capture = asyncio.Event()

        async def inspect_side_effect(question: str) -> dict[str, object]:
            first_capture.set()
            return {
                "success": True,
                "summary": "画面里正在打 boss，角色血量偏低。",
                "model": "qwen3-vl-flash",
                "question": question,
            }

        with patch.object(plugin, "_load_settings", return_value=settings):
            with patch.object(plugin, "_route_visual_command_followup_message", AsyncMock(return_value=True)) as followup:
                with patch("maibot_bilibili_live_adapter_copy.plugin.VisionDesktopInspector") as inspector_cls:
                    inspector = inspector_cls.return_value
                    inspector.inspect = AsyncMock(side_effect=inspect_side_effect)

                    started = await plugin.handle_visual_context_command(
                        {
                            "type": "danmaku",
                            "text": "/vision 这波要不要补血",
                            "summary": "/vision 这波要不要补血",
                            "user_id": "27853192",
                            "username": "IDKWhatID2Use",
                        }
                    )

                    self.assertTrue(started)
                    await asyncio.wait_for(first_capture.wait(), timeout=1.0)

                    planner_request = await plugin.enforce_live_language_prompt(
                        messages=[{"role": "user", "content": "现在继续"}],
                        tool_definitions=[],
                        session_id=live_session_id,
                    )
                    prompt_message = str(planner_request["messages"][0].get("content"))
                    self.assertIn("附加视觉上下文", prompt_message)
                    self.assertIn("角色血量偏低", prompt_message)

                    planner_response = await plugin.enforce_live_language_prompt_for_replyer(
                        tool_calls=[
                            {
                                "function": {
                                    "name": "reply",
                                    "arguments": {"text": "建议先补血", "reference_info": ""},
                                }
                            }
                        ],
                        session_id=live_session_id,
                    )
                    tool_calls = planner_response["modified_kwargs"]["tool_calls"]
                    reference_info = tool_calls[0]["function"]["arguments"]["reference_info"]
                    self.assertIn("附加视觉上下文", reference_info)
                    self.assertIn("角色血量偏低", reference_info)

                    second_request = await plugin.enforce_live_language_prompt(
                        messages=[{"role": "user", "content": "现在继续"}],
                        tool_definitions=[],
                        session_id=live_session_id,
                    )
                    second_prompt = str(second_request["messages"][0].get("content"))
                    self.assertIn("附加视觉上下文", second_prompt)
                    self.assertIn("角色血量偏低", second_prompt)

                    stopped = await plugin.handle_visual_context_command(
                        {
                            "type": "danmaku",
                            "text": "/vision stop",
                            "summary": "/vision stop",
                            "user_id": "27853192",
                            "username": "IDKWhatID2Use",
                        }
                    )
                    self.assertTrue(stopped)
                    await asyncio.sleep(0)

                    third_request = await plugin.enforce_live_language_prompt(
                        messages=[{"role": "user", "content": "现在继续"}],
                        tool_definitions=[],
                        session_id=live_session_id,
                    )
                    third_prompt = str(third_request["messages"][0].get("content"))
                    self.assertNotIn("附加视觉上下文", third_prompt)
                    self.assertEqual(inspector.inspect.await_count, 1)
                    followup.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

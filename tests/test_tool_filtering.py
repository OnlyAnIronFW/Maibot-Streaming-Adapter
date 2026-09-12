import unittest

import _host_bootstrap  # noqa: F401

from maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from maibot_bilibili_live_adapter_copy.plugin import (
    _ensure_live_soundboard_tools_visible,
    _filter_unavailable_tool_definitions,
    _inject_soundboard_tool_hints,
    _live_capability_boundary_prompt,
)


def tool_definition(name: str) -> dict:
    return {"type": "function", "function": {"name": name}}


class ToolFilteringTest(unittest.TestCase):
    def test_filters_tools_for_disabled_modules(self) -> None:
        settings = LiveAdapterSettings()
        settings.plugin.enabled = True
        settings.live2d.enabled = False
        settings.game.enabled = False
        settings.song_request.enabled = False
        settings.vision.enabled = False

        filtered = _filter_unavailable_tool_definitions(
            [
                tool_definition("special_move"),
                tool_definition("control_game"),
                tool_definition("request_rvc_song"),
                tool_definition("inspect_desktop"),
                tool_definition("play_sound_effect"),
                tool_definition("send_sound_effect"),
                tool_definition("finish"),
                tool_definition("unrelated_tool"),
            ],
            settings,
            filter_finish=True,
        )

        self.assertEqual([item["function"]["name"] for item in filtered], ["unrelated_tool"])

    def test_requested_tools_stay_hidden_even_when_modules_are_enabled(self) -> None:
        settings = LiveAdapterSettings()
        settings.plugin.enabled = True
        settings.live2d.enabled = True
        settings.live2d.embodied.enabled = True
        settings.game.enabled = True
        settings.song_request.enabled = True
        settings.vision.enabled = True

        filtered = _filter_unavailable_tool_definitions(
            [
                tool_definition("special_move"),
                tool_definition("control_game"),
                tool_definition("request_rvc_song"),
                tool_definition("inspect_desktop"),
                tool_definition("play_sound_effect"),
                tool_definition("send_sound_effect"),
                tool_definition("finish"),
                tool_definition("unrelated_tool"),
            ],
            settings,
            filter_finish=True,
        )

        self.assertEqual(
            [item["function"]["name"] for item in filtered],
            [
                "inspect_desktop",
                "unrelated_tool",
            ],
        )

    def test_filters_hard_disabled_rvc_even_when_enabled(self) -> None:
        settings = LiveAdapterSettings()
        settings.plugin.enabled = True
        settings.song_request.enabled = True
        settings.song_request.hard_disable = True

        filtered = _filter_unavailable_tool_definitions(
            [tool_definition("request_rvc_song"), tool_definition("unrelated_tool")],
            settings,
        )

        self.assertEqual([item["function"]["name"] for item in filtered], ["unrelated_tool"])

    def test_filters_special_move_when_embodied_live2d_is_disabled(self) -> None:
        settings = LiveAdapterSettings()
        settings.plugin.enabled = True
        settings.live2d.enabled = True
        settings.live2d.embodied.enabled = False

        filtered = _filter_unavailable_tool_definitions(
            [tool_definition("special_move"), tool_definition("unrelated_tool")],
            settings,
        )

        self.assertEqual([item["function"]["name"] for item in filtered], ["unrelated_tool"])

    def test_keeps_auto_soundboard_tool_when_soundboard_auto_tool_is_enabled(self) -> None:
        settings = LiveAdapterSettings()
        settings.plugin.enabled = True
        settings.soundboard.enabled = True
        settings.soundboard.expose_tool = False
        settings.soundboard.expose_auto_tool = True

        filtered = _filter_unavailable_tool_definitions(
            [tool_definition("play_sound_effect"), tool_definition("send_sound_effect"), tool_definition("unrelated_tool")],
            settings,
        )

        self.assertEqual([item["function"]["name"] for item in filtered], ["send_sound_effect", "unrelated_tool"])

    def test_live_soundboard_tools_become_directly_visible_and_leave_reminder(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "plugin": {"enabled": True},
                "soundboard": {
                    "enabled": True,
                    "expose_tool": True,
                    "expose_auto_tool": True,
                },
            }
        )
        messages = [
            {
                "role": "user",
                "content": (
                    "<system-reminder>\n"
                    "以下工具当前未直接暴露给你，但可以通过 tool_search 工具发现并在后续轮次中使用：\n"
                    "1. play_sound_effect: Play a configured live soundboard cue.\n"
                    "2. send_sound_effect: Automatically choose a live soundboard cue.\n"
                    "</system-reminder>"
                ),
            }
        ]
        tools = [{"name": "reply", "description": "reply", "parameters": {"type": "object", "properties": {}}}]

        updated_messages, updated_tools = _ensure_live_soundboard_tools_visible(
            messages,
            tools,
            settings.soundboard,
        )

        updated_tool_names = [tool["name"] for tool in updated_tools]
        self.assertIn("play_sound_effect", updated_tool_names)
        self.assertIn("send_sound_effect", updated_tool_names)
        self.assertNotIn("play_sound_effect:", updated_messages[0]["content"])
        self.assertNotIn("send_sound_effect:", updated_messages[0]["content"])

    def test_soundboard_hint_injection_supports_plain_tool_definitions(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "soundboard": {
                    "enabled": True,
                    "expose_tool": True,
                    "expose_auto_tool": True,
                    "cues": [
                        {
                            "id": "cat_laugh",
                            "usage_hint": "Use when chat is laughing at a joke landing too well.",
                        }
                    ],
                }
            }
        )
        hinted = _inject_soundboard_tool_hints(
            [
                {
                    "name": "play_sound_effect",
                    "description": "Play a configured live soundboard cue.",
                    "parameters": {"type": "object", "properties": {"cue": {"type": "string"}}},
                },
                {
                    "name": "send_sound_effect",
                    "description": "Automatically choose and queue a live soundboard cue.",
                    "parameters": {"type": "object", "properties": {"intent": {"type": "string"}}},
                },
            ],
            settings.soundboard,
        )

        self.assertIn("Available cues:", hinted[0]["description"])
        self.assertIn("cat_laugh", hinted[0]["parameters"]["properties"]["cue"]["enum"])
        self.assertIn("Available reactions:", hinted[1]["description"])
        self.assertIn("cat_laugh", hinted[1]["parameters"]["properties"]["intent"]["description"])

    def test_live_capability_prompt_reflects_current_boundaries(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "plugin": {"enabled": True},
                "bilibili": {"room_id": 4538234},
                "hub_input": {"enabled": True, "use_as_primary_source": True},
                "interaction": {"enabled": True, "llm_enabled": True, "idle_topic_enabled": True},
                "live2d": {
                    "enabled": True,
                    "scheme": "soullink",
                    "sync": {"enabled": True},
                    "soullink": {"enabled": True},
                    "embodied": {"enabled": True},
                },
                "soundboard": {
                    "enabled": True,
                    "expose_tool": True,
                    "expose_auto_tool": True,
                    "auto_select_llm": {
                        "enabled": True,
                        "model_name": "deepseek-v4-flash",
                        "model_identifier": "deepseek-v4-flash",
                    },
                    "cues": [{"id": "laugh", "usage_hint": "Use when chat is laughing."}],
                },
                "vision": {
                    "enabled": True,
                    "expose_tool": False,
                    "command": {"enabled": True, "prefix": "/vision"},
                },
                "sts2": {
                    "enabled": True,
                    "commands": {"super_chat_start_min_price": 20.0},
                },
                "game": {"enabled": False},
                "song_request": {"enabled": False, "hard_disable": True},
                "local_voice": {"enabled": False},
            }
        )

        prompt = _live_capability_boundary_prompt(settings)

        self.assertIn("small Live2D expression/motion adjustments", prompt)
        self.assertIn("hands, legs, flips, spins", prompt)
        self.assertIn("configured soundboard effects", prompt)
        self.assertIn("send_sound_effect", prompt)
        self.assertIn("laugh", prompt)
        self.assertIn("Desktop vision is available but imperfect", prompt)
        self.assertIn("cannot start it yourself", prompt)
        self.assertIn("/sts2start", prompt)
        self.assertIn("cannot sing", prompt)
        self.assertNotIn("special_move", prompt)
        self.assertNotIn("control_game", prompt)
        self.assertNotIn("request_rvc_song", prompt)
        self.assertNotIn("generic game JSON bridge", prompt)
        self.assertNotIn("RVC song requests", prompt)
        self.assertNotIn("Shared Live Hub input is the primary source", prompt)
        self.assertNotIn("room 4538234", prompt)


if __name__ == "__main__":
    unittest.main()

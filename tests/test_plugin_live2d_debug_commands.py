import sys
import tempfile
import unittest

from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from maibot_bilibili_live_adapter_copy.plugin import BilibiliLiveAdapterPlugin


class _EmbodiedRuntimeStub:
    def __init__(self) -> None:
        self.mouse_follow_calls: list[bool] = []
        self.emotion_calls: list[tuple[str, float]] = []
        self.reset_calls = 0

    def set_mouse_follow_enabled(self, enabled: bool) -> None:
        self.mouse_follow_calls.append(bool(enabled))

    async def debug_apply_emotion(self, emotion_intent: str, emotion_gain: float = 1.0) -> dict[str, object]:
        self.emotion_calls.append((str(emotion_intent), float(emotion_gain)))
        return {"success": True, "emotion_intent": emotion_intent, "emotion_gain": emotion_gain}

    async def debug_reset_pose(self) -> dict[str, object]:
        self.reset_calls += 1
        return {"success": True}

    def debug_status(self) -> dict[str, object]:
        return {
            "subscriber_connected": True,
            "disabled_reason": "",
            "mouse_follow_enabled": bool(self.mouse_follow_calls[-1]) if self.mouse_follow_calls else False,
            "source_session_id": "session-live",
            "latest_targets": {"head.x": 0.2},
            "has_snapshot": True,
        }


def _settings() -> LiveAdapterSettings:
    return LiveAdapterSettings.model_validate(
        {
            "plugin": {"enabled": True},
            "bilibili": {"room_id": 1},
            "live2d": {
                "enabled": True,
                "embodied": {
                    "enabled": True,
                    "source_session_id": "session-live",
                    "debug_commands": {
                        "enabled": True,
                        "prefix": "/l2d",
                        "admin_user_ids": ["IDKWhatID2Use"],
                        "drop_non_admin_commands": True,
                    },
                },
            },
        }
    )


class PluginLive2DDebugCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_admin_mouse_command_updates_control_state_and_runtime(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        runtime = _EmbodiedRuntimeStub()
        plugin._embodied_live2d_runtime = runtime  # type: ignore[assignment]

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(plugin, "_load_settings", return_value=_settings()):
                with patch("maibot_bilibili_live_adapter_copy.plugin._plugin_data_dir", return_value=Path(temp_dir)):
                    with patch.object(plugin, "_publish_live2d_debug_feedback", AsyncMock()) as feedback:
                        handled = await plugin.handle_live2d_debug_command(
                            {"text": "/l2d mouse on", "user_id": "IDKWhatID2Use"}
                        )

        self.assertTrue(handled)
        self.assertTrue(plugin._live2d_control_state.mouse_follow_enabled)
        self.assertEqual(runtime.mouse_follow_calls, [True])
        feedback.assert_awaited_once()
        self.assertIn("mouse", feedback.await_args.args[0].lower())

    async def test_non_admin_live2d_command_is_consumed_without_changing_runtime(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        runtime = _EmbodiedRuntimeStub()
        plugin._embodied_live2d_runtime = runtime  # type: ignore[assignment]

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(plugin, "_load_settings", return_value=_settings()):
                with patch("maibot_bilibili_live_adapter_copy.plugin._plugin_data_dir", return_value=Path(temp_dir)):
                    with patch.object(plugin, "_publish_live2d_debug_feedback", AsyncMock()) as feedback:
                        handled = await plugin.handle_live2d_debug_command(
                            {"text": "/l2d mouse on", "user_id": "guest-user"}
                        )

        self.assertTrue(handled)
        self.assertFalse(plugin._live2d_control_state.mouse_follow_enabled)
        self.assertEqual(runtime.mouse_follow_calls, [])
        feedback.assert_awaited_once()
        self.assertIn("admin", feedback.await_args.args[0].lower())

    async def test_admin_emotion_command_dispatches_runtime_preview(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        runtime = _EmbodiedRuntimeStub()
        plugin._embodied_live2d_runtime = runtime  # type: ignore[assignment]

        with patch.object(plugin, "_load_settings", return_value=_settings()):
            with patch.object(plugin, "_publish_live2d_debug_feedback", AsyncMock()) as feedback:
                handled = await plugin.handle_live2d_debug_command(
                    {"text": "/l2d emotion happy 0.8", "user_id": "IDKWhatID2Use"}
                )

        self.assertTrue(handled)
        self.assertEqual(runtime.emotion_calls, [("react_happy", 0.8)])
        feedback.assert_awaited_once()
        self.assertIn("happy", feedback.await_args.args[0].lower())

    async def test_admin_status_command_reports_runtime_state(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        runtime = _EmbodiedRuntimeStub()
        plugin._embodied_live2d_runtime = runtime  # type: ignore[assignment]

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(plugin, "_load_settings", return_value=_settings()):
                with patch("maibot_bilibili_live_adapter_copy.plugin._plugin_data_dir", return_value=Path(temp_dir)):
                    with patch.object(plugin, "_publish_live2d_debug_feedback", AsyncMock()) as feedback:
                        handled = await plugin.handle_live2d_debug_command(
                            {"text": "/l2d status", "user_id": "IDKWhatID2Use"}
                        )

        self.assertTrue(handled)
        feedback.assert_awaited_once()
        message = feedback.await_args.args[0].lower()
        self.assertIn("runtime=on", message)
        self.assertIn("mouse=off", message)

    async def test_admin_reset_command_calls_runtime_reset(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        runtime = _EmbodiedRuntimeStub()
        plugin._embodied_live2d_runtime = runtime  # type: ignore[assignment]

        with patch.object(plugin, "_load_settings", return_value=_settings()):
            with patch.object(plugin, "_publish_live2d_debug_feedback", AsyncMock()) as feedback:
                handled = await plugin.handle_live2d_debug_command(
                    {"text": "/l2d reset", "user_id": "IDKWhatID2Use"}
                )

        self.assertTrue(handled)
        self.assertEqual(runtime.reset_calls, 1)
        feedback.assert_awaited_once()
        self.assertIn("reset", feedback.await_args.args[0].lower())


if __name__ == "__main__":
    unittest.main()

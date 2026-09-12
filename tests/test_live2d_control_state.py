import sys
import tempfile
import unittest

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from maibot_bilibili_live_adapter_copy.config import Live2DEmbodiedConfig
from maibot_bilibili_live_adapter_copy.live2d_control_state import (
    DEFAULT_LIVE2D_CONTROL_STATE,
    Live2DControlState,
    Live2DControlStateStore,
    _coerce_bool,
)


class Live2DControlStateTest(unittest.TestCase):
    def test_coerce_bool_preserves_fallback_for_unrecognized_strings(self) -> None:
        self.assertFalse(_coerce_bool("offf", False))
        self.assertTrue(_coerce_bool("offf", True))
        self.assertFalse(_coerce_bool("0.0", False))
        self.assertTrue(_coerce_bool("0.0", True))
        self.assertTrue(_coerce_bool("yes", False))
        self.assertFalse(_coerce_bool("off", True))

    def test_store_loads_defaults_recovers_from_invalid_json_and_persists_patches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "live2d_control_state.json"
            store = Live2DControlStateStore(path)

            self.assertEqual(store.load(), DEFAULT_LIVE2D_CONTROL_STATE)

            path.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(store.load(), DEFAULT_LIVE2D_CONTROL_STATE)

            state = store.merge_patch(
                {
                    "mouse_follow_enabled": True,
                    "mouse_follow_status": "mouse",
                    "last_mouse_activity_ts": 12.5,
                }
            )
            self.assertEqual(
                state,
                Live2DControlState(
                    mouse_follow_enabled=True,
                    mouse_follow_status="mouse",
                    last_mouse_activity_ts=12.5,
                ),
            )
            self.assertEqual(Live2DControlStateStore(path).load(), state)

            merged = Live2DControlStateStore(path).merge_patch({"mouse_follow_status": "cooldown"})
            self.assertEqual(merged.mouse_follow_enabled, True)
            self.assertEqual(merged.mouse_follow_status, "cooldown")
            self.assertEqual(merged.last_mouse_activity_ts, 12.5)

    def test_store_merge_patch_ignores_unrecognized_boolean_strings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "live2d_control_state.json"
            store = Live2DControlStateStore(path)

            state = store.merge_patch({"mouse_follow_enabled": "offf"})

            self.assertFalse(state.mouse_follow_enabled)
            self.assertEqual(store.load(), state)

    def test_embodied_config_defaults_include_mouse_follow_and_speech_motion(self) -> None:
        config = Live2DEmbodiedConfig()

        self.assertTrue(config.mouse_follow_ui_enabled)
        self.assertFalse(config.mouse_follow_default_enabled)
        self.assertFalse(config.debug_commands.enabled)
        self.assertEqual(config.debug_commands.prefix, "/l2d")
        self.assertEqual(config.debug_commands.admin_user_ids, [])
        self.assertTrue(config.debug_commands.drop_non_admin_commands)
        self.assertEqual(config.mouse_follow_return_after_sec, 1.2)
        self.assertEqual(config.mouse_follow_cooldown_sec, 0.45)
        self.assertEqual(config.mouse_follow_poll_interval_ms, 12)
        self.assertEqual(config.mouse_follow_smoothing_ms, 45)
        self.assertEqual(config.mouse_follow_eye_gain, 0.55)
        self.assertEqual(config.mouse_follow_head_gain, 0.28)
        self.assertEqual(config.mouse_follow_body_gain, 0.12)
        self.assertEqual(config.speech_motion_audio_gain, 1.0)
        self.assertEqual(config.speech_motion_emotion_gain, 1.0)
        self.assertEqual(config.speech_motion_vertical_pump_gain, 1.25)
        self.assertEqual(config.speech_motion_lateral_gain, 1.25)
        self.assertEqual(config.speech_motion_pitch_gain, 1.0)
        self.assertEqual(config.speech_motion_yaw_gain, 1.0)
        self.assertEqual(config.speech_motion_roll_gain, 0.8)
        self.assertEqual(config.speech_motion_shoulder_gain, 0.75)
        self.assertFalse(config.blink.enabled)
        self.assertEqual(config.blink.interval_min_sec, 2.8)
        self.assertEqual(config.blink.interval_max_sec, 5.5)
        self.assertEqual(config.blink.double_blink_chance, 0.03)
        self.assertEqual(config.blink.close_ms, 60)
        self.assertEqual(config.blink.hold_ms, 28)
        self.assertEqual(config.blink.open_ms, 110)
        self.assertEqual(config.blink.double_blink_gap_ms, 140)
        self.assertFalse(config.wink.enabled)
        self.assertEqual(config.wink.close_ms, 65)
        self.assertEqual(config.wink.hold_ms, 90)
        self.assertEqual(config.wink.open_ms, 110)
        self.assertEqual(config.wink.request_cooldown_sec, 1.8)
        self.assertEqual(config.wink.non_target_eye_drop, 0.08)

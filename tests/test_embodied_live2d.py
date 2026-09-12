import asyncio
import contextlib
import sys
import time
import unittest

from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from maibot_bilibili_live_adapter_copy.live2d_adaptive.bridge import InMemoryLive2DBridge
from maibot_bilibili_live_adapter_copy.live2d_adaptive.controller import Live2DController
from maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied import (
    EmbodiedLive2DRuntime,
    EmbodiedParamDriver,
    EmbodiedStateSnapshot,
)
from maibot_bilibili_live_adapter_copy.live2d_adaptive.mouse_follow import MouseFollowSnapshot
from maibot_bilibili_live_adapter_copy.live2d_adaptive.profile import ParameterProfile, ParameterSpec


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(str(message))


class EmbodiedLive2DTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _non_idle_mouth_timeline_events(events: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            dict(event)
            for event in events
            if event.get("type") == "live2d.timeline.frame" and str(event.get("purpose") or "") != "idle-mouth"
        ]

    @staticmethod
    def _speech_motion_profile() -> ParameterProfile:
        return ParameterProfile(
            model_id="hiyori_pro_zh",
            model_name="Hiyori",
            parameters=[
                ParameterSpec("ParamAngleX", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleY", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleZ", -30.0, 30.0, 0.0),
                ParameterSpec("ParamBodyAngleX", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleY", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleZ", -10.0, 10.0, 0.0),
                ParameterSpec("ParamShoulder", -1.0, 1.0, 0.0),
                ParameterSpec("ParamMouthOpenY"),
                ParameterSpec("ParamMouthForm"),
            ],
        )

    @staticmethod
    def _hiyori_face_profile() -> ParameterProfile:
        return ParameterProfile(
            model_id="hiyori_pro_zh",
            model_name="Hiyori",
            parameters=[
                ParameterSpec("ParamAngleX", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleY", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleZ", -30.0, 30.0, 0.0),
                ParameterSpec("ParamBodyAngleX", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleY", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleZ", -10.0, 10.0, 0.0),
                ParameterSpec("ParamEyeBallX", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeBallY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeLOpen"),
                ParameterSpec("ParamEyeROpen"),
                ParameterSpec("ParamEyeLSmile"),
                ParameterSpec("ParamEyeRSmile"),
                ParameterSpec("ParamBrowLY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowLAngle", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRAngle", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowLForm", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRForm", -1.0, 1.0, 0.0),
                ParameterSpec("ParamCheek", 0.0, 1.0, 0.0),
                ParameterSpec("ParamBreath", 0.0, 1.0, 0.0),
                ParameterSpec("ParamShoulder", -1.0, 1.0, 0.0),
                ParameterSpec("ParamHairAhoge", -10.0, 10.0, 0.0),
                ParameterSpec("ParamHairFront", -1.0, 1.0, 0.0),
                ParameterSpec("ParamHairBack", -1.0, 1.0, 0.0),
                ParameterSpec("ParamRibbon", -1.0, 1.0, 0.0),
                ParameterSpec("ParamSideupRibbon", -1.0, 1.0, 0.0),
            ],
        )

    def _make_hiyori_face_driver(
        self,
        *,
        blink_enabled: bool = False,
        wink_enabled: bool = False,
    ) -> EmbodiedParamDriver:
        controller = Live2DController(
            bridge=InMemoryLive2DBridge(),
            profile=self._hiyori_face_profile(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        return EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
            blink_enabled=blink_enabled,
            wink_enabled=wink_enabled,
        )

    @staticmethod
    async def _collect_timeline_events(
        bridge: InMemoryLive2DBridge,
        *,
        minimum_count: int = 1,
        attempts: int = 20,
    ) -> list[dict[str, object]]:
        for _ in range(max(1, attempts)):
            events = [dict(event) for event in bridge.events if event.get("type") == "live2d.timeline.frame"]
            if len(events) >= minimum_count:
                return events
            await asyncio.sleep(0.01)
        return [dict(event) for event in bridge.events if event.get("type") == "live2d.timeline.frame"]

    @classmethod
    async def _collect_non_idle_mouth_timeline_events(
        cls,
        bridge: InMemoryLive2DBridge,
        *,
        minimum_count: int = 1,
        attempts: int = 20,
    ) -> list[dict[str, object]]:
        for _ in range(max(1, attempts)):
            events = cls._non_idle_mouth_timeline_events([dict(event) for event in bridge.events])
            if len(events) >= minimum_count:
                return events
            await asyncio.sleep(0.01)
        return cls._non_idle_mouth_timeline_events([dict(event) for event in bridge.events])

    @staticmethod
    def _reply_emotion_snapshot() -> EmbodiedStateSnapshot:
        return EmbodiedStateSnapshot(
            session_id="session-reply-emotion-directions",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.02,
            arousal=0.12,
            attention=0.62,
            cognitive_load=0.24,
            confidence=0.32,
            social_approach=0.05,
            energy=0.42,
            gaze_x=0.0,
            gaze_y=0.0,
        )

    @staticmethod
    def _targets_with_reply_emotion(
        driver: EmbodiedParamDriver,
        snapshot: EmbodiedStateSnapshot,
        *,
        now: float,
        emotion_intent: str = "",
        text: str = "",
        emotion_gain: float = 1.0,
    ) -> dict[str, float]:
        if emotion_intent or text:
            driver._remember_reply_emotion(
                emotion_intent=emotion_intent,
                text=text,
                emotion_gain=emotion_gain,
                now=now,
            )
        return driver._build_semantic_targets(snapshot, now=now + 0.1)

    def test_thinking_neutral_face_stays_attentive_not_sad(self) -> None:
        controller = Live2DController(
            bridge=InMemoryLive2DBridge(),
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        snapshot = EmbodiedStateSnapshot(
            session_id="session-thinking",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.0,
            arousal=0.08,
            attention=0.62,
            cognitive_load=0.65,
            confidence=0.24,
            social_approach=0.02,
            energy=0.3,
            gaze_x=0.0,
            gaze_y=0.0,
        )

        targets = driver._build_semantic_targets(snapshot, now=10.0)

        self.assertGreaterEqual(targets["brow.left.y"], -0.10)
        self.assertGreaterEqual(targets["brow.right.y"], -0.10)
        self.assertGreaterEqual(targets["eye.open"], 0.70)

    def test_driver_auto_blink_overrides_eye_open_but_not_mouth_or_gaze(self) -> None:
        driver = self._make_hiyori_face_driver(blink_enabled=True)
        snapshot = self._reply_emotion_snapshot()
        base_targets = driver._build_semantic_targets(snapshot, now=10.0)
        parameters = driver._resolve_parameters(base_targets)

        driver._blink_next_at_monotonic = 9.0
        driver._blink_started_at_monotonic = 10.0
        driver._blink_active = True
        driver._blink_is_double = False

        overlaid = driver._apply_eye_overlay(parameters, {"type": "live2d.timeline.frame"}, now=10.03)
        overlaid_by_id = {str(item["id"]): float(item["value"]) for item in overlaid}
        base_by_id = {str(item["id"]): float(item["value"]) for item in parameters}

        self.assertLess(overlaid_by_id["ParamEyeLOpen"], base_by_id["ParamEyeLOpen"])
        self.assertLess(overlaid_by_id["ParamEyeROpen"], base_by_id["ParamEyeROpen"])
        self.assertEqual(overlaid_by_id["ParamEyeBallX"], base_by_id["ParamEyeBallX"])
        self.assertEqual(overlaid_by_id["ParamEyeBallY"], base_by_id["ParamEyeBallY"])
        self.assertNotIn("ParamMouthOpenY", overlaid_by_id)
        self.assertNotIn("ParamMouthForm", overlaid_by_id)

    def test_driver_blink_respects_emotional_eye_open_ceiling(self) -> None:
        driver = self._make_hiyori_face_driver(blink_enabled=True)
        driver._remember_eye_open_base(
            [
                {"id": "ParamEyeLOpen", "value": 0.62, "weight": 0.70},
                {"id": "ParamEyeROpen", "value": 0.58, "weight": 0.70},
            ]
        )
        driver._blink_next_at_monotonic = 9.0
        driver._blink_started_at_monotonic = 10.0
        driver._blink_active = True
        driver._blink_is_double = False

        overlaid = driver._apply_eye_overlay(
            [
                {"id": "ParamEyeLOpen", "value": 1.0, "weight": 0.70},
                {"id": "ParamEyeROpen", "value": 1.0, "weight": 0.70},
            ],
            {"type": "live2d.timeline.frame"},
            now=10.03,
        )
        by_id = {str(item["id"]): float(item["value"]) for item in overlaid}

        self.assertLess(by_id["ParamEyeLOpen"], 0.35)
        self.assertLess(by_id["ParamEyeROpen"], 0.33)

    def test_default_blink_timing_prefers_single_softer_blinks(self) -> None:
        driver = self._make_hiyori_face_driver(blink_enabled=True)

        self.assertLessEqual(driver._blink_timing.double_blink_chance, 0.05)
        self.assertGreaterEqual(driver._blink_timing.close_sec, 0.055)
        self.assertGreaterEqual(driver._blink_timing.hold_sec, 0.025)
        self.assertGreaterEqual(driver._blink_timing.open_sec, 0.095)
        self.assertGreaterEqual(driver._blink_timing.double_gap_sec, 0.12)

    def test_blink_curve_eases_edges_instead_of_linear_snap(self) -> None:
        driver = self._make_hiyori_face_driver(blink_enabled=True)
        close_elapsed = driver._blink_timing.close_sec * 0.2
        reopen_elapsed = (
            driver._blink_timing.close_sec
            + driver._blink_timing.hold_sec
            + (driver._blink_timing.open_sec * 0.2)
        )

        close_amount = driver._blink_amount_at_elapsed(close_elapsed, is_double=False)
        reopen_amount = driver._blink_amount_at_elapsed(reopen_elapsed, is_double=False)

        self.assertLess(close_amount, 0.18)
        self.assertGreater(reopen_amount, 0.82)

    def test_blink_clip_uses_current_eye_anchor_not_future_clip_endpoint_for_ceiling(self) -> None:
        driver = self._make_hiyori_face_driver(blink_enabled=True)
        start_targets = {"eye.open": 0.08}
        end_targets = {"eye.open": 0.82}
        start_parameters = driver._resolve_parameters(start_targets)
        start_by_id = {str(item["id"]): float(item["value"]) for item in start_parameters}

        driver._build_transition_clip_frames(
            start_targets=start_targets,
            end_targets=end_targets,
            purpose="expressive",
            frame_count=6,
            interval_ms=16,
        )
        blink_clip = driver._build_blink_eye_clip(now=0.0, is_double=False)

        self.assertIsNotNone(blink_clip)
        blink_max_by_id = {
            parameter_id: max(
                float(parameter["value"])
                for frame in blink_clip.frames
                for parameter in list(frame.get("parameters") or [])
                if isinstance(parameter, dict) and str(parameter.get("id") or "") == parameter_id
            )
            for parameter_id in ("ParamEyeLOpen", "ParamEyeROpen")
        }

        self.assertLessEqual(blink_max_by_id["ParamEyeLOpen"], start_by_id["ParamEyeLOpen"] + 1e-6)
        self.assertLessEqual(blink_max_by_id["ParamEyeROpen"], start_by_id["ParamEyeROpen"] + 1e-6)

    def test_reply_expression_eye_open_stays_below_extreme_raw_upper_limit(self) -> None:
        driver = self._make_hiyori_face_driver(blink_enabled=True)
        snapshot = self._reply_emotion_snapshot()

        semantic_targets = driver._build_semantic_targets(snapshot, now=1.0)
        parameters = driver._resolve_parameters(semantic_targets)
        by_id = {str(item["id"]): float(item["value"]) for item in parameters}

        self.assertLessEqual(by_id["ParamEyeLOpen"], 1.08)
        self.assertLessEqual(by_id["ParamEyeROpen"], 1.08)

    def test_speaking_expression_eye_open_stays_within_default_model_ceiling(self) -> None:
        driver = self._make_hiyori_face_driver(blink_enabled=True)
        snapshot = EmbodiedStateSnapshot(
            session_id="session-speaking-eye-ceiling",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.25,
            arousal=0.65,
            attention=0.9,
            cognitive_load=0.15,
            confidence=0.72,
            social_approach=0.35,
            energy=0.8,
            gaze_x=0.0,
            gaze_y=0.0,
        )

        semantic_targets = driver._build_semantic_targets(snapshot, now=1.0)
        parameters = driver._resolve_parameters(semantic_targets)
        by_id = {str(item["id"]): float(item["value"]) for item in parameters}

        self.assertLessEqual(by_id["ParamEyeLOpen"], 1.0)
        self.assertLessEqual(by_id["ParamEyeROpen"], 1.0)

    def test_request_special_move_accepts_ahoge_spin_and_builds_ahoge_only_clip(self) -> None:
        driver = self._make_hiyori_face_driver()

        accepted = driver.request_special_move(
            action="Special_move",
            move="ahoge_spin",
            duration_sec=10.0,
            now=12.0,
        )

        self.assertTrue(accepted)
        self.assertTrue(driver._special_move_active)
        self.assertEqual(driver._special_move_name, "ahoge_spin")
        clip = driver._build_special_move_clip(now=12.0)
        self.assertIsNotNone(clip)
        parameter_ids = {
            str(parameter["id"])
            for frame in clip.frames
            for parameter in list(frame.get("parameters") or [])
            if isinstance(parameter, dict)
        }
        self.assertEqual(
            parameter_ids,
            {"ParamHairAhoge", "ParamHairFront", "ParamHairBack", "ParamRibbon", "ParamSideupRibbon"},
        )
        self.assertGreaterEqual(clip.duration_sec, 9.9)

    def test_request_special_move_accepts_ahoge_spin_alias_with_space(self) -> None:
        driver = self._make_hiyori_face_driver()

        accepted = driver.request_special_move(
            action="Special_move",
            move="ahoge spin",
            duration_sec=10.0,
            now=12.0,
        )

        self.assertTrue(accepted)
        self.assertTrue(driver._special_move_active)
        self.assertEqual(driver._special_move_name, "ahoge_spin")

    def test_request_special_move_uses_high_amplitude_sway_pattern_for_hiyori_ahoge(self) -> None:
        driver = self._make_hiyori_face_driver()

        accepted = driver.request_special_move(
            action="Special_move",
            move="ahoge_spin",
            duration_sec=2.0,
            now=12.0,
        )

        self.assertTrue(accepted)
        clip = driver._build_special_move_clip(now=12.0)
        self.assertIsNotNone(clip)
        values = [
            float(parameter["value"])
            for frame in clip.frames
            for parameter in list(frame.get("parameters") or [])
            if isinstance(parameter, dict) and str(parameter.get("id")) == "ParamHairAhoge"
        ]
        self.assertTrue(values)
        self.assertGreaterEqual(max(values), 9.5)
        self.assertLessEqual(min(values), -9.5)
        sign_changes = 0
        last_sign = 0
        for value in values[:64]:
            sign = 1 if value > 1.0 else -1 if value < -1.0 else 0
            if sign != 0:
                if last_sign != 0 and sign != last_sign:
                    sign_changes += 1
                last_sign = sign
        self.assertGreaterEqual(sign_changes, 10)

    def test_request_special_move_refreshes_existing_ahoge_spin_window(self) -> None:
        driver = self._make_hiyori_face_driver()

        accepted_first = driver.request_special_move(
            action="Special_move",
            move="ahoge_spin",
            duration_sec=10.0,
            now=5.0,
        )
        accepted_second = driver.request_special_move(
            action="Special_move",
            move="ahoge_spin",
            duration_sec=10.0,
            now=8.0,
        )

        self.assertTrue(accepted_first)
        self.assertTrue(accepted_second)
        self.assertAlmostEqual(driver._special_move_until, 18.0)

    async def test_driver_background_blink_dispatches_eye_timeline_without_new_carrier_events(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=self._hiyori_face_profile(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
            blink_enabled=True,
            blink_interval_min_sec=0.01,
            blink_interval_max_sec=0.01,
            blink_double_blink_chance=0.0,
            blink_close_ms=16,
            blink_hold_ms=8,
            blink_open_ms=16,
        )
        await controller.start()
        await driver.start()
        try:
            result = await driver.handle_snapshot(self._reply_emotion_snapshot())
            await controller._queue.join()
            self.assertTrue(result["success"])
            baseline_event_count = len(bridge.events)
            driver._blink_next_at_monotonic = time.monotonic() + 0.01
            driver._eye_overlay_wake.set()

            for _ in range(10):
                await asyncio.sleep(0.02)
                await controller._queue.join()
                blink_events = [
                    dict(event)
                    for event in bridge.events[baseline_event_count:]
                    if event.get("type") == "live2d.timeline.frame" and str(event.get("purpose") or "") == "blink"
                ]
                if blink_events:
                    break
            else:
                blink_events = []

            self.assertTrue(blink_events)
            blink_params = {
                str(item["id"]): float(item["value"])
                for item in list(blink_events[0].get("parameters") or [])
                if isinstance(item, dict)
            }
            self.assertIn("ParamEyeLOpen", blink_params)
            self.assertIn("ParamEyeROpen", blink_params)
        finally:
            await driver.stop()
            await controller.stop()

    async def test_driver_wink_closes_only_target_eye_and_resets_cleanly(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=self._hiyori_face_profile(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
            blink_enabled=False,
            wink_enabled=True,
        )
        await controller.start()
        await driver.start()
        try:
            accepted = driver.request_wink(side="left", now=20.0)
            self.assertTrue(accepted)

            result = await driver.handle_snapshot(self._reply_emotion_snapshot())
            await controller._queue.join()
            self.assertTrue(result["success"])

            for _ in range(10):
                await asyncio.sleep(0.02)
                await controller._queue.join()
                timeline_events = [
                    dict(event)
                    for event in bridge.events
                    if event.get("type") == "live2d.timeline.frame" and str(event.get("purpose") or "") == "wink"
                ]
                if timeline_events:
                    break
            else:
                timeline_events = []

            self.assertTrue(timeline_events)
            wink_frames = [
                {
                    str(item["id"]): float(item["value"])
                    for item in list(event.get("parameters") or [])
                    if isinstance(item, dict)
                }
                for event in timeline_events
            ]
            self.assertTrue(any("ParamEyeLOpen" in frame and "ParamEyeROpen" in frame for frame in wink_frames))
            self.assertLess(
                min(frame["ParamEyeLOpen"] for frame in wink_frames if "ParamEyeLOpen" in frame),
                min(frame["ParamEyeROpen"] for frame in wink_frames if "ParamEyeROpen" in frame),
            )
            self.assertTrue(all("ParamMouthOpenY" not in frame for frame in wink_frames))
            self.assertTrue(all("ParamMouthForm" not in frame for frame in wink_frames))

            await driver.handle_reset()
            self.assertFalse(driver._wink_active)
            self.assertFalse(driver._blink_active)
        finally:
            await driver.stop()
            await controller.stop()

    async def test_reply_text_emotion_overlay_makes_face_follow_content(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=self._speech_motion_profile(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        snapshot = EmbodiedStateSnapshot(
            session_id="session-reply-emotion",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.0,
            arousal=0.12,
            attention=0.62,
            cognitive_load=0.3,
            confidence=0.32,
            social_approach=0.05,
            energy=0.42,
            gaze_x=0.0,
            gaze_y=0.0,
        )
        audio_timeline = {
            "audio_duration_ms": 240,
            "amplitudes": [
                {"offset_ms": 0, "value": 0.0},
                {"offset_ms": 80, "value": 0.78},
                {"offset_ms": 160, "value": 0.88},
                {"offset_ms": 240, "value": 0.0},
            ],
        }

        await controller.start()
        try:
            with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=20.0):
                baseline = driver._build_semantic_targets(snapshot, now=20.0)
                result = await driver.handle_speech_envelope(
                    audio_timeline,
                    snapshot=snapshot,
                    emotion_gain=1.0,
                    text="哈哈，太好了，今天真开心",
                )
            with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=20.1):
                emotional = driver._build_semantic_targets(snapshot, now=20.1)
        finally:
            await controller.stop()

        self.assertTrue(result["success"])
        self.assertGreater(emotional["eye.left.smile"], baseline["eye.left.smile"] + 0.10)
        self.assertGreater(emotional["eye.right.smile"], baseline["eye.right.smile"] + 0.10)
        self.assertGreater(emotional["face.blush"], baseline["face.blush"] + 0.08)
        self.assertGreater(emotional["brow.left.y"], baseline["brow.left.y"] + 0.04)

    async def test_driver_mouse_follow_is_applied_only_at_output_layer_and_decays_after_lease_expiry(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        await controller.start()
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
            mouse_follow_enabled=True,
            mouse_follow_smoothing_ms=45,
            mouse_follow_return_after_sec=1.2,
            mouse_follow_cooldown_sec=0.45,
            mouse_follow_eye_gain=0.55,
            mouse_follow_head_gain=0.28,
            mouse_follow_body_gain=0.12,
        )
        await driver.start()
        snapshot = EmbodiedStateSnapshot(
            session_id="session-mouse",
            ts=1.0,
            seq=1,
            agent_state="wait",
            valence=0.0,
            arousal=0.0,
            attention=0.35,
            cognitive_load=0.1,
            confidence=0.25,
            social_approach=0.05,
            energy=0.2,
            gaze_x=0.0,
            gaze_y=0.0,
        )
        baseline_targets = driver._build_semantic_targets(snapshot, now=5.0)

        with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=5.0):
            driver.update_mouse_follow_snapshot(
                MouseFollowSnapshot(
                    x_norm=0.8,
                    y_norm=-0.4,
                    active=True,
                    activity_ts=5.0,
                )
            )
            fresh_targets = driver._build_semantic_targets(snapshot, now=5.0)
            result = await controller.send_timeline_clip(
                [
                    {
                        "offset_ms": 0,
                        "parameters": [
                            {"id": "ParamEyeBallX", "value": 0.0, "weight": 0.8},
                            {"id": "ParamEyeBallY", "value": 0.0, "weight": 0.8},
                            {"id": "ParamAngleX", "value": 0.0, "weight": 0.8},
                            {"id": "ParamAngleY", "value": 0.0, "weight": 0.8},
                            {"id": "ParamBodyAngleX", "value": 0.0, "weight": 0.8},
                            {"id": "ParamBodyAngleY", "value": 0.0, "weight": 0.8},
                        ],
                        "purpose": "idle",
                    }
                ],
                purpose="idle",
            )
            await controller._queue.join()

        for now_value in (5.012, 5.024, 5.036):
            with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=now_value):
                driver.update_mouse_follow_snapshot(
                    MouseFollowSnapshot(
                        x_norm=0.8,
                        y_norm=-0.4,
                        active=True,
                        activity_ts=5.0,
                    )
                )

        with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=5.048):
            await controller.send_timeline_clip(
                [
                    {
                        "offset_ms": 0,
                        "parameters": [
                            {"id": "ParamEyeBallX", "value": 0.0, "weight": 0.8},
                            {"id": "ParamEyeBallY", "value": 0.0, "weight": 0.8},
                            {"id": "ParamAngleX", "value": 0.0, "weight": 0.8},
                            {"id": "ParamAngleY", "value": 0.0, "weight": 0.8},
                            {"id": "ParamBodyAngleX", "value": 0.0, "weight": 0.8},
                            {"id": "ParamBodyAngleY", "value": 0.0, "weight": 0.8},
                        ],
                        "purpose": "idle",
                    }
                ],
                purpose="idle",
            )
            await controller._queue.join()

        self.assertEqual(fresh_targets, baseline_targets)
        self.assertTrue(result["success"])
        first_parameters = {
            str(parameter["id"]): float(parameter["value"])
            for parameter in list(bridge.events[-2].get("parameters") or [])
            if isinstance(parameter, dict)
        }
        smoothed_parameters = {
            str(parameter["id"]): float(parameter["value"])
            for parameter in list(bridge.events[-1].get("parameters") or [])
            if isinstance(parameter, dict)
        }
        self.assertGreater(first_parameters["ParamEyeBallX"], 0.0)
        self.assertLess(first_parameters["ParamEyeBallX"], 0.30)
        self.assertLess(first_parameters["ParamEyeBallY"], 0.0)
        self.assertGreater(first_parameters["ParamEyeBallY"], -0.20)
        self.assertGreater(smoothed_parameters["ParamEyeBallX"], first_parameters["ParamEyeBallX"])
        self.assertLess(smoothed_parameters["ParamEyeBallY"], first_parameters["ParamEyeBallY"])
        self.assertGreater(smoothed_parameters["ParamAngleX"], first_parameters["ParamAngleX"])
        self.assertLess(smoothed_parameters["ParamAngleY"], first_parameters["ParamAngleY"])
        self.assertGreater(smoothed_parameters["ParamBodyAngleX"], first_parameters["ParamBodyAngleX"])
        self.assertLess(smoothed_parameters["ParamBodyAngleY"], first_parameters["ParamBodyAngleY"])

        with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=6.35):
            await controller.send_timeline_clip(
                [
                    {
                        "offset_ms": 0,
                        "parameters": [
                            {"id": "ParamEyeBallX", "value": 0.0, "weight": 0.8},
                            {"id": "ParamAngleX", "value": 0.0, "weight": 0.8},
                        ],
                        "purpose": "idle",
                    }
                ],
                purpose="idle",
            )
            await controller._queue.join()
        cooldown_parameters = {
            str(parameter["id"]): float(parameter["value"])
            for parameter in list(bridge.events[-1].get("parameters") or [])
            if isinstance(parameter, dict)
        }
        self.assertGreater(cooldown_parameters["ParamEyeBallX"], 0.0)
        self.assertLess(cooldown_parameters["ParamEyeBallX"], smoothed_parameters["ParamEyeBallX"])
        self.assertGreater(cooldown_parameters["ParamAngleX"], 0.0)
        self.assertLess(cooldown_parameters["ParamAngleX"], smoothed_parameters["ParamAngleX"])

        with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=6.8):
            await controller.send_timeline_clip(
                [
                    {
                        "offset_ms": 0,
                        "parameters": [
                            {"id": "ParamEyeBallX", "value": 0.0, "weight": 0.8},
                            {"id": "ParamAngleX", "value": 0.0, "weight": 0.8},
                        ],
                        "purpose": "idle",
                    }
                ],
                purpose="idle",
            )
            await controller._queue.join()
        expired_parameters = {
            str(parameter["id"]): float(parameter["value"])
            for parameter in list(bridge.events[-1].get("parameters") or [])
            if isinstance(parameter, dict)
        }
        self.assertAlmostEqual(expired_parameters["ParamEyeBallX"], 0.0, places=6)
        self.assertAlmostEqual(expired_parameters["ParamAngleX"], 0.0, places=6)

        await driver.stop()
        await controller.stop()

    async def test_driver_disabling_mouse_follow_keeps_motion_state_but_removes_output_bias(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        await controller.start()
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
            mouse_follow_enabled=True,
            mouse_follow_smoothing_ms=45,
            mouse_follow_return_after_sec=1.2,
            mouse_follow_cooldown_sec=0.45,
            mouse_follow_eye_gain=0.55,
            mouse_follow_head_gain=0.28,
            mouse_follow_body_gain=0.12,
        )
        snapshot = EmbodiedStateSnapshot(
            session_id="session-mouse-disable",
            ts=1.0,
            seq=1,
            agent_state="wait",
            valence=0.0,
            arousal=0.0,
            attention=0.35,
            cognitive_load=0.1,
            confidence=0.25,
            social_approach=0.05,
            energy=0.2,
            gaze_x=0.0,
            gaze_y=0.0,
        )

        with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=5.0):
            driver.update_mouse_follow_snapshot(
                MouseFollowSnapshot(
                    x_norm=0.8,
                    y_norm=-0.4,
                    active=True,
                    activity_ts=5.0,
                )
            )
            await driver.handle_snapshot(snapshot)
            await controller._queue.join()
            enabled_targets = driver.debug_latest_targets()

        driver.set_mouse_follow_enabled(False)
        retained_targets = driver.debug_latest_targets()

        with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=5.05):
            await driver.handle_snapshot(snapshot)
            await controller._queue.join()
            disabled_targets = driver.debug_latest_targets()
            semantic_targets = driver._build_semantic_targets(snapshot, now=5.05)
            await controller.send_timeline_clip(
                [
                    {
                        "offset_ms": 0,
                        "parameters": [
                            {"id": "ParamEyeBallX", "value": 0.0, "weight": 0.8},
                            {"id": "ParamAngleX", "value": 0.0, "weight": 0.8},
                        ],
                        "purpose": "idle",
                    }
                ],
                purpose="idle",
            )
            await controller._queue.join()
            disabled_probe_parameters = {
                str(parameter["id"]): float(parameter["value"])
                for parameter in list(bridge.events[-1].get("parameters") or [])
                if isinstance(parameter, dict)
            }

        await driver.stop()
        await controller.stop()

        self.assertLess(abs(enabled_targets["eye.gaze.x"] - semantic_targets["eye.gaze.x"]), 0.02)
        self.assertEqual(retained_targets, enabled_targets)
        self.assertLess(abs(disabled_targets["eye.gaze.x"] - semantic_targets["eye.gaze.x"]), 0.02)
        self.assertLess(abs(disabled_targets["eye.gaze.y"] - semantic_targets["eye.gaze.y"]), 0.02)
        self.assertLess(abs(disabled_targets["head.yaw"] - semantic_targets["head.yaw"]), 0.02)
        self.assertLess(abs(disabled_targets["body.yaw"] - semantic_targets["body.yaw"]), 0.02)
        self.assertAlmostEqual(disabled_probe_parameters["ParamEyeBallX"], 0.0, places=6)
        self.assertAlmostEqual(disabled_probe_parameters["ParamAngleX"], 0.0, places=6)

    def test_sparse_speech_amplitudes_are_resampled_for_smoother_motion(self) -> None:
        driver = self._make_hiyori_face_driver()
        audio_timeline = {
            "audio_duration_ms": 320,
            "amplitudes": [
                {"offset_ms": 0, "value": 0.0},
                {"offset_ms": 80, "value": 0.72},
                {"offset_ms": 160, "value": 0.94},
                {"offset_ms": 240, "value": 0.64},
                {"offset_ms": 320, "value": 0.0},
            ],
        }

        samples = driver._extract_speech_amplitude_samples(audio_timeline)

        self.assertGreater(len(samples), len(audio_timeline["amplitudes"]))
        self.assertEqual(samples[0][0], 0)
        self.assertEqual(samples[-1][0], 320)
        self.assertLess(samples[1][0], 80)

    def test_driver_mouse_follow_output_modifier_smooths_towards_cursor_target(self) -> None:
        controller = Live2DController(
            bridge=InMemoryLive2DBridge(),
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
            mouse_follow_enabled=True,
            mouse_follow_smoothing_ms=60,
            mouse_follow_eye_gain=0.55,
            mouse_follow_head_gain=0.28,
            mouse_follow_body_gain=0.12,
        )
        base_parameters = [
            {"id": "ParamEyeBallX", "value": 0.0, "weight": 0.8},
            {"id": "ParamAngleX", "value": 0.0, "weight": 0.8},
        ]

        with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=5.0):
            driver.update_mouse_follow_snapshot(
                MouseFollowSnapshot(
                    x_norm=1.0,
                    y_norm=0.0,
                    active=True,
                    activity_ts=5.0,
                )
            )
            first = driver._apply_mouse_follow_output_modifier(base_parameters, {"purpose": "idle"})

        for now_value in (5.012, 5.024, 5.036):
            with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=now_value):
                driver.update_mouse_follow_snapshot(
                    MouseFollowSnapshot(
                        x_norm=1.0,
                        y_norm=0.0,
                        active=True,
                        activity_ts=5.0,
                    )
                )

        with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=5.048):
            settled = driver._apply_mouse_follow_output_modifier(base_parameters, {"purpose": "idle"})

        first_by_id = {str(parameter["id"]): float(parameter["value"]) for parameter in first}
        settled_by_id = {str(parameter["id"]): float(parameter["value"]) for parameter in settled}
        self.assertGreater(first_by_id["ParamEyeBallX"], 0.0)
        self.assertLess(first_by_id["ParamEyeBallX"], 0.44)
        self.assertGreater(settled_by_id["ParamEyeBallX"], first_by_id["ParamEyeBallX"])
        self.assertLess(settled_by_id["ParamEyeBallX"], 0.44)
        self.assertGreater(settled_by_id["ParamAngleX"], first_by_id["ParamAngleX"])
        self.assertLess(settled_by_id["ParamAngleX"], 22.4)

    async def test_driver_mouse_follow_output_modifier_does_not_dirty_reset_pose(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        await controller.start()
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
            mouse_follow_enabled=True,
        )
        await driver.start()

        with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=5.0):
            driver.update_mouse_follow_snapshot(
                MouseFollowSnapshot(
                    x_norm=0.8,
                    y_norm=-0.4,
                    active=True,
                    activity_ts=5.0,
                )
            )
            result = await driver.handle_reset()
            await controller._queue.join()

        await driver.stop()
        await controller.stop()

        self.assertTrue(result["success"])
        reset_parameters = {
            str(parameter["id"]): float(parameter["value"])
            for parameter in list(bridge.events[-1].get("parameters") or [])
            if isinstance(parameter, dict)
        }
        self.assertAlmostEqual(reset_parameters["ParamEyeBallX"], 0.0, places=6)
        self.assertAlmostEqual(reset_parameters["ParamEyeBallY"], 0.0, places=6)
        self.assertAlmostEqual(reset_parameters["ParamAngleX"], 0.0, places=6)
        self.assertAlmostEqual(reset_parameters["ParamAngleY"], 0.0, places=6)

    async def test_driver_speech_motion_balances_vertical_and_lateral_channels_without_mouth_parameters(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=self._speech_motion_profile(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        snapshot = EmbodiedStateSnapshot(
            session_id="session-speech-motion",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.4,
            arousal=0.75,
            attention=0.82,
            cognitive_load=0.2,
            confidence=0.58,
            social_approach=0.22,
            energy=0.78,
            gaze_x=0.18,
            gaze_y=-0.08,
        )
        audio_timeline = {
            "audio_duration_ms": 320,
            "amplitudes": [
                {"offset_ms": 0, "value": 0.0},
                {"offset_ms": 80, "value": 0.72},
                {"offset_ms": 160, "value": 0.94},
                {"offset_ms": 240, "value": 0.64},
                {"offset_ms": 320, "value": 0.0},
            ],
        }

        await controller.start()
        try:
            with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=10.0):
                semantic_targets = driver._build_semantic_targets(snapshot, now=10.0)
                smoothed_targets = driver._smooth_targets(semantic_targets, now=10.0)
                result = await driver.handle_speech_envelope(
                    audio_timeline,
                    snapshot=snapshot,
                    emotion_gain=1.0,
                )
        finally:
            await controller.stop()

        self.assertTrue(result["success"])
        self.assertEqual(result["purpose"], "speech")
        self.assertFalse(result.get("queued", False))
        self.assertGreater(result["frame_count"], len(audio_timeline["amplitudes"]))

        parameter_ids = {str(item["id"]) for item in result["parameters"]}
        self.assertIn("ParamAngleX", parameter_ids)
        self.assertIn("ParamAngleY", parameter_ids)
        self.assertIn("ParamAngleZ", parameter_ids)
        self.assertIn("ParamBodyAngleX", parameter_ids)
        self.assertIn("ParamBodyAngleY", parameter_ids)
        self.assertIn("ParamShoulder", parameter_ids)
        self.assertNotIn("ParamMouthOpenY", parameter_ids)
        self.assertNotIn("ParamMouthForm", parameter_ids)

        timeline_events = self._non_idle_mouth_timeline_events(await self._collect_timeline_events(
            bridge,
            minimum_count=result["frame_count"],
        ))
        self.assertEqual(len(timeline_events), result["frame_count"])
        bridged_ids = {
            str(parameter.get("id") or "")
            for event in timeline_events
            if str(event.get("purpose") or "") != "idle-mouth"
            for parameter in list(event.get("parameters") or [])
            if isinstance(parameter, dict)
        }
        self.assertIn("ParamAngleX", bridged_ids)
        self.assertIn("ParamAngleY", bridged_ids)
        self.assertIn("ParamBodyAngleX", bridged_ids)
        self.assertIn("ParamBodyAngleY", bridged_ids)
        self.assertIn("ParamShoulder", bridged_ids)
        self.assertNotIn("ParamMouthOpenY", bridged_ids)
        self.assertNotIn("ParamMouthForm", bridged_ids)

        def _parameter_track(parameter_id: str) -> list[float]:
            values: list[float] = []
            for event in timeline_events:
                for parameter in list(event.get("parameters") or []):
                    if isinstance(parameter, dict) and str(parameter.get("id") or "") == parameter_id:
                        values.append(float(parameter.get("value") or 0.0))
                        break
            return values

        self.assertGreater(max(_parameter_track("ParamAngleX")) - min(_parameter_track("ParamAngleX")), 4.0)
        self.assertGreater(max(_parameter_track("ParamAngleY")) - min(_parameter_track("ParamAngleY")), 6.0)
        self.assertGreater(max(_parameter_track("ParamAngleZ")) - min(_parameter_track("ParamAngleZ")), 4.0)
        self.assertGreater(max(_parameter_track("ParamBodyAngleX")) - min(_parameter_track("ParamBodyAngleX")), 1.5)
        self.assertGreater(max(_parameter_track("ParamBodyAngleY")) - min(_parameter_track("ParamBodyAngleY")), 2.0)
        self.assertGreater(max(_parameter_track("ParamShoulder")) - min(_parameter_track("ParamShoulder")), 0.15)

        speech_end_targets = result["end_targets"]
        self.assertLess(abs(speech_end_targets["body.pitch"] - smoothed_targets["body.pitch"]), 0.45)

    def test_speech_clip_starts_from_anchor_targets_before_motion_kicks_in(self) -> None:
        driver = self._make_hiyori_face_driver()
        anchor_targets = {
            "head.yaw": 0.42,
            "head.pitch": -0.08,
            "head.roll": 0.16,
            "body.yaw": 0.24,
            "body.pitch": 0.05,
            "body.shoulder": 0.18,
            "brow.left.y": 0.10,
            "brow.right.y": 0.10,
            "brow.left.angle": 0.05,
            "brow.right.angle": 0.05,
            "brow.left.form": 0.02,
            "brow.right.form": 0.02,
            "eye.left.smile": 0.08,
            "eye.right.smile": 0.08,
            "face.blush": 0.05,
        }
        snapshot = EmbodiedStateSnapshot(
            session_id="session-speech-anchor",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.35,
            arousal=0.5,
            attention=0.72,
            cognitive_load=0.18,
            confidence=0.48,
            social_approach=0.2,
            energy=0.62,
            gaze_x=0.16,
            gaze_y=-0.05,
        )
        audio_timeline = {
            "audio_duration_ms": 240,
            "amplitudes": [
                {"offset_ms": 0, "value": 0.0},
                {"offset_ms": 60, "value": 0.6},
                {"offset_ms": 120, "value": 0.85},
                {"offset_ms": 180, "value": 0.55},
                {"offset_ms": 240, "value": 0.0},
            ],
        }

        driver._playback_anchor_targets = dict(anchor_targets)
        clip = driver._build_speech_clip(
            audio_timeline,
            snapshot=snapshot,
            base_targets=anchor_targets,
            emotion_gain=0.8,
            timeline_id="speech-anchor",
        )

        self.assertIsNotNone(clip)
        assert clip is not None
        first_frame = min(clip.frames, key=lambda item: int(item.get("offset_ms") or 0))
        self.assertEqual(int(first_frame.get("offset_ms") or 0), 0)
        first_parameters = {
            str(parameter.get("id") or ""): float(parameter.get("value") or 0.0)
            for parameter in list(first_frame.get("parameters") or [])
            if isinstance(parameter, dict)
        }
        expected_parameters = {
            str(parameter.get("id") or ""): float(parameter.get("value") or 0.0)
            for parameter in driver._resolve_speech_parameters(anchor_targets)
            if isinstance(parameter, dict)
        }

        self.assertAlmostEqual(first_parameters["ParamAngleX"], expected_parameters["ParamAngleX"], places=6)
        self.assertAlmostEqual(first_parameters["ParamAngleY"], expected_parameters["ParamAngleY"], places=6)
        self.assertAlmostEqual(first_parameters["ParamBodyAngleX"], expected_parameters["ParamBodyAngleX"], places=6)
        self.assertAlmostEqual(first_parameters["ParamBodyAngleY"], expected_parameters["ParamBodyAngleY"], places=6)
        self.assertAlmostEqual(first_parameters["ParamShoulder"], expected_parameters["ParamShoulder"], places=6)

    async def test_snapshot_updates_do_not_displace_queued_speech_motion(self) -> None:
        controller = Live2DController(
            bridge=InMemoryLive2DBridge(),
            profile=self._speech_motion_profile(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        speech_snapshot = EmbodiedStateSnapshot(
            session_id="session-queued-speech",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.42,
            arousal=0.72,
            attention=0.78,
            cognitive_load=0.22,
            confidence=0.56,
            social_approach=0.2,
            energy=0.74,
            gaze_x=0.12,
            gaze_y=-0.06,
        )
        followup_snapshot = EmbodiedStateSnapshot(
            session_id="session-queued-speech",
            ts=1.1,
            seq=2,
            agent_state="running",
            valence=0.18,
            arousal=0.28,
            attention=0.52,
            cognitive_load=0.16,
            confidence=0.42,
            social_approach=0.08,
            energy=0.38,
            gaze_x=-0.04,
            gaze_y=0.02,
        )
        audio_timeline = {
            "audio_duration_ms": 260,
            "amplitudes": [
                {"offset_ms": 0, "value": 0.0},
                {"offset_ms": 65, "value": 0.64},
                {"offset_ms": 130, "value": 0.92},
                {"offset_ms": 195, "value": 0.58},
                {"offset_ms": 260, "value": 0.0},
            ],
        }

        await controller.start()
        await driver.start()
        try:
            driver._clip_busy_until = asyncio.get_running_loop().time() + 60.0
            speech_result = await driver.handle_speech_envelope(
                audio_timeline,
                snapshot=speech_snapshot,
                emotion_gain=1.0,
            )
            snapshot_result = await driver.handle_snapshot(followup_snapshot)
            queued_purposes = [clip.purpose for clip in driver._motion_queue]
        finally:
            await driver.stop()
            await controller.stop()

        self.assertTrue(speech_result["success"])
        self.assertTrue(speech_result.get("queued", False))
        self.assertTrue(snapshot_result["success"])
        self.assertTrue(snapshot_result.get("queued", False))
        self.assertIn("speech", queued_purposes)
        self.assertEqual(queued_purposes[0], "speech")

    async def test_snapshot_updates_wait_until_speaking_finishes_before_dispatch(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        snapshot = EmbodiedStateSnapshot(
            session_id="session-speaking-hold",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.12,
            arousal=0.22,
            attention=0.64,
            cognitive_load=0.18,
            confidence=0.34,
            social_approach=0.08,
            energy=0.42,
            gaze_x=0.04,
            gaze_y=-0.02,
        )

        await controller.start()
        await driver.start()
        try:
            controller._speaking = True
            result = await driver.handle_snapshot(snapshot)
            await asyncio.sleep(0.08)
            timeline_events = self._non_idle_mouth_timeline_events([dict(event) for event in bridge.events])

            self.assertTrue(result["success"])
            self.assertTrue(result.get("queued", False))
            self.assertEqual(timeline_events, [])

            controller._speaking = False
            timeline_events = await self._collect_non_idle_mouth_timeline_events(bridge, minimum_count=1)
        finally:
            await driver.stop()
            await controller.stop()

        self.assertTrue(timeline_events)
        self.assertEqual(str(timeline_events[0].get("purpose") or ""), "expressive")

    async def test_snapshot_updates_wait_until_speech_transition_grace_period_expires_before_dispatch(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        snapshot = EmbodiedStateSnapshot(
            session_id="session-speaking-grace-hold",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.14,
            arousal=0.24,
            attention=0.65,
            cognitive_load=0.18,
            confidence=0.36,
            social_approach=0.08,
            energy=0.44,
            gaze_x=0.05,
            gaze_y=-0.03,
        )

        await controller.start()
        await driver.start()
        try:
            driver._speech_transition_hold_until = time.monotonic() + 0.12
            result = await driver.handle_snapshot(snapshot)
            await asyncio.sleep(0.08)
            timeline_events = self._non_idle_mouth_timeline_events([dict(event) for event in bridge.events])

            self.assertTrue(result["success"])
            self.assertTrue(result.get("queued", False))
            self.assertEqual(timeline_events, [])

            timeline_events = await self._collect_non_idle_mouth_timeline_events(bridge, minimum_count=1, attempts=40)
        finally:
            await driver.stop()
            await controller.stop()

        self.assertTrue(timeline_events)
        self.assertEqual(str(timeline_events[0].get("purpose") or ""), "expressive")

    async def test_scheduler_does_not_generate_idle_clip_while_controller_is_speaking(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        snapshot = EmbodiedStateSnapshot(
            session_id="session-speaking-idle-gap",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.08,
            arousal=0.16,
            attention=0.58,
            cognitive_load=0.14,
            confidence=0.30,
            social_approach=0.05,
            energy=0.38,
            gaze_x=0.02,
            gaze_y=-0.01,
        )

        await controller.start()
        await driver.start()
        try:
            driver._received_snapshot = True
            driver._latest_snapshot = snapshot
            driver._latest_targets = driver._build_semantic_targets(snapshot, now=10.0)
            controller._speaking = True

            await asyncio.sleep(0.08)
            idle_events = [
                event
                for event in bridge.events
                if event.get("type") == "live2d.timeline.frame" and str(event.get("purpose") or "") == "idle"
            ]
            self.assertEqual(idle_events, [])

            controller._speaking = False
            idle_events = []
            for _ in range(20):
                idle_events = [
                    event
                    for event in bridge.events
                    if event.get("type") == "live2d.timeline.frame" and str(event.get("purpose") or "") == "idle"
                ]
                if idle_events:
                    break
                await asyncio.sleep(0.02)
        finally:
            await driver.stop()
            await controller.stop()

        self.assertTrue(idle_events)

    async def test_scheduler_does_not_generate_idle_clip_during_speech_transition_grace_period(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        snapshot = EmbodiedStateSnapshot(
            session_id="session-speaking-idle-grace-gap",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.08,
            arousal=0.16,
            attention=0.58,
            cognitive_load=0.14,
            confidence=0.30,
            social_approach=0.05,
            energy=0.38,
            gaze_x=0.02,
            gaze_y=-0.01,
        )

        await controller.start()
        await driver.start()
        try:
            driver._received_snapshot = True
            driver._latest_snapshot = snapshot
            driver._latest_targets = driver._build_semantic_targets(snapshot, now=10.0)
            driver._speech_transition_hold_until = time.monotonic() + 0.12

            await asyncio.sleep(0.08)
            idle_events = [
                event
                for event in bridge.events
                if event.get("type") == "live2d.timeline.frame" and str(event.get("purpose") or "") == "idle"
            ]
            self.assertEqual(idle_events, [])

            idle_events = []
            for _ in range(40):
                idle_events = [
                    event
                    for event in bridge.events
                    if event.get("type") == "live2d.timeline.frame" and str(event.get("purpose") or "") == "idle"
                ]
                if idle_events:
                    break
                await asyncio.sleep(0.02)
        finally:
            await driver.stop()
            await controller.stop()

        self.assertTrue(idle_events)

    async def test_driver_speech_motion_scales_with_emotion_gain(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=self._speech_motion_profile(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        snapshot = EmbodiedStateSnapshot(
            session_id="session-speech-emotion",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.55,
            arousal=0.82,
            attention=0.85,
            cognitive_load=0.18,
            confidence=0.62,
            social_approach=0.3,
            energy=0.8,
            gaze_x=0.16,
            gaze_y=-0.1,
        )
        audio_timeline = {
            "audio_duration_ms": 300,
            "amplitudes": [
                {"offset_ms": 0, "value": 0.0},
                {"offset_ms": 75, "value": 0.68},
                {"offset_ms": 150, "value": 0.96},
                {"offset_ms": 225, "value": 0.74},
                {"offset_ms": 300, "value": 0.0},
            ],
        }

        baseline_events: list[dict[str, object]] = []
        amplified_events: list[dict[str, object]] = []
        await controller.start()
        try:
            with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=12.0):
                baseline = await driver.handle_speech_envelope(
                    audio_timeline,
                    snapshot=snapshot,
                    emotion_gain=0.35,
                )
            baseline_events = await self._collect_timeline_events(
                bridge,
                minimum_count=baseline["frame_count"],
            )
            driver._smoothed_values = {}
            driver._latest_targets = {}
            driver._playback_anchor_targets = {}
            bridge.events.clear()
            with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=12.0):
                amplified = await driver.handle_speech_envelope(
                    audio_timeline,
                    snapshot=snapshot,
                    emotion_gain=1.0,
                )
            amplified_events = await self._collect_timeline_events(
                bridge,
                minimum_count=amplified["frame_count"],
            )
        finally:
            await controller.stop()

        self.assertTrue(baseline["success"])
        self.assertTrue(amplified["success"])

        def _parameter_range(events: list[dict[str, object]], parameter_id: str) -> float:
            values: list[float] = []
            for event in events:
                for parameter in list(event.get("parameters") or []):
                    if isinstance(parameter, dict) and str(parameter.get("id") or "") == parameter_id:
                        values.append(float(parameter.get("value") or 0.0))
                        break
            return max(values) - min(values) if values else 0.0

        self.assertGreater(_parameter_range(amplified_events, "ParamBodyAngleY"), _parameter_range(baseline_events, "ParamBodyAngleY"))
        self.assertGreater(_parameter_range(amplified_events, "ParamBodyAngleX"), _parameter_range(baseline_events, "ParamBodyAngleX"))
        self.assertGreater(_parameter_range(amplified_events, "ParamAngleY"), _parameter_range(baseline_events, "ParamAngleY"))
        self.assertGreaterEqual(_parameter_range(amplified_events, "ParamAngleX"), _parameter_range(baseline_events, "ParamAngleX"))
        self.assertGreater(_parameter_range(amplified_events, "ParamAngleZ"), _parameter_range(baseline_events, "ParamAngleZ"))
        self.assertGreater(_parameter_range(amplified_events, "ParamShoulder"), _parameter_range(baseline_events, "ParamShoulder"))

    async def test_driver_speech_motion_limits_frame_to_frame_speed(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=self._speech_motion_profile(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        snapshot = EmbodiedStateSnapshot(
            session_id="session-speech-speed",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.4,
            arousal=0.75,
            attention=0.82,
            cognitive_load=0.2,
            confidence=0.58,
            social_approach=0.22,
            energy=0.78,
            gaze_x=0.18,
            gaze_y=-0.08,
        )
        audio_timeline = {
            "audio_duration_ms": 320,
            "amplitudes": [
                {"offset_ms": 0, "value": 0.0},
                {"offset_ms": 80, "value": 0.72},
                {"offset_ms": 160, "value": 0.94},
                {"offset_ms": 240, "value": 0.64},
                {"offset_ms": 320, "value": 0.0},
            ],
        }

        await controller.start()
        try:
            await driver.handle_speech_envelope(
                audio_timeline,
                snapshot=snapshot,
                emotion_gain=1.0,
            )
        finally:
            await controller.stop()

        timeline_events = [event for event in bridge.events if event.get("type") == "live2d.timeline.frame"]

        def _parameter_track(parameter_id: str) -> list[float]:
            values: list[float] = []
            for event in timeline_events:
                for parameter in list(event.get("parameters") or []):
                    if isinstance(parameter, dict) and str(parameter.get("id") or "") == parameter_id:
                        values.append(float(parameter.get("value") or 0.0))
                        break
            return values

        def _max_step(parameter_id: str) -> float:
            track = _parameter_track(parameter_id)
            if len(track) < 2:
                return 0.0
            return max(abs(current - previous) for previous, current in zip(track, track[1:]))

        self.assertLess(_max_step("ParamAngleX"), 8.0)
        self.assertLess(_max_step("ParamAngleY"), 8.0)
        self.assertLess(_max_step("ParamBodyAngleY"), 3.5)

    def test_speech_frame_step_limit_scales_with_emotion_intensity(self) -> None:
        driver = self._make_hiyori_face_driver()
        previous_targets = {
            "head.yaw": 0.0,
            "head.pitch": 0.0,
            "head.roll": 0.0,
            "body.yaw": 0.0,
            "body.pitch": 0.0,
            "body.shoulder": 0.0,
        }
        desired_targets = {
            "head.yaw": 1.0,
            "head.pitch": 1.0,
            "head.roll": 1.0,
            "body.yaw": 1.0,
            "body.pitch": 1.0,
            "body.shoulder": 1.0,
        }
        calm_snapshot = EmbodiedStateSnapshot(
            session_id="session-calm-speed-limit",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.04,
            arousal=0.10,
            attention=0.48,
            cognitive_load=0.14,
            confidence=0.28,
            social_approach=0.04,
            energy=0.22,
            gaze_x=0.0,
            gaze_y=0.0,
        )
        intense_snapshot = EmbodiedStateSnapshot(
            session_id="session-intense-speed-limit",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.58,
            arousal=0.88,
            attention=0.82,
            cognitive_load=0.10,
            confidence=0.62,
            social_approach=0.26,
            energy=0.84,
            gaze_x=0.0,
            gaze_y=0.0,
        )

        calm_limited = driver._limit_speech_frame_step(
            previous_targets=previous_targets,
            current_targets=desired_targets,
            snapshot=calm_snapshot,
            emotion_gain=0.2,
        )
        intense_limited = driver._limit_speech_frame_step(
            previous_targets=previous_targets,
            current_targets=desired_targets,
            snapshot=intense_snapshot,
            emotion_gain=1.0,
        )

        self.assertLess(calm_limited["head.yaw"], intense_limited["head.yaw"])
        self.assertLess(calm_limited["head.pitch"], intense_limited["head.pitch"])
        self.assertLess(calm_limited["body.pitch"], intense_limited["body.pitch"])
        self.assertLess(calm_limited["body.shoulder"], intense_limited["body.shoulder"])

    def test_speech_motion_filter_smooths_body_more_than_head(self) -> None:
        driver = self._make_hiyori_face_driver()
        previous_targets = {
            "head.yaw": 0.0,
            "head.pitch": 0.0,
            "head.roll": 0.0,
            "body.yaw": 0.0,
            "body.pitch": 0.0,
            "body.shoulder": 0.0,
            "eye.left.smile": 0.15,
        }
        desired_targets = {
            "head.yaw": 1.0,
            "head.pitch": 1.0,
            "head.roll": 1.0,
            "body.yaw": 1.0,
            "body.pitch": 1.0,
            "body.shoulder": 1.0,
            "eye.left.smile": 0.75,
        }
        calm_snapshot = EmbodiedStateSnapshot(
            session_id="session-calm-motion-filter",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.06,
            arousal=0.10,
            attention=0.46,
            cognitive_load=0.14,
            confidence=0.26,
            social_approach=0.04,
            energy=0.24,
            gaze_x=0.0,
            gaze_y=0.0,
        )
        intense_snapshot = EmbodiedStateSnapshot(
            session_id="session-intense-motion-filter",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.62,
            arousal=0.92,
            attention=0.84,
            cognitive_load=0.08,
            confidence=0.66,
            social_approach=0.28,
            energy=0.88,
            gaze_x=0.0,
            gaze_y=0.0,
        )

        calm_filtered = driver._filter_speech_motion_targets(
            previous_targets=previous_targets,
            current_targets=desired_targets,
            snapshot=calm_snapshot,
            emotion_gain=0.2,
        )
        intense_filtered = driver._filter_speech_motion_targets(
            previous_targets=previous_targets,
            current_targets=desired_targets,
            snapshot=intense_snapshot,
            emotion_gain=1.0,
        )

        self.assertLess(calm_filtered["head.pitch"], desired_targets["head.pitch"])
        self.assertLess(calm_filtered["body.pitch"], calm_filtered["head.pitch"])
        self.assertLess(calm_filtered["body.shoulder"], calm_filtered["head.yaw"])
        self.assertEqual(calm_filtered["eye.left.smile"], desired_targets["eye.left.smile"])
        self.assertGreater(intense_filtered["head.pitch"], calm_filtered["head.pitch"])
        self.assertGreater(intense_filtered["body.pitch"], calm_filtered["body.pitch"])

    def test_hiyori_reference_profile_applies_motion_calibration(self) -> None:
        profile = ParameterProfile(
            model_id="hiyori_pro_zh",
            model_name="Hiyori",
            parameters=[
                ParameterSpec("ParamAngleX", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleY", -30.0, 30.0, 0.0),
                ParameterSpec("ParamBodyAngleX", -10.0, 10.0, 0.0),
                ParameterSpec("ParamEyeBallX", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeBallY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeLOpen"),
                ParameterSpec("ParamEyeROpen"),
                ParameterSpec("ParamEyeLSmile"),
                ParameterSpec("ParamEyeRSmile"),
                ParameterSpec("ParamBrowLY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowLAngle", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRAngle", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowLForm", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRForm", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBreath", 0.0, 1.0, 0.0),
                ParameterSpec("ParamCheek"),
                ParameterSpec("ParamShoulder"),
                ParameterSpec("ParamHairAhoge"),
            ],
        )

        self.assertEqual(profile.parameters["ParamShoulder"].role, "body.shoulder")
        self.assertEqual(profile.parameters["ParamShoulder"].minimum, -1.0)
        self.assertEqual(profile.parameters["ParamShoulder"].maximum, 1.0)
        self.assertEqual(profile.parameters["ParamHairAhoge"].role, "accessory.ahoge")
        self.assertEqual(profile.parameters["ParamHairAhoge"].minimum, -10.0)
        self.assertEqual(profile.parameters["ParamHairAhoge"].maximum, 10.0)
        self.assertEqual(profile.parameters["ParamEyeLOpen"].maximum, 1.2)
        self.assertEqual(profile.parameters["ParamEyeROpen"].maximum, 1.2)
        self.assertEqual(profile.parameters["ParamBrowLAngle"].role, "brow.left.angle")
        self.assertEqual(profile.parameters["ParamBrowRAngle"].role, "brow.right.angle")
        self.assertEqual(profile.parameters["ParamBrowLForm"].role, "brow.left.form")
        self.assertEqual(profile.parameters["ParamBrowRForm"].role, "brow.right.form")

    def test_hiyori_idle_face_bias_stays_visibly_positive(self) -> None:
        profile = ParameterProfile(
            model_id="hiyori_pro_zh",
            model_name="Hiyori",
            parameters=[
                ParameterSpec("ParamAngleX", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleY", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleZ", -30.0, 30.0, 0.0),
                ParameterSpec("ParamBodyAngleX", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleY", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleZ", -10.0, 10.0, 0.0),
                ParameterSpec("ParamEyeBallX", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeBallY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeLOpen"),
                ParameterSpec("ParamEyeROpen"),
                ParameterSpec("ParamEyeLSmile"),
                ParameterSpec("ParamEyeRSmile"),
                ParameterSpec("ParamBrowLY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowLAngle", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRAngle", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowLForm", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRForm", -1.0, 1.0, 0.0),
                ParameterSpec("ParamCheek", 0.0, 1.0, 0.0),
                ParameterSpec("ParamBreath", 0.0, 1.0, 0.0),
                ParameterSpec("ParamShoulder", -1.0, 1.0, 0.0),
                ParameterSpec("ParamHairAhoge", -10.0, 10.0, 0.0),
            ],
        )
        controller = Live2DController(
            bridge=InMemoryLive2DBridge(),
            profile=profile,
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        snapshot = EmbodiedStateSnapshot(
            session_id="session-hiyori-idle",
            ts=1.0,
            seq=1,
            agent_state="wait",
            valence=0.08,
            arousal=0.03,
            attention=0.08,
            cognitive_load=0.02,
            confidence=0.05,
            social_approach=0.04,
            energy=0.05,
            gaze_x=0.0,
            gaze_y=0.0,
        )

        targets = driver._build_semantic_targets(snapshot, now=20.0)
        parameters = driver._resolve_parameters(targets)
        parameter_ids = {str(item["id"]) for item in parameters}

        self.assertGreaterEqual(targets["eye.left.smile"], 0.08)
        self.assertGreaterEqual(targets["face.blush"], 0.05)
        self.assertGreaterEqual(targets["brow.left.y"], 0.12)
        self.assertIn("ParamBrowLAngle", parameter_ids)
        self.assertIn("ParamBrowRAngle", parameter_ids)
        self.assertIn("ParamBrowLForm", parameter_ids)
        self.assertIn("ParamBrowRForm", parameter_ids)

    def test_hiyori_idle_face_keeps_brow_shape_close_to_neutral(self) -> None:
        profile = ParameterProfile(
            model_id="hiyori_pro_zh",
            model_name="Hiyori",
            parameters=[
                ParameterSpec("ParamAngleX", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleY", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleZ", -30.0, 30.0, 0.0),
                ParameterSpec("ParamBodyAngleX", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleY", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleZ", -10.0, 10.0, 0.0),
                ParameterSpec("ParamEyeBallX", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeBallY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeLOpen"),
                ParameterSpec("ParamEyeROpen"),
                ParameterSpec("ParamEyeLSmile"),
                ParameterSpec("ParamEyeRSmile"),
                ParameterSpec("ParamBrowLY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowLAngle", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRAngle", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowLForm", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRForm", -1.0, 1.0, 0.0),
                ParameterSpec("ParamCheek", 0.0, 1.0, 0.0),
                ParameterSpec("ParamBreath", 0.0, 1.0, 0.0),
                ParameterSpec("ParamShoulder", -1.0, 1.0, 0.0),
                ParameterSpec("ParamHairAhoge", -10.0, 10.0, 0.0),
            ],
        )
        controller = Live2DController(
            bridge=InMemoryLive2DBridge(),
            profile=profile,
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        snapshot = EmbodiedStateSnapshot(
            session_id="session-hiyori-neutral-face",
            ts=1.0,
            seq=1,
            agent_state="wait",
            valence=0.08,
            arousal=0.03,
            attention=0.08,
            cognitive_load=0.02,
            confidence=0.05,
            social_approach=0.04,
            energy=0.05,
            gaze_x=0.0,
            gaze_y=0.0,
        )

        targets = driver._build_semantic_targets(snapshot, now=20.0)

        self.assertLessEqual(abs(targets["brow.left.angle"]), 0.04)
        self.assertLessEqual(abs(targets["brow.left.form"]), 0.04)
        self.assertGreaterEqual(targets["brow.left.y"], 0.14)
        self.assertGreaterEqual(targets["eye.left.smile"], 0.12)
        self.assertGreaterEqual(targets["face.blush"], 0.08)

    def test_idle_random_follow_signal_is_deterministic_and_slow(self) -> None:
        driver = self._make_hiyori_face_driver()

        start = driver._idle_random_follow_value("yaw_follow", 0.0, interval_sec=4.2, amplitude=1.0)
        same = driver._idle_random_follow_value("yaw_follow", 0.0, interval_sec=4.2, amplitude=1.0)
        near = driver._idle_random_follow_value("yaw_follow", 0.3, interval_sec=4.2, amplitude=1.0)
        far = driver._idle_random_follow_value("yaw_follow", 4.8, interval_sec=4.2, amplitude=1.0)

        self.assertAlmostEqual(start, same)
        self.assertLess(abs(near - start), 0.12)
        self.assertGreater(abs(far - start), 0.05)

    def test_idle_targets_add_smaller_random_follow_to_head_and_body(self) -> None:
        driver = self._make_hiyori_face_driver()
        snapshot = EmbodiedStateSnapshot(
            session_id="session-idle-follow",
            ts=1.0,
            seq=1,
            agent_state="wait",
            valence=0.12,
            arousal=0.08,
            attention=0.34,
            cognitive_load=0.08,
            confidence=0.22,
            social_approach=0.04,
            energy=0.18,
            gaze_x=0.0,
            gaze_y=0.0,
        )
        base_targets = {
            "head.yaw": 0.0,
            "head.pitch": 0.0,
            "head.roll": 0.0,
            "body.yaw": 0.0,
            "body.pitch": 0.0,
            "body.roll": 0.0,
            "body.shoulder": 0.0,
            "eye.gaze.x": 0.0,
            "eye.gaze.y": 0.0,
            "breath": 0.2,
        }

        def _baseline_follow(_axis: str, _motion_t: float, *, interval_sec: float, amplitude: float) -> float:
            return 0.0

        def _active_follow(axis: str, _motion_t: float, *, interval_sec: float, amplitude: float) -> float:
            mapping = {
                "yaw_follow": 0.55,
                "pitch_follow": -0.45,
                "roll_follow": 0.28,
                "shoulder_follow": 0.24,
            }
            return mapping[axis] * amplitude

        with patch.object(driver, "_idle_random_follow_value", side_effect=_baseline_follow):
            baseline_targets = driver._build_idle_targets(snapshot, base_targets=base_targets, motion_t=3.6)
        with patch.object(driver, "_idle_random_follow_value", side_effect=_active_follow):
            follow_targets = driver._build_idle_targets(snapshot, base_targets=base_targets, motion_t=3.6)

        self.assertGreater(follow_targets["head.yaw"], baseline_targets["head.yaw"])
        self.assertGreater(follow_targets["body.yaw"], baseline_targets["body.yaw"])
        self.assertLess(follow_targets["head.pitch"], baseline_targets["head.pitch"])
        self.assertLess(follow_targets["body.pitch"], baseline_targets["body.pitch"])
        self.assertGreater(
            follow_targets["head.yaw"] - baseline_targets["head.yaw"],
            follow_targets["body.yaw"] - baseline_targets["body.yaw"],
        )
        self.assertGreater(
            abs(follow_targets["head.pitch"] - baseline_targets["head.pitch"]),
            abs(follow_targets["body.pitch"] - baseline_targets["body.pitch"]),
        )

    async def test_idle_motion_limits_frame_to_frame_speed(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=self._speech_motion_profile(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        snapshot = EmbodiedStateSnapshot(
            session_id="session-idle-smooth",
            ts=1.0,
            seq=1,
            agent_state="wait",
            valence=0.08,
            arousal=0.06,
            attention=0.26,
            cognitive_load=0.08,
            confidence=0.24,
            social_approach=0.05,
            energy=0.20,
            gaze_x=0.0,
            gaze_y=0.0,
        )
        driver._latest_snapshot = snapshot
        driver._received_snapshot = True
        driver._latest_targets = {
            "head.yaw": 0.92,
            "head.pitch": -0.74,
            "head.roll": 0.42,
            "body.yaw": 0.68,
            "body.pitch": -0.46,
            "body.roll": 0.26,
            "body.shoulder": 0.24,
            "eye.gaze.x": 0.28,
            "eye.gaze.y": -0.18,
            "breath": 0.26,
        }
        driver._playback_anchor_targets = dict(driver._latest_targets)

        await controller.start()
        try:
            await asyncio.sleep(0.05)
        finally:
            await driver.stop()
            await controller.stop()

        timeline_events = [event for event in bridge.events if event.get("type") == "live2d.timeline.frame" and event.get("purpose") == "idle"]

        def _parameter_track(parameter_id: str) -> list[float]:
            values: list[float] = []
            for event in timeline_events:
                for parameter in list(event.get("parameters") or []):
                    if isinstance(parameter, dict) and str(parameter.get("id") or "") == parameter_id:
                        values.append(float(parameter.get("value") or 0.0))
                        break
            return values

        def _max_step(parameter_id: str) -> float:
            track = _parameter_track(parameter_id)
            if len(track) < 2:
                return 0.0
            return max(abs(current - previous) for previous, current in zip(track, track[1:]))

        self.assertLess(_max_step("ParamAngleX"), 3.0)
        self.assertLess(_max_step("ParamAngleY"), 2.2)
        self.assertLess(_max_step("ParamBodyAngleX"), 1.1)
        self.assertLess(_max_step("ParamBodyAngleY"), 0.85)

    def test_idle_frame_step_limit_keeps_transition_smooth(self) -> None:
        driver = self._make_hiyori_face_driver()
        previous_targets = {
            "head.yaw": 0.92,
            "head.pitch": -0.74,
            "head.roll": 0.42,
            "body.yaw": 0.68,
            "body.pitch": -0.46,
            "body.roll": 0.26,
            "body.shoulder": 0.24,
            "eye.gaze.x": 0.28,
            "eye.gaze.y": -0.18,
        }
        desired_targets = {
            "head.yaw": 0.48,
            "head.pitch": 0.24,
            "head.roll": 0.08,
            "body.yaw": 0.26,
            "body.pitch": 0.12,
            "body.roll": 0.05,
            "body.shoulder": 0.03,
            "eye.gaze.x": 0.12,
            "eye.gaze.y": 0.02,
        }

        limited = driver._limit_idle_frame_step(
            previous_targets=previous_targets,
            current_targets=desired_targets,
        )

        self.assertLess(abs(limited["head.yaw"] - previous_targets["head.yaw"]), 0.08)
        self.assertLess(abs(limited["head.pitch"] - previous_targets["head.pitch"]), 0.08)
        self.assertLess(abs(limited["body.yaw"] - previous_targets["body.yaw"]), 0.05)
        self.assertLess(abs(limited["body.pitch"] - previous_targets["body.pitch"]), 0.05)

    def test_reply_emotion_happy_and_shy_shift_face_positive_in_different_ways(self) -> None:
        driver = self._make_hiyori_face_driver()
        snapshot = self._reply_emotion_snapshot()

        baseline = self._targets_with_reply_emotion(driver, snapshot, now=20.0)
        happy = self._targets_with_reply_emotion(
            driver,
            snapshot,
            now=21.0,
            emotion_intent="react_happy",
        )
        shy = self._targets_with_reply_emotion(
            driver,
            snapshot,
            now=22.0,
            emotion_intent="react_shy",
        )

        self.assertGreater(happy["eye.left.smile"], baseline["eye.left.smile"] + 0.10)
        self.assertGreater(happy["face.blush"], baseline["face.blush"] + 0.10)
        self.assertGreater(happy["brow.left.angle"], baseline["brow.left.angle"] + 0.07)
        self.assertGreater(happy["brow.left.form"], baseline["brow.left.form"] + 0.05)
        self.assertGreater(shy["face.blush"], baseline["face.blush"] + 0.06)
        self.assertGreater(shy["brow.left.angle"], baseline["brow.left.angle"] + 0.08)
        self.assertGreater(shy["brow.left.form"], baseline["brow.left.form"] + 0.05)
        self.assertLess(shy["brow.left.angle"], happy["brow.left.angle"])
        self.assertLess(shy["brow.left.form"], happy["brow.left.form"])

    def test_reply_emotion_sad_and_angry_shift_face_negative_with_angry_stronger(self) -> None:
        driver = self._make_hiyori_face_driver()
        snapshot = self._reply_emotion_snapshot()

        baseline = self._targets_with_reply_emotion(driver, snapshot, now=30.0)
        sad = self._targets_with_reply_emotion(
            driver,
            snapshot,
            now=31.0,
            emotion_intent="react_sad",
        )
        angry = self._targets_with_reply_emotion(
            driver,
            snapshot,
            now=32.0,
            emotion_intent="react_angry",
        )

        self.assertLess(sad["brow.left.y"], baseline["brow.left.y"] - 0.12)
        self.assertLess(sad["brow.left.angle"], baseline["brow.left.angle"] - 0.16)
        self.assertLess(sad["brow.left.form"], baseline["brow.left.form"] - 0.14)
        self.assertLess(sad["eye.left.smile"], baseline["eye.left.smile"] - 0.08)
        self.assertLess(angry["brow.left.y"], sad["brow.left.y"] - 0.015)
        self.assertLessEqual(angry["brow.left.angle"], sad["brow.left.angle"])
        self.assertLessEqual(angry["brow.left.form"], sad["brow.left.form"])
        self.assertLess(angry["eye.open"], baseline["eye.open"] - 0.03)

    def test_reply_emotion_surprised_and_confused_have_distinct_high_arousal_face_directions(self) -> None:
        driver = self._make_hiyori_face_driver()
        snapshot = self._reply_emotion_snapshot()

        baseline = self._targets_with_reply_emotion(driver, snapshot, now=40.0)
        happy = self._targets_with_reply_emotion(
            driver,
            snapshot,
            now=41.0,
            emotion_intent="react_happy",
        )
        surprised = self._targets_with_reply_emotion(
            driver,
            snapshot,
            now=42.0,
            emotion_intent="react_surprised",
        )
        confused = self._targets_with_reply_emotion(
            driver,
            snapshot,
            now=43.0,
            emotion_intent="react_confused",
        )

        self.assertGreater(surprised["brow.left.y"], baseline["brow.left.y"] + 0.03)
        self.assertGreater(surprised["brow.left.angle"], baseline["brow.left.angle"] + 0.02)
        self.assertGreater(surprised["face.blush"], baseline["face.blush"] + 0.05)
        self.assertLess(surprised["eye.left.smile"], happy["eye.left.smile"] - 0.10)
        self.assertLess(confused["brow.left.y"], baseline["brow.left.y"] - 0.05)
        self.assertLess(confused["brow.left.angle"], baseline["brow.left.angle"] - 0.02)
        self.assertLess(confused["brow.left.form"], baseline["brow.left.form"] - 0.015)
        self.assertLess(confused["eye.left.smile"], baseline["eye.left.smile"] - 0.08)

    def test_hiyori_happy_face_uses_clearer_brow_shape_than_idle(self) -> None:
        profile = ParameterProfile(
            model_id="hiyori_pro_zh",
            model_name="Hiyori",
            parameters=[
                ParameterSpec("ParamAngleX", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleY", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleZ", -30.0, 30.0, 0.0),
                ParameterSpec("ParamBodyAngleX", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleY", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleZ", -10.0, 10.0, 0.0),
                ParameterSpec("ParamEyeBallX", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeBallY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeLOpen"),
                ParameterSpec("ParamEyeROpen"),
                ParameterSpec("ParamEyeLSmile"),
                ParameterSpec("ParamEyeRSmile"),
                ParameterSpec("ParamBrowLY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowLAngle", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRAngle", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowLForm", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRForm", -1.0, 1.0, 0.0),
                ParameterSpec("ParamCheek", 0.0, 1.0, 0.0),
                ParameterSpec("ParamBreath", 0.0, 1.0, 0.0),
                ParameterSpec("ParamShoulder", -1.0, 1.0, 0.0),
                ParameterSpec("ParamHairAhoge", -10.0, 10.0, 0.0),
            ],
        )
        controller = Live2DController(
            bridge=InMemoryLive2DBridge(),
            profile=profile,
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        idle_snapshot = EmbodiedStateSnapshot(
            session_id="session-hiyori-idle-brow",
            ts=1.0,
            seq=1,
            agent_state="wait",
            valence=0.08,
            arousal=0.03,
            attention=0.08,
            cognitive_load=0.02,
            confidence=0.05,
            social_approach=0.04,
            energy=0.05,
            gaze_x=0.0,
            gaze_y=0.0,
        )
        happy_snapshot = EmbodiedStateSnapshot(
            session_id="session-hiyori-happy-brow",
            ts=1.0,
            seq=2,
            agent_state="running",
            valence=0.65,
            arousal=0.45,
            attention=0.72,
            cognitive_load=0.10,
            confidence=0.55,
            social_approach=0.35,
            energy=0.55,
            gaze_x=0.0,
            gaze_y=0.0,
        )

        idle_targets = driver._build_semantic_targets(idle_snapshot, now=20.0)
        happy_targets = driver._build_semantic_targets(happy_snapshot, now=20.0)

        self.assertGreater(happy_targets["brow.left.y"], idle_targets["brow.left.y"] + 0.12)
        self.assertGreater(happy_targets["brow.left.angle"], idle_targets["brow.left.angle"] + 0.10)
        self.assertGreater(happy_targets["brow.left.form"], idle_targets["brow.left.form"] + 0.06)

    def test_hiyori_surprised_face_lifts_brows_more_than_idle(self) -> None:
        profile = ParameterProfile(
            model_id="hiyori_pro_zh",
            model_name="Hiyori",
            parameters=[
                ParameterSpec("ParamAngleX", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleY", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleZ", -30.0, 30.0, 0.0),
                ParameterSpec("ParamBodyAngleX", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleY", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleZ", -10.0, 10.0, 0.0),
                ParameterSpec("ParamEyeBallX", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeBallY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeLOpen"),
                ParameterSpec("ParamEyeROpen"),
                ParameterSpec("ParamEyeLSmile"),
                ParameterSpec("ParamEyeRSmile"),
                ParameterSpec("ParamBrowLY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowLAngle", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRAngle", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowLForm", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRForm", -1.0, 1.0, 0.0),
                ParameterSpec("ParamCheek", 0.0, 1.0, 0.0),
                ParameterSpec("ParamBreath", 0.0, 1.0, 0.0),
                ParameterSpec("ParamShoulder", -1.0, 1.0, 0.0),
                ParameterSpec("ParamHairAhoge", -10.0, 10.0, 0.0),
            ],
        )
        controller = Live2DController(
            bridge=InMemoryLive2DBridge(),
            profile=profile,
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )
        idle_snapshot = EmbodiedStateSnapshot(
            session_id="session-hiyori-idle-surprise",
            ts=1.0,
            seq=1,
            agent_state="wait",
            valence=0.08,
            arousal=0.03,
            attention=0.08,
            cognitive_load=0.02,
            confidence=0.05,
            social_approach=0.04,
            energy=0.05,
            gaze_x=0.0,
            gaze_y=0.0,
        )
        surprised_snapshot = EmbodiedStateSnapshot(
            session_id="session-hiyori-surprised-brow",
            ts=1.0,
            seq=2,
            agent_state="running",
            valence=0.25,
            arousal=0.85,
            attention=0.86,
            cognitive_load=0.10,
            confidence=0.30,
            social_approach=0.10,
            energy=0.78,
            gaze_x=0.0,
            gaze_y=0.0,
        )

        idle_targets = driver._build_semantic_targets(idle_snapshot, now=20.0)
        surprised_targets = driver._build_semantic_targets(surprised_snapshot, now=20.0)

        self.assertGreater(surprised_targets["brow.left.y"], idle_targets["brow.left.y"] + 0.08)
        self.assertGreater(surprised_targets["brow.left.angle"], idle_targets["brow.left.angle"] + 0.05)
        self.assertGreater(surprised_targets["brow.left.form"], idle_targets["brow.left.form"] + 0.03)

    def test_embodied_config_defaults_to_safe_off(self) -> None:
        settings = LiveAdapterSettings()

        self.assertFalse(settings.live2d.embodied.enabled)
        self.assertEqual(settings.live2d.embodied.source_url, "ws://127.0.0.1:8001/api/webui/ws")
        self.assertEqual(settings.live2d.embodied.source_token, "")
        self.assertEqual(settings.live2d.embodied.source_session_id, "")
        self.assertTrue(settings.live2d.embodied.fallback_to_legacy)

    async def test_driver_maps_snapshot_without_touching_mouth_parameters(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        await controller.start()
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )

        result = await driver.handle_snapshot(
            EmbodiedStateSnapshot(
                session_id="session-1",
                ts=1.0,
                seq=1,
                agent_state="running",
                valence=0.5,
                arousal=0.3,
                attention=0.8,
                cognitive_load=0.4,
                confidence=0.6,
                social_approach=0.2,
                energy=0.7,
                gaze_x=0.25,
                gaze_y=-0.15,
            )
        )
        await controller.stop()

        self.assertTrue(result["success"])
        sent_parameters = result["parameters"]
        self.assertTrue(sent_parameters)
        parameter_ids = {item["id"] for item in sent_parameters}
        self.assertIn("ParamEyeBallX", parameter_ids)
        self.assertIn("ParamAngleX", parameter_ids)
        self.assertIn("ParamBodyAngleX", parameter_ids)
        self.assertIn("ParamBodyAngleY", parameter_ids)
        self.assertIn("ParamBodyAngleZ", parameter_ids)
        self.assertIn("ParamBrowLY", parameter_ids)
        self.assertIn("ParamBreath", parameter_ids)
        self.assertNotIn("ParamMouthOpenY", parameter_ids)
        self.assertNotIn("ParamMouthForm", parameter_ids)
        timeline_events = [event for event in bridge.events if event.get("type") == "live2d.timeline.frame"]
        self.assertGreaterEqual(len(timeline_events), 3)
        self.assertFalse(any(event.get("type") == "live2d.parameters" for event in bridge.events))
        timeline_ids = {
            str(event.get("timeline_id") or "")
            for event in timeline_events
            if str(event.get("purpose") or "") != "idle-mouth"
        }
        self.assertEqual(len(timeline_ids), 1)
        self.assertTrue(all(int(event.get("offset_ms") or 0) >= 0 for event in timeline_events))
        bridged_ids = {
            str(parameter.get("id") or "")
            for event in timeline_events
            if str(event.get("purpose") or "") != "idle-mouth"
            for parameter in list(event.get("parameters") or [])
            if isinstance(parameter, dict)
        }
        self.assertIn("ParamBodyAngleY", bridged_ids)
        self.assertIn("ParamBodyAngleZ", bridged_ids)
        self.assertNotIn("ParamMouthOpenY", bridged_ids)
        self.assertNotIn("ParamMouthForm", bridged_ids)

    async def test_controller_embodied_mode_does_not_queue_legacy_live2d_reset_events_during_reply(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
            prepare_ms=0,
            release_ms=120,
        )

        await controller.start()
        bridge.events.clear()
        await controller.play_reply(
            "测试切换抖动",
            audio_timeline={
                "audio_duration_ms": 240,
                "amplitudes": [
                    {"offset_ms": 0, "value": 0.0},
                    {"offset_ms": 80, "value": 0.85},
                    {"offset_ms": 160, "value": 0.65},
                ],
            },
        )
        await asyncio.sleep(0.45)
        await controller.stop()

        live2d_parameter_events = [event for event in bridge.events if event.get("type") == "live2d.parameters"]
        self.assertEqual(live2d_parameter_events, [])
        self.assertTrue(any(event.get("type") == "bot_reply.start" for event in bridge.events))
        self.assertTrue(
            any(
                event.get("type") == "live2d.timeline.frame" and str(event.get("purpose") or "") == "lipsync"
                for event in bridge.events
            )
        )

    async def test_driver_uses_hiyori_motion_reference_channels_when_available(self) -> None:
        bridge = InMemoryLive2DBridge()
        profile = ParameterProfile(
            model_id="hiyori_pro_zh",
            model_name="Hiyori",
            parameters=[
                ParameterSpec("ParamAngleX", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleY", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleZ", -30.0, 30.0, 0.0),
                ParameterSpec("ParamBodyAngleX", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleY", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleZ", -10.0, 10.0, 0.0),
                ParameterSpec("ParamEyeBallX", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeBallY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeLOpen"),
                ParameterSpec("ParamEyeROpen"),
                ParameterSpec("ParamEyeLSmile"),
                ParameterSpec("ParamEyeRSmile"),
                ParameterSpec("ParamBrowLY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBreath", 0.0, 1.0, 0.0),
                ParameterSpec("ParamCheek"),
                ParameterSpec("ParamShoulder"),
                ParameterSpec("ParamHairAhoge"),
            ],
        )
        controller = Live2DController(
            bridge=bridge,
            profile=profile,
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        await controller.start()
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )

        with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=1.25):
            result = await driver.handle_snapshot(
                EmbodiedStateSnapshot(
                    session_id="session-hiyori",
                    ts=1.0,
                    seq=1,
                    agent_state="running",
                    valence=0.8,
                    arousal=0.6,
                    attention=0.85,
                    cognitive_load=0.15,
                    confidence=0.65,
                    social_approach=0.35,
                    energy=0.75,
                    gaze_x=0.2,
                    gaze_y=-0.1,
                )
            )
        await controller.stop()

        self.assertTrue(result["success"])
        parameter_ids = {str(item["id"]) for item in result["parameters"]}
        self.assertIn("ParamCheek", parameter_ids)
        self.assertIn("ParamBodyAngleY", parameter_ids)
        self.assertIn("ParamBodyAngleZ", parameter_ids)
        self.assertIn("ParamShoulder", parameter_ids)
        self.assertIn("ParamEyeLSmile", parameter_ids)
        self.assertIn("ParamEyeRSmile", parameter_ids)
        self.assertIn("ParamHairAhoge", parameter_ids)
        self.assertNotIn("ParamMouthOpenY", parameter_ids)
        self.assertNotIn("ParamMouthForm", parameter_ids)

    async def test_driver_pushes_more_visible_head_and_body_motion_for_engaged_snapshot(self) -> None:
        bridge = InMemoryLive2DBridge()
        profile = ParameterProfile(
            model_id="hiyori_pro_zh",
            model_name="Hiyori",
            parameters=[
                ParameterSpec("ParamAngleX", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleY", -30.0, 30.0, 0.0),
                ParameterSpec("ParamAngleZ", -30.0, 30.0, 0.0),
                ParameterSpec("ParamBodyAngleX", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleY", -10.0, 10.0, 0.0),
                ParameterSpec("ParamBodyAngleZ", -10.0, 10.0, 0.0),
                ParameterSpec("ParamEyeBallX", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeBallY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamEyeLOpen"),
                ParameterSpec("ParamEyeROpen"),
                ParameterSpec("ParamEyeLSmile"),
                ParameterSpec("ParamEyeRSmile"),
                ParameterSpec("ParamBrowLY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBrowRY", -1.0, 1.0, 0.0),
                ParameterSpec("ParamBreath", 0.0, 1.0, 0.0),
                ParameterSpec("ParamCheek"),
                ParameterSpec("ParamShoulder"),
                ParameterSpec("ParamHairAhoge"),
            ],
        )
        controller = Live2DController(
            bridge=bridge,
            profile=profile,
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        await controller.start()
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )

        with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=0.0):
            result = await driver.handle_snapshot(
                EmbodiedStateSnapshot(
                    session_id="session-visible",
                    ts=1.0,
                    seq=1,
                    agent_state="running",
                    valence=0.8,
                    arousal=0.75,
                    attention=0.92,
                    cognitive_load=0.08,
                    confidence=0.72,
                    social_approach=0.55,
                    energy=0.85,
                    gaze_x=0.5,
                    gaze_y=-0.18,
                )
            )
        await controller.stop()

        self.assertTrue(result["success"])
        parameters_by_id = {
            str(item["id"]): float(item["value"])
            for item in result["parameters"]
        }
        self.assertGreater(abs(parameters_by_id["ParamAngleX"]), 28.0)
        self.assertGreater(abs(parameters_by_id["ParamBodyAngleX"]), 9.0)
        self.assertGreater(abs(parameters_by_id["ParamBodyAngleY"]), 9.0)
        self.assertGreater(abs(parameters_by_id["ParamBodyAngleZ"]), 8.6)
        self.assertGreater(abs(parameters_by_id["ParamShoulder"]), 0.95)

    async def test_driver_continues_idle_motion_in_background(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        await controller.start()
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )

        await driver.start()
        await driver.handle_snapshot(
            EmbodiedStateSnapshot(
                session_id="session-idle",
                ts=1.0,
                seq=1,
                agent_state="wait",
                valence=0.2,
                arousal=0.1,
                attention=0.35,
                cognitive_load=0.1,
                confidence=0.25,
                social_approach=0.05,
                energy=0.2,
                gaze_x=0.0,
                gaze_y=0.0,
            )
        )
        await asyncio.sleep(1.05)
        await driver.stop()
        await controller.stop()

        timeline_events = [event for event in bridge.events if event.get("type") == "live2d.timeline.frame"]
        timeline_ids = [str(event.get("timeline_id") or "") for event in timeline_events]
        purposes = {str(event.get("purpose") or "") for event in timeline_events}
        self.assertGreater(len(set(timeline_ids)), 1)
        self.assertIn("expressive", purposes)
        self.assertIn("idle", purposes)

    async def test_idle_motion_keeps_head_loop_continuous(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        await controller.start()
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )

        await driver.start()
        await driver.handle_snapshot(
            EmbodiedStateSnapshot(
                session_id="session-loop",
                ts=1.0,
                seq=1,
                agent_state="wait",
                valence=0.1,
                arousal=0.1,
                attention=0.3,
                cognitive_load=0.1,
                confidence=0.2,
                social_approach=0.05,
                energy=0.2,
                gaze_x=0.0,
                gaze_y=0.0,
            )
        )
        await asyncio.sleep(1.35)
        await driver.stop()
        await controller.stop()

        timeline_events = [event for event in bridge.events if event.get("type") == "live2d.timeline.frame"]
        idle_timelines: dict[str, list[dict[str, object]]] = {}
        for event in timeline_events:
            if str(event.get("purpose") or "") != "idle":
                continue
            idle_timelines.setdefault(str(event.get("timeline_id") or ""), []).append(event)
        idle_end_frames = []
        for frames in idle_timelines.values():
            last_frame = max(frames, key=lambda item: int(item.get("offset_ms") or 0))
            params = {
                str(parameter.get("id") or ""): float(parameter.get("value") or 0.0)
                for parameter in list(last_frame.get("parameters") or [])
                if isinstance(parameter, dict)
            }
            idle_end_frames.append(params)
        self.assertGreaterEqual(len(idle_end_frames), 2)
        for previous, current in zip(idle_end_frames, idle_end_frames[1:]):
            self.assertLess(abs(current.get("ParamAngleX", 0.0) - previous.get("ParamAngleX", 0.0)), 6.5)

    async def test_idle_clip_starts_from_previous_clip_endpoint(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        await controller.start()
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )

        await driver.start()
        await driver.handle_snapshot(
            EmbodiedStateSnapshot(
                session_id="session-anchor",
                ts=1.0,
                seq=1,
                agent_state="wait",
                valence=0.1,
                arousal=0.1,
                attention=0.3,
                cognitive_load=0.1,
                confidence=0.2,
                social_approach=0.05,
                energy=0.2,
                gaze_x=0.0,
                gaze_y=0.0,
            )
        )
        await asyncio.sleep(1.35)
        await driver.stop()
        await controller.stop()

        timeline_events = [event for event in bridge.events if event.get("type") == "live2d.timeline.frame"]
        idle_timelines: dict[str, list[dict[str, object]]] = {}
        for event in timeline_events:
            if str(event.get("purpose") or "") != "idle":
                continue
            idle_timelines.setdefault(str(event.get("timeline_id") or ""), []).append(event)
        ordered_timelines = list(idle_timelines.values())
        self.assertGreaterEqual(len(ordered_timelines), 2)
        previous_last = max(ordered_timelines[0], key=lambda item: int(item.get("offset_ms") or 0))
        current_first = min(ordered_timelines[1], key=lambda item: int(item.get("offset_ms") or 0))
        previous_params = {
            str(parameter.get("id") or ""): float(parameter.get("value") or 0.0)
            for parameter in list(previous_last.get("parameters") or [])
            if isinstance(parameter, dict)
        }
        current_params = {
            str(parameter.get("id") or ""): float(parameter.get("value") or 0.0)
            for parameter in list(current_first.get("parameters") or [])
            if isinstance(parameter, dict)
        }
        self.assertAlmostEqual(current_params.get("ParamAngleX", 0.0), previous_params.get("ParamAngleX", 0.0), places=6)
        self.assertAlmostEqual(current_params.get("ParamBodyAngleX", 0.0), previous_params.get("ParamBodyAngleX", 0.0), places=6)

    async def test_driver_falls_back_when_no_valid_parameters_exist(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile(parameters=[]),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        await controller.start()
        fallback_events: list[str] = []
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=True,
            on_disable=lambda reason: fallback_events.append(reason),
        )

        result = await driver.handle_snapshot(
            EmbodiedStateSnapshot(
                session_id="session-2",
                ts=2.0,
                seq=1,
                agent_state="running",
                valence=0.0,
                arousal=0.0,
                attention=0.2,
                cognitive_load=0.3,
                confidence=0.1,
                social_approach=0.0,
                energy=0.2,
                gaze_x=0.0,
                gaze_y=0.0,
            )
        )
        await controller.stop()

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "no_valid_parameters")
        self.assertEqual(fallback_events, ["no_valid_parameters"])

    async def test_driver_reset_restores_parameter_defaults(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        await controller.start()
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )

        with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.time.monotonic", return_value=0.0):
            await driver.handle_snapshot(
                EmbodiedStateSnapshot(
                    session_id="session-3",
                    ts=3.0,
                    seq=1,
                    agent_state="running",
                    valence=0.8,
                    arousal=0.6,
                    attention=0.9,
                    cognitive_load=0.4,
                    confidence=0.6,
                    social_approach=0.3,
                    energy=0.7,
                    gaze_x=0.35,
                    gaze_y=-0.2,
                )
            )
            result = await driver.handle_reset()
        await controller.stop()

        self.assertTrue(result["success"])
        parameters_by_id = {str(item["id"]): float(item["value"]) for item in result["parameters"]}
        self.assertEqual(parameters_by_id["ParamAngleX"], 0.0)
        self.assertEqual(parameters_by_id["ParamAngleY"], 0.0)
        self.assertEqual(parameters_by_id["ParamEyeBallX"], 0.0)
        self.assertEqual(parameters_by_id["ParamBreath"], 0.0)

    async def test_driver_does_not_fallback_before_first_snapshot(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        await controller.start()
        fallback_events: list[str] = []
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=True,
            on_disable=lambda reason: fallback_events.append(reason),
            stale_after_sec=0.25,
        )

        await driver.start()
        await asyncio.sleep(0.45)
        await driver.stop()
        await controller.stop()

        self.assertTrue(driver.active)
        self.assertEqual(fallback_events, [])

    async def test_driver_keeps_idle_motion_after_snapshot_stream_goes_quiet(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        await controller.start()
        fallback_events: list[str] = []
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=True,
            on_disable=lambda reason: fallback_events.append(reason),
            stale_after_sec=0.25,
        )

        await driver.start()
        await driver.handle_snapshot(
            EmbodiedStateSnapshot(
                session_id="session-quiet",
                ts=1.0,
                seq=1,
                agent_state="wait",
                valence=0.1,
                arousal=0.1,
                attention=0.35,
                cognitive_load=0.1,
                confidence=0.25,
                social_approach=0.05,
                energy=0.2,
                gaze_x=0.0,
                gaze_y=0.0,
            )
        )
        await asyncio.sleep(0.55)
        await driver.stop()
        await controller.stop()

        idle_events = [
            event
            for event in bridge.events
            if event.get("type") == "live2d.timeline.frame" and str(event.get("purpose") or "") == "idle"
        ]
        self.assertTrue(driver.active)
        self.assertEqual(fallback_events, [])
        self.assertGreaterEqual(len(idle_events), 2)

    async def test_runtime_keeps_legacy_mode_until_subscriber_connects(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        await controller.start()
        runtime = EmbodiedLive2DRuntime(
            controller=controller,
            source_url="ws://127.0.0.1:8001/api/webui/ws",
            source_token="maibot-avatar-state-local",
            source_session_id="session-runtime",
            fallback_to_legacy=True,
            logger=_Logger(),
        )

        async def fake_subscriber_start() -> None:
            return None

        async def fake_subscriber_stop() -> None:
            return None

        runtime.subscriber.start = fake_subscriber_start  # type: ignore[method-assign]
        runtime.subscriber.stop = fake_subscriber_stop  # type: ignore[method-assign]

        await runtime.start()
        await runtime.stop()
        await controller.stop()

        self.assertFalse(controller.embodied_mode_enabled)

    async def test_runtime_keeps_embodied_mode_after_subscriber_disconnect(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        await controller.start()
        runtime = EmbodiedLive2DRuntime(
            controller=controller,
            source_url="ws://127.0.0.1:8001/api/webui/ws",
            source_token="maibot-avatar-state-local",
            source_session_id="session-runtime",
            fallback_to_legacy=True,
            logger=_Logger(),
        )

        async def fake_subscriber_start() -> None:
            return None

        async def fake_subscriber_stop() -> None:
            return None

        runtime.subscriber.start = fake_subscriber_start  # type: ignore[method-assign]
        runtime.subscriber.stop = fake_subscriber_stop  # type: ignore[method-assign]

        await runtime.start()
        await runtime._handle_subscriber_connected()
        await runtime._handle_subscriber_disconnected("socket closed")
        self.assertTrue(controller.embodied_mode_enabled)
        await runtime.stop()
        await controller.stop()

    async def test_runtime_request_wink_reaches_driver(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=self._hiyori_face_profile(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        await controller.start()
        runtime = EmbodiedLive2DRuntime(
            controller=controller,
            source_url="ws://127.0.0.1:8001/api/webui/ws",
            source_token="maibot-avatar-state-local",
            source_session_id="session-runtime",
            fallback_to_legacy=True,
            wink_enabled=True,
            logger=_Logger(),
        )

        async def fake_subscriber_start() -> None:
            return None

        async def fake_subscriber_stop() -> None:
            return None

        runtime.subscriber.start = fake_subscriber_start  # type: ignore[method-assign]
        runtime.subscriber.stop = fake_subscriber_stop  # type: ignore[method-assign]

        try:
            await runtime.start()
            accepted = await runtime.request_wink(side="right")
            self.assertTrue(accepted)
            self.assertTrue(runtime.driver._wink_active)
            self.assertEqual(runtime.driver._wink_side, "right")
        finally:
            await runtime.stop()
            await controller.stop()

    async def test_runtime_request_special_move_reaches_driver(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=self._hiyori_face_profile(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        await controller.start()
        runtime = EmbodiedLive2DRuntime(
            controller=controller,
            source_url="ws://127.0.0.1:8001/api/webui/ws",
            source_token="maibot-avatar-state-local",
            source_session_id="session-runtime",
            fallback_to_legacy=True,
            logger=_Logger(),
        )

        async def fake_subscriber_start() -> None:
            return None

        async def fake_subscriber_stop() -> None:
            return None

        runtime.subscriber.start = fake_subscriber_start  # type: ignore[method-assign]
        runtime.subscriber.stop = fake_subscriber_stop  # type: ignore[method-assign]

        try:
            await runtime.start()
            accepted = await runtime.request_special_move(
                action="Special_move",
                move="ahoge_spin",
                duration_sec=10.0,
            )
            self.assertTrue(accepted)
            self.assertTrue(runtime.driver._special_move_active)
            self.assertEqual(runtime.driver._special_move_name, "ahoge_spin")
        finally:
            await runtime.stop()
            await controller.stop()

    async def test_runtime_starts_and_stops_global_mouse_follow_runtime(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        await controller.start()
        runtime = EmbodiedLive2DRuntime(
            controller=controller,
            source_url="ws://127.0.0.1:8001/api/webui/ws",
            source_token="maibot-avatar-state-local",
            source_session_id="session-runtime",
            fallback_to_legacy=True,
            mouse_follow_enabled=True,
            mouse_follow_poll_interval_ms=33,
            mouse_follow_return_after_sec=1.2,
            mouse_follow_cooldown_sec=0.45,
            mouse_follow_eye_gain=0.55,
            mouse_follow_head_gain=0.28,
            mouse_follow_body_gain=0.12,
            logger=_Logger(),
        )

        class _MouseRuntimeStub:
            def __init__(self) -> None:
                self.started = 0
                self.stopped = 0
                self.latest = MouseFollowSnapshot(x_norm=0.25, y_norm=-0.5, active=True, activity_ts=9.0)

            async def start(self) -> None:
                self.started += 1

            async def stop(self) -> None:
                self.stopped += 1

            def latest_snapshot(self) -> MouseFollowSnapshot:
                return self.latest

        mouse_runtime = _MouseRuntimeStub()
        runtime.mouse_follow_runtime = mouse_runtime  # type: ignore[assignment]

        async def fake_subscriber_start() -> None:
            return None

        async def fake_subscriber_stop() -> None:
            return None

        runtime.subscriber.start = fake_subscriber_start  # type: ignore[method-assign]
        runtime.subscriber.stop = fake_subscriber_stop  # type: ignore[method-assign]

        await runtime.start()
        self.assertEqual(mouse_runtime.started, 1)
        self.assertFalse(controller.embodied_mode_enabled)

        runtime.set_mouse_follow_enabled(False)
        self.assertFalse(runtime.driver.mouse_follow_enabled)
        runtime.set_mouse_follow_enabled(True)
        self.assertTrue(runtime.driver.mouse_follow_enabled)
        self.assertEqual(runtime.driver.debug_latest_targets(), {})

        await runtime._handle_subscriber_connected()
        self.assertTrue(controller.embodied_mode_enabled)
        self.assertEqual(runtime.driver._latest_mouse_snapshot, mouse_runtime.latest)

        await runtime.stop()
        await controller.stop()

        self.assertEqual(mouse_runtime.stopped, 1)
        self.assertFalse(controller.embodied_mode_enabled)

    async def test_runtime_skips_mouse_follow_runtime_when_disabled_at_start(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        await controller.start()
        runtime = EmbodiedLive2DRuntime(
            controller=controller,
            source_url="ws://127.0.0.1:8001/api/webui/ws",
            source_token="maibot-avatar-state-local",
            source_session_id="session-runtime",
            fallback_to_legacy=True,
            mouse_follow_enabled=False,
            mouse_follow_poll_interval_ms=33,
            logger=_Logger(),
        )

        class _MouseRuntimeStub:
            def __init__(self) -> None:
                self.started = 0
                self.stopped = 0

            async def start(self) -> None:
                self.started += 1

            async def stop(self) -> None:
                self.stopped += 1

            def latest_snapshot(self) -> MouseFollowSnapshot:
                return MouseFollowSnapshot()

        mouse_runtime = _MouseRuntimeStub()
        runtime.mouse_follow_runtime = mouse_runtime  # type: ignore[assignment]

        async def fake_subscriber_start() -> None:
            return None

        async def fake_subscriber_stop() -> None:
            return None

        runtime.subscriber.start = fake_subscriber_start  # type: ignore[method-assign]
        runtime.subscriber.stop = fake_subscriber_stop  # type: ignore[method-assign]

        await runtime.start()
        self.assertEqual(mouse_runtime.started, 0)
        self.assertIsNone(runtime._mouse_follow_task)

        await runtime.stop()
        await controller.stop()

        self.assertEqual(mouse_runtime.stopped, 1)
        self.assertFalse(controller.embodied_mode_enabled)

    async def test_runtime_can_toggle_mouse_follow_without_restart(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        await controller.start()
        runtime = EmbodiedLive2DRuntime(
            controller=controller,
            source_url="ws://127.0.0.1:8001/api/webui/ws",
            source_token="maibot-avatar-state-local",
            source_session_id="session-runtime",
            fallback_to_legacy=True,
            mouse_follow_enabled=False,
            mouse_follow_poll_interval_ms=33,
            logger=_Logger(),
        )

        class _MouseRuntimeStub:
            def __init__(self) -> None:
                self.started = 0
                self.stopped = 0

            async def start(self) -> None:
                self.started += 1

            async def stop(self) -> None:
                self.stopped += 1

            def latest_snapshot(self) -> MouseFollowSnapshot:
                return MouseFollowSnapshot(x_norm=0.2, y_norm=-0.3, active=True, activity_ts=1.0)

        mouse_runtime = _MouseRuntimeStub()
        runtime.mouse_follow_runtime = mouse_runtime  # type: ignore[assignment]

        async def fake_subscriber_start() -> None:
            return None

        async def fake_subscriber_stop() -> None:
            return None

        runtime.subscriber.start = fake_subscriber_start  # type: ignore[method-assign]
        runtime.subscriber.stop = fake_subscriber_stop  # type: ignore[method-assign]

        await runtime.start()
        self.assertEqual(mouse_runtime.started, 0)
        self.assertIsNone(runtime._mouse_follow_task)

        runtime.set_mouse_follow_enabled(True)
        await asyncio.sleep(0.08)
        self.assertEqual(mouse_runtime.started, 1)
        self.assertIsNotNone(runtime._mouse_follow_task)
        self.assertFalse(runtime._mouse_follow_task.done())

        runtime.set_mouse_follow_enabled(False)
        await asyncio.sleep(0.08)
        self.assertEqual(mouse_runtime.stopped, 1)
        self.assertIsNone(runtime._mouse_follow_task)

        runtime.set_mouse_follow_enabled(True)
        await asyncio.sleep(0.08)
        self.assertEqual(mouse_runtime.started, 2)
        self.assertIsNotNone(runtime._mouse_follow_task)

        await runtime.stop()
        await controller.stop()

        self.assertEqual(mouse_runtime.stopped, 2)
        self.assertFalse(controller.embodied_mode_enabled)

    async def test_runtime_ignores_speech_envelope_after_stop(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=self._speech_motion_profile(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        await controller.start()
        runtime = EmbodiedLive2DRuntime(
            controller=controller,
            source_url="ws://127.0.0.1:8001/api/webui/ws",
            source_token="maibot-avatar-state-local",
            source_session_id="session-runtime",
            fallback_to_legacy=True,
            logger=_Logger(),
        )

        async def fake_subscriber_start() -> None:
            return None

        async def fake_subscriber_stop() -> None:
            return None

        runtime.subscriber.start = fake_subscriber_start  # type: ignore[method-assign]
        runtime.subscriber.stop = fake_subscriber_stop  # type: ignore[method-assign]

        await runtime.start()
        await runtime._handle_subscriber_connected()
        runtime.driver._latest_snapshot = EmbodiedStateSnapshot(
            session_id="session-runtime",
            ts=1.0,
            seq=1,
            agent_state="running",
            valence=0.4,
            arousal=0.7,
            attention=0.8,
            cognitive_load=0.1,
            confidence=0.6,
            social_approach=0.25,
            energy=0.75,
            gaze_x=0.1,
            gaze_y=0.0,
        )
        bridge.events.clear()

        await runtime.stop()
        await runtime._handle_speech_envelope(
            text="after stop",
            audio_timeline={
                "audio_duration_ms": 240,
                "amplitudes": [
                    {"offset_ms": 0, "value": 0.0},
                    {"offset_ms": 80, "value": 1.0},
                    {"offset_ms": 160, "value": 0.8},
                ],
            },
            emotion_gain=1.0,
            timeline_id="late-envelope",
        )
        await controller.stop()

        timeline_events = self._non_idle_mouth_timeline_events([dict(event) for event in bridge.events])
        self.assertEqual(timeline_events, [])

    async def test_runtime_mouse_follow_loop_uses_configured_poll_interval(self) -> None:
        controller = Live2DController(
            bridge=InMemoryLive2DBridge(),
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="vts_native",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        runtime = EmbodiedLive2DRuntime(
            controller=controller,
            source_url="ws://127.0.0.1:8001/api/webui/ws",
            source_token="maibot-avatar-state-local",
            source_session_id="session-runtime",
            fallback_to_legacy=True,
            mouse_follow_enabled=True,
            mouse_follow_poll_interval_ms=7,
            logger=_Logger(),
        )

        class _MouseRuntimeStub:
            def latest_snapshot(self) -> MouseFollowSnapshot:
                return MouseFollowSnapshot(x_norm=0.2, y_norm=-0.1, active=True, activity_ts=1.0)

        runtime.mouse_follow_runtime = _MouseRuntimeStub()  # type: ignore[assignment]
        captured_snapshots: list[MouseFollowSnapshot] = []
        runtime.driver.update_mouse_follow_snapshot = captured_snapshots.append  # type: ignore[assignment]
        sleep_delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleep_delays.append(float(delay))
            raise asyncio.CancelledError

        with patch("maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied.asyncio.sleep", side_effect=fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await runtime._mouse_follow_loop()

        self.assertEqual(len(captured_snapshots), 1)
        self.assertAlmostEqual(sleep_delays[0], 0.007, places=6)

    async def test_runtime_cancels_inflight_speech_envelope_on_stop(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=self._speech_motion_profile(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        await controller.start()
        runtime = EmbodiedLive2DRuntime(
            controller=controller,
            source_url="ws://127.0.0.1:8001/api/webui/ws",
            source_token="maibot-avatar-state-local",
            source_session_id="session-runtime",
            fallback_to_legacy=True,
            logger=_Logger(),
        )

        async def fake_subscriber_start() -> None:
            return None

        async def fake_subscriber_stop() -> None:
            return None

        runtime.subscriber.start = fake_subscriber_start  # type: ignore[method-assign]
        runtime.subscriber.stop = fake_subscriber_stop  # type: ignore[method-assign]

        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow_speech_envelope(*args, **kwargs) -> dict[str, object]:
            del args, kwargs
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return {"success": True}

        runtime.driver.handle_speech_envelope = slow_speech_envelope  # type: ignore[method-assign]

        await runtime.start()
        await runtime._handle_subscriber_connected()
        speech_task = asyncio.create_task(
            runtime._handle_speech_envelope(
                text="during stop",
                audio_timeline={
                    "audio_duration_ms": 240,
                    "amplitudes": [
                        {"offset_ms": 0, "value": 0.0},
                        {"offset_ms": 80, "value": 1.0},
                    ],
                },
                emotion_gain=1.0,
                timeline_id="inflight-envelope",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)

        await runtime.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await speech_task
        await controller.stop()

        self.assertTrue(cancelled.is_set())
        timeline_events = self._non_idle_mouth_timeline_events([dict(event) for event in bridge.events])
        self.assertEqual(timeline_events, [])

    def test_runtime_can_toggle_mouse_follow_from_sync_plugin_api_after_start(self) -> None:
        async def scenario() -> tuple[EmbodiedLive2DRuntime, Live2DController, object]:
            bridge = InMemoryLive2DBridge()
            controller = Live2DController(
                bridge=bridge,
                profile=ParameterProfile.standard_fallback(),
                mouth_sync_mode="vts_native",
                idle_motion_enabled=False,
                idle_sway_enabled=False,
                speech_sway_enabled=False,
                embodied_mode=False,
            )
            await controller.start()
            runtime = EmbodiedLive2DRuntime(
                controller=controller,
                source_url="ws://127.0.0.1:8001/api/webui/ws",
                source_token="maibot-avatar-state-local",
                source_session_id="session-runtime",
                fallback_to_legacy=True,
                mouse_follow_enabled=False,
                mouse_follow_poll_interval_ms=33,
                logger=_Logger(),
            )

            class _MouseRuntimeStub:
                def __init__(self) -> None:
                    self.started = 0
                    self.stopped = 0

                async def start(self) -> None:
                    self.started += 1

                async def stop(self) -> None:
                    self.stopped += 1

                def latest_snapshot(self) -> MouseFollowSnapshot:
                    return MouseFollowSnapshot(x_norm=0.2, y_norm=-0.3, active=True, activity_ts=1.0)

            mouse_runtime = _MouseRuntimeStub()
            runtime.mouse_follow_runtime = mouse_runtime  # type: ignore[assignment]

            async def fake_subscriber_start() -> None:
                return None

            async def fake_subscriber_stop() -> None:
                return None

            runtime.subscriber.start = fake_subscriber_start  # type: ignore[method-assign]
            runtime.subscriber.stop = fake_subscriber_stop  # type: ignore[method-assign]

            await runtime.start()
            return runtime, controller, mouse_runtime

        runtime: EmbodiedLive2DRuntime | None = None
        controller: Live2DController | None = None
        mouse_runtime = None
        runner = asyncio.Runner()
        try:
            runtime, controller, mouse_runtime = runner.run(scenario())
            self.assertEqual(mouse_runtime.started, 0)
            self.assertIsNone(runtime._mouse_follow_task)

            runtime.set_mouse_follow_enabled(True)
            runner.run(asyncio.sleep(0.08))
            self.assertEqual(mouse_runtime.started, 1)
            self.assertIsNotNone(runtime._mouse_follow_task)
            self.assertFalse(runtime._mouse_follow_task.done())

            runtime.set_mouse_follow_enabled(False)
            runner.run(asyncio.sleep(0.08))
            self.assertEqual(mouse_runtime.stopped, 1)
            self.assertIsNone(runtime._mouse_follow_task)

            runtime.set_mouse_follow_enabled(False)
            runner.run(asyncio.sleep(0.02))
            self.assertEqual(mouse_runtime.stopped, 1)

            runtime.set_mouse_follow_enabled(True)
            runner.run(asyncio.sleep(0.08))
            self.assertEqual(mouse_runtime.started, 2)
            self.assertIsNotNone(runtime._mouse_follow_task)
            self.assertFalse(runtime._mouse_follow_task.done())
        finally:
            if runtime is not None:
                runner.run(runtime.stop())
            if controller is not None:
                runner.run(controller.stop())
            runner.close()

        self.assertIsNotNone(mouse_runtime)
        self.assertEqual(mouse_runtime.stopped, 2)


if __name__ == "__main__":
    unittest.main()

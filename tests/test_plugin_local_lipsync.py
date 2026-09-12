import asyncio
import sys
import unittest

from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from maibot_bilibili_live_adapter_copy.live2d_adaptive.bridge import InMemoryLive2DBridge
from maibot_bilibili_live_adapter_copy.live2d_adaptive.controller import Live2DController
from maibot_bilibili_live_adapter_copy.live2d_adaptive.profile import ParameterProfile, ParameterSpec


def _mouth_parameter_ids(events: list[dict[str, object]]) -> set[str]:
    parameter_ids: set[str] = set()
    for event in events:
        for parameter in list(event.get("parameters") or []):
            if isinstance(parameter, dict):
                parameter_ids.add(str(parameter.get("id") or ""))
    return parameter_ids


def _max_parameter_value(events: list[dict[str, object]], parameter_id: str) -> float:
    maximum = 0.0
    for event in events:
        for parameter in list(event.get("parameters") or []):
            if not isinstance(parameter, dict):
                continue
            if str(parameter.get("id") or "") != parameter_id:
                continue
            maximum = max(maximum, abs(float(parameter.get("value") or 0.0)))
    return maximum


def _last_parameter_value(events: list[dict[str, object]], parameter_id: str) -> float | None:
    for event in reversed(events):
        for parameter in reversed(list(event.get("parameters") or [])):
            if not isinstance(parameter, dict):
                continue
            if str(parameter.get("id") or "") != parameter_id:
                continue
            return float(parameter.get("value") or 0.0)
    return None


class PluginLocalLipSyncTest(unittest.IsolatedAsyncioTestCase):
    def test_live2d_sync_normalizes_legacy_native_mode_to_plugin_local(self) -> None:
        settings = LiveAdapterSettings.model_validate({"live2d": {"sync": {"mouth_sync_mode": "vts_native"}}})

        self.assertEqual(settings.live2d.sync.mouth_sync_mode, "plugin_local")

    async def test_controller_emits_plugin_local_mouth_frames_for_reply_audio(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        await controller.start()
        await controller.play_reply(
            "你好呀",
            audio_timeline={
                "audio_duration_ms": 280,
                "amplitudes": [
                    {"offset_ms": 0, "value": 0.0},
                    {"offset_ms": 70, "value": 0.45},
                    {"offset_ms": 140, "value": 0.92},
                    {"offset_ms": 210, "value": 0.25},
                ],
            },
        )
        await controller.stop()

        timeline_events = [event for event in bridge.events if event.get("type") == "live2d.timeline.frame"]
        self.assertTrue(timeline_events)
        parameter_ids = _mouth_parameter_ids(timeline_events)
        self.assertIn("ParamMouthOpenY", parameter_ids)
        self.assertIn("ParamMouthForm", parameter_ids)

    async def test_legacy_native_mode_still_uses_plugin_local_mouth_frames(self) -> None:
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
        await controller.play_reply(
            "测试一下",
            audio_timeline={
                "audio_duration_ms": 300,
                "amplitudes": [
                    {"offset_ms": 0, "value": 0.0},
                    {"offset_ms": 60, "value": 0.40},
                    {"offset_ms": 120, "value": 0.85},
                    {"offset_ms": 180, "value": 0.60},
                    {"offset_ms": 240, "value": 0.15},
                ],
            },
        )
        await controller.stop()

        timeline_events = [event for event in bridge.events if event.get("type") == "live2d.timeline.frame"]
        self.assertTrue(timeline_events)
        parameter_ids = _mouth_parameter_ids(timeline_events)
        self.assertIn("ParamMouthOpenY", parameter_ids)
        self.assertIn("ParamMouthForm", parameter_ids)

    async def test_plugin_local_mouth_frames_are_visibly_open_on_loud_audio(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        await controller.start()
        await controller.play_reply(
            "a e i o u",
            audio_timeline={
                "audio_duration_ms": 360,
                "amplitudes": [
                    {"offset_ms": 0, "value": 0.0},
                    {"offset_ms": 60, "value": 0.78},
                    {"offset_ms": 120, "value": 1.0},
                    {"offset_ms": 180, "value": 0.92},
                    {"offset_ms": 240, "value": 0.84},
                    {"offset_ms": 300, "value": 0.18},
                ],
            },
        )
        await controller.stop()

        timeline_events = [event for event in bridge.events if event.get("type") == "live2d.timeline.frame"]
        self.assertTrue(timeline_events)
        self.assertGreater(_max_parameter_value(timeline_events, "ParamMouthOpenY"), 0.72)
        self.assertGreater(_max_parameter_value(timeline_events, "ParamMouthForm"), 0.35)

    async def test_controller_speech_callback_runs_without_changing_plugin_local_mouth_frames(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        speech_sink = AsyncMock(return_value=None)
        controller.set_speech_envelope_sink(speech_sink)

        await controller.start()
        await controller.play_reply(
            "a e i o u",
            audio_timeline={
                "audio_duration_ms": 360,
                "amplitudes": [
                    {"offset_ms": 0, "value": 0.0},
                    {"offset_ms": 60, "value": 0.78},
                    {"offset_ms": 120, "value": 1.0},
                    {"offset_ms": 180, "value": 0.92},
                    {"offset_ms": 240, "value": 0.84},
                    {"offset_ms": 300, "value": 0.18},
                ],
            },
            emotion_intent="react_surprised",
        )
        await controller.stop()

        speech_sink.assert_awaited_once()
        call = speech_sink.await_args
        self.assertGreater(call.kwargs["emotion_gain"], controller.speech_sway_intensity)
        self.assertEqual(call.kwargs["emotion_intent"], "react_surprised")
        self.assertEqual(call.kwargs["text"], "a e i o u")
        self.assertEqual(call.kwargs["audio_timeline"]["audio_duration_ms"], 360)

        timeline_events = [event for event in bridge.events if event.get("type") == "live2d.timeline.frame"]
        self.assertTrue(timeline_events)
        parameter_ids = _mouth_parameter_ids(timeline_events)
        self.assertIn("ParamMouthOpenY", parameter_ids)
        self.assertIn("ParamMouthForm", parameter_ids)
        self.assertGreater(_max_parameter_value(timeline_events, "ParamMouthOpenY"), 0.72)
        self.assertGreater(_max_parameter_value(timeline_events, "ParamMouthForm"), 0.35)

    async def test_controller_keeps_prior_speech_envelope_running_when_new_one_arrives(self) -> None:
        controller = Live2DController(
            bridge=InMemoryLive2DBridge(),
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        release = asyncio.Event()
        started: list[str] = []
        completed: list[str] = []
        cancelled: list[str] = []

        async def speech_sink(**kwargs: object) -> None:
            timeline_id = str(kwargs.get("timeline_id") or "")
            started.append(timeline_id)
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(timeline_id)
                raise
            completed.append(timeline_id)

        controller.set_speech_envelope_sink(speech_sink)

        controller._dispatch_speech_envelope(
            text="first",
            audio_timeline={"audio_duration_ms": 240, "amplitudes": [{"offset_ms": 0, "value": 0.0}, {"offset_ms": 120, "value": 1.0}]},
            emotion_intent="react_happy",
            emotion_gain=1.0,
            timeline_id="timeline-1",
        )
        await asyncio.sleep(0)
        controller._dispatch_speech_envelope(
            text="second",
            audio_timeline={"audio_duration_ms": 240, "amplitudes": [{"offset_ms": 0, "value": 0.0}, {"offset_ms": 120, "value": 0.8}]},
            emotion_intent="react_surprised",
            emotion_gain=1.0,
            timeline_id="timeline-2",
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.sleep(0)
        await controller.stop()

        self.assertEqual(started, ["timeline-1", "timeline-2"])
        self.assertEqual(cancelled, [])
        self.assertEqual(completed, ["timeline-1", "timeline-2"])

    async def test_hiyori_mouth_form_returns_to_slightly_positive_closed_pose_after_speech(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile(
                model_id="hiyori_pro_zh",
                model_name="Hiyori",
                parameters=[
                    ParameterSpec("ParamMouthOpenY", 0.0, 1.0, 0.0),
                    ParameterSpec("ParamMouthForm", -2.0, 1.0, 0.0),
                ],
            ),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        await controller.start()
        await controller.play_reply(
            "你好呀",
            audio_timeline={
                "audio_duration_ms": 600,
                "amplitudes": [
                    {"offset_ms": 0, "value": 0.0},
                    {"offset_ms": 120, "value": 0.75},
                    {"offset_ms": 240, "value": 0.95},
                    {"offset_ms": 360, "value": 0.55},
                    {"offset_ms": 480, "value": 0.15},
                ],
            },
        )
        await controller.stop()

        timeline_events = [event for event in bridge.events if event.get("type") == "live2d.timeline.frame"]
        self.assertTrue(timeline_events)
        final_mouth_form = _last_parameter_value(timeline_events, "ParamMouthForm")
        self.assertIsNotNone(final_mouth_form)
        assert final_mouth_form is not None
        self.assertGreaterEqual(final_mouth_form, 0.12)

    async def test_hiyori_mouth_form_resets_after_mouth_has_fully_closed(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile(
                model_id="hiyori_pro_zh",
                model_name="Hiyori",
                parameters=[
                    ParameterSpec("ParamMouthOpenY", 0.0, 1.0, 0.0),
                    ParameterSpec("ParamMouthForm", -2.0, 1.0, 0.0),
                ],
            ),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=False,
        )
        await controller.start()
        await controller.play_reply(
            "aaaa",
            audio_timeline={
                "audio_duration_ms": 400,
                "amplitudes": [
                    {"offset_ms": 0, "value": 0.0},
                    {"offset_ms": 80, "value": 1.0},
                    {"offset_ms": 160, "value": 1.0},
                    {"offset_ms": 240, "value": 0.9},
                    {"offset_ms": 320, "value": 0.2},
                ],
            },
        )
        await controller.stop()

        timeline_events = [event for event in bridge.events if event.get("type") == "live2d.timeline.frame"]
        self.assertGreaterEqual(len(timeline_events), 3)
        tail_events = timeline_events[-3:]
        tail_values = []
        for event in tail_events:
            mouth_open = _last_parameter_value([event], "ParamMouthOpenY")
            mouth_form = _last_parameter_value([event], "ParamMouthForm")
            tail_values.append((int(event.get("offset_ms") or 0), mouth_open, mouth_form))

        self.assertIsNotNone(tail_values[-2][1])
        self.assertIsNotNone(tail_values[-2][2])
        self.assertIsNotNone(tail_values[-1][1])
        self.assertIsNotNone(tail_values[-1][2])
        assert tail_values[-2][1] is not None
        assert tail_values[-2][2] is not None
        assert tail_values[-1][1] is not None
        assert tail_values[-1][2] is not None
        self.assertLessEqual(tail_values[-2][1], 0.01)
        self.assertGreater(tail_values[-2][2], 0.60)
        self.assertGreater(tail_values[-1][0], tail_values[-2][0])
        self.assertLessEqual(tail_values[-1][1], 0.01)
        self.assertLess(tail_values[-1][2], tail_values[-2][2] - 0.25)

    async def test_controller_emits_idle_base_mouth_parameters_while_not_speaking(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile(
                model_id="hiyori_pro_zh",
                model_name="Hiyori",
                parameters=[
                    ParameterSpec("ParamMouthOpenY", 0.0, 1.0, 0.0),
                    ParameterSpec("ParamMouthForm", -2.0, 1.0, 0.0),
                ],
            ),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            parameter_keepalive_ms=40,
            embodied_mode=False,
        )

        await controller.start()
        await asyncio.sleep(0.08)
        await controller.stop()

        idle_mouth_events = [
            event
            for event in bridge.events
            if event.get("type") == "live2d.timeline.frame" and str(event.get("purpose") or "") == "idle-mouth"
        ]
        self.assertTrue(idle_mouth_events)
        mouth_open = _last_parameter_value(idle_mouth_events, "ParamMouthOpenY")
        mouth_form = _last_parameter_value(idle_mouth_events, "ParamMouthForm")
        self.assertIsNotNone(mouth_open)
        self.assertIsNotNone(mouth_form)
        assert mouth_open is not None
        assert mouth_form is not None
        self.assertLessEqual(mouth_open, 0.01)
        self.assertGreaterEqual(mouth_form, 0.12)

    async def test_controller_restores_idle_base_mouth_parameters_after_speech_finishes(self) -> None:
        bridge = InMemoryLive2DBridge()
        controller = Live2DController(
            bridge=bridge,
            profile=ParameterProfile(
                model_id="hiyori_pro_zh",
                model_name="Hiyori",
                parameters=[
                    ParameterSpec("ParamMouthOpenY", 0.0, 1.0, 0.0),
                    ParameterSpec("ParamMouthForm", -2.0, 1.0, 0.0),
                ],
            ),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            prepare_ms=0,
            release_ms=40,
            parameter_keepalive_ms=40,
            embodied_mode=False,
        )

        await controller.start()
        bridge.events.clear()
        await controller.play_reply(
            "aaaa",
            audio_timeline={
                "audio_duration_ms": 180,
                "amplitudes": [
                    {"offset_ms": 0, "value": 0.0},
                    {"offset_ms": 60, "value": 1.0},
                    {"offset_ms": 120, "value": 0.7},
                ],
            },
        )
        await asyncio.sleep(0.75)
        await controller.stop()

        idle_mouth_events = [
            event
            for event in bridge.events
            if event.get("type") == "live2d.timeline.frame" and str(event.get("purpose") or "") == "idle-mouth"
        ]
        self.assertTrue(idle_mouth_events)
        mouth_form = _last_parameter_value(idle_mouth_events, "ParamMouthForm")
        self.assertIsNotNone(mouth_form)
        assert mouth_form is not None
        self.assertGreaterEqual(mouth_form, 0.12)

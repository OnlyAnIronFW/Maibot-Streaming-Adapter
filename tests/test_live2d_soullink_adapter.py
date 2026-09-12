import sys
import types
import unittest
import asyncio
import tempfile

from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parents[1]

for candidate in (str(REPO_ROOT), str(PLUGIN_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


from plugins.maibot_bilibili_live_adapter_copy.config import Live2DConfig
from plugins.maibot_bilibili_live_adapter_copy.live2d_adaptive.profile import ParameterProfile, ParameterSpec
from plugins.maibot_bilibili_live_adapter_copy.live2d_shell_protocol import (
    ShellExpressionMessage,
    ShellLoadModelMessage,
    ShellTtsMotionFrameMessage,
    message_to_payload,
)
from plugins.maibot_bilibili_live_adapter_copy.live2d_soullink_vendor.src.generators.expression import (
    ExpressionGenerator,
)
from plugins.maibot_bilibili_live_adapter_copy.live2d_soullink_vendor.src.config.models import (
    APIConfig as SoulLinkAPIConfig,
)
from plugins.maibot_bilibili_live_adapter_copy.plugin import BilibiliLiveAdapterPlugin
from plugins.maibot_bilibili_live_adapter_copy.live2d_adaptive.soullink import (
    ShellSoulLinkSink,
    SoulLinkLive2DController,
    VtsSoulLinkSink,
    build_soullink_available_parameters,
    resolve_live2d_scheme,
    resolve_soullink_model_config,
)


class Live2DSoulLinkAdapterTests(unittest.TestCase):
    class _FakeBaseController:
        def __init__(self) -> None:
            self.timeline_calls: list[dict[str, object]] = []
            self.parameter_calls: list[dict[str, object]] = []
            self.start_calls = 0
            self.stop_calls = 0
            self.play_reply_calls: list[dict[str, object]] = []

        async def send_timeline_clip(self, keyframes, **kwargs):
            self.timeline_calls.append({"keyframes": keyframes, "kwargs": kwargs})
            return {"success": True}

        async def send_parameters(self, parameters, **kwargs):
            self.parameter_calls.append({"parameters": parameters, "kwargs": kwargs})
            return {"success": True}

        async def start(self) -> None:
            self.start_calls += 1

        async def stop(self) -> None:
            self.stop_calls += 1

        async def play_reply(self, text, **kwargs):
            self.play_reply_calls.append({"text": text, "kwargs": kwargs})
            return types.SimpleNamespace(
                timeline_id="tl-play",
                estimated_duration_ms=1000,
                release_ms=600,
                events=[],
            )

    class _FakeShellRuntime:
        def __init__(self) -> None:
            self.broadcast_calls: list[dict[str, object]] = []
            self.registered_model_paths: list[str] = []

        async def broadcast(self, payload):
            self.broadcast_calls.append(dict(payload))

        def register_model_file(self, model_path):
            self.registered_model_paths.append(str(model_path))
            return "http://127.0.0.1:18183/shell/model/hiyori_pro_t11.model3.json"

    def test_message_to_payload_uses_expected_shell_shapes(self) -> None:
        load_payload = message_to_payload(
            ShellLoadModelMessage(model={"id": "hiyori", "name": "Hiyori"})
        )
        expression_payload = message_to_payload(
            ShellExpressionMessage(
                parameters={"ParamAngleX": 6.0},
                duration_ms=240,
            )
        )
        frame_payload = message_to_payload(
            ShellTtsMotionFrameMessage(
                parameters={"ParamAngleX": 8.0},
                timeline_id="tl-1",
                offset_ms=500,
                duration_ms=500,
            )
        )

        self.assertEqual(load_payload, {"type": "load_model", "model": {"id": "hiyori", "name": "Hiyori"}})
        self.assertEqual(
            expression_payload,
            {
                "type": "expression",
                "parameters": {"ParamAngleX": 6.0},
                "duration_ms": 240,
            },
        )
        self.assertEqual(
            frame_payload,
            {
                "type": "tts_motion_frame",
                "parameters": {"ParamAngleX": 8.0},
                "timeline_id": "tl-1",
                "offset_ms": 500,
                "duration_ms": 500,
            },
        )

    def test_vts_sink_expression_delegates_to_base_controller(self) -> None:
        fake = self._FakeBaseController()
        sink = VtsSoulLinkSink(base_controller=fake, profile=ParameterProfile())

        asyncio.run(
            sink.send_expression(
                [{"id": "ParamAngleX", "value": 4.0, "weight": 0.82}],
                duration_ms=300,
                timeline_id="tl-vts",
                purpose="soullink-initial",
            )
        )

        self.assertEqual(len(fake.parameter_calls), 1)
        self.assertEqual(fake.parameter_calls[0]["kwargs"]["timeline_id"], "tl-vts")

    def test_shell_sink_broadcasts_load_model_expression_timeline_frames_and_reset_payloads(self) -> None:
        runtime = self._FakeShellRuntime()
        with tempfile.TemporaryDirectory() as temp_dir:
            model_file = Path(temp_dir) / "hiyori_pro_t11.model3.json"
            model_file.write_text("{}", encoding="utf-8")
            sink = ShellSoulLinkSink(
                runtime=runtime,
                profile=ParameterProfile(model_id="hiyori", model_name="Hiyori"),
                model_path=str(model_file),
            )

            async def _run() -> None:
                await sink.start()
                await sink.send_expression(
                    [{"id": "ParamAngleX", "value": 4.0, "weight": 0.82}],
                    duration_ms=300,
                    timeline_id="tl-shell",
                    purpose="soullink-initial",
                )
                await sink.send_timeline_clip(
                    [
                        {
                            "offset_ms": 0,
                            "parameters": [{"id": "ParamAngleX", "value": 6.0, "weight": 0.82}],
                            "duration_ms": 0,
                            "purpose": "soullink-frame-0",
                        },
                        {
                            "offset_ms": 300,
                            "parameters": [{"id": "ParamAngleX", "value": 8.0, "weight": 0.82}],
                            "duration_ms": 300,
                            "purpose": "soullink-frame-1",
                        }
                    ],
                    timeline_id="tl-shell",
                    easing="easeInOutCubic",
                    blend="replace",
                    priority=5,
                    purpose="soullink-frame-1",
                )
                await asyncio.gather(*list(sink._clip_tasks))
                await sink.stop()

            asyncio.run(_run())

        self.assertEqual(runtime.registered_model_paths, [str(model_file)])
        self.assertEqual(
            runtime.broadcast_calls[0],
            {
                "type": "load_model",
                "model": {
                    "id": "hiyori",
                    "name": "Hiyori",
                    "path": "http://127.0.0.1:18183/shell/model/hiyori_pro_t11.model3.json",
                },
            },
        )
        self.assertEqual(
            runtime.broadcast_calls[1:],
            [
                {"type": "reset", "duration_ms": 0},
                {
                    "type": "expression",
                    "parameters": {"ParamAngleX": 4.0},
                    "duration_ms": 300,
                },
                {
                    "type": "tts_motion_frame",
                    "parameters": {"ParamAngleX": 6.0},
                    "timeline_id": "tl-shell",
                    "offset_ms": 0,
                    "duration_ms": 0,
                },
                {
                    "type": "tts_motion_frame",
                    "parameters": {"ParamAngleX": 8.0},
                    "timeline_id": "tl-shell",
                    "offset_ms": 300,
                    "duration_ms": 300,
                },
                {"type": "reset", "duration_ms": 0},
            ],
        )

    def test_resolve_scheme_defaults_to_embodied_when_auto_and_embodied_is_ready(self) -> None:
        config = Live2DConfig.model_validate(
            {
                "scheme": "auto",
                "embodied": {
                    "enabled": True,
                    "source_session_id": "session-123",
                },
            }
        )
        self.assertEqual(resolve_live2d_scheme(config), "embodied")

    def test_resolve_scheme_defaults_to_legacy_when_auto_and_embodied_is_not_ready(self) -> None:
        config = Live2DConfig.model_validate(
            {
                "scheme": "auto",
                "embodied": {
                    "enabled": True,
                    "source_session_id": "",
                },
            }
        )
        self.assertEqual(resolve_live2d_scheme(config), "legacy")

    def test_build_available_parameters_skips_disabled_specs(self) -> None:
        profile = ParameterProfile(
            parameters=[
                ParameterSpec(id="ParamAngleX", minimum=-30.0, maximum=30.0, default=0.0, role="head.x", enabled=True),
                ParameterSpec(id="ParamArmLA", minimum=-1.0, maximum=1.0, default=0.0, role="arm.left", enabled=False),
            ]
        )
        payload = build_soullink_available_parameters(profile)
        self.assertIn("ParamAngleX", payload)
        self.assertNotIn("ParamArmLA", payload)
        self.assertEqual(payload["ParamAngleX"]["name"], "head.x")

    def test_vendor_extract_json_error_includes_preview_when_no_object_exists(self) -> None:
        with self.assertRaisesRegex(ValueError, r"no_object.*not json at all"):
            ExpressionGenerator._extract_json("not json at all")

    def test_vendor_extract_json_error_includes_preview_when_object_is_invalid(self) -> None:
        with self.assertRaisesRegex(ValueError, r"invalid_object.*\"a\": }"):
            ExpressionGenerator._extract_json('{"a": }')

    def test_vendor_generate_requests_json_object_and_disables_deepseek_thinking(self) -> None:
        generator = ExpressionGenerator(
            SoulLinkAPIConfig(
                provider="DeepSeek",
                api_key="test-key",
                base_url="https://api.deepseek.com",
                model="deepseek-v4-flash",
                temperature=0.35,
                max_tokens=420,
                enable_thinking=False,
                response_format_json_object=True,
            )
        )
        generator.available_parameters = {"ParamAngleX": {"name": "head.x", "min": -30.0, "max": 30.0}}
        captured_request: dict[str, object] = {}

        async def _fake_call_llm(self, request_body: dict, log_prefix: str = "") -> dict:
            captured_request.clear()
            captured_request.update(request_body)
            return {"parameters": {}}

        with patch.object(ExpressionGenerator, "_call_llm", new=_fake_call_llm):
            result = asyncio.run(generator.generate("hello"))

        self.assertEqual(result["parameters"], {})
        self.assertEqual(captured_request["response_format"], {"type": "json_object"})
        self.assertEqual(captured_request["thinking"], {"type": "disabled"})

    def test_resolve_soullink_model_prefers_explicit_provider_identifier_over_model_name_alias(self) -> None:
        config = Live2DConfig.model_validate(
            {
                "soullink": {
                    "model_name": "deepseek-v4-flash bailian",
                    "api_provider": "DeepSeek",
                    "model_identifier": "deepseek-v4-flash",
                }
            }
        ).soullink

        resolved = resolve_soullink_model_config(config)

        self.assertEqual(resolved.provider_name, "DeepSeek")
        self.assertEqual(resolved.model_identifier, "deepseek-v4-flash")
        self.assertIn("api.deepseek.com", resolved.base_url)

    def test_convert_parameters_filters_mouth_open_and_optional_mouth_form(self) -> None:
        profile = ParameterProfile(
            parameters=[
                ParameterSpec(id="ParamAngleX", minimum=-30.0, maximum=30.0, default=0.0, role="head.x", enabled=True),
                ParameterSpec(
                    id="ParamMouthOpenY",
                    minimum=0.0,
                    maximum=1.0,
                    default=0.0,
                    role="mouth.open",
                    enabled=True,
                ),
                ParameterSpec(
                    id="ParamMouthForm",
                    minimum=-1.0,
                    maximum=1.0,
                    default=0.0,
                    role="mouth.form",
                    enabled=True,
                ),
            ]
        )
        controller = object.__new__(SoulLinkLive2DController)
        controller.profile = profile
        controller.config = types.SimpleNamespace(
            parameter_weight=0.82,
            keep_mouth_form=False,
            tts_motion_keep_lip_sync=True,
        )
        converted = controller._convert_parameters(
            {
                "ParamAngleX": 12.0,
                "ParamMouthOpenY": 0.9,
                "ParamMouthForm": 0.4,
            }
        )
        self.assertEqual(converted, [{"id": "ParamAngleX", "value": 12.0, "weight": 0.82}])

    def test_convert_parameters_keeps_mouth_when_soullink_lipsync_is_disabled(self) -> None:
        profile = ParameterProfile(
            parameters=[
                ParameterSpec(id="ParamAngleX", minimum=-30.0, maximum=30.0, default=0.0, role="head.x", enabled=True),
                ParameterSpec(id="ParamMouthOpenY", minimum=0.0, maximum=1.0, default=0.0, role="mouth.open", enabled=True),
                ParameterSpec(id="ParamMouthForm", minimum=-1.0, maximum=1.0, default=0.0, role="mouth.form", enabled=True),
            ]
        )
        controller = object.__new__(SoulLinkLive2DController)
        controller.profile = profile
        controller.config = types.SimpleNamespace(
            parameter_weight=0.82,
            keep_mouth_form=False,
            tts_motion_keep_lip_sync=False,
        )
        converted = controller._convert_parameters(
            {
                "ParamAngleX": 12.0,
                "ParamMouthOpenY": 0.9,
                "ParamMouthForm": 0.4,
            }
        )
        self.assertEqual(
            converted,
            [
                {"id": "ParamAngleX", "value": 12.0, "weight": 0.82},
                {"id": "ParamMouthOpenY", "value": 0.9, "weight": 0.82},
                {"id": "ParamMouthForm", "value": 0.4, "weight": 0.82},
            ],
        )

    def test_plugin_wink_falls_back_to_soullink_shell_runtime(self) -> None:
        runtime = self._FakeShellRuntime()
        plugin = object.__new__(BilibiliLiveAdapterPlugin)
        plugin._embodied_live2d_runtime = None
        plugin._soullink_shell_runtime = runtime
        plugin._logger = lambda: None

        accepted = asyncio.run(plugin.handle_live2d_wink_request({"wink_side": "left"}))

        self.assertTrue(accepted)
        self.assertEqual(len(runtime.broadcast_calls), 2)
        self.assertEqual(runtime.broadcast_calls[0]["type"], "expression")
        self.assertEqual(runtime.broadcast_calls[0]["parameters"]["ParamEyeLOpen"], 0.0)
        self.assertEqual(runtime.broadcast_calls[0]["parameters"]["ParamEyeROpen"], 1.0)
        self.assertEqual(runtime.broadcast_calls[1]["parameters"]["ParamEyeLOpen"], 1.0)
        self.assertEqual(runtime.broadcast_calls[1]["parameters"]["ParamEyeROpen"], 1.0)

    def test_dispatch_timeline_expression_result_uses_timeline_clip(self) -> None:
        profile = ParameterProfile(
            parameters=[
                ParameterSpec(id="ParamAngleX", minimum=-30.0, maximum=30.0, default=0.0, role="head.x", enabled=True),
                ParameterSpec(id="ParamMouthOpenY", minimum=0.0, maximum=1.0, default=0.0, role="mouth.open", enabled=True),
            ]
        )
        fake = self._FakeBaseController()
        controller = object.__new__(SoulLinkLive2DController)
        controller.sink = VtsSoulLinkSink(base_controller=fake, profile=profile)
        controller.profile = profile
        controller.config = types.SimpleNamespace(parameter_weight=0.82, keep_mouth_form=False)
        asyncio.run(
            controller._dispatch_timeline_expression_result(
                {"parameters": {"ParamAngleX": 8.0, "ParamMouthOpenY": 1.0}, "duration": 320},
                timeline_id="tl-1",
                purpose="soullink-frame-0",
                offset_ms=320,
                fallback_duration_ms=320,
            )
        )
        self.assertEqual(len(fake.timeline_calls), 1)
        self.assertEqual(fake.parameter_calls, [])
        call = fake.timeline_calls[0]
        self.assertEqual(call["kwargs"]["timeline_id"], "tl-1")
        frame = call["keyframes"][0]
        self.assertEqual(frame["offset_ms"], 320)
        self.assertEqual(frame["duration_ms"], 320)
        self.assertEqual(frame["parameters"], [{"id": "ParamAngleX", "value": 8.0, "weight": 0.82}])

    def test_dispatch_timeline_expression_result_expands_sparse_target_into_eased_keyframes(self) -> None:
        profile = ParameterProfile(
            parameters=[
                ParameterSpec(id="ParamAngleX", minimum=-30.0, maximum=30.0, default=0.0, role="head.x", enabled=True),
            ]
        )
        fake = self._FakeBaseController()
        controller = object.__new__(SoulLinkLive2DController)
        controller.sink = VtsSoulLinkSink(base_controller=fake, profile=profile)
        controller.profile = profile
        controller.config = types.SimpleNamespace(
            parameter_weight=0.82,
            keep_mouth_form=False,
            transition_sample_interval_ms=16,
        )
        controller._timeline_states = {
            "tl-1": {
                "offset_ms": 0,
                "parameters": [{"id": "ParamAngleX", "value": 0.0, "weight": 0.82}],
            }
        }
        asyncio.run(
            controller._dispatch_timeline_expression_result(
                {"parameters": {"ParamAngleX": 16.0}, "duration": 64},
                timeline_id="tl-1",
                purpose="soullink-frame-1",
                offset_ms=64,
                fallback_duration_ms=64,
            )
        )
        self.assertEqual(len(fake.timeline_calls), 1)
        keyframes = fake.timeline_calls[0]["keyframes"]
        self.assertGreater(len(keyframes), 1)
        self.assertEqual([frame["offset_ms"] for frame in keyframes], [16, 32, 48, 64])
        first_value = keyframes[0]["parameters"][0]["value"]
        self.assertAlmostEqual(first_value, 1.0, places=3)
        self.assertEqual(keyframes[-1]["parameters"][0]["value"], 16.0)

    def test_build_soullink_lipsync_keyframes_uses_original_mouth_wave_shape(self) -> None:
        profile = ParameterProfile(
            parameters=[
                ParameterSpec(id="ParamMouthOpenY", minimum=0.0, maximum=1.0, default=0.0, role="mouth.open", enabled=True),
                ParameterSpec(id="ParamMouthForm", minimum=-1.0, maximum=1.0, default=0.0, role="mouth.form", enabled=True),
            ]
        )
        controller = object.__new__(SoulLinkLive2DController)
        controller.profile = profile
        with patch(
            "plugins.maibot_bilibili_live_adapter_copy.live2d_adaptive.soullink.random.Random.random",
            return_value=0.5,
        ):
            keyframes = controller._build_soullink_lipsync_keyframes(
                timeline_id="tl-mouth",
                duration_ms=100,
                prepare_ms=20,
            )
        self.assertEqual([frame["offset_ms"] for frame in keyframes], [20, 70, 120])
        self.assertEqual(keyframes[0]["parameters"][0]["id"], "ParamMouthOpenY")
        self.assertGreater(keyframes[0]["parameters"][0]["value"], 0.0)
        self.assertEqual(keyframes[-1]["parameters"][0]["value"], 0.0)

    def test_play_reply_suppresses_builtin_lipsync_and_queues_soullink_lipsync(self) -> None:
        fake = self._FakeBaseController()
        profile = ParameterProfile(
            parameters=[
                ParameterSpec(id="ParamMouthOpenY", minimum=0.0, maximum=1.0, default=0.0, role="mouth.open", enabled=True),
                ParameterSpec(id="ParamMouthForm", minimum=-1.0, maximum=1.0, default=0.0, role="mouth.form", enabled=True),
            ]
        )
        controller = object.__new__(SoulLinkLive2DController)
        controller.base_controller = fake
        controller.profile = profile
        controller.config = types.SimpleNamespace(
            tts_motion_keep_lip_sync=True,
            tts_motion_enabled=True,
            parameter_weight=0.82,
            keep_mouth_form=True,
        )
        controller._reply_motion_tasks = set()
        controller._run_reply_motion = types.MethodType(lambda self, **kwargs: _completed_future(None), controller)

        async def _run() -> None:
            await controller.play_reply("hello", audio_timeline={"audio_duration_ms": 1000})
            if controller._reply_motion_tasks:
                await asyncio.gather(*list(controller._reply_motion_tasks))

        with patch(
            "plugins.maibot_bilibili_live_adapter_copy.live2d_adaptive.soullink.random.Random.random",
            return_value=0.5,
        ):
            asyncio.run(_run())

        self.assertTrue(fake.play_reply_calls)
        self.assertTrue(fake.play_reply_calls[0]["kwargs"]["suppress_lipsync"])
        self.assertTrue(fake.play_reply_calls[0]["kwargs"]["suppress_expression_overlay"])
        self.assertEqual(len(fake.timeline_calls), 1)
        self.assertEqual(fake.timeline_calls[0]["kwargs"]["purpose"], "lipsync")

    def test_run_reply_motion_continues_when_initial_expression_generation_fails(self) -> None:
        controller = object.__new__(SoulLinkLive2DController)
        controller.config = types.SimpleNamespace(tts_motion_enabled=True)
        controller._log_warning_messages = []
        controller._log_warning = controller._log_warning_messages.append
        controller._build_reply_context = types.MethodType(lambda self, emotion: "ctx", controller)
        controller._safe_generate = types.MethodType(
            lambda self, text, context: _failed_future(ValueError("无法解析 LLM 返回的 JSON")),
            controller,
        )
        dispatched_motion: list[dict[str, object]] = []

        async def _dispatch_motion(self, **kwargs):
            dispatched_motion.append(dict(kwargs))

        controller._dispatch_tts_motion = types.MethodType(_dispatch_motion, controller)
        controller._dispatch_timeline_expression_result = types.MethodType(
            lambda self, *args, **kwargs: _completed_future(None),
            controller,
        )

        asyncio.run(
            controller._run_reply_motion(
                timeline=types.SimpleNamespace(timeline_id="tl-initial", estimated_duration_ms=1000),
                text="hello",
                audio_timeline={"audio_duration_ms": 1000},
                emotion_intent="happy",
            )
        )

        self.assertEqual(len(dispatched_motion), 1)
        self.assertIn("initial expression generation failed", controller._log_warning_messages[0])

    def test_dispatch_tts_motion_accumulates_timeline_offsets(self) -> None:
        fake = self._FakeBaseController()
        controller = object.__new__(SoulLinkLive2DController)
        controller.profile = ParameterProfile(
            parameters=[
                ParameterSpec(id="ParamAngleX", minimum=-30.0, maximum=30.0, default=0.0, role="head.x", enabled=True),
            ]
        )
        controller.sink = VtsSoulLinkSink(base_controller=fake, profile=controller.profile)
        controller.config = types.SimpleNamespace(
            tts_motion_frame_duration_ms=500,
            parameter_weight=0.82,
            keep_mouth_form=False,
        )
        controller._log_warning = lambda message: None
        controller._safe_generate_motion_plan = types.MethodType(
            lambda self, **kwargs: _completed_future(
                [
                    {"frameIndex": 0, "action": "left"},
                    {"frameIndex": 1, "action": "right"},
                ]
            ),
            controller,
        )
        controller._safe_generate_tts_motion_frame_with_plan = types.MethodType(
            lambda self, **kwargs: _completed_future(
                {
                    "parameters": {"ParamAngleX": 10.0 if kwargs["frame_index"] == 0 else -10.0},
                    "duration": 500,
                }
            ),
            controller,
        )
        asyncio.run(
            controller._dispatch_tts_motion(
                timeline=types.SimpleNamespace(timeline_id="tl-2", estimated_duration_ms=1000),
                text="hello",
                audio_timeline={"audio_duration_ms": 1000},
                context="ctx",
            )
        )
        self.assertEqual(len(fake.timeline_calls), 2)
        first_keyframes = fake.timeline_calls[0]["keyframes"]
        second_keyframes = fake.timeline_calls[1]["keyframes"]
        self.assertEqual(first_keyframes[-1]["offset_ms"], 500)
        self.assertEqual(second_keyframes[-1]["offset_ms"], 1000)
        self.assertGreater(len(second_keyframes), 1)
        self.assertEqual(fake.parameter_calls, [])

    def test_dispatch_tts_motion_streams_frames_before_later_generation_finishes(self) -> None:
        fake = self._FakeBaseController()
        controller = object.__new__(SoulLinkLive2DController)
        controller.profile = ParameterProfile(
            parameters=[
                ParameterSpec(id="ParamAngleX", minimum=-30.0, maximum=30.0, default=0.0, role="head.x", enabled=True),
            ]
        )
        controller.sink = VtsSoulLinkSink(base_controller=fake, profile=controller.profile)
        controller.config = types.SimpleNamespace(
            tts_motion_frame_duration_ms=500,
            parameter_weight=0.82,
            keep_mouth_form=False,
            transition_sample_interval_ms=16,
        )
        controller._timeline_states = {}
        controller._log_warning = lambda message: None
        controller._safe_generate_motion_plan = types.MethodType(
            lambda self, **kwargs: _completed_future(
                [
                    {"frameIndex": 0, "action": "left"},
                    {"frameIndex": 1, "action": "right"},
                ]
            ),
            controller,
        )

        async def _generate_frame(self, **kwargs):
            frame_index = kwargs["frame_index"]
            if frame_index == 0:
                return {"parameters": {"ParamAngleX": 10.0}, "duration": 500}
            await second_frame_gate.wait()
            return {"parameters": {"ParamAngleX": -10.0}, "duration": 500}

        controller._safe_generate_tts_motion_frame_with_plan = types.MethodType(_generate_frame, controller)

        async def _run() -> tuple[int, bool, int]:
            task = asyncio.create_task(
                controller._dispatch_tts_motion(
                    timeline=types.SimpleNamespace(timeline_id="tl-3", estimated_duration_ms=1000),
                    text="hello",
                    audio_timeline={"audio_duration_ms": 1000},
                    context="ctx",
                )
            )
            await asyncio.sleep(0.05)
            first_call_count = len(fake.timeline_calls)
            task_done_before_release = task.done()
            second_frame_gate.set()
            await task
            return first_call_count, task_done_before_release, len(fake.timeline_calls)

        second_frame_gate = asyncio.Event()
        first_call_count, task_done_before_release, final_call_count = asyncio.run(_run())
        self.assertEqual(first_call_count, 1)
        self.assertFalse(task_done_before_release)
        self.assertEqual(final_call_count, 2)

    def test_dispatch_tts_motion_prefetches_configured_window_before_first_frame_finishes(self) -> None:
        fake = self._FakeBaseController()
        controller = object.__new__(SoulLinkLive2DController)
        controller.profile = ParameterProfile(
            parameters=[
                ParameterSpec(id="ParamAngleX", minimum=-30.0, maximum=30.0, default=0.0, role="head.x", enabled=True),
            ]
        )
        controller.sink = VtsSoulLinkSink(base_controller=fake, profile=controller.profile)
        controller.config = types.SimpleNamespace(
            tts_motion_frame_duration_ms=500,
            tts_motion_prefetch_frames=5,
            parameter_weight=0.82,
            keep_mouth_form=False,
            transition_sample_interval_ms=16,
        )
        controller._timeline_states = {}
        controller._log_warning = lambda message: None
        controller._safe_generate_motion_plan = types.MethodType(
            lambda self, **kwargs: _completed_future(
                [{"frameIndex": index, "action": f"pose-{index}"} for index in range(6)]
            ),
            controller,
        )
        first_frame_gate = asyncio.Event()
        scheduled_frames: list[int] = []

        async def _generate_frame(self, **kwargs):
            frame_index = kwargs["frame_index"]
            scheduled_frames.append(frame_index)
            if frame_index == 0:
                await first_frame_gate.wait()
            return {"parameters": {"ParamAngleX": float(frame_index)}, "duration": 500}

        controller._safe_generate_tts_motion_frame_with_plan = types.MethodType(_generate_frame, controller)

        async def _run() -> list[int]:
            task = asyncio.create_task(
                controller._dispatch_tts_motion(
                    timeline=types.SimpleNamespace(timeline_id="tl-prefetch", estimated_duration_ms=3000),
                    text="hello",
                    audio_timeline={"audio_duration_ms": 3000},
                    context="ctx",
                )
            )
            await asyncio.sleep(0.05)
            prefetched = list(scheduled_frames)
            first_frame_gate.set()
            await task
            return prefetched

        prefetched = asyncio.run(_run())
        self.assertEqual(prefetched[:5], [0, 1, 2, 3, 4])

    def test_dispatch_tts_motion_falls_back_to_default_motion_plan_when_plan_generation_fails(self) -> None:
        fake = self._FakeBaseController()
        controller = object.__new__(SoulLinkLive2DController)
        controller.profile = ParameterProfile(
            parameters=[
                ParameterSpec(id="ParamAngleX", minimum=-30.0, maximum=30.0, default=0.0, role="head.x", enabled=True),
            ]
        )
        controller.sink = VtsSoulLinkSink(base_controller=fake, profile=controller.profile)
        controller.config = types.SimpleNamespace(
            tts_motion_frame_duration_ms=500,
            tts_motion_prefetch_frames=4,
            parameter_weight=0.82,
            keep_mouth_form=False,
            transition_sample_interval_ms=16,
        )
        controller._timeline_states = {}
        controller._log_warning_messages = []
        controller._log_warning = controller._log_warning_messages.append
        controller._safe_generate_motion_plan = types.MethodType(
            lambda self, **kwargs: _failed_future(ValueError("无法解析 LLM 返回的 JSON")),
            controller,
        )
        captured_actions: list[str] = []

        async def _generate_frame(self, **kwargs):
            captured_actions.append(str(kwargs["frame_plan"]["action"]))
            return {"parameters": {"ParamAngleX": 6.0}, "duration": 500}

        controller._safe_generate_tts_motion_frame_with_plan = types.MethodType(_generate_frame, controller)

        asyncio.run(
            controller._dispatch_tts_motion(
                timeline=types.SimpleNamespace(timeline_id="tl-fallback", estimated_duration_ms=1000),
                text="hello",
                audio_timeline={"audio_duration_ms": 1000},
                context="ctx",
            )
        )

        self.assertEqual(captured_actions, ["自然动作", "自然动作"])
        self.assertTrue(any("falling back to default motion plan" in message for message in controller._log_warning_messages))

    def test_dispatch_tts_motion_sanitizes_non_bmp_chars_in_frame_plan_action(self) -> None:
        fake = self._FakeBaseController()
        controller = object.__new__(SoulLinkLive2DController)
        controller.profile = ParameterProfile(
            parameters=[
                ParameterSpec(id="ParamAngleX", minimum=-30.0, maximum=30.0, default=0.0, role="head.x", enabled=True),
            ]
        )
        controller.sink = VtsSoulLinkSink(base_controller=fake, profile=controller.profile)
        controller.config = types.SimpleNamespace(
            tts_motion_frame_duration_ms=500,
            parameter_weight=0.82,
            keep_mouth_form=False,
            transition_sample_interval_ms=16,
        )
        controller._timeline_states = {}
        controller._log_warning = lambda message: None
        controller._safe_generate_motion_plan = types.MethodType(
            lambda self, **kwargs: _completed_future([{"frameIndex": 0, "action": "🎭 左转点头"}]),
            controller,
        )
        captured_actions: list[str] = []

        async def _generate_frame(self, **kwargs):
            captured_actions.append(kwargs["frame_plan"]["action"])
            return {"parameters": {"ParamAngleX": 8.0}, "duration": 500}

        controller._safe_generate_tts_motion_frame_with_plan = types.MethodType(_generate_frame, controller)
        asyncio.run(
            controller._dispatch_tts_motion(
                timeline=types.SimpleNamespace(timeline_id="tl-4", estimated_duration_ms=500),
                text="hello 🎭",
                audio_timeline={"audio_duration_ms": 500},
                context="ctx 🎭",
            )
        )
        self.assertEqual(captured_actions, [" 左转点头"])

    def test_dispatch_timeline_expression_result_reanchors_late_frame_to_current_time(self) -> None:
        profile = ParameterProfile(
            parameters=[
                ParameterSpec(id="ParamAngleX", minimum=-30.0, maximum=30.0, default=0.0, role="head.x", enabled=True),
            ]
        )
        fake = self._FakeBaseController()
        controller = object.__new__(SoulLinkLive2DController)
        controller.sink = VtsSoulLinkSink(base_controller=fake, profile=profile)
        controller.profile = profile
        controller.config = types.SimpleNamespace(
            parameter_weight=0.82,
            keep_mouth_form=False,
            transition_sample_interval_ms=16,
        )
        controller._timeline_states = {
            "tl-late": {
                "offset_ms": 500,
                "parameters": [{"id": "ParamAngleX", "value": 0.0, "weight": 0.82}],
            }
        }
        with patch(
            "plugins.maibot_bilibili_live_adapter_copy.live2d_adaptive.soullink.time.monotonic",
            return_value=10.9,
        ):
            asyncio.run(
                controller._dispatch_timeline_expression_result(
                    {"parameters": {"ParamAngleX": 12.0}, "duration": 500},
                    timeline_id="tl-late",
                    purpose="soullink-frame-late",
                    offset_ms=500,
                    fallback_duration_ms=500,
                    motion_started_at_monotonic=10.0,
                )
            )
        keyframes = fake.timeline_calls[0]["keyframes"]
        self.assertGreater(keyframes[0]["offset_ms"], 900)
        self.assertGreater(keyframes[-1]["offset_ms"], 1300)


def _completed_future(value):
    async def _coro():
        return value

    return _coro()


def _failed_future(exc: Exception):
    async def _coro():
        raise exc

    return _coro()


if __name__ == "__main__":
    unittest.main()

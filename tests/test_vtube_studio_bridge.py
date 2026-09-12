import importlib.util
import os
import sys
import unittest
import asyncio
import json
import tempfile

from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

# 依赖本机 VTube Studio 模型的测试需要通过该环境变量指定 .vtube.json 路径，未设置时对应测试自动跳过。
HIYORI_VTUBE_MODEL = Path(os.environ["VTUBE_STUDIO_TEST_MODEL"]) if os.environ.get("VTUBE_STUDIO_TEST_MODEL") else None


def _load_bridge_module():
    module_name = "test_vtube_studio_bridge_module"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    module_path = PLUGIN_ROOT / "tools" / "vtube_studio_bridge.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load vtube_studio_bridge module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


bridge_module = _load_bridge_module()

BridgeConfig = bridge_module.BridgeConfig
MaiBotVTubeStudioBridge = bridge_module.MaiBotVTubeStudioBridge
VTubeStudioClient = bridge_module.VTubeStudioClient
ModelInputBinding = getattr(bridge_module, "ModelInputBinding", None)


class _Logger:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.debugs: list[str] = []

    def info(self, message: str, *args: object) -> None:
        self.infos.append(str(message) % args if args else str(message))

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append(str(message) % args if args else str(message))

    def error(self, message: str, *args: object) -> None:
        self.errors.append(str(message) % args if args else str(message))

    def debug(self, message: str, *args: object) -> None:
        self.debugs.append(str(message) % args if args else str(message))


def _input_specs_from_vtube_model(path: Path) -> dict[str, dict[str, float | str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    settings = raw.get("ParameterSettings")
    if not isinstance(settings, list):
        return {}
    specs: dict[str, dict[str, float | str]] = {}
    for raw_setting in settings:
        if not isinstance(raw_setting, dict):
            continue
        input_name = str(raw_setting.get("Input") or "").strip()
        if not input_name:
            continue
        minimum = float(raw_setting.get("InputRangeLower", -1.0))
        maximum = float(raw_setting.get("InputRangeUpper", 1.0))
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        default_value = 0.0 if minimum <= 0.0 <= maximum else (minimum + maximum) / 2.0
        specs[input_name] = {
            "name": input_name,
            "min": minimum,
            "max": maximum,
            "default": default_value,
        }
    return specs


class _TimeoutSession:
    async def ws_connect(self, _url: str):
        raise TimeoutError()


class VTubeStudioBridgeTest(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(HIYORI_VTUBE_MODEL, "requires a local VTube Studio model via VTUBE_STUDIO_TEST_MODEL")
    def test_locate_vts_model_file_finds_vtube_json_by_model_stem(self) -> None:
        root = HIYORI_VTUBE_MODEL.parents[2]

        with mock.patch.object(bridge_module, "_candidate_vts_model_roots", return_value=[root]):
            resolved = bridge_module.locate_vts_model_file(vts_model_name="hiyori_pro_t11", model_name="Hiyori")

        self.assertEqual(resolved, HIYORI_VTUBE_MODEL)

    async def test_connect_timeout_message_is_actionable(self) -> None:
        logger = _Logger()
        client = VTubeStudioClient(BridgeConfig(vts_url="ws://127.0.0.1:8002"), logger)
        client._session = _TimeoutSession()

        with self.assertRaises(RuntimeError) as ctx:
            await client.connect()

        message = str(ctx.exception)
        self.assertIn("websocket handshake timed out", message)
        self.assertIn("Plugin API access", message)
        self.assertIn("ws://127.0.0.1:8002", message)

    async def test_bridge_start_degrades_when_vts_unavailable(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(), logger)

        async def failing_auth() -> None:
            raise RuntimeError("handshake timeout")

        bridge.vts.ensure_authenticated = failing_auth  # type: ignore[method-assign]
        await bridge.start()

        self.assertTrue(bridge._started)
        self.assertIsNotNone(bridge._inject_worker)
        self.assertTrue(any("running in degraded mode" in item for item in logger.warnings))

        await bridge.close()

    async def test_run_bridge_reports_actionable_error_when_listen_port_is_occupied(self) -> None:
        async def fake_start(_self) -> None:
            raise OSError(10048, "address already in use")

        async def fake_existing_bridge(_config, _logger) -> bool:
            return False

        with mock.patch.object(bridge_module.web.TCPSite, "start", new=fake_start):
            with mock.patch.object(bridge_module, "_existing_bridge_reachable", new=fake_existing_bridge):
                with self.assertRaises(RuntimeError) as ctx:
                    await bridge_module.run_bridge(BridgeConfig())

        message = str(ctx.exception)
        self.assertIn("18081", message)
        self.assertIn("already in use", message)
        self.assertIn("existing bridge", message)

    async def test_run_bridge_reuses_existing_bridge_when_listen_port_is_occupied_by_same_service(self) -> None:
        logger = _Logger()

        async def fake_start(_self) -> None:
            raise OSError(10048, "address already in use")

        async def fake_existing_bridge(_config, _logger) -> bool:
            return True

        with mock.patch.object(bridge_module.web.TCPSite, "start", new=fake_start):
            with mock.patch.object(bridge_module, "_existing_bridge_reachable", new=fake_existing_bridge):
                with mock.patch.object(bridge_module.logging, "getLogger", return_value=logger):
                    await bridge_module.run_bridge(BridgeConfig())

        self.assertTrue(any("already running" in item for item in logger.infos))

    def test_main_exits_cleanly_when_run_bridge_fails(self) -> None:
        logger = _Logger()

        async def fake_run_bridge(_config) -> None:
            raise RuntimeError("bridge listen port 18081 is already in use")

        with mock.patch.object(bridge_module, "parse_args", return_value=BridgeConfig()):
            with mock.patch.object(bridge_module, "run_bridge", new=fake_run_bridge):
                with mock.patch.object(bridge_module.logging, "getLogger", return_value=logger):
                    with self.assertRaises(SystemExit) as ctx:
                        bridge_module.main()

        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(any("18081" in item for item in logger.errors))

    async def test_parameter_mapping_refreshes_inputs_before_custom_creation(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(), logger)
        refresh_calls: list[str] = []

        async def fake_refresh() -> None:
            refresh_calls.append("refresh")
            bridge.mapper.update_input_names({"FaceAngleX"})

        async def fake_custom(_parameter_id: str) -> str:
            self.fail("custom parameter creation should not run when refresh recovers built-in mapping")

        bridge.refresh_input_parameters = fake_refresh  # type: ignore[method-assign]
        bridge.ensure_custom_parameter = fake_custom  # type: ignore[method-assign]

        result = await bridge._to_vts_parameter_values({"id": "ParamAngleX", "value": 0.5, "weight": 1.0})

        self.assertEqual(refresh_calls, ["refresh"])
        self.assertEqual(result, [{"id": "FaceAngleX", "value": 0.5, "weight": 1.0}])

    async def test_parameter_mapping_supports_hiyori_style_input_bindings(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(), logger)
        bridge.mapper.update_input_names(
            {
                "FaceAngleX",
                "FaceAngleY",
                "FaceAngleZ",
                "ParamBodyAngleX",
                "ParamEyeLSmile",
                "CheekPuff",
                "Brows",
            }
        )

        body = await bridge._to_vts_parameter_values({"id": "ParamBodyAngleX", "value": 0.25, "weight": 1.0})
        cheek = await bridge._to_vts_parameter_values({"id": "ParamCheek", "value": 0.6, "weight": 0.8})
        left_smile = await bridge._to_vts_parameter_values({"id": "ParamEyeLSmile", "value": 0.4, "weight": 1.0})
        brow = await bridge._to_vts_parameter_values({"id": "ParamBrowLY", "value": 0.2, "weight": 0.7})

        self.assertEqual(body, [{"id": "FaceAngleX", "value": 0.25, "weight": 1.0}])
        self.assertEqual(cheek, [{"id": "CheekPuff", "value": 0.6, "weight": 0.8}])
        self.assertEqual(left_smile, [{"id": "ParamEyeLSmile", "value": 0.4, "weight": 1.0}])
        self.assertEqual(brow, [{"id": "Brows", "value": 0.2, "weight": 0.7}])

    async def test_parameter_mapping_prefers_bound_default_inputs_over_unbound_custom_angles(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(), logger)
        bridge.mapper.update_input_names(
            {
                "FaceAngleX",
                "FaceAngleY",
                "FaceAngleZ",
                "ParamBodyAngleX",
                "ParamBodyAngleY",
                "ParamBodyAngleZ",
            }
        )

        body_x = await bridge._to_vts_parameter_values({"id": "ParamBodyAngleX", "value": 0.25, "weight": 1.0})
        body_y = await bridge._to_vts_parameter_values({"id": "ParamBodyAngleY", "value": -0.1, "weight": 0.8})
        body_z = await bridge._to_vts_parameter_values({"id": "ParamBodyAngleZ", "value": 0.4, "weight": 0.6})

        self.assertEqual(body_x, [{"id": "FaceAngleX", "value": 0.25, "weight": 1.0}])
        self.assertEqual(body_y, [{"id": "FaceAngleY", "value": -0.1, "weight": 0.8}])
        self.assertEqual(body_z, [{"id": "FaceAngleZ", "value": 0.4, "weight": 0.6}])

    async def test_parameter_mapping_uses_model_binding_ranges(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(), logger)
        bridge.mapper.update_input_names({"FaceAngleX", "FaceAngleY", "Brows"})

        with tempfile.TemporaryDirectory() as temp_dir:
            vtube_path = Path(temp_dir) / "model.vtube.json"
            vtube_path.write_text(
                json.dumps(
                    {
                        "ParameterSettings": [
                            {
                                "Input": "FaceAngleX",
                                "InputRangeLower": -30.0,
                                "InputRangeUpper": 30.0,
                                "OutputRangeLower": -10.0,
                                "OutputRangeUpper": 10.0,
                                "ClampInput": False,
                                "OutputLive2D": "ParamBodyAngleX",
                            },
                            {
                                "Input": "FaceAngleY",
                                "InputRangeLower": -20.0,
                                "InputRangeUpper": 20.0,
                                "OutputRangeLower": -30.0,
                                "OutputRangeUpper": 30.0,
                                "ClampInput": False,
                                "OutputLive2D": "ParamAngleY",
                            },
                            {
                                "Input": "Brows",
                                "InputRangeLower": 0.0,
                                "InputRangeUpper": 1.0,
                                "OutputRangeLower": -1.0,
                                "OutputRangeUpper": 1.0,
                                "ClampInput": False,
                                "OutputLive2D": "ParamBrowLY",
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            bridge.model_input_bindings = bridge_module.load_model_input_bindings(vtube_path)

        body = await bridge._to_vts_parameter_values({"id": "ParamBodyAngleX", "value": 5.0, "weight": 1.0})
        pitch = await bridge._to_vts_parameter_values({"id": "ParamAngleY", "value": 30.0, "weight": 0.8})
        brow = await bridge._to_vts_parameter_values({"id": "ParamBrowLY", "value": -1.0, "weight": 0.6})

        self.assertEqual(body, [{"id": "FaceAngleX", "value": 15.0, "weight": 1.0}])
        self.assertEqual(pitch, [{"id": "FaceAngleY", "value": 20.0, "weight": 0.8}])
        self.assertEqual(brow, [{"id": "Brows", "value": 0.0, "weight": 0.6}])

    @unittest.skipUnless(HIYORI_VTUBE_MODEL, "requires a local VTube Studio model via VTUBE_STUDIO_TEST_MODEL")
    async def test_model_bound_positive_only_inputs_are_clamped_to_safe_range(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(), logger)
        bridge.mapper.update_input_names({"MouthSmile"})
        bridge.model_input_bindings = bridge_module.load_model_input_bindings(HIYORI_VTUBE_MODEL)

        cheek = await bridge._to_vts_parameter_values({"id": "ParamCheek", "value": 0.1, "weight": 0.8})

        self.assertEqual(cheek, [{"id": "MouthSmile", "value": 0.0, "weight": 0.8}])

    async def test_parameter_mapping_prefers_model_bound_custom_eye_inputs(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(), logger)
        bridge.mapper.update_input_names({"EyeLeftX", "EyeRightX", "ParamEyeBallX"})
        bridge.model_input_bindings = {
            "ParamEyeBallX": ModelInputBinding(
                input_parameter="ParamEyeBallX",
                input_range_lower=-1.0,
                input_range_upper=1.0,
                output_range_lower=-1.0,
                output_range_upper=1.0,
                clamp_input=False,
            )
        }

        eye = await bridge._to_vts_parameter_values({"id": "ParamEyeBallX", "value": 0.55, "weight": 1.0})

        self.assertEqual(eye, [{"id": "ParamEyeBallX", "value": 0.55, "weight": 1.0}])

    @unittest.skipUnless(HIYORI_VTUBE_MODEL, "requires a local VTube Studio model via VTUBE_STUDIO_TEST_MODEL")
    def test_current_hiyori_model_binds_shoulder_custom_input(self) -> None:
        self.assertTrue(HIYORI_VTUBE_MODEL.exists())
        bindings = bridge_module.load_model_input_bindings(HIYORI_VTUBE_MODEL)

        self.assertIn("ParamShoulder", bindings)
        self.assertEqual(bindings["ParamShoulder"].input_parameter, "ParamShoulder")
        self.assertEqual(bindings["ParamShoulder"].output_range_lower, -1.0)
        self.assertEqual(bindings["ParamShoulder"].output_range_upper, 1.0)

    @unittest.skipUnless(HIYORI_VTUBE_MODEL, "requires a local VTube Studio model via VTUBE_STUDIO_TEST_MODEL")
    def test_current_hiyori_model_binds_plugin_local_mouth_inputs(self) -> None:
        self.assertTrue(HIYORI_VTUBE_MODEL.exists())
        bindings = bridge_module.load_model_input_bindings(HIYORI_VTUBE_MODEL)

        self.assertIn("ParamMouthOpenY", bindings)
        self.assertIn("ParamMouthForm", bindings)
        self.assertEqual(bindings["ParamMouthOpenY"].input_parameter, "MouthOpen")
        self.assertEqual(bindings["ParamMouthForm"].input_parameter, "MouthX")
        self.assertEqual(bindings["ParamMouthOpenY"].output_range_upper, 1.0)

    @unittest.skipUnless(HIYORI_VTUBE_MODEL, "requires a local VTube Studio model via VTUBE_STUDIO_TEST_MODEL")
    def test_current_hiyori_model_binds_custom_eye_inputs(self) -> None:
        self.assertTrue(HIYORI_VTUBE_MODEL.exists())
        bindings = bridge_module.load_model_input_bindings(HIYORI_VTUBE_MODEL)

        self.assertIn("ParamEyeBallX", bindings)
        self.assertIn("ParamEyeBallY", bindings)
        self.assertEqual(bindings["ParamEyeBallX"].input_parameter, "ParamEyeBallX")
        self.assertEqual(bindings["ParamEyeBallY"].input_parameter, "ParamEyeBallY")

    @unittest.skipUnless(HIYORI_VTUBE_MODEL, "requires a local VTube Studio model via VTUBE_STUDIO_TEST_MODEL")
    def test_current_hiyori_model_binds_ahoge_companion_inputs(self) -> None:
        self.assertTrue(HIYORI_VTUBE_MODEL.exists())
        bindings = bridge_module.load_model_input_bindings(HIYORI_VTUBE_MODEL)

        self.assertIn("ParamHairAhoge", bindings)
        self.assertIn("ParamHairFront", bindings)
        self.assertIn("ParamHairBack", bindings)
        self.assertIn("ParamRibbon", bindings)
        self.assertIn("ParamSideupRibbon", bindings)
        self.assertEqual(bindings["ParamHairAhoge"].input_parameter, "ParamHairAhoge")
        self.assertEqual(bindings["ParamHairFront"].input_parameter, "ParamHairFront")
        self.assertEqual(bindings["ParamHairBack"].input_parameter, "ParamHairBack")
        self.assertEqual(bindings["ParamRibbon"].input_parameter, "ParamRibbon")
        self.assertEqual(bindings["ParamSideupRibbon"].input_parameter, "ParamSideupRibbon")

    @unittest.skipUnless(HIYORI_VTUBE_MODEL, "requires a local VTube Studio model via VTUBE_STUDIO_TEST_MODEL")
    def test_current_hiyori_model_binds_independent_body_inputs(self) -> None:
        self.assertTrue(HIYORI_VTUBE_MODEL.exists())
        bindings = bridge_module.load_model_input_bindings(HIYORI_VTUBE_MODEL)

        self.assertIn("ParamBodyAngleX", bindings)
        self.assertIn("ParamBodyAngleY", bindings)
        self.assertIn("ParamBodyAngleZ", bindings)
        self.assertEqual(bindings["ParamBodyAngleX"].input_parameter, "ParamBodyAngleX")
        self.assertEqual(bindings["ParamBodyAngleY"].input_parameter, "ParamBodyAngleY")
        self.assertEqual(bindings["ParamBodyAngleZ"].input_parameter, "ParamBodyAngleZ")

    async def test_parameter_injection_keeps_strongest_shared_input_value(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(), logger)
        bridge.mapper.update_input_names({"FaceAngleX"})
        bridge.model_input_bindings = {
            "ParamAngleX": ModelInputBinding(
                input_parameter="FaceAngleX",
                input_range_lower=-30.0,
                input_range_upper=30.0,
                output_range_lower=-30.0,
                output_range_upper=30.0,
                clamp_input=False,
            ),
            "ParamBodyAngleX": ModelInputBinding(
                input_parameter="FaceAngleX",
                input_range_lower=-30.0,
                input_range_upper=30.0,
                output_range_lower=-10.0,
                output_range_upper=10.0,
                clamp_input=False,
            ),
        }

        result = await bridge.inject_event_parameters(
            {
                "parameters": [
                    {"id": "ParamAngleX", "value": 9.0, "weight": 1.0},
                    {"id": "ParamBodyAngleX", "value": 1.0, "weight": 1.0},
                ]
            }
        )

        self.assertEqual(result["parameter_values"], [{"id": "FaceAngleX", "value": 9.0, "weight": 1.0}])
        self.assertEqual(bridge._active_parameter_values["FaceAngleX"], {"id": "FaceAngleX", "value": 9.0, "weight": 1.0})

    async def test_parameter_injection_averages_shared_positive_only_expression_inputs(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(), logger)
        bridge.input_parameter_specs = {"Brows": {"name": "Brows", "min": 0.0, "max": 1.0, "default": 0.5}}

        merged = bridge._merge_parameter_values(
            [
                {"id": "Brows", "value": 0.62, "weight": 0.8},
                {"id": "Brows", "value": 0.48, "weight": 0.6},
                {"id": "Brows", "value": 0.56, "weight": 1.0},
            ]
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "Brows")
        self.assertAlmostEqual(float(merged[0]["value"]), 0.56, places=4)
        self.assertEqual(float(merged[0]["weight"]), 1.0)

    async def test_idle_contribution_yields_to_non_idle_on_same_target(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=True), logger)
        bridge.input_parameter_specs = {
            "EyeOpenLeft": {"name": "EyeOpenLeft", "min": 0.0, "max": 1.0, "default": 1.0},
        }
        bridge.mapper.update_input_names({"EyeOpenLeft"})

        await bridge.inject_event_parameters(
            {
                "type": "live2d.parameters",
                "purpose": "idle-sway",
                "duration_ms": 600,
                "parameters": [{"id": "ParamEyeLOpen", "value": 0.85, "weight": 1.0}],
            }
        )
        await bridge.inject_event_parameters(
            {
                "type": "live2d.parameters",
                "purpose": "blink",
                "duration_ms": 120,
                "parameters": [{"id": "ParamEyeLOpen", "value": 0.05, "weight": 1.0}],
            }
        )

        parameter_values = bridge._compose_active_parameter_values(now=bridge_module.time.monotonic())

        self.assertEqual(parameter_values, [{"id": "EyeOpenLeft", "value": 0.05, "weight": 1.0}])

    async def test_idle_contribution_remains_when_no_active_override_exists(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=True), logger)
        bridge.input_parameter_specs = {
            "BodyAngleX": {"name": "BodyAngleX", "min": -30.0, "max": 30.0, "default": 0.0},
        }
        bridge.mapper.update_input_names({"BodyAngleX"})

        await bridge.inject_event_parameters(
            {
                "type": "live2d.parameters",
                "purpose": "idle-sway",
                "duration_ms": 900,
                "parameters": [{"id": "ParamBodyAngleX", "value": 4.0, "weight": 0.7}],
            }
        )

        parameter_values = bridge._compose_active_parameter_values(now=bridge_module.time.monotonic())

        self.assertEqual(parameter_values, [{"id": "BodyAngleX", "value": 4.0, "weight": 0.7}])

    async def test_non_idle_merge_behavior_stays_unchanged_after_idle_filtering(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=True), logger)
        bridge.input_parameter_specs = {"Brows": {"name": "Brows", "min": 0.0, "max": 1.0, "default": 0.5}}
        bridge.mapper.update_input_names({"Brows"})

        await bridge.inject_event_parameters(
            {
                "type": "live2d.parameters",
                "purpose": "idle-sway",
                "duration_ms": 900,
                "parameters": [{"id": "ParamBrowLY", "value": 0.08, "weight": 0.3}],
            }
        )
        await bridge.inject_event_parameters(
            {
                "type": "live2d.parameters",
                "purpose": "expressive",
                "duration_ms": 320,
                "parameters": [
                    {"id": "ParamBrowLY", "value": 0.40, "weight": 0.5},
                    {"id": "ParamBrowRY", "value": 0.75, "weight": 1.0},
                ],
            }
        )

        parameter_values = bridge._compose_active_parameter_values(now=bridge_module.time.monotonic())

        self.assertEqual(len(parameter_values), 1)
        self.assertEqual(parameter_values[0]["id"], "Brows")
        self.assertAlmostEqual(float(parameter_values[0]["value"]), 0.6333333333333333, places=4)
        self.assertEqual(float(parameter_values[0]["weight"]), 1.0)

    async def test_expired_active_contribution_falls_back_to_idle(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=True), logger)
        bridge.input_parameter_specs = {
            "EyeOpenLeft": {"name": "EyeOpenLeft", "min": 0.0, "max": 1.0, "default": 1.0},
        }
        bridge.mapper.update_input_names({"EyeOpenLeft"})

        with mock.patch.object(bridge_module.time, "monotonic", side_effect=[10.0, 10.0, 10.0, 10.0, 10.3]):
            await bridge.inject_event_parameters(
                {
                    "type": "live2d.parameters",
                    "purpose": "idle-sway",
                    "duration_ms": 1000,
                    "parameters": [{"id": "ParamEyeLOpen", "value": 0.80, "weight": 1.0}],
                }
            )
            await bridge.inject_event_parameters(
                {
                    "type": "live2d.parameters",
                    "purpose": "wink",
                    "duration_ms": 120,
                    "parameters": [{"id": "ParamEyeLOpen", "value": 0.05, "weight": 1.0}],
                }
            )
            parameter_values = bridge._compose_active_parameter_values(now=10.3)

        self.assertEqual(parameter_values, [{"id": "EyeOpenLeft", "value": 0.80, "weight": 1.0}])

    def test_soullink_frame_purposes_share_single_motion_lane(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=True), logger)

        self.assertEqual(bridge._resolve_source_lane({"purpose": "soullink-frame-1"}), "soullink-motion")
        self.assertEqual(bridge._resolve_source_lane({"purpose": "soullink-frame-9"}), "soullink-motion")
        self.assertEqual(
            bridge._resolve_parameter_lane({"purpose": "soullink-frame-4"}, "MouthX"),
            "mouth",
        )

    async def test_newer_soullink_frame_replaces_older_same_motion_lane(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=True), logger)
        bridge.input_parameter_specs = {
            "BodyAngleX": {"name": "BodyAngleX", "min": -30.0, "max": 30.0, "default": 0.0},
        }
        bridge.mapper.update_input_names({"BodyAngleX"})

        await bridge.inject_event_parameters(
            {
                "type": "live2d.parameters",
                "timeline_id": "soullink-clip-a",
                "purpose": "soullink-frame-8",
                "duration_ms": 120,
                "parameters": [{"id": "ParamBodyAngleX", "value": 6.0, "weight": 1.0}],
            },
            now=10.0,
            trigger_wake=False,
        )
        await bridge.inject_event_parameters(
            {
                "type": "live2d.parameters",
                "timeline_id": "soullink-clip-b",
                "purpose": "soullink-frame-10",
                "duration_ms": 120,
                "parameters": [{"id": "ParamBodyAngleX", "value": 1.0, "weight": 1.0}],
            },
            now=10.1,
            trigger_wake=False,
        )

        parameter_values = bridge._compose_active_parameter_values(now=10.1)

        self.assertEqual(parameter_values, [{"id": "BodyAngleX", "value": 1.0, "weight": 1.0}])
        self.assertEqual(len(bridge._active_parameter_contributions["BodyAngleX"]), 1)
        self.assertEqual(bridge._active_parameter_contributions["BodyAngleX"][0].lane, "soullink-motion")

    async def test_soullink_mouth_form_shares_lane_with_lipsync(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=True), logger)
        bridge.input_parameter_specs = {
            "MouthX": {"name": "MouthX", "min": -1.0, "max": 1.0, "default": 0.0},
        }
        bridge.mapper.update_input_names({"MouthX"})

        await bridge.inject_event_parameters(
            {
                "type": "live2d.parameters",
                "timeline_id": "lipsync-mouth",
                "purpose": "lipsync",
                "duration_ms": 120,
                "parameters": [{"id": "ParamMouthForm", "value": 0.25, "weight": 1.0}],
            },
            now=10.0,
            trigger_wake=False,
        )
        await bridge.inject_event_parameters(
            {
                "type": "live2d.parameters",
                "timeline_id": "soullink-mouth",
                "purpose": "soullink-frame-11",
                "duration_ms": 120,
                "parameters": [{"id": "ParamMouthForm", "value": 0.75, "weight": 1.0}],
            },
            now=10.02,
            trigger_wake=False,
        )

        parameter_values = bridge._compose_active_parameter_values(now=10.02)

        self.assertEqual(parameter_values, [{"id": "MouthX", "value": 0.75, "weight": 1.0}])
        self.assertEqual(len(bridge._active_parameter_contributions["MouthX"]), 1)
        self.assertEqual(bridge._active_parameter_contributions["MouthX"][0].lane, "mouth")

    async def test_newer_same_lane_contribution_replaces_older_motion_value(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=True), logger)
        bridge.input_parameter_specs = {
            "BodyAngleX": {"name": "BodyAngleX", "min": -30.0, "max": 30.0, "default": 0.0},
        }
        bridge.mapper.update_input_names({"BodyAngleX"})

        with mock.patch.object(bridge_module.time, "monotonic", side_effect=[10.0, 10.0, 10.1, 10.1, 10.1]):
            await bridge.inject_event_parameters(
                {
                    "type": "live2d.parameters",
                    "timeline_id": "expressive-old",
                    "purpose": "expressive",
                    "duration_ms": 600,
                    "parameters": [{"id": "ParamBodyAngleX", "value": 6.0, "weight": 1.0}],
                }
            )
            await bridge.inject_event_parameters(
                {
                    "type": "live2d.parameters",
                    "timeline_id": "expressive-new",
                    "purpose": "expressive",
                    "duration_ms": 600,
                    "parameters": [{"id": "ParamBodyAngleX", "value": 1.0, "weight": 1.0}],
                }
            )
            parameter_values = bridge._compose_active_parameter_values(now=10.1)

        self.assertEqual(parameter_values, [{"id": "BodyAngleX", "value": 1.0, "weight": 1.0}])

    async def test_eye_open_prefers_latest_active_value_instead_of_averaging_sources(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=True), logger)
        bridge.input_parameter_specs = {
            "EyeOpenLeft": {"name": "EyeOpenLeft", "min": 0.0, "max": 1.0, "default": 1.0},
        }
        bridge.mapper.update_input_names({"EyeOpenLeft"})

        with mock.patch.object(bridge_module.time, "monotonic", side_effect=[10.0, 10.0, 10.1, 10.1, 10.1]):
            await bridge.inject_event_parameters(
                {
                    "type": "live2d.parameters",
                    "timeline_id": "expressive-eyes",
                    "purpose": "expressive",
                    "duration_ms": 600,
                    "parameters": [{"id": "ParamEyeLOpen", "value": 0.20, "weight": 1.0}],
                }
            )
            await bridge.inject_event_parameters(
                {
                    "type": "live2d.parameters",
                    "timeline_id": "speech-eyes",
                    "purpose": "speech",
                    "duration_ms": 600,
                    "parameters": [{"id": "ParamEyeLOpen", "value": 0.90, "weight": 1.0}],
                }
            )
            parameter_values = bridge._compose_active_parameter_values(now=10.1)

        self.assertEqual(parameter_values, [{"id": "EyeOpenLeft", "value": 0.90, "weight": 1.0}])

    @unittest.skipUnless(HIYORI_VTUBE_MODEL, "requires a local VTube Studio model via VTUBE_STUDIO_TEST_MODEL")
    async def test_current_hiyori_shared_expression_inputs_preserve_emotion_direction(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(), logger)
        bridge.model_input_bindings = bridge_module.load_model_input_bindings(HIYORI_VTUBE_MODEL)
        bridge.input_parameter_specs = _input_specs_from_vtube_model(HIYORI_VTUBE_MODEL)
        bridge.mapper.update_input_names(set(bridge.input_parameter_specs))

        async def convert(raw_parameters: list[dict[str, float | str]]) -> dict[str, float]:
            converted: list[dict[str, float | str]] = []
            for raw_parameter in raw_parameters:
                converted.extend(await bridge._to_vts_parameter_values(raw_parameter))
            merged = bridge._merge_parameter_values(converted)
            return {
                str(item["id"]): float(item["value"])
                for item in merged
            }

        base = await convert(
            [
                {"id": "ParamEyeLSmile", "value": 0.1634, "weight": 0.75},
                {"id": "ParamEyeRSmile", "value": 0.1634, "weight": 0.75},
                {"id": "ParamCheek", "value": 0.0871, "weight": 0.75},
                {"id": "ParamBrowLY", "value": 0.1574, "weight": 0.75},
                {"id": "ParamBrowRY", "value": 0.1574, "weight": 0.75},
                {"id": "ParamBrowLForm", "value": 0.0110, "weight": 0.75},
                {"id": "ParamBrowRForm", "value": 0.0110, "weight": 0.75},
            ]
        )
        happy = await convert(
            [
                {"id": "ParamEyeLSmile", "value": 0.2822, "weight": 0.75},
                {"id": "ParamEyeRSmile", "value": 0.2822, "weight": 0.75},
                {"id": "ParamCheek", "value": 0.1858, "weight": 0.75},
                {"id": "ParamBrowLY", "value": 0.2331, "weight": 0.75},
                {"id": "ParamBrowRY", "value": 0.2331, "weight": 0.75},
                {"id": "ParamBrowLForm", "value": 0.0688, "weight": 0.75},
                {"id": "ParamBrowRForm", "value": 0.0688, "weight": 0.75},
            ]
        )
        sad = await convert(
            [
                {"id": "ParamEyeLSmile", "value": 0.0882, "weight": 0.75},
                {"id": "ParamEyeRSmile", "value": 0.0882, "weight": 0.75},
                {"id": "ParamCheek", "value": 0.0422, "weight": 0.75},
                {"id": "ParamBrowLY", "value": 0.0472, "weight": 0.75},
                {"id": "ParamBrowRY", "value": 0.0472, "weight": 0.75},
                {"id": "ParamBrowLForm", "value": -0.1022, "weight": 0.75},
                {"id": "ParamBrowRForm", "value": -0.1022, "weight": 0.75},
            ]
        )

        self.assertIn("MouthSmile", base)
        self.assertIn("Brows", base)
        self.assertGreaterEqual(base["MouthSmile"], 0.0)
        self.assertGreater(happy["MouthSmile"], base["MouthSmile"] + 0.05)
        self.assertGreaterEqual(sad["MouthSmile"], 0.0)
        self.assertGreater(happy["Brows"], base["Brows"] + 0.02)
        self.assertLess(sad["Brows"], base["Brows"] - 0.04)

    async def test_eye_smile_does_not_fallback_to_mouth_smile(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(), logger)
        bridge.config.create_custom_parameters = False
        bridge.mapper.update_input_names({"MouthSmile"})

        left_smile = await bridge._to_vts_parameter_values({"id": "ParamEyeLSmile", "value": 0.4, "weight": 1.0})
        right_smile = await bridge._to_vts_parameter_values({"id": "ParamEyeRSmile", "value": 0.5, "weight": 1.0})

        self.assertEqual(left_smile, [])
        self.assertEqual(right_smile, [])

    async def test_timeline_control_keeps_reinjecting_latest_pose(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=False), logger)
        bridge.mapper.update_input_names({"FaceAngleX"})
        inject_calls: list[list[dict[str, object]]] = []

        async def fake_request(message_type: str, data=None, **_kwargs):
            if message_type == "InjectParameterDataRequest":
                inject_calls.append(list((data or {}).get("parameterValues") or []))
            return {"messageType": message_type.replace("Request", "Response"), "data": {}}

        async def fake_auth() -> None:
            return None

        async def fake_refresh() -> None:
            return None

        bridge.vts.request = fake_request  # type: ignore[method-assign]
        bridge.vts.ensure_authenticated = fake_auth  # type: ignore[method-assign]
        bridge.refresh_input_parameters = fake_refresh  # type: ignore[method-assign]

        await bridge.start()
        bridge._schedule_or_inject(
            {
                "type": "live2d.timeline.frame",
                "timeline_id": "idle-loop",
                "offset_ms": 0,
                "parameters": [{"id": "ParamAngleX", "value": 1.25, "weight": 1.0}],
            }
        )
        await asyncio.sleep(0.09)
        await bridge.close()

        self.assertGreaterEqual(len(inject_calls), 2)
        self.assertTrue(
            all(call == [{"id": "FaceAngleX", "value": 1.25, "weight": 1.0}] for call in inject_calls),
        )

    async def test_clock_cycle_merges_all_due_payloads_before_single_injection(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=False), logger)
        bridge.input_parameter_specs = {
            "FaceAngleX": {"name": "FaceAngleX", "min": -30.0, "max": 30.0, "default": 0.0},
        }
        bridge.mapper.update_input_names({"FaceAngleX"})
        inject_calls: list[list[dict[str, object]]] = []

        async def fake_request(message_type: str, data=None, **_kwargs):
            if message_type == "InjectParameterDataRequest":
                inject_calls.append(list((data or {}).get("parameterValues") or []))
            return {"messageType": message_type.replace("Request", "Response"), "data": {}}

        bridge.vts.request = fake_request  # type: ignore[method-assign]
        bridge._start_inject_worker = lambda: None  # type: ignore[method-assign]
        bridge._start_output_worker = lambda: None  # type: ignore[method-assign]

        with mock.patch.object(bridge_module.time, "monotonic", return_value=10.0):
            bridge._schedule_payload(
                {
                    "type": "live2d.parameters",
                    "timeline_id": "a",
                    "parameters": [{"id": "ParamAngleX", "value": 1.0, "weight": 1.0}],
                }
            )
            bridge._schedule_payload(
                {
                    "type": "live2d.parameters",
                    "timeline_id": "b",
                    "parameters": [{"id": "ParamAngleX", "value": 4.0, "weight": 1.0}],
                }
            )

        await bridge._run_clock_cycle(now=10.0)

        self.assertEqual(len(inject_calls), 1)
        self.assertEqual(inject_calls[0], [{"id": "FaceAngleX", "value": 4.0, "weight": 1.0}])

    async def test_push_active_parameters_skips_immediate_unchanged_duplicate_batch(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=False), logger)
        bridge.input_parameter_specs = {
            "FaceAngleX": {"name": "FaceAngleX", "min": -30.0, "max": 30.0, "default": 0.0},
        }
        bridge.mapper.update_input_names({"FaceAngleX"})
        inject_calls: list[list[dict[str, object]]] = []

        async def fake_request(message_type: str, data=None, **_kwargs):
            if message_type == "InjectParameterDataRequest":
                inject_calls.append(list((data or {}).get("parameterValues") or []))
            return {"messageType": message_type.replace("Request", "Response"), "data": {}}

        bridge.vts.request = fake_request  # type: ignore[method-assign]

        await bridge.inject_event_parameters(
            {
                "type": "live2d.parameters",
                "timeline_id": "dedup-test",
                "purpose": "soullink-frame-1",
                "duration_ms": 120,
                "parameters": [{"id": "ParamAngleX", "value": 2.0, "weight": 1.0}],
            },
            now=10.0,
            trigger_wake=False,
        )

        first = await bridge._push_active_parameters(now=10.0)
        second = await bridge._push_active_parameters(now=10.002)

        self.assertTrue(first["success"])
        self.assertEqual(second, {"success": True, "skipped": True, "reason": "duplicate_parameter_batch"})
        self.assertEqual(len(inject_calls), 1)
        self.assertEqual(inject_calls[0], [{"id": "FaceAngleX", "value": 2.0, "weight": 1.0}])

    async def test_push_active_parameters_interpolates_toward_target_value(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=False), logger)
        bridge.input_parameter_specs = {
            "FaceAngleX": {"name": "FaceAngleX", "min": -30.0, "max": 30.0, "default": 0.0},
        }
        bridge.mapper.update_input_names({"FaceAngleX"})
        inject_calls: list[list[dict[str, object]]] = []

        async def fake_request(message_type: str, data=None, **_kwargs):
            if message_type == "InjectParameterDataRequest":
                inject_calls.append(list((data or {}).get("parameterValues") or []))
            return {"messageType": message_type.replace("Request", "Response"), "data": {}}

        bridge.vts.request = fake_request  # type: ignore[method-assign]

        await bridge.inject_event_parameters(
            {
                "type": "live2d.parameters",
                "timeline_id": "interp-a",
                "purpose": "soullink-frame-1",
                "duration_ms": 120,
                "parameters": [{"id": "ParamAngleX", "value": 0.0, "weight": 1.0}],
            },
            now=10.0,
            trigger_wake=False,
        )
        await bridge._push_active_parameters(now=10.0)

        await bridge.inject_event_parameters(
            {
                "type": "live2d.parameters",
                "timeline_id": "interp-b",
                "purpose": "soullink-frame-2",
                "duration_ms": 120,
                "parameters": [{"id": "ParamAngleX", "value": 12.0, "weight": 1.0}],
            },
            now=10.016,
            trigger_wake=False,
        )
        await bridge._push_active_parameters(now=10.016)

        self.assertEqual(len(inject_calls), 2)
        self.assertEqual(inject_calls[0], [{"id": "FaceAngleX", "value": 0.0, "weight": 1.0}])
        second_value = float(inject_calls[1][0]["value"])
        self.assertGreater(second_value, 0.0)
        self.assertLess(second_value, 12.0)

    def test_bridge_tick_interval_matches_16ms_authored_keyframes(self) -> None:
        self.assertLessEqual(bridge_module.TIMELINE_INTERPOLATION_INTERVAL_MS, 16)

    async def test_timeline_frame_without_duration_expires_within_two_bridge_ticks(self) -> None:
        logger = _Logger()
        bridge = MaiBotVTubeStudioBridge(BridgeConfig(dry_run=True), logger)
        bridge.input_parameter_specs = {
            "FaceAngleX": {"name": "FaceAngleX", "min": -30.0, "max": 30.0, "default": 0.0},
        }
        bridge.mapper.update_input_names({"FaceAngleX"})

        with mock.patch.object(bridge_module.time, "monotonic", side_effect=[10.0, 10.0, 10.0, 10.0, 10.021, 10.050]):
            await bridge.inject_event_parameters(
                {
                    "type": "live2d.timeline.frame",
                    "timeline_id": "speech-clip",
                    "offset_ms": 0,
                    "parameters": [{"id": "ParamAngleX", "value": 3.0, "weight": 1.0}],
                }
            )
            still_active = bridge._compose_active_parameter_values(now=10.021)
            expired = bridge._compose_active_parameter_values(now=10.050)

        self.assertEqual(still_active, [{"id": "FaceAngleX", "value": 3.0, "weight": 1.0}])
        self.assertEqual(expired, [])


if __name__ == "__main__":
    unittest.main()

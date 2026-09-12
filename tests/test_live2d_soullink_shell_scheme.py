import builtins
import sys
import types
import unittest

from unittest.mock import patch

from . import _host_bootstrap  # noqa: F401

from plugins.maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from plugins.maibot_bilibili_live_adapter_copy.live2d_adaptive.profile import ParameterProfile
from plugins.maibot_bilibili_live_adapter_copy.live2d_adaptive.soullink import ShellSoulLinkSink, VtsSoulLinkSink
from plugins.maibot_bilibili_live_adapter_copy.plugin import BilibiliLiveAdapterPlugin
from plugins.maibot_bilibili_live_adapter_copy.live2d_adaptive.soullink import resolve_live2d_scheme


class _LoggerStub:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, message: str) -> None:
        self.infos.append(str(message))

    def warning(self, message: str) -> None:
        self.warnings.append(str(message))


class _BridgeStub:
    def __init__(self, **kwargs) -> None:
        self.kwargs = dict(kwargs)
        self.started = False

    async def start(self) -> None:
        self.started = True


class _ProbeStub:
    def __init__(self, bridge, logger=None) -> None:
        self.bridge = bridge
        self.logger = logger

    async def discover(self, **kwargs):
        return ParameterProfile(model_id="hiyori", model_name="Hiyori")


class _ControllerStub:
    def __init__(self, **kwargs) -> None:
        self.kwargs = dict(kwargs)
        self.embodied_mode = bool(kwargs.get("embodied_mode"))
        self.set_embodied_mode_calls: list[bool] = []
        self.started = False

    async def set_embodied_mode(self, enabled: bool) -> None:
        self.embodied_mode = bool(enabled)
        self.set_embodied_mode_calls.append(bool(enabled))

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False


class _SoulLinkControllerStub:
    def __init__(self, *, base_controller, profile, config, sink, logger=None) -> None:
        self.base_controller = base_controller
        self.profile = profile
        self.config = config
        self.sink = sink
        self.logger = logger
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False


class _FailingSoulLinkControllerStub(_SoulLinkControllerStub):
    async def start(self) -> None:
        raise RuntimeError("soullink controller failed")


class _ShellRuntimeStub:
    instances: list["_ShellRuntimeStub"] = []
    start_exception: Exception | None = None

    def __init__(self, *, config, logger=None) -> None:
        self.config = config
        self.logger = logger
        self.started = False
        self.stop_calls = 0
        type(self).instances.append(self)

    async def start(self) -> None:
        if type(self).start_exception is not None:
            raise type(self).start_exception
        self.started = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.started = False

    async def broadcast(self, payload) -> None:
        return None


def _make_settings(*, fallback_to_vts: bool = True) -> LiveAdapterSettings:
    return LiveAdapterSettings.model_validate(
        {
            "live2d": {
                "enabled": True,
                "scheme": "soullink_shell",
                "driver": "json",
                "soullink": {"enabled": True},
                "soullink_shell": {
                    "enabled": True,
                    "fallback_to_vts": fallback_to_vts,
                },
            }
        }
    )


class SoulLinkShellSchemeTest(unittest.TestCase):
    def test_resolve_live2d_scheme_accepts_soullink_shell(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "live2d": {
                    "scheme": "soullink_shell",
                    "soullink": {"enabled": True},
                    "embodied": {"enabled": True},
                }
            }
        )

        self.assertEqual(resolve_live2d_scheme(settings.live2d), "soullink_shell")

    def test_soullink_shell_config_defaults_safe_off(self) -> None:
        settings = LiveAdapterSettings.model_validate({})

        self.assertFalse(settings.live2d.soullink_shell.enabled)
        self.assertEqual(settings.live2d.soullink_shell.host, "127.0.0.1")
        self.assertEqual(settings.live2d.soullink_shell.port, 18183)
        self.assertTrue(settings.live2d.soullink_shell.click_through_default)
        self.assertTrue(settings.live2d.soullink_shell.always_on_top)
        self.assertTrue(settings.live2d.soullink_shell.remember_window_geometry)
        self.assertFalse(settings.live2d.soullink_shell.start_interactive)
        self.assertFalse(settings.live2d.soullink_shell.open_devtools)
        self.assertTrue(settings.live2d.soullink_shell.fallback_to_vts)

    def test_live2d_scheme_description_mentions_soullink_shell(self) -> None:
        self.assertIn("soullink_shell", LiveAdapterSettings.model_fields["live2d"].annotation.model_fields["scheme"].description)


class SoulLinkShellStartupFallbackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _ShellRuntimeStub.instances.clear()
        _ShellRuntimeStub.start_exception = None

    async def test_missing_shell_runtime_module_falls_back_even_when_fallback_disabled(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        logger = _LoggerStub()
        settings = _make_settings(fallback_to_vts=False)
        module_name = "plugins.maibot_bilibili_live_adapter_copy.live2d_shell_runtime"
        previous_module = sys.modules.pop(module_name, None)
        original_import = builtins.__import__

        def _missing_shell_import(name, globals=None, locals=None, fromlist=(), level=0):
            target_name = "plugins.maibot_bilibili_live_adapter_copy.live2d_shell_runtime"
            if name == target_name or (level == 1 and name == "live2d_shell_runtime"):
                raise ModuleNotFoundError(name=target_name)
            return original_import(name, globals, locals, fromlist, level)

        try:
            with patch("builtins.__import__", side_effect=_missing_shell_import):
                with patch.object(plugin, "_logger", return_value=logger):
                    with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.JsonLive2DBridge", _BridgeStub):
                        with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.CapabilityProbe", _ProbeStub):
                            with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.Live2DController", _ControllerStub):
                                with patch(
                                    "plugins.maibot_bilibili_live_adapter_copy.plugin.SoulLinkLive2DController",
                                    _SoulLinkControllerStub,
                                ):
                                    controller = await plugin._build_live2d_controller(settings)
        finally:
            if previous_module is not None:
                sys.modules[module_name] = previous_module

        self.assertIsInstance(controller, _SoulLinkControllerStub)
        self.assertTrue(controller.started)
        self.assertTrue(controller.base_controller.embodied_mode)
        self.assertIsNone(plugin._soullink_shell_runtime)
        self.assertTrue(any("shell unavailable in this build" in message for message in logger.warnings))

    async def test_shell_runtime_start_failure_uses_normal_soullink_controller_path(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        logger = _LoggerStub()
        settings = _make_settings(fallback_to_vts=True)
        module_name = "plugins.maibot_bilibili_live_adapter_copy.live2d_shell_runtime"
        runtime_module = types.ModuleType(module_name)
        runtime_module.SoulLinkShellRuntime = _ShellRuntimeStub
        _ShellRuntimeStub.start_exception = RuntimeError("shell bind failed")

        with patch.dict(sys.modules, {module_name: runtime_module}):
            with patch.object(plugin, "_logger", return_value=logger):
                with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.JsonLive2DBridge", _BridgeStub):
                    with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.CapabilityProbe", _ProbeStub):
                        with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.Live2DController", _ControllerStub):
                            with patch(
                                "plugins.maibot_bilibili_live_adapter_copy.plugin.SoulLinkLive2DController",
                                _SoulLinkControllerStub,
                            ):
                                controller = await plugin._build_live2d_controller(settings)

        self.assertIsInstance(controller, _SoulLinkControllerStub)
        self.assertTrue(controller.started)
        self.assertTrue(controller.base_controller.embodied_mode)
        self.assertIsNone(plugin._soullink_shell_runtime)
        self.assertTrue(any("falling back to current soullink/VTS path" in message for message in logger.warnings))

    async def test_soullink_shell_scheme_uses_shell_sink(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        logger = _LoggerStub()
        settings = _make_settings(fallback_to_vts=True)
        module_name = "plugins.maibot_bilibili_live_adapter_copy.live2d_shell_runtime"
        runtime_module = types.ModuleType(module_name)
        runtime_module.SoulLinkShellRuntime = _ShellRuntimeStub

        with patch.dict(sys.modules, {module_name: runtime_module}):
            with patch.object(plugin, "_logger", return_value=logger):
                with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.JsonLive2DBridge", _BridgeStub):
                    with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.CapabilityProbe", _ProbeStub):
                        with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.Live2DController", _ControllerStub):
                            with patch(
                                "plugins.maibot_bilibili_live_adapter_copy.plugin.SoulLinkLive2DController",
                                _SoulLinkControllerStub,
                            ):
                                controller = await plugin._build_live2d_controller(settings)

        self.assertIsInstance(controller, _SoulLinkControllerStub)
        self.assertIsInstance(controller.sink, ShellSoulLinkSink)
        self.assertIsInstance(controller.sink.profile, ParameterProfile)
        self.assertIs(plugin._soullink_shell_runtime, _ShellRuntimeStub.instances[0])

    async def test_plain_soullink_scheme_uses_vts_sink(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        logger = _LoggerStub()
        settings = LiveAdapterSettings.model_validate(
            {
                "live2d": {
                    "enabled": True,
                    "scheme": "soullink",
                    "driver": "json",
                    "soullink": {"enabled": True},
                }
            }
        )

        with patch.object(plugin, "_logger", return_value=logger):
            with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.JsonLive2DBridge", _BridgeStub):
                with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.CapabilityProbe", _ProbeStub):
                    with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.Live2DController", _ControllerStub):
                        with patch(
                            "plugins.maibot_bilibili_live_adapter_copy.plugin.SoulLinkLive2DController",
                            _SoulLinkControllerStub,
                        ):
                            controller = await plugin._build_live2d_controller(settings)

        self.assertIsInstance(controller, _SoulLinkControllerStub)
        self.assertIsInstance(controller.sink, VtsSoulLinkSink)
        self.assertIsNone(plugin._soullink_shell_runtime)

    async def test_started_shell_runtime_is_stopped_when_soullink_controller_init_fails(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        logger = _LoggerStub()
        settings = _make_settings(fallback_to_vts=True)
        module_name = "plugins.maibot_bilibili_live_adapter_copy.live2d_shell_runtime"
        runtime_module = types.ModuleType(module_name)
        runtime_module.SoulLinkShellRuntime = _ShellRuntimeStub

        with patch.dict(sys.modules, {module_name: runtime_module}):
            with patch.object(plugin, "_logger", return_value=logger):
                with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.JsonLive2DBridge", _BridgeStub):
                    with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.CapabilityProbe", _ProbeStub):
                        with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.Live2DController", _ControllerStub):
                            with patch(
                                "plugins.maibot_bilibili_live_adapter_copy.plugin.SoulLinkLive2DController",
                                _FailingSoulLinkControllerStub,
                            ):
                                controller = await plugin._build_live2d_controller(settings)

        self.assertIsInstance(controller, _ControllerStub)
        self.assertTrue(controller.started)
        self.assertEqual(len(_ShellRuntimeStub.instances), 1)
        self.assertEqual(_ShellRuntimeStub.instances[0].stop_calls, 1)
        self.assertIsNone(plugin._soullink_shell_runtime)

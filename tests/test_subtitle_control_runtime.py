import asyncio
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from . import _host_bootstrap  # noqa: F401

from plugins.maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from plugins.maibot_bilibili_live_adapter_copy.live2d_control_state import Live2DControlState
from plugins.maibot_bilibili_live_adapter_copy.plugin import BilibiliLiveAdapterPlugin
from plugins.maibot_bilibili_live_adapter_copy.subtitle_native_runtime import SubtitleNativeUIRuntime
from plugins.maibot_bilibili_live_adapter_copy.subtitle_native_state import SubtitleUISettingsStore
from plugins.maibot_bilibili_live_adapter_copy.subtitle_webui import SubtitleWebUIService


class _VarStub:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value

    def get(self):
        return self.value


class _LoggerStub:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, message: str) -> None:
        self.infos.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


class _RuntimeStub:
    last_instance = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.is_running = False
        self.updated_shell_controls: list[dict[str, object]] = []
        self.show_windows_calls = 0
        _RuntimeStub.last_instance = self

    def start(self) -> None:
        self.is_running = True

    def stop(self) -> None:
        self.is_running = False

    def update_shell_controls(self, payload) -> None:
        self.updated_shell_controls.append(dict(payload))

    def show_windows(self) -> None:
        self.show_windows_calls += 1


class _SubtitleWebUIServiceStub:
    last_instance = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        self.updated_shell_controls: list[dict[str, object]] = []
        self.show_windows_calls = 0
        _SubtitleWebUIServiceStub.last_instance = self

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    def update_shell_controls(self, payload) -> None:
        self.updated_shell_controls.append(dict(payload))

    def show_windows(self) -> None:
        self.show_windows_calls += 1


class _ShellRuntimeStub:
    def __init__(self) -> None:
        self.payload = {
            "enabled": True,
            "click_through": True,
            "interactive": False,
            "actions": ["toggle_click_through", "unlock_drag", "reset_position"],
        }
        self.actions: list[str] = []

    def build_control_surface_payload(self) -> dict[str, object]:
        return dict(self.payload)

    async def dispatch_control_action(self, action: str) -> dict[str, object]:
        normalized = str(action)
        self.actions.append(normalized)
        if normalized == "toggle_click_through":
            self.payload["click_through"] = not bool(self.payload["click_through"])
            self.payload["interactive"] = not bool(self.payload["click_through"])
        elif normalized == "unlock_drag":
            self.payload["click_through"] = False
            self.payload["interactive"] = True
        return dict(self.payload)


class SubtitleControlRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def test_runtime_surfaces_invalid_control_state_and_syncs_widget_state_on_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SubtitleUISettingsStore(Path(temp_dir) / "subtitle_ui_settings.json")
            received_patches: list[dict[str, object]] = []
            logger = _LoggerStub()

            runtime = SubtitleNativeUIRuntime(
                settings_store=store,
                logger=logger,
                on_control_state_changed=received_patches.append,
                control_state="not-a-mapping",
            )
            runtime._mouse_follow_enabled_var = _VarStub()
            runtime._mouse_follow_status_var = _VarStub()

            self.assertEqual(runtime._control_state, Live2DControlState())
            self.assertTrue(any("invalid control_state" in message.lower() for message in logger.warnings))

            self.assertEqual(runtime._live2d_control_status_text("mouse"), "Mouse follow active")
            self.assertEqual(runtime._live2d_control_status_text("cooldown"), "Mouse follow cooling down")
            self.assertEqual(runtime._live2d_control_status_text("ai"), "AI motion active")
            self.assertEqual(runtime._live2d_control_status_text("disabled"), "Mouse follow disabled")
            self.assertEqual(runtime._live2d_control_status_text(None), "Mouse follow disabled")

            runtime._set_mouse_follow_status_hint("cooldown")
            self.assertEqual(runtime._mouse_follow_status_var.value, "Mouse follow cooling down")

            runtime._handle_mouse_follow_toggle(True)
            runtime._handle_mouse_follow_toggle(False)
            partial_runtime = SubtitleNativeUIRuntime(
                settings_store=store,
                logger=logger,
                control_state={"mouse_follow_enabled": True},
            )

            self.assertEqual(
                received_patches,
                [
                    {"mouse_follow_enabled": True, "mouse_follow_status": "mouse"},
                    {"mouse_follow_enabled": False, "mouse_follow_status": "disabled"},
                ],
            )
            self.assertEqual(runtime._mouse_follow_enabled_var.value, False)
            self.assertEqual(runtime._mouse_follow_status_var.value, "Mouse follow disabled")
            self.assertEqual(
                partial_runtime._control_state,
                Live2DControlState(mouse_follow_enabled=True, mouse_follow_status="disabled", last_mouse_activity_ts=0.0),
            )

    async def test_webui_service_validates_callback_and_surfaces_invalid_control_state(self) -> None:
        callback = lambda patch: patch
        shell_callback = lambda action: action
        control_state = Live2DControlState(mouse_follow_enabled=True, mouse_follow_status="mouse")
        logger = _LoggerStub()
        shell_controls = {
            "enabled": True,
            "click_through": True,
            "interactive": False,
            "actions": ["toggle_click_through"],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("plugins.maibot_bilibili_live_adapter_copy.subtitle_webui._plugin_data_dir", return_value=Path(temp_dir)):
                with patch("plugins.maibot_bilibili_live_adapter_copy.subtitle_webui.SubtitleNativeUIRuntime", _RuntimeStub):
                    service = SubtitleWebUIService(
                        host="127.0.0.1",
                        port=18182,
                        control_state=control_state.to_dict(),
                        on_control_state_changed=callback,
                        shell_controls=shell_controls,
                        on_shell_action_requested=shell_callback,
                        logger=logger,
                    )

                    await service.start()
                    service.update_shell_controls(
                        {
                            "enabled": True,
                            "click_through": False,
                            "interactive": True,
                            "actions": ["unlock_drag"],
                        }
                    )
                    service.show_windows()

                    malformed_service = SubtitleWebUIService(
                        host="127.0.0.1",
                        port=18182,
                        control_state="bad-state",
                        on_control_state_changed="bad-callback",
                        shell_controls="bad-shell-state",
                        on_shell_action_requested="bad-shell-callback",
                        logger=logger,
                    )

        runtime = _RuntimeStub.last_instance
        self.assertIsNotNone(runtime)
        self.assertEqual(runtime.kwargs["control_state"], control_state)
        self.assertIs(runtime.kwargs["on_control_state_changed"], callback)
        self.assertEqual(runtime.kwargs["shell_controls"], shell_controls)
        self.assertTrue(callable(runtime.kwargs["on_shell_action_requested"]))
        self.assertEqual(
            runtime.updated_shell_controls,
            [
                {
                    "enabled": True,
                    "click_through": False,
                    "interactive": True,
                    "actions": ["unlock_drag"],
                }
            ],
        )
        self.assertEqual(runtime.show_windows_calls, 1)
        self.assertEqual(malformed_service.control_state, Live2DControlState())
        self.assertIsNone(malformed_service.on_control_state_changed)
        self.assertEqual(
            malformed_service.shell_controls,
            {"enabled": False, "click_through": True, "interactive": False, "actions": []},
        )
        self.assertIsNone(malformed_service.on_shell_action_requested)
        self.assertTrue(any("invalid control_state" in message.lower() for message in logger.warnings))
        self.assertTrue(any("on_control_state_changed" in message for message in logger.warnings))
        self.assertTrue(any("on_shell_action_requested" in message for message in logger.warnings))

    async def test_plugin_persists_live2d_control_state_patch_and_passes_state_to_webui(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = LiveAdapterSettings()
        patch_payload = {
            "mouse_follow_enabled": True,
            "mouse_follow_status": "mouse",
            "last_mouse_activity_ts": 42.5,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("plugins.maibot_bilibili_live_adapter_copy.plugin._plugin_data_dir", return_value=Path(temp_dir)):
                with patch("plugins.maibot_bilibili_live_adapter_copy.plugin.SubtitleWebUIService", _SubtitleWebUIServiceStub):
                    plugin._soullink_shell_runtime = _ShellRuntimeStub()
                    updated_state = plugin._handle_live2d_control_state_patch(patch_payload)

                    self.assertEqual(updated_state.mouse_follow_enabled, True)
                    self.assertEqual(updated_state.mouse_follow_status, "mouse")
                    self.assertEqual(updated_state.last_mouse_activity_ts, 42.5)

                    await plugin._start_subtitle_webui(settings)

        service = _SubtitleWebUIServiceStub.last_instance
        self.assertIsNotNone(service)
        self.assertEqual(service.kwargs["control_state"], updated_state)
        self.assertTrue(callable(service.kwargs["on_control_state_changed"]))
        self.assertEqual(
            service.kwargs["shell_controls"],
            {
                "enabled": True,
                "click_through": True,
                "interactive": False,
                "actions": ["toggle_click_through", "unlock_drag", "reset_position"],
            },
        )

    async def test_plugin_shell_action_updates_runtime_and_refreshes_webui_controls(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        plugin._soullink_shell_runtime = _ShellRuntimeStub()
        plugin._subtitle_webui = _SubtitleWebUIServiceStub()

        plugin._handle_soullink_shell_action_requested("unlock_drag")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(plugin._soullink_shell_runtime.actions, ["unlock_drag"])
        self.assertEqual(
            plugin._subtitle_webui.updated_shell_controls[-1],
            {
                "enabled": True,
                "click_through": False,
                "interactive": True,
                "actions": ["toggle_click_through", "unlock_drag", "reset_position"],
            },
        )

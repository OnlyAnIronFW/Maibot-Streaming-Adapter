import unittest
import tempfile
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import _host_bootstrap  # noqa: F401

from aiohttp import ClientSession

from plugins.maibot_bilibili_live_adapter_copy.live2d_shell_runtime import SoulLinkShellRuntime
from plugins.maibot_bilibili_live_adapter_copy.live2d_shell_window import (
    SoulLinkShellWindowHost,
    SoulLinkShellWindowOptions,
)


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SoulLinkShellAssetTest(unittest.TestCase):
    def test_shell_assets_exist(self) -> None:
        base = Path(__file__).resolve().parents[1] / "webui" / "soullink_shell"
        self.assertTrue((base / "index.html").exists())
        self.assertTrue((base / "shell.css").exists())
        self.assertTrue((base / "shell.js").exists())
        self.assertTrue((base / "shell-legacy-compat.js").exists())


class SoulLinkShellRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_exposes_shell_url_when_started(self) -> None:
        port = _unused_port()
        runtime = SoulLinkShellRuntime(
            config=type(
                "_Config",
                (),
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "click_through_default": True,
                    "always_on_top": True,
                    "remember_window_geometry": True,
                    "start_interactive": False,
                    "open_devtools": False,
                    "fallback_to_vts": True,
                },
            )(),
            logger=None,
        )
        await runtime.start(start_window=False)
        try:
            self.assertTrue(runtime.shell_url.startswith(f"http://127.0.0.1:{port}"))
        finally:
            await runtime.stop()

    async def test_runtime_uses_loopback_shell_url_for_unspecified_bind_host(self) -> None:
        port = _unused_port()
        runtime = SoulLinkShellRuntime(
            config=type(
                "_Config",
                (),
                {
                    "host": "0.0.0.0",
                    "port": port,
                    "click_through_default": True,
                    "always_on_top": True,
                    "remember_window_geometry": True,
                    "start_interactive": False,
                    "open_devtools": False,
                    "fallback_to_vts": True,
                },
            )(),
            logger=None,
        )
        await runtime.start(start_window=False)
        try:
            self.assertEqual(runtime.shell_url, f"http://127.0.0.1:{port}/shell")
        finally:
            await runtime.stop()

    async def test_runtime_serves_shell_index_and_vendor_assets(self) -> None:
        port = _unused_port()
        runtime = SoulLinkShellRuntime(
            config=type(
                "_Config",
                (),
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "click_through_default": True,
                    "always_on_top": True,
                    "remember_window_geometry": True,
                    "start_interactive": False,
                    "open_devtools": False,
                    "fallback_to_vts": True,
                },
            )(),
            logger=None,
        )
        await runtime.start(start_window=False)
        try:
            async with ClientSession() as session:
                async with session.get(runtime.shell_url) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("MaiBot SoulLink Shell", await response.text())
                async with session.get(f"http://127.0.0.1:{port}/shell-legacy-compat.js") as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("initOcclusionLayers", await response.text())
                async with session.get(
                    f"http://127.0.0.1:{port}/live2d_soullink_vendor/frontend_legacy/services/expression.js"
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("transitionToExpression", await response.text())
        finally:
            await runtime.stop()

    async def test_runtime_replays_cached_model_payload_to_new_shell_socket(self) -> None:
        port = _unused_port()
        runtime = SoulLinkShellRuntime(
            config=type(
                "_Config",
                (),
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "click_through_default": True,
                    "always_on_top": True,
                    "remember_window_geometry": True,
                    "start_interactive": False,
                    "open_devtools": False,
                    "fallback_to_vts": True,
                },
            )(),
            logger=None,
        )
        await runtime.start(start_window=False)
        try:
            await runtime.broadcast(
                {
                    "type": "load_model",
                    "model": {
                        "id": "hiyori",
                        "name": "Hiyori",
                        "path": f"http://127.0.0.1:{port}/shell/model/hiyori_pro_t11.model3.json",
                    },
                }
            )
            await runtime.broadcast({"type": "reset", "duration_ms": 0})
            async with ClientSession() as session:
                async with session.ws_connect(f"ws://127.0.0.1:{port}/shell/ws") as ws:
                    first = await ws.receive_json(timeout=1.0)
                    second = await ws.receive_json(timeout=1.0)
                    third = await ws.receive_json(timeout=1.0)
        finally:
            await runtime.stop()

        self.assertEqual(first["type"], "set_interactive")
        self.assertEqual(second["type"], "shell_controls")
        self.assertEqual(second["actions"][0], "reopen_window")
        self.assertEqual(third["type"], "load_model")
        self.assertEqual(third["model"]["name"], "Hiyori")

    async def test_runtime_control_actions_update_payload_and_window_host(self) -> None:
        port = _unused_port()
        runtime = SoulLinkShellRuntime(
            config=type(
                "_Config",
                (),
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "click_through_default": True,
                    "always_on_top": True,
                    "remember_window_geometry": True,
                    "start_interactive": False,
                    "open_devtools": False,
                    "fallback_to_vts": True,
                },
            )(),
            logger=None,
        )

        class _WindowHostStub:
            def __init__(self) -> None:
                self.click_through_calls: list[bool] = []
                self.reset_calls = 0
                self.focus_calls = 0
                self.minimize_calls = 0
                self.resize_calls: list[tuple[int, int]] = []
                self.stop_calls = 0

            def apply_click_through(self, enabled: bool) -> None:
                self.click_through_calls.append(bool(enabled))

            def reset_position(self) -> None:
                self.reset_calls += 1

            def focus(self) -> None:
                self.focus_calls += 1

            def minimize(self) -> None:
                self.minimize_calls += 1

            def resize_by(self, width_delta: int, height_delta: int) -> None:
                self.resize_calls.append((int(width_delta), int(height_delta)))

            def stop(self) -> None:
                self.stop_calls += 1

        window_host = _WindowHostStub()
        runtime._window_host = window_host

        payload = await runtime.dispatch_control_action("unlock_drag")
        self.assertFalse(payload["click_through"])
        self.assertTrue(payload["interactive"])
        self.assertEqual(window_host.click_through_calls[-1], False)

        payload = await runtime.dispatch_control_action("toggle_click_through")
        self.assertTrue(payload["click_through"])
        self.assertFalse(payload["interactive"])
        self.assertEqual(window_host.click_through_calls[-1], True)

        await runtime.dispatch_control_action("reset_position")
        await runtime.dispatch_control_action("open_settings")
        await runtime.dispatch_control_action("grow_window")
        await runtime.dispatch_control_action("shrink_window")
        await runtime.dispatch_control_action("minimize_window")
        payload = await runtime.dispatch_control_action("close_window")
        self.assertEqual(window_host.reset_calls, 1)
        self.assertEqual(window_host.focus_calls, 1)
        self.assertEqual(window_host.resize_calls, [(120, 120), (-120, -120)])
        self.assertEqual(window_host.minimize_calls, 1)
        self.assertEqual(window_host.stop_calls, 1)
        self.assertIn("reopen_window", payload["actions"])

    async def test_runtime_loads_saved_window_geometry_into_window_options(self) -> None:
        port = _unused_port()
        runtime = SoulLinkShellRuntime(
            config=type(
                "_Config",
                (),
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "click_through_default": True,
                    "always_on_top": True,
                    "remember_window_geometry": True,
                    "start_interactive": False,
                    "open_devtools": False,
                    "fallback_to_vts": True,
                },
            )(),
            logger=None,
        )
        runtime.shell_url = f"http://127.0.0.1:{port}/shell"

        class _WindowHostStub:
            last_options = None

            def __init__(self, *, options, logger=None) -> None:
                type(self).last_options = options

            def start(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            geometry_path = Path(temp_dir) / "soullink_shell_window_geometry.json"
            geometry_path.write_text(
                json.dumps({"x": 320, "y": 140, "width": 880, "height": 960}),
                encoding="utf-8",
            )
            with patch.object(runtime, "_geometry_store_path", return_value=geometry_path):
                with patch(
                    "plugins.maibot_bilibili_live_adapter_copy.live2d_shell_runtime.SoulLinkShellWindowHost",
                    _WindowHostStub,
                ):
                    runtime._start_window_host()

        self.assertIsNotNone(_WindowHostStub.last_options)
        self.assertEqual(_WindowHostStub.last_options.x, 320)
        self.assertEqual(_WindowHostStub.last_options.y, 140)
        self.assertEqual(_WindowHostStub.last_options.width, 880)
        self.assertEqual(_WindowHostStub.last_options.height, 960)

    async def test_runtime_persists_window_geometry_on_stop(self) -> None:
        port = _unused_port()
        runtime = SoulLinkShellRuntime(
            config=type(
                "_Config",
                (),
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "click_through_default": True,
                    "always_on_top": True,
                    "remember_window_geometry": True,
                    "start_interactive": False,
                    "open_devtools": False,
                    "fallback_to_vts": True,
                },
            )(),
            logger=None,
        )

        class _WindowHostStub:
            def __init__(self) -> None:
                self.stopped = False

            def capture_geometry(self) -> dict[str, int]:
                return {"x": 410, "y": 160, "width": 900, "height": 980}

            def stop(self) -> None:
                self.stopped = True

        window_host = _WindowHostStub()
        runtime._window_host = window_host

        with tempfile.TemporaryDirectory() as temp_dir:
            geometry_path = Path(temp_dir) / "soullink_shell_window_geometry.json"
            with patch.object(runtime, "_geometry_store_path", return_value=geometry_path):
                await runtime.stop()
                stored = json.loads(geometry_path.read_text(encoding="utf-8"))

        self.assertTrue(window_host.stopped)
        self.assertEqual(stored, {"x": 410, "y": 160, "width": 900, "height": 980})


class SoulLinkShellWindowHostTest(unittest.TestCase):
    def test_start_launches_subprocess_window_runner(self) -> None:
        host = SoulLinkShellWindowHost(options=SoulLinkShellWindowOptions(url="http://127.0.0.1:18183/shell"))
        fake_process = SimpleNamespace(pid=4321, poll=lambda: None)

        with patch(
            "plugins.maibot_bilibili_live_adapter_copy.live2d_shell_window.subprocess.Popen",
            return_value=fake_process,
        ) as popen:
            host.start()

        self.assertIs(host._process, fake_process)
        self.assertIsNone(host._thread)
        popen.assert_called_once()

    def test_apply_click_through_uses_window_title_for_subprocess_host(self) -> None:
        host = SoulLinkShellWindowHost(options=SoulLinkShellWindowOptions(url="http://127.0.0.1:18183/shell"))
        host._process = SimpleNamespace(pid=4321, poll=lambda: None)
        applied: list[tuple[int, bool]] = []

        with (
            patch(
                "plugins.maibot_bilibili_live_adapter_copy.live2d_shell_window.ctypes.windll.user32.FindWindowW",
                return_value=2468,
            ),
            patch.object(
                host,
                "_set_click_through_style",
                side_effect=lambda hwnd, enabled: applied.append((hwnd, enabled)),
            ),
        ):
            host.apply_click_through(True)

        self.assertEqual(applied, [(2468, True)])

    def test_stop_closes_subprocess_window_even_without_local_window_object(self) -> None:
        host = SoulLinkShellWindowHost(options=SoulLinkShellWindowOptions(url="http://127.0.0.1:18183/shell"))

        class _FakeProcess:
            def __init__(self) -> None:
                self.wait_calls: list[float] = []
                self.terminate_calls = 0
                self._poll = None

            def poll(self):
                return self._poll

            def wait(self, timeout: float | None = None) -> None:
                self.wait_calls.append(timeout)
                self._poll = 0

            def terminate(self) -> None:
                self.terminate_calls += 1
                self._poll = 0

        fake_process = _FakeProcess()
        host._process = fake_process

        with (
            patch.object(host, "_resolve_hwnd", return_value=1357),
            patch(
                "plugins.maibot_bilibili_live_adapter_copy.live2d_shell_window.ctypes.windll.user32.PostMessageW"
            ) as post_message,
        ):
            host.stop()

        self.assertEqual(post_message.call_count, 1)
        self.assertEqual(fake_process.wait_calls, [2.0])
        self.assertEqual(fake_process.terminate_calls, 0)
        self.assertIsNone(host._process)

    def test_apply_click_through_when_ready_retries_until_hwnd_exists(self) -> None:
        host = SoulLinkShellWindowHost(options=SoulLinkShellWindowOptions(url="http://127.0.0.1:18183/shell"))
        hwnds = iter((0, 0, 123))
        applied: list[tuple[int, bool]] = []
        sleeps: list[float] = []

        with (
            patch.object(host, "_resolve_hwnd", side_effect=lambda: next(hwnds)),
            patch.object(host, "_set_click_through_style", side_effect=lambda hwnd, enabled: applied.append((hwnd, enabled))),
            patch("plugins.maibot_bilibili_live_adapter_copy.live2d_shell_window.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)),
        ):
            applied_ok = host._apply_click_through_when_ready(True, max_attempts=4, retry_interval_sec=0.01)

        self.assertTrue(applied_ok)
        self.assertEqual(applied, [(123, True)])
        self.assertEqual(sleeps, [0.01, 0.01])

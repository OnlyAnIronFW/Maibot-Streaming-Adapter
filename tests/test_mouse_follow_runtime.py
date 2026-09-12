import asyncio
import ctypes
from collections import namedtuple
import sys
import unittest
import types
from unittest.mock import patch

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from maibot_bilibili_live_adapter_copy.live2d_adaptive.mouse_follow import (
    GlobalMouseFollowRuntime,
    MouseFollowSnapshot,
    _default_button_provider,
)


class MouseFollowRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def test_default_cursor_provider_uses_wintypes_point_when_available(self) -> None:
        class FakeUser32:
            def GetCursorPos(self, point_ref) -> int:
                point = point_ref._obj
                point.x = 123
                point.y = 456
                return 1

        fake_wintypes = types.ModuleType("ctypes.wintypes")

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        fake_wintypes.POINT = POINT

        with patch.dict(sys.modules, {"ctypes.wintypes": fake_wintypes}):
            with patch.object(ctypes, "windll", type("Windll", (), {"user32": FakeUser32()})(), create=True):
                from maibot_bilibili_live_adapter_copy.live2d_adaptive.mouse_follow import _default_cursor_provider

                self.assertEqual(_default_cursor_provider(), (123.0, 456.0))

    def test_default_cursor_provider_treats_false_return_as_failure(self) -> None:
        class FakeUser32:
            def GetCursorPos(self, point_ref) -> int:
                point = point_ref._obj
                point.x = 999
                point.y = 888
                return 0

        fake_wintypes = types.ModuleType("ctypes.wintypes")

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        fake_wintypes.POINT = POINT

        with patch.dict(sys.modules, {"ctypes.wintypes": fake_wintypes}):
            with patch.object(ctypes, "windll", type("Windll", (), {"user32": FakeUser32()})(), create=True):
                from maibot_bilibili_live_adapter_copy.live2d_adaptive.mouse_follow import _default_cursor_provider

                self.assertEqual(_default_cursor_provider(), (0.0, 0.0))

    def test_default_cursor_provider_falls_back_when_wintypes_is_missing_or_invalid(self) -> None:
        class FakeUser32:
            def GetCursorPos(self, _point_ref) -> int:
                return 1

        class MissingWintypesCtypes:
            pass

        class InvalidWintypesModule(types.ModuleType):
            pass

        with patch.dict(sys.modules, {"ctypes": MissingWintypesCtypes()}):
            from maibot_bilibili_live_adapter_copy.live2d_adaptive.mouse_follow import _default_cursor_provider as missing_import_provider

            self.assertEqual(missing_import_provider(), (0.0, 0.0))

        invalid_wintypes = InvalidWintypesModule("ctypes.wintypes")
        with patch.dict(sys.modules, {"ctypes.wintypes": invalid_wintypes}):
            with patch.object(ctypes, "windll", type("Windll", (), {"user32": FakeUser32()})(), create=True):
                from maibot_bilibili_live_adapter_copy.live2d_adaptive.mouse_follow import _default_cursor_provider as invalid_wintypes_provider

                self.assertEqual(invalid_wintypes_provider(), (0.0, 0.0))

    async def test_runtime_polls_cursor_and_buttons_and_returns_snapshot_copy(self) -> None:
        cursor_positions = iter([(400, 300), (800, 300), (800, 300), (800, 300)])
        button_states = iter([(False, False, False), (False, False, False), (True, False, False), (False, False, False)])
        cursor_calls = 0
        button_calls = 0

        def cursor_provider() -> tuple[float, float]:
            nonlocal cursor_calls
            cursor_calls += 1
            return next(cursor_positions)

        def button_provider() -> tuple[bool, bool, bool]:
            nonlocal button_calls
            button_calls += 1
            return next(button_states)

        def screen_provider() -> tuple[float, float]:
            return (800, 600)

        runtime = GlobalMouseFollowRuntime(
            poll_interval_ms=10,
            cursor_provider=cursor_provider,
            button_provider=button_provider,
            screen_provider=screen_provider,
        )

        self.assertEqual(runtime.latest_snapshot(), MouseFollowSnapshot())

        await runtime.start()
        try:
            await asyncio.sleep(0.06)
            snapshot = runtime.latest_snapshot()
        finally:
            await runtime.stop()

        self.assertAlmostEqual(snapshot.x_norm, 1.0, places=4)
        self.assertAlmostEqual(snapshot.y_norm, 0.0, places=4)
        self.assertGreater(cursor_calls, 0)
        self.assertGreater(button_calls, 0)
        self.assertGreater(snapshot.activity_ts, 0.0)

        snapshot.x_norm = -123.0
        self.assertNotEqual(runtime.latest_snapshot().x_norm, snapshot.x_norm)

    def test_default_screen_provider_uses_virtual_screen_metrics(self) -> None:
        class FakeUser32:
            def GetSystemMetrics(self, index: int) -> float:
                metrics = {
                    76: -1920.0,
                    77: 0.0,
                    78: 3840.0,
                    79: 1080.0,
                }
                return metrics[index]

        class FakeCtypes:
            class windll:
                user32 = FakeUser32()

        with patch.dict(sys.modules, {"ctypes": FakeCtypes()}):
            from maibot_bilibili_live_adapter_copy.live2d_adaptive.mouse_follow import _default_screen_provider

            self.assertEqual(_default_screen_provider(), (-1920.0, 0.0, 3840.0, 1080.0))

    def test_default_screen_provider_falls_back_to_neutral_minimal_bounds_when_metrics_unavailable(self) -> None:
        class FakeCtypes:
            pass

        with patch.dict(sys.modules, {"ctypes": FakeCtypes()}):
            from maibot_bilibili_live_adapter_copy.live2d_adaptive.mouse_follow import _default_screen_provider

            self.assertEqual(_default_screen_provider(), (0.0, 0.0, 1.0, 1.0))

    def test_default_button_provider_degrades_when_ctypes_is_unavailable(self) -> None:
        class FakeCtypes:
            pass

        with patch.dict(sys.modules, {"ctypes": FakeCtypes()}):
            self.assertEqual(_default_button_provider(), (False, False, False))

    def test_default_button_provider_degrades_when_getasynckeystate_raises(self) -> None:
        class FakeUser32:
            def GetAsyncKeyState(self, _vk: int) -> int:
                raise RuntimeError("unavailable")

        class FakeCtypes:
            class windll:
                user32 = FakeUser32()

        with patch.dict(sys.modules, {"ctypes": FakeCtypes()}):
            self.assertEqual(_default_button_provider(), (False, False, False))

    def test_default_button_provider_ignores_low_bit_only_state(self) -> None:
        class FakeUser32:
            def GetAsyncKeyState(self, vk: int) -> int:
                return {0x01: 0x0001, 0x02: 0x0000, 0x04: 0x0001}[vk]

        class FakeCtypes:
            class windll:
                user32 = FakeUser32()

        with patch.dict(sys.modules, {"ctypes": FakeCtypes()}):
            self.assertEqual(_default_button_provider(), (False, False, False))

    def test_runtime_uses_virtual_screen_origin_for_normalization(self) -> None:
        runtime = GlobalMouseFollowRuntime(
            cursor_provider=lambda: (-1920.0, 540.0),
            button_provider=lambda: (False, False, False),
            screen_provider=lambda: (-1920.0, 0.0, 3840.0, 1080.0),
        )

        runtime._poll_once()

        snapshot = runtime.latest_snapshot()
        self.assertAlmostEqual(snapshot.x_norm, -1.0, places=4)
        self.assertAlmostEqual(snapshot.y_norm, 0.0, places=4)
        self.assertFalse(snapshot.active)
        self.assertEqual(snapshot.activity_ts, 0.0)

    def test_runtime_keeps_button_activity_when_cursor_provider_raises(self) -> None:
        calls = iter([(100.0, 100.0)])
        buttons = iter([(False, False, False), (True, False, False)])

        def cursor_provider() -> tuple[float, float]:
            return next(calls)

        def button_provider() -> tuple[bool, bool, bool]:
            return next(buttons)

        runtime = GlobalMouseFollowRuntime(
            cursor_provider=cursor_provider,
            button_provider=button_provider,
            screen_provider=lambda: (800.0, 600.0),
        )

        runtime._poll_once()
        active_snapshot = runtime.latest_snapshot()
        self.assertFalse(active_snapshot.active)
        self.assertEqual(active_snapshot.activity_ts, 0.0)

        runtime._poll_once()
        snapshot = runtime.latest_snapshot()

        self.assertTrue(snapshot.active)
        self.assertGreater(snapshot.activity_ts, active_snapshot.activity_ts)
        self.assertAlmostEqual(snapshot.x_norm, active_snapshot.x_norm, places=4)
        self.assertAlmostEqual(snapshot.y_norm, active_snapshot.y_norm, places=4)

    def test_runtime_reuses_last_screen_bounds_after_transient_failure(self) -> None:
        screen_calls = iter([(800.0, 600.0)])

        def cursor_provider() -> tuple[float, float]:
            return (400.0, 300.0)

        def button_provider() -> tuple[bool, bool, bool]:
            return (False, False, False)

        def screen_provider() -> tuple[float, float]:
            return next(screen_calls)

        runtime = GlobalMouseFollowRuntime(
            cursor_provider=cursor_provider,
            button_provider=button_provider,
            screen_provider=screen_provider,
        )

        runtime._poll_once()
        first_snapshot = runtime.latest_snapshot()
        self.assertAlmostEqual(first_snapshot.x_norm, 0.0, places=4)
        self.assertAlmostEqual(first_snapshot.y_norm, 0.0, places=4)

        runtime._poll_once()
        second_snapshot = runtime.latest_snapshot()
        self.assertAlmostEqual(second_snapshot.x_norm, 0.0, places=4)
        self.assertAlmostEqual(second_snapshot.y_norm, 0.0, places=4)

    def test_runtime_uses_neutral_minimal_screen_fallback_before_first_success(self) -> None:
        runtime = GlobalMouseFollowRuntime(
            cursor_provider=lambda: (2.0, 0.0),
            button_provider=lambda: (False, False, False),
            screen_provider=lambda: (_ for _ in ()).throw(RuntimeError("screen unavailable")),
        )

        runtime._poll_once()

        snapshot = runtime.latest_snapshot()
        self.assertAlmostEqual(snapshot.x_norm, 1.0, places=4)
        self.assertAlmostEqual(snapshot.y_norm, -1.0, places=4)
        self.assertFalse(snapshot.active)

    def test_runtime_accepts_generic_sequence_screen_bounds(self) -> None:
        Bounds = namedtuple("Bounds", ["left", "top", "width", "height"])
        runtime = GlobalMouseFollowRuntime(
            cursor_provider=lambda: (0.0, 0.0),
            button_provider=lambda: (False, False, False),
            screen_provider=lambda: Bounds(-50.0, -25.0, 100.0, 50.0),
        )

        runtime._poll_once()

        snapshot = runtime.latest_snapshot()
        self.assertAlmostEqual(snapshot.x_norm, 0.0, places=4)
        self.assertAlmostEqual(snapshot.y_norm, 0.0, places=4)

    def test_runtime_accepts_list_screen_bounds(self) -> None:
        runtime = GlobalMouseFollowRuntime(
            cursor_provider=lambda: (0.0, 0.0),
            button_provider=lambda: (False, False, False),
            screen_provider=lambda: [-50.0, -25.0, 100.0, 50.0],
        )

        runtime._poll_once()

        snapshot = runtime.latest_snapshot()
        self.assertAlmostEqual(snapshot.x_norm, 0.0, places=4)
        self.assertAlmostEqual(snapshot.y_norm, 0.0, places=4)

    def test_coerce_screen_bounds_rejects_non_positive_dimensions(self) -> None:
        with self.assertRaises(ValueError):
            GlobalMouseFollowRuntime._coerce_screen_bounds((0.0, 0.0, 0.0, 1080.0))

    def test_runtime_keeps_last_position_and_inactive_when_cursor_and_buttons_fail(self) -> None:
        runtime = GlobalMouseFollowRuntime(
            cursor_provider=lambda: (_ for _ in ()).throw(RuntimeError("cursor unavailable")),
            button_provider=lambda: (_ for _ in ()).throw(RuntimeError("buttons unavailable")),
            screen_provider=lambda: (800.0, 600.0),
        )
        runtime._snapshot = MouseFollowSnapshot(x_norm=0.25, y_norm=-0.5, active=True, activity_ts=12.0)

        runtime._poll_once()

        snapshot = runtime.latest_snapshot()
        self.assertFalse(snapshot.active)
        self.assertEqual(snapshot.activity_ts, 12.0)
        self.assertAlmostEqual(snapshot.x_norm, 0.25, places=4)
        self.assertAlmostEqual(snapshot.y_norm, -0.5, places=4)

    def test_runtime_keeps_active_false_once_cursor_is_stationary_again(self) -> None:
        cursor_positions = iter([(100.0, 100.0), (140.0, 100.0), (140.0, 100.0)])

        def cursor_provider() -> tuple[float, float]:
            return next(cursor_positions)

        runtime = GlobalMouseFollowRuntime(
            cursor_provider=cursor_provider,
            button_provider=lambda: (False, False, False),
            screen_provider=lambda: (200.0, 200.0),
        )

        runtime._poll_once()
        runtime._poll_once()
        moved_snapshot = runtime.latest_snapshot()
        self.assertTrue(moved_snapshot.active)

        runtime._poll_once()
        stationary_snapshot = runtime.latest_snapshot()

        self.assertFalse(stationary_snapshot.active)
        self.assertEqual(stationary_snapshot.activity_ts, moved_snapshot.activity_ts)


if __name__ == "__main__":
    unittest.main()

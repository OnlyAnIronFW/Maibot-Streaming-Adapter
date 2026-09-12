"""Global mouse-follow runtime for adaptive Live2D control."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Callable, Sequence

import contextlib
import time

CursorProvider = Callable[[], tuple[float, float]]
ButtonProvider = Callable[[], tuple[bool, bool, bool]]
ScreenProvider = Callable[[], Sequence[float]]


@dataclass
class MouseFollowSnapshot:
    x_norm: float = 0.0
    y_norm: float = 0.0
    active: bool = False
    activity_ts: float = 0.0


def _default_cursor_provider() -> tuple[float, float]:
    try:
        import ctypes
        from ctypes import wintypes

        point = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return 0.0, 0.0
        return float(point.x), float(point.y)
    except Exception:
        pass
    return 0.0, 0.0


def _default_button_provider() -> tuple[bool, bool, bool]:
    try:
        import ctypes

        user32 = ctypes.windll.user32
        return tuple(bool(user32.GetAsyncKeyState(v) & 0x8000) for v in (0x01, 0x02, 0x04))
    except Exception:
        return (False, False, False)


def _default_screen_provider() -> tuple[float, float, float, float]:
    try:
        import ctypes

        user32 = ctypes.windll.user32
        left = float(user32.GetSystemMetrics(76))
        top = float(user32.GetSystemMetrics(77))
        width = float(user32.GetSystemMetrics(78))
        height = float(user32.GetSystemMetrics(79))
        return left, top, width, height
    except Exception:
        return (0.0, 0.0, 1.0, 1.0)


class GlobalMouseFollowRuntime:
    """Background poller that tracks cursor activity and normalized position."""

    def __init__(
        self,
        *,
        poll_interval_ms: int = 12,
        cursor_provider: CursorProvider | None = None,
        button_provider: ButtonProvider | None = None,
        screen_provider: ScreenProvider | None = None,
    ) -> None:
        self.poll_interval_ms = max(1, int(poll_interval_ms))
        self._cursor_provider = cursor_provider or _default_cursor_provider
        self._button_provider = button_provider or _default_button_provider
        self._screen_provider = screen_provider or _default_screen_provider
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._snapshot = MouseFollowSnapshot()
        self._last_cursor: tuple[float, float] | None = None
        self._last_screen_bounds: tuple[float, float, float, float] | None = None

    async def start(self) -> None:
        """Start the background polling loop."""

        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._poll_loop(), name="live2d_adaptive.mouse_follow")

    async def stop(self) -> None:
        """Stop the background polling loop."""

        task = self._task
        if task is None:
            return
        self._stop_event.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._task = None

    def latest_snapshot(self) -> MouseFollowSnapshot:
        """Return a copy of the most recent mouse-follow snapshot."""

        return replace(self._snapshot)

    async def _poll_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._poll_once()
                await asyncio.sleep(self.poll_interval_ms / 1000.0)
        except asyncio.CancelledError:
            raise

    def _poll_once(self) -> None:
        try:
            cursor_x, cursor_y = self._cursor_provider()
        except Exception:
            self._handle_cursor_failure()
            return

        try:
            buttons = self._button_provider()
        except Exception:
            buttons = (False, False, False)

        screen_left, screen_top, screen_width, screen_height = self._read_screen_bounds()

        cursor = (float(cursor_x), float(cursor_y))
        screen_width = max(1.0, float(screen_width))
        screen_height = max(1.0, float(screen_height))
        screen_left = float(screen_left)
        screen_top = float(screen_top)

        x_norm = self._normalize(cursor[0] - screen_left, screen_width)
        y_norm = self._normalize(cursor[1] - screen_top, screen_height)
        moved = self._last_cursor is not None and cursor != self._last_cursor
        active = moved or any(bool(button) for button in buttons)

        if active:
            self._snapshot = MouseFollowSnapshot(
                x_norm=x_norm,
                y_norm=y_norm,
                active=True,
                activity_ts=time.time(),
            )
        else:
            self._snapshot = MouseFollowSnapshot(
                x_norm=x_norm,
                y_norm=y_norm,
                active=False,
                activity_ts=self._snapshot.activity_ts,
            )

        self._last_cursor = cursor
        self._last_screen_bounds = (screen_left, screen_top, screen_width, screen_height)

    @staticmethod
    def _normalize(value: float, dimension: float) -> float:
        normalized = (float(value) / float(dimension)) * 2.0 - 1.0
        if normalized < -1.0:
            return -1.0
        if normalized > 1.0:
            return 1.0
        return normalized

    @staticmethod
    def _coerce_screen_bounds(
        screen: Sequence[float],
    ) -> tuple[float, float, float, float]:
        values = tuple(screen)
        if len(values) == 2:
            left = 0.0
            top = 0.0
            width, height = values
        elif len(values) == 4:
            left, top, width, height = values
        else:
            raise ValueError("screen provider must return 2 or 4 values")
        left = float(left)
        top = float(top)
        width = float(width)
        height = float(height)
        if width <= 0.0 or height <= 0.0:
            raise ValueError("screen provider must return positive dimensions")
        return left, top, width, height

    def _read_screen_bounds(self) -> tuple[float, float, float, float]:
        try:
            screen = self._screen_provider()
            bounds = self._coerce_screen_bounds(screen)
        except Exception:
            bounds = self._last_screen_bounds or (0.0, 0.0, 1.0, 1.0)
        return bounds

    def _handle_cursor_failure(self) -> None:
        try:
            buttons = self._button_provider()
        except Exception:
            buttons = (False, False, False)

        if any(bool(button) for button in buttons):
            self._snapshot = replace(self._snapshot, active=True, activity_ts=time.time())
        else:
            self._snapshot = replace(self._snapshot, active=False)

from __future__ import annotations

import contextlib
import ctypes
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


@dataclass(slots=True)
class SoulLinkShellWindowOptions:
    url: str
    title: str = "MaiBot SoulLink Shell"
    transparent: bool = True
    frameless: bool = True
    on_top: bool = True
    click_through: bool = True
    width: int = 960
    height: int = 1080
    x: int = 240
    y: int = 120
    open_devtools: bool = False


class SoulLinkShellWindowHost:
    """Small wrapper around the Windows shell webview host."""

    def __init__(self, options: SoulLinkShellWindowOptions, logger: Any = None) -> None:
        self.options = options
        self.logger = logger
        self._window: Any = None
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[Any] | None = None

    def start(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            return
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._process = subprocess.Popen(
            self._build_child_command(),
            cwd=str(self._script_dir()),
            creationflags=creationflags,
        )
        self._apply_click_through_when_ready(self.options.click_through, max_attempts=60, retry_interval_sec=0.05)

    def stop(self) -> None:
        process = self._process
        window = self._window
        if window is not None:
            destroy = getattr(window, "destroy", None)
            if callable(destroy):
                try:
                    destroy()
                except Exception:
                    self._log_debug("Ignoring shell window destroy failure.")
        hwnd = self._resolve_hwnd()
        if hwnd != 0:
            with contextlib.suppress(Exception):
                ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    process.terminate()
                    process.wait(timeout=2.0)
        self._window = None
        self._process = None
        self._thread = None

    def apply_click_through(self, enabled: bool) -> None:
        self.options.click_through = bool(enabled)
        hwnd = self._resolve_hwnd()
        if hwnd == 0:
            return
        self._set_click_through_style(hwnd, enabled)

    def reset_position(self) -> None:
        hwnd = self._resolve_hwnd()
        if hwnd == 0:
            return
        ctypes.windll.user32.MoveWindow(
            hwnd,
            int(self.options.x),
            int(self.options.y),
            int(self.options.width),
            int(self.options.height),
            True,
        )
        if self.options.on_top:
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)

    def focus(self) -> None:
        hwnd = self._resolve_hwnd()
        if hwnd == 0:
            return
        with contextlib.suppress(Exception):
            ctypes.windll.user32.ShowWindow(hwnd, 5)
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetForegroundWindow(hwnd)

    def minimize(self) -> None:
        hwnd = self._resolve_hwnd()
        if hwnd == 0:
            return
        with contextlib.suppress(Exception):
            ctypes.windll.user32.ShowWindow(hwnd, 6)

    def resize_by(self, width_delta: int, height_delta: int) -> None:
        hwnd = self._resolve_hwnd()
        if hwnd == 0:
            return
        geometry = self.capture_geometry() or {
            "x": int(self.options.x),
            "y": int(self.options.y),
            "width": int(self.options.width),
            "height": int(self.options.height),
        }
        width = max(320, int(geometry["width"]) + int(width_delta))
        height = max(420, int(geometry["height"]) + int(height_delta))
        x = int(geometry["x"])
        y = int(geometry["y"])
        ctypes.windll.user32.MoveWindow(hwnd, x, y, width, height, True)

    def capture_geometry(self) -> dict[str, int] | None:
        hwnd = self._resolve_hwnd()
        if hwnd == 0:
            return None
        rect = _Rect()
        if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)) == 0:
            return None
        width = max(0, int(rect.right - rect.left))
        height = max(0, int(rect.bottom - rect.top))
        return {
            "x": int(rect.left),
            "y": int(rect.top),
            "width": width,
            "height": height,
        }

    def _on_webview_ready(self) -> None:
        self._apply_click_through_when_ready(self.options.click_through)

    def _apply_click_through_when_ready(
        self,
        enabled: bool,
        *,
        max_attempts: int = 10,
        retry_interval_sec: float = 0.05,
    ) -> bool:
        attempts = max(1, int(max_attempts))
        for index in range(attempts):
            hwnd = self._resolve_hwnd()
            if hwnd != 0:
                self._set_click_through_style(hwnd, enabled)
                return True
            if index + 1 < attempts:
                time.sleep(max(0.0, float(retry_interval_sec)))
        return False

    def _set_click_through_style(self, hwnd: int, enabled: bool) -> None:
        ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        layered = 0x00080000
        transparent = 0x00000020
        new_style = ex_style | layered
        if enabled:
            new_style |= transparent
        else:
            new_style &= ~transparent
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, new_style)

    def _resolve_hwnd(self) -> int:
        process = self._process
        if process is not None and process.poll() is None:
            title_hwnd = int(ctypes.windll.user32.FindWindowW(None, self.options.title))
            if title_hwnd != 0:
                return title_hwnd
        window = self._window
        if window is None:
            return 0
        native = getattr(window, "native", None)
        for candidate in (native, window):
            if candidate is None:
                continue
            for attr in ("Handle", "handle", "hwnd"):
                value = getattr(candidate, attr, None)
                if isinstance(value, int):
                    return value
        return 0

    def _build_child_command(self) -> list[str]:
        options_payload = {
            "url": self.options.url,
            "title": self.options.title,
            "transparent": bool(self.options.transparent),
            "frameless": bool(self.options.frameless),
            "on_top": bool(self.options.on_top),
            "click_through": bool(self.options.click_through),
            "width": int(self.options.width),
            "height": int(self.options.height),
            "x": int(self.options.x),
            "y": int(self.options.y),
            "open_devtools": bool(self.options.open_devtools),
        }
        return [
            sys.executable,
            str(self._script_dir() / "live2d_shell_window_process.py"),
            "--options-json",
            json.dumps(options_payload, ensure_ascii=False),
        ]

    def _script_dir(self) -> Path:
        return Path(__file__).resolve().parent

    def _import_webview(self) -> Any:
        try:
            import webview
        except Exception as exc:  # pragma: no cover - import depends on host environment
            raise RuntimeError("pywebview is required to start the SoulLink shell window") from exc
        return webview

    def _log_debug(self, message: str) -> None:
        if self.logger is not None and hasattr(self.logger, "debug"):
            self.logger.debug(message)

from __future__ import annotations

import argparse
import ctypes
import json
import time

from live2d_shell_window import SoulLinkShellWindowOptions


def _import_webview():
    try:
        import webview
    except Exception as exc:  # pragma: no cover - depends on host environment
        raise RuntimeError("pywebview is required to start the SoulLink shell window") from exc
    return webview


def _resolve_hwnd(window: object, title: str) -> int:
    native = getattr(window, "native", None)
    for candidate in (native, window):
        if candidate is None:
            continue
        for attr in ("Handle", "handle", "hwnd"):
            value = getattr(candidate, attr, None)
            if isinstance(value, int) and value != 0:
                return value
    return int(ctypes.windll.user32.FindWindowW(None, title))


def _set_click_through_style(hwnd: int, enabled: bool) -> None:
    ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
    layered = 0x00080000
    transparent = 0x00000020
    new_style = ex_style | layered
    if enabled:
        new_style |= transparent
    else:
        new_style &= ~transparent
    ctypes.windll.user32.SetWindowLongW(hwnd, -20, new_style)


def _apply_click_through_when_ready(window: object, title: str, enabled: bool) -> None:
    for index in range(60):
        hwnd = _resolve_hwnd(window, title)
        if hwnd != 0:
            _set_click_through_style(hwnd, enabled)
            return
        if index + 1 < 60:
            time.sleep(0.05)


def _parse_args() -> SoulLinkShellWindowOptions:
    parser = argparse.ArgumentParser()
    parser.add_argument("--options-json", required=True)
    args = parser.parse_args()
    payload = json.loads(args.options_json)
    return SoulLinkShellWindowOptions(**payload)


def main() -> None:
    options = _parse_args()
    webview = _import_webview()
    window = webview.create_window(
        options.title,
        options.url,
        transparent=options.transparent,
        frameless=options.frameless,
        on_top=options.on_top,
        resizable=True,
        zoomable=True,
        easy_drag=True,
        draggable=True,
        width=options.width,
        height=options.height,
        x=options.x,
        y=options.y,
    )

    def _on_ready() -> None:
        _apply_click_through_when_ready(window, options.title, options.click_through)

    webview.start(func=_on_ready, debug=options.open_devtools)


if __name__ == "__main__":
    main()

"""原生字幕桌面 UI 的状态与持久化辅助函数。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import json
import re


CHROMA_KEY_COLOR = "#00ff00"

DEFAULT_SUBTITLE_UI_SETTINGS: dict[str, Any] = {
    "box_width_px": 960,
    "box_height_px": 260,
    "left_px": 72,
    "bottom_px": 72,
    "background_color": "#0a0e16",
    "background_opacity": 0,
    "font_family": '"Microsoft YaHei UI", "PingFang SC", sans-serif',
    "font_size_px": 34,
    "text_color": "#f7f8fb",
}

_RGBA_PATTERN = re.compile(
    r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*([01]?(?:\.\d+)?))?\s*\)$",
    re.IGNORECASE,
)


def normalize_subtitle_ui_settings(
    settings: Mapping[str, Any] | None,
    *,
    defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """将任意字幕 UI 配置规整为原生界面可直接使用的字典。"""

    merged_defaults = dict(DEFAULT_SUBTITLE_UI_SETTINGS)
    if isinstance(defaults, Mapping):
        merged_defaults.update(dict(defaults))
    source = dict(merged_defaults)
    if isinstance(settings, Mapping):
        source.update(dict(settings))

    return {
        "box_width_px": _clamp_int(_pick(source, "box_width_px", "boxWidthPx"), 240, 2400, merged_defaults["box_width_px"]),
        "box_height_px": _clamp_int(
            _pick(source, "box_height_px", "boxHeightPx"), 96, 1200, merged_defaults["box_height_px"]
        ),
        "left_px": _clamp_int(_pick(source, "left_px", "leftPx"), 0, 2400, merged_defaults["left_px"]),
        "bottom_px": _clamp_int(_pick(source, "bottom_px", "bottomPx"), 0, 1600, merged_defaults["bottom_px"]),
        "background_color": normalize_hex_color(
            _pick(source, "background_color", "backgroundColor"),
            fallback=str(merged_defaults["background_color"]),
        ),
        "background_opacity": _clamp_int(
            _pick(source, "background_opacity", "backgroundOpacity"), 0, 100, merged_defaults["background_opacity"]
        ),
        "font_family": str(_pick(source, "font_family", "fontFamily") or merged_defaults["font_family"]).strip()
        or str(merged_defaults["font_family"]),
        "font_size_px": _clamp_int(_pick(source, "font_size_px", "fontSizePx"), 16, 144, merged_defaults["font_size_px"]),
        "text_color": normalize_hex_color(
            _pick(source, "text_color", "textColor"),
            fallback=str(merged_defaults["text_color"]),
        ),
    }


def subtitle_defaults_to_settings(subtitle_defaults: Mapping[str, Any] | None) -> dict[str, Any]:
    """将插件配置中的字幕默认值转换为原生 UI 设置。"""

    defaults = dict(subtitle_defaults or {})
    background_color, background_opacity = parse_background_defaults(
        _pick(defaults, "background_color", "backgroundColor")
    )
    return normalize_subtitle_ui_settings(
        {
            "box_width_px": _pick(defaults, "box_width_px", "boxWidthPx"),
            "box_height_px": _pick(defaults, "box_height_px", "boxHeightPx"),
            "left_px": _pick(defaults, "left_px", "leftPx"),
            "bottom_px": _pick(defaults, "bottom_px", "bottomPx"),
            "background_color": background_color,
            "background_opacity": background_opacity,
            "font_family": _pick(defaults, "font_family", "fontFamily"),
            "font_size_px": _pick(defaults, "font_size_px", "fontSizePx"),
            "text_color": _pick(defaults, "text_color", "textColor"),
        }
    )


def parse_background_defaults(raw_value: Any) -> tuple[str, int]:
    """解析配置中的 rgba/hex 背景值。"""

    normalized = str(raw_value or "").strip()
    match = _RGBA_PATTERN.match(normalized)
    if match is not None:
        red, green, blue, alpha = match.groups()
        opacity = 100
        if alpha is not None and alpha != "":
            try:
                opacity = round(max(0.0, min(1.0, float(alpha))) * 100)
            except ValueError:
                opacity = 100
        return rgb_to_hex(int(red), int(green), int(blue)), opacity
    normalized_hex = normalize_hex_color(normalized, fallback="")
    if normalized_hex:
        return normalized_hex, 100
    return str(DEFAULT_SUBTITLE_UI_SETTINGS["background_color"]), int(DEFAULT_SUBTITLE_UI_SETTINGS["background_opacity"])


def normalize_hex_color(raw_value: Any, *, fallback: str = "#f7f8fb") -> str:
    """规范化颜色值，只保留 6 位 hex。"""

    normalized = str(raw_value or "").strip().lower()
    if re.fullmatch(r"#[0-9a-f]{6}", normalized):
        return normalized
    if re.fullmatch(r"#[0-9a-f]{3}", normalized):
        return f"#{normalized[1] * 2}{normalized[2] * 2}{normalized[3] * 2}"
    return str(fallback or "").strip().lower()


def rgb_to_hex(red: int, green: int, blue: int) -> str:
    """将 RGB 通道转换为 hex 字符串。"""

    return f"#{_clamp_channel(red):02x}{_clamp_channel(green):02x}{_clamp_channel(blue):02x}"


def blend_hex_colors(base_color: str, overlay_color: str, overlay_alpha: float) -> str:
    """将 overlay 颜色按 alpha 混合到 base 上。"""

    alpha = max(0.0, min(1.0, float(overlay_alpha)))
    base_rgb = hex_to_rgb(base_color)
    overlay_rgb = hex_to_rgb(overlay_color)
    blended = []
    for base_channel, overlay_channel in zip(base_rgb, overlay_rgb, strict=True):
        blended.append(round(base_channel * (1.0 - alpha) + overlay_channel * alpha))
    return rgb_to_hex(*blended)


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    """将 6 位 hex 转换为 RGB 元组。"""

    normalized = normalize_hex_color(color, fallback="#000000")
    return (
        int(normalized[1:3], 16),
        int(normalized[3:5], 16),
        int(normalized[5:7], 16),
    )


def select_tk_font_family(font_family: str) -> str:
    """从 CSS 风格字体栈中挑一个 Tk 能直接识别的字体名。"""

    candidates = [item.strip().strip('"').strip("'") for item in str(font_family or "").split(",")]
    for candidate in candidates:
        if candidate:
            return candidate
    return "Microsoft YaHei UI"


class SubtitleUISettingsStore:
    """负责原生字幕 UI 设置的 JSON 持久化。"""

    def __init__(
        self,
        path: Path,
        *,
        defaults: Mapping[str, Any] | None = None,
    ) -> None:
        self.path = Path(path)
        self.defaults = normalize_subtitle_ui_settings(defaults)

    def load(self) -> dict[str, Any]:
        """读取已保存设置，不存在或损坏时回落到默认值。"""

        try:
            raw_text = self.path.read_text(encoding="utf-8")
            payload = json.loads(raw_text)
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
            payload = {}
        if not isinstance(payload, Mapping):
            payload = {}
        return normalize_subtitle_ui_settings(payload, defaults=self.defaults)

    def save(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """保存设置并返回规范化后的值。"""

        normalized = normalize_subtitle_ui_settings(settings, defaults=self.defaults)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        return normalized


def _pick(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _clamp_int(value: Any, minimum: int, maximum: int, fallback: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(fallback)
    return max(minimum, min(maximum, parsed))


def _clamp_channel(value: int) -> int:
    return max(0, min(255, int(value)))


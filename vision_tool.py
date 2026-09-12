"""Desktop screenshot capture and vision-model summarization."""

from __future__ import annotations

import base64
import ctypes
import sys

from dataclasses import dataclass
from io import BytesIO
from typing import Any, Awaitable, Callable, Mapping

from openai import AsyncOpenAI
from PIL import Image, ImageGrab

from src.config.config import config_manager
from src.config.model_configs import APIProvider, ModelInfo
from src.llm_models.openai_compat import build_openai_compatible_client_config, split_openai_request_overrides

from .config import VisionConfig


@dataclass(frozen=True)
class VisionImagePayload:
    """Compressed screenshot payload for OpenAI-compatible vision calls."""

    data_url: str
    mime_type: str
    width: int
    height: int
    original_width: int
    original_height: int
    byte_size: int


DescribeImage = Callable[[VisionImagePayload, str], Awaitable[str]]
ScreenshotProvider = Callable[[], Image.Image]


class VisionDesktopInspector:
    """Capture the current monitor, compress it, and ask a vision model for a summary."""

    def __init__(
        self,
        config: VisionConfig,
        *,
        screenshot_provider: ScreenshotProvider | None = None,
        describe_image: DescribeImage | None = None,
        logger: Any = None,
    ) -> None:
        self.config = config
        self.screenshot_provider = screenshot_provider or capture_current_monitor_image
        self.describe_image = describe_image or self._describe_with_model
        self.logger = logger

    async def inspect(self, question: str = "") -> dict[str, Any]:
        if not self.config.enabled:
            return {"success": False, "error": "desktop vision tool is not enabled"}
        normalized_question = _normalize_question(question)
        try:
            image = self.screenshot_provider()
            payload = compress_image_for_vision(
                image,
                max_image_edge_px=self.config.max_image_edge_px,
                jpeg_quality=self.config.jpeg_quality,
            )
            summary = (await self.describe_image(payload, normalized_question)).strip()
        except Exception as exc:
            _log(self.logger, "exception", f"Desktop vision inspection failed: {exc}")
            return {"success": False, "error": str(exc)}
        if not summary:
            return {"success": False, "error": "vision model returned an empty summary"}
        return {
            "success": True,
            "summary": summary,
            "model": self._configured_model_label(),
            "image": {
                "width": payload.width,
                "height": payload.height,
                "original_width": payload.original_width,
                "original_height": payload.original_height,
                "jpeg_bytes": payload.byte_size,
            },
        }

    async def _describe_with_model(self, payload: VisionImagePayload, question: str) -> str:
        provider, model_identifier, model_extra_params = self._resolve_provider_and_model()
        client_config = build_openai_compatible_client_config(provider)
        request_overrides = split_openai_request_overrides(
            {
                **model_extra_params,
                "enable_thinking": bool(self.config.enable_thinking),
            }
        )
        client = AsyncOpenAI(
            api_key=client_config.api_key,
            base_url=client_config.base_url,
            timeout=self.config.timeout_sec,
            max_retries=provider.max_retry,
            default_headers=client_config.default_headers or None,
            default_query=client_config.default_query or None,
        )
        response = await client.chat.completions.create(
            model=model_identifier,
            messages=[
                {"role": "system", "content": self.config.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": payload.data_url}},
                    ],
                },
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            extra_headers=request_overrides.extra_headers or None,
            extra_query=request_overrides.extra_query or None,
            extra_body=request_overrides.extra_body or None,
        )
        message = response.choices[0].message if response.choices else None
        return _message_content_to_text(getattr(message, "content", ""))

    def _resolve_provider_and_model(self) -> tuple[APIProvider, str, dict[str, Any]]:
        model_config = config_manager.get_model_config()
        models_by_name = {model.name: model for model in model_config.models}
        providers_by_name = {provider.name: provider for provider in model_config.api_providers}

        model_info: ModelInfo | None = None
        if self.config.model_name:
            model_info = models_by_name.get(self.config.model_name)
            if model_info is None:
                raise RuntimeError(f"Vision model_name not found in model_config: {self.config.model_name}")

        if model_info is not None:
            provider = providers_by_name.get(model_info.api_provider)
            if provider is None:
                raise RuntimeError(f"Vision provider not found in model_config: {model_info.api_provider}")
            return provider, model_info.model_identifier, dict(model_info.extra_params or {})

        provider = providers_by_name.get(self.config.api_provider)
        if provider is None:
            raise RuntimeError(f"Vision api_provider not found in model_config: {self.config.api_provider}")
        model_identifier = self.config.model_identifier.strip()
        if not model_identifier:
            raise RuntimeError("Vision model_identifier is empty.")
        return provider, model_identifier, {}

    def _configured_model_label(self) -> str:
        return self.config.model_name or self.config.model_identifier


def compress_image_for_vision(
    image: Image.Image,
    *,
    max_image_edge_px: int,
    jpeg_quality: int,
) -> VisionImagePayload:
    """Resize a screenshot and encode it as a JPEG data URL."""

    original_width, original_height = image.size
    max_edge = min(1920, max(256, int(max_image_edge_px or 960)))
    quality = min(95, max(35, int(jpeg_quality or 65)))
    compressed = _to_rgb(image)
    compressed.thumbnail((max_edge, max_edge), _resample_filter())
    output = BytesIO()
    compressed.save(output, format="JPEG", quality=quality, optimize=True)
    data = output.getvalue()
    encoded = base64.b64encode(data).decode("ascii")
    return VisionImagePayload(
        data_url=f"data:image/jpeg;base64,{encoded}",
        mime_type="image/jpeg",
        width=compressed.width,
        height=compressed.height,
        original_width=original_width,
        original_height=original_height,
        byte_size=len(data),
    )


def capture_current_monitor_image() -> Image.Image:
    """Capture the monitor that currently contains the mouse cursor."""

    bbox = _current_monitor_bbox()
    if bbox is not None:
        try:
            return ImageGrab.grab(bbox=bbox, all_screens=True)
        except TypeError:
            return ImageGrab.grab(bbox=bbox)
    try:
        return ImageGrab.grab(all_screens=True)
    except TypeError:
        return ImageGrab.grab()


def _current_monitor_bbox() -> tuple[int, int, int, int] | None:
    if sys.platform != "win32":
        return None
    try:
        return _windows_current_monitor_bbox()
    except Exception:
        return None


def _windows_current_monitor_bbox() -> tuple[int, int, int, int] | None:
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.c_ulong),
        ]

    point = POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        return None
    monitor = ctypes.windll.user32.MonitorFromPoint(point, 2)
    if not monitor:
        return None
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    rect = info.rcMonitor
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image.copy()
    if image.mode in {"RGBA", "LA"} or ("transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (0, 0, 0))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _resample_filter() -> Any:
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def _normalize_question(question: str) -> str:
    normalized = str(question or "").strip()
    if normalized:
        return normalized
    return "请用简短中文总结当前桌面画面，重点说明可见应用、游戏状态、重要文字和需要主播回应的变化。"


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text")
                if text is not None:
                    parts.append(str(text))
                continue
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(str(text))
        return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    return str(content or "").strip()


def _log(logger: Any, level: str, message: str) -> None:
    if logger is None:
        return
    method = getattr(logger, level, None)
    if callable(method):
        method(message)


"""Bilibili live adapter plugin package."""

try:
    from .plugin import BilibiliLiveAdapterPlugin, create_plugin
except Exception:  # pragma: no cover - allows lightweight unit imports without full MaiBot host env.
    BilibiliLiveAdapterPlugin = None  # type: ignore[assignment]
    create_plugin = None  # type: ignore[assignment]

__all__ = ["BilibiliLiveAdapterPlugin", "create_plugin"]

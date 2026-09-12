"""livehub 共享工具函数（供 capture / hub / server 复用）。

数值归一化（as_float / normalize_epoch_seconds）统一由
``livehub.bilibili_protocol`` 提供（与插件侧共享的单一实现），此处仅 re-export。
"""

from __future__ import annotations

from typing import Any

from .bilibili_protocol import as_float, normalize_epoch_seconds  # noqa: F401 - re-export


def normalize_text(value: Any) -> str:
    """将任意值规范化为去除首尾空白的字符串；None / 空值返回空串。"""
    return str(value or "").strip()


def normalize_duration_ms(value: Any) -> int:
    """将 expected_duration_ms 规范化为非负整数；非法值回退 0。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

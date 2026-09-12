"""Structured payload helpers for the SoulLink transparent shell runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any


@dataclass(frozen=True)
class ShellLoadModelMessage:
    """Request the shell frontend to load or switch models."""

    model: dict[str, Any]
    type: str = field(default="load_model", init=False)


@dataclass(frozen=True)
class ShellExpressionMessage:
    """Apply a one-shot expression update in the shell frontend."""

    parameters: dict[str, float]
    duration_ms: int
    type: str = field(default="expression", init=False)


@dataclass(frozen=True)
class ShellTtsMotionFrameMessage:
    """Apply one SoulLink motion frame in the shell frontend."""

    timeline_id: str
    parameters: dict[str, float]
    duration_ms: int
    offset_ms: int
    type: str = field(default="tts_motion_frame", init=False)


def message_to_payload(message: Any) -> dict[str, Any]:
    """Convert a shell message dataclass into a JSON-ready payload."""

    if not is_dataclass(message):
        raise TypeError("message_to_payload expects a dataclass instance")
    return asdict(message)

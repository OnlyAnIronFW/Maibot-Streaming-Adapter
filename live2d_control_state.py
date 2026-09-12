"""Persistent Live2D control state for mouse-follow behavior."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Literal, Mapping


Live2DControlStatus = Literal["ai", "mouse", "cooldown", "disabled"]


@dataclass(frozen=True)
class Live2DControlState:
    mouse_follow_enabled: bool = False
    mouse_follow_status: Live2DControlStatus = "disabled"
    last_mouse_activity_ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mouse_follow_enabled": bool(self.mouse_follow_enabled),
            "mouse_follow_status": self.mouse_follow_status,
            "last_mouse_activity_ts": float(self.last_mouse_activity_ts),
        }

    def merge_patch(self, patch: Mapping[str, Any] | None) -> "Live2DControlState":
        if not isinstance(patch, Mapping):
            return self
        return Live2DControlState(
            mouse_follow_enabled=_coerce_bool(patch.get("mouse_follow_enabled"), self.mouse_follow_enabled),
            mouse_follow_status=_coerce_status(patch.get("mouse_follow_status"), self.mouse_follow_status),
            last_mouse_activity_ts=_coerce_float(patch.get("last_mouse_activity_ts"), self.last_mouse_activity_ts),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "Live2DControlState | None":
        if not isinstance(payload, Mapping):
            return None
        return DEFAULT_LIVE2D_CONTROL_STATE.merge_patch(payload)


DEFAULT_LIVE2D_CONTROL_STATE = Live2DControlState()


class Live2DControlStateStore:
    """Load, save, and patch the Live2D control state JSON file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> Live2DControlState:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
            return DEFAULT_LIVE2D_CONTROL_STATE
        state = Live2DControlState.from_dict(payload)
        return state if state is not None else DEFAULT_LIVE2D_CONTROL_STATE

    def save(self, state: Live2DControlState) -> Live2DControlState:
        normalized = Live2DControlState.from_dict(state.to_dict()) or DEFAULT_LIVE2D_CONTROL_STATE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(normalized.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return normalized

    def merge_patch(self, patch: Mapping[str, Any] | None) -> Live2DControlState:
        merged = self.load().merge_patch(patch)
        self.save(merged)
        return merged


def _coerce_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(fallback)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return bool(fallback)
    return bool(value)


def _coerce_status(value: Any, fallback: Live2DControlStatus) -> Live2DControlStatus:
    normalized = str(value or "").strip().lower()
    if normalized in {"ai", "mouse", "cooldown", "disabled"}:
        return normalized  # type: ignore[return-value]
    return fallback


def _coerce_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)

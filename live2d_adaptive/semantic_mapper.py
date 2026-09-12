"""Map high-level Live2D intents to concrete parameter batches."""

from __future__ import annotations

from typing import Any, Mapping

import math

from .profile import ParameterProfile


class Live2DSemanticMapper:
    """Build parameter batches from semantic intents."""

    def __init__(self, profile: ParameterProfile) -> None:
        self.profile = profile

    def build_intent(
        self,
        intent: str,
        *,
        intensity: float = 0.6,
        target: Mapping[str, Any] | None = None,
    ) -> list[dict[str, float | str]]:
        """Build parameter payloads for a high-level intent."""

        normalized_intent = str(intent or "").strip().lower()
        normalized_intensity = min(1.0, max(0.0, float(intensity)))
        target = target or {}
        builders = {
            "speak": self._speak,
            "look_at": self._look_at,
            "react_happy": self._happy,
            "react_surprised": self._surprised,
            "react_shy": self._shy,
            "react_confused": self._confused,
            "react_emphasis": self._emphasis,
            "toggle_accessory": self._toggle_accessory,
            "idle_breath": self._idle_breath,
            "idle_sway": self._idle_sway,
        }
        builder = builders.get(normalized_intent)
        if builder is None:
            return []
        return builder(normalized_intensity, target)

    def reset_payloads(self, roles: list[str] | None = None) -> list[dict[str, float | str]]:
        """Build reset payloads for matching semantic roles."""

        role_prefixes = tuple(roles or ["head.", "body.", "eye.", "mouth.", "face.", "accessory."])
        payloads: list[dict[str, float | str]] = []
        for parameter in self.profile.parameters.values():
            if not parameter.enabled or not parameter.role.startswith(role_prefixes):
                continue
            payloads.append({"id": parameter.id, "value": parameter.default, "weight": 0.7})
        return payloads

    def _resolve(self, role: str, value: float, *, weight: float = 0.7) -> list[dict[str, float | str]]:
        payload = self.profile.resolve(role, value, weight=weight)
        return [payload] if payload else []

    def _resolve_any(self, roles: list[str], value: float, *, weight: float = 0.7) -> list[dict[str, float | str]]:
        for role in roles:
            payload = self.profile.resolve(role, value, weight=weight)
            if payload:
                return [payload]
        return []

    def build_sway(
        self,
        phase: float,
        *,
        intensity: float = 0.3,
        speaking: bool = False,
    ) -> list[dict[str, float | str]]:
        """Build natural body/head sway parameters from a normalized phase."""

        normalized_intensity = min(1.0, max(0.0, float(intensity)))
        phase = float(phase)
        if speaking:
            body_yaw_amp = 0.10 + normalized_intensity * 0.24
            body_pitch_amp = 0.05 + normalized_intensity * 0.14
            body_roll_amp = 0.05 + normalized_intensity * 0.12
            head_yaw_amp = 0.06 + normalized_intensity * 0.16
            head_pitch_amp = 0.04 + normalized_intensity * 0.10
            body_weight = 0.30 + normalized_intensity * 0.30
            head_weight = 0.25 + normalized_intensity * 0.25
        else:
            body_yaw_amp = 0.06 + normalized_intensity * 0.09
            body_pitch_amp = 0.03 + normalized_intensity * 0.05
            body_roll_amp = 0.025 + normalized_intensity * 0.045
            head_yaw_amp = 0.025 + normalized_intensity * 0.05
            head_pitch_amp = 0.02 + normalized_intensity * 0.035
            body_weight = 0.18 + normalized_intensity * 0.12
            head_weight = 0.16 + normalized_intensity * 0.10
        return [
            *self._resolve("body.yaw", math.sin(phase) * body_yaw_amp, weight=body_weight),
            *self._resolve("body.pitch", math.sin(phase * 0.72 + 0.8) * body_pitch_amp, weight=body_weight),
            *self._resolve("body.roll", -math.sin(phase * 0.88 + 0.35) * body_roll_amp, weight=body_weight),
            *self._resolve("head.yaw", math.sin(phase + 0.45) * head_yaw_amp, weight=head_weight),
            *self._resolve("head.pitch", math.sin(phase * 0.65 + 1.2) * head_pitch_amp, weight=head_weight),
        ]

    def _speak(self, intensity: float, target: Mapping[str, Any]) -> list[dict[str, float | str]]:
        text = str(target.get("text") or "")
        punctuation_boost = 0.2 if any(mark in text for mark in "!?？！") else 0.0
        open_value = min(1.0, 0.25 + intensity * 0.6 + punctuation_boost)
        form_value = 0.25 if any(mark in text for mark in "!?？！") else 0.05
        return [
            *self._resolve("mouth.open", open_value, weight=0.9),
            *self._resolve("mouth.form", form_value, weight=0.7),
        ]

    def _look_at(self, intensity: float, target: Mapping[str, Any]) -> list[dict[str, float | str]]:
        x = _coerce_unit(target.get("x"), 0.0)
        y = _coerce_unit(target.get("y"), 0.0)
        head_weight = 0.45 + intensity * 0.25
        eye_weight = 0.65 + intensity * 0.2
        return [
            *self._resolve("head.yaw", x * intensity, weight=head_weight),
            *self._resolve("head.pitch", y * intensity, weight=head_weight),
            *self._resolve("eye.gaze.x", x, weight=eye_weight),
            *self._resolve("eye.gaze.y", y, weight=eye_weight),
        ]

    def _happy(self, intensity: float, target: Mapping[str, Any]) -> list[dict[str, float | str]]:
        del target
        return [
            *self._resolve("mouth.smile", intensity, weight=0.85),
            *self._resolve("eye.left.smile", intensity, weight=0.75),
            *self._resolve("eye.right.smile", intensity, weight=0.75),
            *self._resolve("face.blush", intensity * 0.35, weight=0.5),
            *self._resolve("head.roll", intensity * 0.25, weight=0.45),
        ]

    def _surprised(self, intensity: float, target: Mapping[str, Any]) -> list[dict[str, float | str]]:
        del target
        return [
            *self._resolve("mouth.open", 0.35 + intensity * 0.5, weight=0.9),
            *self._resolve("eye.left.open", 1.0, weight=0.8),
            *self._resolve("eye.right.open", 1.0, weight=0.8),
            *self._resolve_any(["eye.open"], 1.0, weight=0.75),
            *self._resolve("head.pitch", intensity * 0.35, weight=0.45),
        ]

    def _shy(self, intensity: float, target: Mapping[str, Any]) -> list[dict[str, float | str]]:
        del target
        return [
            *self._resolve("face.blush", intensity, weight=0.8),
            *self._resolve("head.pitch", -0.35 * intensity, weight=0.55),
            *self._resolve("head.yaw", -0.25 * intensity, weight=0.45),
            *self._resolve("eye.gaze.x", -0.5 * intensity, weight=0.65),
            *self._resolve("mouth.smile", intensity * 0.35, weight=0.5),
        ]

    def _confused(self, intensity: float, target: Mapping[str, Any]) -> list[dict[str, float | str]]:
        del target
        return [
            *self._resolve("head.roll", -0.35 * intensity, weight=0.55),
            *self._resolve("head.yaw", 0.2 * intensity, weight=0.45),
            *self._resolve("mouth.form", -0.35 * intensity, weight=0.65),
        ]

    def _emphasis(self, intensity: float, target: Mapping[str, Any]) -> list[dict[str, float | str]]:
        del target
        return [
            *self._resolve("body.pitch", 0.35 * intensity, weight=0.55),
            *self._resolve("head.pitch", 0.25 * intensity, weight=0.55),
            *self._resolve("mouth.open", 0.45 * intensity, weight=0.85),
        ]

    def _toggle_accessory(self, intensity: float, target: Mapping[str, Any]) -> list[dict[str, float | str]]:
        name = str(target.get("name") or target.get("accessory") or "").strip().lower()
        state = target.get("enabled", target.get("state", True))
        value = intensity if bool(state) else 0.0
        roles = [f"accessory.{name}"] if name else []
        roles.append("accessory.toggle")
        return self._resolve_any(roles, value, weight=1.0)

    def _idle_breath(self, intensity: float, target: Mapping[str, Any]) -> list[dict[str, float | str]]:
        del target
        return [
            *self._resolve("body.pitch", 0.08 * intensity, weight=0.25),
            *self._resolve("mouth.open", 0.04 * intensity, weight=0.2),
        ]

    def _idle_sway(self, intensity: float, target: Mapping[str, Any]) -> list[dict[str, float | str]]:
        phase = _coerce_float(target.get("phase"), 0.0)
        return self.build_sway(phase, intensity=intensity, speaking=False)


def _coerce_unit(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(1.0, max(-1.0, parsed))


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

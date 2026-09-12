"""Live2D parameter profile and semantic role inference."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

FORBIDDEN_ROLE_PREFIXES = ("hand.", "arm.", "leg.", "foot.", "gesture.")
FORBIDDEN_ID_TOKENS = ("hand", "arm", "leg", "foot", "finger", "gesture")
ALLOWED_ROLE_PREFIXES = ("head.", "body.", "eye.", "mouth.", "face.", "accessory.", "brow.")


@dataclass(frozen=True)
class ParameterSpec:
    """A discovered Live2D model parameter."""

    id: str
    minimum: float = -1.0
    maximum: float = 1.0
    default: float = 0.0
    current: float | None = None
    role: str = ""
    confidence: float = 0.0
    safe_amplitude: float = 0.8
    enabled: bool = True

    def clamp(self, value: float) -> float:
        """Clamp a raw value into this parameter range."""

        lower = min(self.minimum, self.maximum)
        upper = max(self.minimum, self.maximum)
        return min(upper, max(lower, float(value)))

    def semantic_to_raw(self, semantic_value: float) -> float:
        """Map a semantic value in [-1, 1] or [0, 1] to the real parameter range."""

        value = max(-1.0, min(1.0, float(semantic_value)))
        span_negative = self.default - self.minimum
        span_positive = self.maximum - self.default
        if value < 0:
            raw = self.default + value * span_negative * self.safe_amplitude
        else:
            raw = self.default + value * span_positive * self.safe_amplitude
        return self.clamp(raw)


class ParameterProfile:
    """A model-specific Live2D parameter profile."""

    def __init__(
        self,
        *,
        model_id: str = "",
        model_name: str = "",
        parameters: list[ParameterSpec] | None = None,
        groups: Mapping[str, list[str]] | None = None,
        min_confidence: float = 0.6,
    ) -> None:
        self.model_id = model_id
        self.model_name = model_name
        self.groups = {str(key): list(value) for key, value in dict(groups or {}).items()}
        self.min_confidence = min(1.0, max(0.0, float(min_confidence)))
        self.parameters: dict[str, ParameterSpec] = {}
        self.disabled_parameter_ids: set[str] = set()
        for parameter in parameters or []:
            self.add(parameter)
        self.apply_reference_hints()

    @classmethod
    def from_capabilities(
        cls,
        payload: Mapping[str, Any],
        *,
        min_confidence: float = 0.6,
        overrides: Mapping[str, Any] | None = None,
    ) -> "ParameterProfile":
        """Build a profile from a custom bridge capabilities response."""

        raw_parameters = payload.get("parameters", [])
        groups = payload.get("groups", {})
        profile = cls(
            model_id=str(payload.get("model_id") or ""),
            model_name=str(payload.get("model_name") or ""),
            groups=groups if isinstance(groups, Mapping) else {},
            min_confidence=min_confidence,
        )
        if isinstance(raw_parameters, list):
            for raw_parameter in raw_parameters:
                spec = _spec_from_mapping(raw_parameter)
                if spec is not None:
                    profile.add(profile.infer(spec))
        profile.apply_reference_hints()
        profile.apply_overrides(overrides or {})
        return profile

    @classmethod
    def from_vts_parameters(
        cls,
        parameters: list[Mapping[str, Any]],
        *,
        min_confidence: float = 0.6,
        overrides: Mapping[str, Any] | None = None,
    ) -> "ParameterProfile":
        """Build a profile from VTube Studio style parameter records."""

        profile = cls(model_id="vts", model_name="VTube Studio", min_confidence=min_confidence)
        for raw_parameter in parameters:
            spec = _spec_from_mapping(
                {
                    "id": raw_parameter.get("name") or raw_parameter.get("parameterName") or raw_parameter.get("id"),
                    "min": _first_present(raw_parameter, "min", "minimum"),
                    "max": _first_present(raw_parameter, "max", "maximum"),
                    "default": _first_present(raw_parameter, "default", "defaultValue"),
                    "current": _first_present(raw_parameter, "value", "current", "currentValue"),
                }
            )
            if spec is not None:
                profile.add(profile.infer(spec))
        profile.apply_reference_hints()
        profile.apply_overrides(overrides or {})
        return profile

    @classmethod
    def standard_fallback(
        cls,
        *,
        min_confidence: float = 0.6,
        overrides: Mapping[str, Any] | None = None,
    ) -> "ParameterProfile":
        """Build a useful profile from common Live2D standard parameter ids."""

        profile = cls(model_id="standard", model_name="Live2D Standard", min_confidence=min_confidence)
        for spec in STANDARD_PARAMETERS:
            profile.add(profile.infer(spec))
        profile.apply_reference_hints()
        profile.apply_overrides(overrides or {})
        return profile

    def add(self, parameter: ParameterSpec) -> None:
        """Add a parameter, respecting disabled roles and ids."""

        inferred = self.infer(parameter)
        if not inferred.enabled:
            self.disabled_parameter_ids.add(inferred.id)
        self.parameters[inferred.id] = inferred

    def infer(self, parameter: ParameterSpec) -> ParameterSpec:
        """Infer role and confidence for a parameter."""

        role, confidence = infer_role(parameter.id, self.groups)
        if parameter.role:
            role = parameter.role
            confidence = max(confidence, parameter.confidence or 0.95)
        else:
            confidence = max(confidence, parameter.confidence)
        enabled = parameter.enabled and is_allowed_role(role) and not is_forbidden_id(parameter.id)
        if role.startswith("accessory."):
            confidence = max(confidence, 0.7)
        return replace(parameter, role=role, confidence=confidence, enabled=enabled)

    def apply_overrides(self, overrides: Mapping[str, Any]) -> None:
        """Apply user-provided role/range overrides."""

        for parameter_id, raw_override in overrides.items():
            if not isinstance(raw_override, Mapping):
                raw_override = _model_to_mapping(raw_override)
            current = self.parameters.get(str(parameter_id))
            role = str(raw_override.get("role") or (current.role if current else "")).strip()
            minimum = _optional_float(raw_override.get("min"))
            maximum = _optional_float(raw_override.get("max"))
            default = _optional_float(raw_override.get("default"))
            safe_amplitude = _optional_float(raw_override.get("safe_amplitude"))
            enabled = bool(raw_override.get("enabled", True))
            if current is None:
                current = ParameterSpec(id=str(parameter_id))
            updated = replace(
                current,
                minimum=current.minimum if minimum is None else minimum,
                maximum=current.maximum if maximum is None else maximum,
                default=current.default if default is None else default,
                role=role or current.role,
                confidence=max(current.confidence, 0.95 if role else current.confidence),
                safe_amplitude=current.safe_amplitude if safe_amplitude is None else safe_amplitude,
                enabled=enabled,
            )
            self.parameters[updated.id] = self.infer(updated)

    def apply_reference_hints(self) -> None:
        """Apply model-specific range and role hints derived from reference motions."""

        reference_hints = _match_reference_hints(self.model_id, self.model_name)
        if not reference_hints:
            return
        for parameter_id, raw_hint in reference_hints.items():
            current = self.parameters.get(str(parameter_id))
            if current is None:
                continue
            minimum = _optional_float(raw_hint.get("min"))
            maximum = _optional_float(raw_hint.get("max"))
            default = _optional_float(raw_hint.get("default"))
            safe_amplitude = _optional_float(raw_hint.get("safe_amplitude"))
            confidence = _optional_float(raw_hint.get("confidence"))
            role = str(raw_hint.get("role") or current.role).strip()
            enabled = bool(raw_hint.get("enabled", current.enabled))
            updated = replace(
                current,
                minimum=current.minimum if minimum is None else minimum,
                maximum=current.maximum if maximum is None else maximum,
                default=current.default if default is None else default,
                role=role or current.role,
                confidence=max(current.confidence, 0.0 if confidence is None else confidence),
                safe_amplitude=current.safe_amplitude if safe_amplitude is None else safe_amplitude,
                enabled=enabled,
            )
            self.parameters[updated.id] = self.infer(updated)

    def find_by_role(self, role: str) -> ParameterSpec | None:
        """Find the best enabled parameter for a semantic role."""

        candidates = [
            parameter
            for parameter in self.parameters.values()
            if parameter.enabled and parameter.role == role and parameter.confidence >= self.min_confidence
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: item.confidence, reverse=True)[0]

    def resolve(self, role: str, semantic_value: float, *, weight: float = 0.7) -> dict[str, float | str] | None:
        """Resolve a semantic role/value into a bridge parameter payload."""

        parameter = self.find_by_role(role)
        if parameter is None:
            return None
        value = parameter.clamp(semantic_value) if role == "mouth.form" else parameter.semantic_to_raw(semantic_value)
        return {
            "id": parameter.id,
            "value": value,
            "weight": min(1.0, max(0.0, float(weight))),
        }

    def mark_failed(self, parameter_id: str) -> None:
        """Disable a parameter for this runtime session after a bridge failure."""

        parameter = self.parameters.get(parameter_id)
        if parameter is None:
            self.disabled_parameter_ids.add(parameter_id)
            return
        self.parameters[parameter_id] = replace(parameter, enabled=False, confidence=0.0)
        self.disabled_parameter_ids.add(parameter_id)


STANDARD_PARAMETERS = [
    ParameterSpec("ParamAngleX", -30.0, 30.0, 0.0),
    ParameterSpec("ParamAngleY", -30.0, 30.0, 0.0),
    ParameterSpec("ParamAngleZ", -30.0, 30.0, 0.0),
    ParameterSpec("ParamBodyAngleX", -10.0, 10.0, 0.0),
    ParameterSpec("ParamBodyAngleY", -10.0, 10.0, 0.0),
    ParameterSpec("ParamBodyAngleZ", -10.0, 10.0, 0.0),
    ParameterSpec("ParamEyeLOpen", 0.0, 1.0, 1.0),
    ParameterSpec("ParamEyeROpen", 0.0, 1.0, 1.0),
    ParameterSpec("ParamEyeLSmile", 0.0, 1.0, 0.0),
    ParameterSpec("ParamEyeRSmile", 0.0, 1.0, 0.0),
    ParameterSpec("ParamEyeBallX", -1.0, 1.0, 0.0),
    ParameterSpec("ParamEyeBallY", -1.0, 1.0, 0.0),
    ParameterSpec("ParamMouthOpenY", 0.0, 1.0, 0.0),
    ParameterSpec("ParamMouthForm", -2.0, 1.0, 0.0),
    ParameterSpec("ParamMouthSmile", 0.0, 1.0, 0.0),
    ParameterSpec("ParamBrowLY", -1.0, 1.0, 0.0),
    ParameterSpec("ParamBrowRY", -1.0, 1.0, 0.0),
    ParameterSpec("ParamCheek", 0.0, 1.0, 0.0),
    ParameterSpec("ParamBreath", 0.0, 1.0, 0.0),
]


REFERENCE_MOTION_HINTS: dict[str, dict[str, Any]] = {
    "hiyori": {
        "identity_tokens": ("hiyori",),
        "parameters": {
            "ParamAngleX": {"role": "head.yaw", "min": -30.0, "max": 30.0, "default": 0.0, "safe_amplitude": 1.90},
            "ParamAngleY": {"role": "head.pitch", "min": -30.0, "max": 30.0, "default": 0.0, "safe_amplitude": 1.90},
            "ParamAngleZ": {"role": "head.roll", "min": -30.0, "max": 30.0, "default": 0.0, "safe_amplitude": 1.85},
            "ParamBodyAngleX": {"role": "body.yaw", "min": -10.0, "max": 10.0, "default": 0.0, "safe_amplitude": 2.20},
            "ParamBodyAngleY": {"role": "body.pitch", "min": -10.0, "max": 10.0, "default": 0.0, "safe_amplitude": 2.20},
            "ParamBodyAngleZ": {"role": "body.roll", "min": -10.0, "max": 10.0, "default": 0.0, "safe_amplitude": 2.10},
            "ParamEyeLOpen": {"role": "eye.left.open", "min": 0.0, "max": 1.2, "default": 1.0, "safe_amplitude": 0.45},
            "ParamEyeROpen": {"role": "eye.right.open", "min": 0.0, "max": 1.2, "default": 1.0, "safe_amplitude": 0.45},
            "ParamEyeLSmile": {"role": "eye.left.smile", "min": 0.0, "max": 1.0, "default": 0.0, "safe_amplitude": 1.18},
            "ParamEyeRSmile": {"role": "eye.right.smile", "min": 0.0, "max": 1.0, "default": 0.0, "safe_amplitude": 1.18},
            "ParamBrowLY": {"role": "brow.left.y", "min": -1.0, "max": 1.0, "default": 0.0, "safe_amplitude": 1.32},
            "ParamBrowRY": {"role": "brow.right.y", "min": -1.0, "max": 1.0, "default": 0.0, "safe_amplitude": 1.32},
            "ParamBrowLAngle": {"role": "brow.left.angle", "min": -1.0, "max": 1.0, "default": 0.0, "safe_amplitude": 1.36},
            "ParamBrowRAngle": {"role": "brow.right.angle", "min": -1.0, "max": 1.0, "default": 0.0, "safe_amplitude": 1.36},
            "ParamBrowLForm": {"role": "brow.left.form", "min": -1.0, "max": 1.0, "default": 0.0, "safe_amplitude": 1.30},
            "ParamBrowRForm": {"role": "brow.right.form", "min": -1.0, "max": 1.0, "default": 0.0, "safe_amplitude": 1.30},
            "ParamCheek": {"role": "face.blush", "min": 0.0, "max": 1.0, "default": 0.0, "safe_amplitude": 1.20},
            "ParamShoulder": {"role": "body.shoulder", "min": -1.0, "max": 1.0, "default": 0.0, "safe_amplitude": 2.55},
            "ParamHairAhoge": {"role": "accessory.ahoge", "min": -10.0, "max": 10.0, "default": 0.0, "safe_amplitude": 0.72},
        },
    }
}


EXACT_ROLE_BY_ID = {
    "paramanglex": "head.yaw",
    "paramangley": "head.pitch",
    "paramanglez": "head.roll",
    "parambodyanglex": "body.yaw",
    "parambodyangley": "body.pitch",
    "parambodyanglez": "body.roll",
    "parameyelopen": "eye.left.open",
    "parameyeropen": "eye.right.open",
    "parameyelsmile": "eye.left.smile",
    "parameyersmile": "eye.right.smile",
    "parameyeballx": "eye.gaze.x",
    "parameyebally": "eye.gaze.y",
    "parammouthopeny": "mouth.open",
    "parammouthform": "mouth.form",
    "parammouthsmile": "mouth.smile",
    "parambrowly": "brow.left.y",
    "parambrowry": "brow.right.y",
    "parambrowlangle": "brow.left.angle",
    "parambrowrangle": "brow.right.angle",
    "parambrowlform": "brow.left.form",
    "parambrowrform": "brow.right.form",
    "paramcheek": "face.blush",
    "parambreath": "breath",
    "paramshoulder": "body.shoulder",
    "paramhairahoge": "accessory.ahoge",
}


def infer_role(parameter_id: str, groups: Mapping[str, list[str]] | None = None) -> tuple[str, float]:
    """Infer a semantic role for a Live2D parameter id."""

    normalized_id = parameter_id.strip()
    lowered = normalized_id.lower()
    if lowered in EXACT_ROLE_BY_ID:
        return EXACT_ROLE_BY_ID[lowered], 1.0
    if is_forbidden_id(normalized_id):
        return "gesture.blocked", 1.0
    for group_name, ids in dict(groups or {}).items():
        lowered_group = group_name.lower()
        if normalized_id not in ids:
            continue
        if "eyeblink" in lowered_group:
            return "eye.open", 0.9
        if "lipsync" in lowered_group:
            return "mouth.open", 0.9
    compact = lowered.replace("_", "").replace("-", "")
    if "accessory" in compact or "glasses" in compact or "hat" in compact:
        suffix = compact.replace("param", "").replace("accessory", "") or "toggle"
        return f"accessory.{suffix}", 0.7
    if "mouth" in compact and ("open" in compact or "y" in compact):
        return "mouth.open", 0.7
    if "mouth" in compact and ("form" in compact or "shape" in compact):
        return "mouth.form", 0.7
    if "mouth" in compact and "smile" in compact:
        return "mouth.smile", 0.75
    if "eye" in compact and "open" in compact:
        return "eye.open", 0.65
    if "eye" in compact and ("ballx" in compact or "gazex" in compact):
        return "eye.gaze.x", 0.7
    if "eye" in compact and ("bally" in compact or "gazey" in compact):
        return "eye.gaze.y", 0.7
    if "brow" in compact and ("left" in compact or "ly" in compact or compact.endswith("l")):
        return "brow.left.y", 0.7
    if "brow" in compact and ("right" in compact or "ry" in compact or compact.endswith("r")):
        return "brow.right.y", 0.7
    if "cheek" in compact or "blush" in compact:
        return "face.blush", 0.7
    if "shoulder" in compact:
        return "body.shoulder", 0.7
    if "ahoge" in compact:
        return "accessory.ahoge", 0.75
    if "breath" in compact:
        return "breath", 0.8
    return "", 0.0


def is_allowed_role(role: str) -> bool:
    """Return whether a semantic role is inside the requested Live2D control surface."""

    if not role:
        return False
    if role == "breath":
        return True
    if role.startswith(FORBIDDEN_ROLE_PREFIXES):
        return False
    return role.startswith(ALLOWED_ROLE_PREFIXES)


def is_forbidden_id(parameter_id: str) -> bool:
    """Return whether a parameter id looks like a hand/arm/leg/foot control."""

    lowered = parameter_id.lower()
    return any(token in lowered for token in FORBIDDEN_ID_TOKENS)


def _spec_from_mapping(raw_parameter: Any) -> ParameterSpec | None:
    if not isinstance(raw_parameter, Mapping):
        return None
    parameter_id = str(raw_parameter.get("id") or raw_parameter.get("name") or "").strip()
    if not parameter_id:
        return None
    minimum = _optional_float(raw_parameter.get("min"))
    maximum = _optional_float(raw_parameter.get("max"))
    default = _optional_float(raw_parameter.get("default", raw_parameter.get("defaultValue")))
    current = _optional_float(raw_parameter.get("current", raw_parameter.get("value", raw_parameter.get("currentValue"))))
    return ParameterSpec(
        id=parameter_id,
        minimum=-1.0 if minimum is None else minimum,
        maximum=1.0 if maximum is None else maximum,
        default=0.0 if default is None else default,
        current=current,
    )


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _model_to_mapping(value: Any) -> Mapping[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, Mapping) else {}
    if hasattr(value, "dict"):
        dumped = value.dict()
        return dumped if isinstance(dumped, Mapping) else {}
    return {}


def _match_reference_hints(model_id: str, model_name: str) -> Mapping[str, Mapping[str, Any]]:
    identity = f"{model_id} {model_name}".strip().lower()
    if not identity:
        return {}
    for raw_hint in REFERENCE_MOTION_HINTS.values():
        identity_tokens = tuple(str(token).strip().lower() for token in raw_hint.get("identity_tokens", ()))
        if identity_tokens and any(token and token in identity for token in identity_tokens):
            parameters = raw_hint.get("parameters")
            if isinstance(parameters, Mapping):
                return parameters
    return {}

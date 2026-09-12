"""Live2D capability discovery helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import json

from .bridge import Live2DBridgeProtocol
from .profile import ParameterProfile, ParameterSpec


class CapabilityProbe:
    """Discover Live2D parameters from bridge, VTS, model files, or fallback data."""

    def __init__(self, bridge: Live2DBridgeProtocol | None = None, logger: Any = None) -> None:
        self.bridge = bridge
        self.logger = logger

    async def discover(
        self,
        *,
        driver: str = "auto",
        model_path: str = "",
        min_confidence: float = 0.6,
        overrides: Mapping[str, Any] | None = None,
    ) -> ParameterProfile:
        """Discover a Live2D profile using the configured order."""

        normalized_driver = str(driver or "auto").strip().lower()
        if normalized_driver in {"auto", "json"} and self.bridge is not None:
            bridge_profile = await self._from_bridge(min_confidence=min_confidence, overrides=overrides)
            if bridge_profile is not None and bridge_profile.parameters:
                return bridge_profile
        if normalized_driver in {"auto", "vts"} and self.bridge is not None:
            vts_profile = await self._from_vts(min_confidence=min_confidence, overrides=overrides)
            if vts_profile is not None and vts_profile.parameters:
                return vts_profile
        if normalized_driver in {"auto", "model_file"} and model_path:
            model_profile = self.from_model_path(model_path, min_confidence=min_confidence, overrides=overrides)
            if model_profile is not None and model_profile.parameters:
                return model_profile
        return ParameterProfile.standard_fallback(min_confidence=min_confidence, overrides=overrides)

    async def _from_bridge(
        self,
        *,
        min_confidence: float,
        overrides: Mapping[str, Any] | None,
    ) -> ParameterProfile | None:
        try:
            payload = await self.bridge.request_capabilities() if self.bridge is not None else None
        except Exception as exc:
            self._log_warning(f"Live2D capability request failed: {exc}")
            return None
        if not isinstance(payload, Mapping):
            return None
        if isinstance(payload.get("data"), Mapping):
            payload = payload["data"]
        parameters = payload.get("parameters")
        if isinstance(parameters, list):
            return ParameterProfile.from_capabilities(payload, min_confidence=min_confidence, overrides=overrides)
        vts_parameters = payload.get("modelParameters") or payload.get("parameters")
        if isinstance(vts_parameters, list):
            return ParameterProfile.from_vts_parameters(
                vts_parameters,
                min_confidence=min_confidence,
                overrides=overrides,
            )
        return None

    async def _from_vts(
        self,
        *,
        min_confidence: float,
        overrides: Mapping[str, Any] | None,
    ) -> ParameterProfile | None:
        request_method = getattr(self.bridge, "request_vts_parameters", None)
        if request_method is None:
            return None
        try:
            payload = await request_method()
        except Exception as exc:
            self._log_warning(f"VTube Studio parameter request failed: {exc}")
            return None
        if not isinstance(payload, Mapping):
            return None
        parameters = payload.get("parameters")
        if not isinstance(parameters, list):
            return None
        profile = ParameterProfile.from_vts_parameters(parameters, min_confidence=min_confidence, overrides=overrides)
        profile.model_id = str(payload.get("model_id") or profile.model_id)
        profile.model_name = str(payload.get("model_name") or profile.model_name)
        profile.apply_reference_hints()
        return profile

    def from_model_path(
        self,
        model_path: str,
        *,
        min_confidence: float = 0.6,
        overrides: Mapping[str, Any] | None = None,
    ) -> ParameterProfile | None:
        """Discover parameters from a model3.json path or model directory."""

        path = Path(model_path)
        if path.is_dir():
            model_files = list(path.glob("*.model3.json"))
            if not model_files:
                model_files = list(path.glob("*.json"))
            if not model_files:
                return None
            path = model_files[0]
        if not path.exists() or not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._log_warning(f"Failed to read Live2D model file {path}: {exc}")
            return None
        groups = _extract_model_groups(raw)
        raw_parameters = _extract_model_parameters(raw)
        profile = ParameterProfile(
            model_id=str(raw.get("FileReferences", {}).get("Moc") or path.stem),
            model_name=str(raw.get("Name") or path.stem),
            groups=groups,
            min_confidence=min_confidence,
        )
        for parameter in raw_parameters:
            profile.add(profile.infer(parameter))
        profile.apply_reference_hints()
        profile.apply_overrides(overrides or {})
        return profile

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)


def _extract_model_groups(raw: Mapping[str, Any]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    raw_groups = raw.get("Groups", [])
    if not isinstance(raw_groups, list):
        return groups
    for raw_group in raw_groups:
        if not isinstance(raw_group, Mapping):
            continue
        group_name = str(raw_group.get("Name") or raw_group.get("Target") or "").strip()
        ids = raw_group.get("Ids", [])
        if group_name and isinstance(ids, list):
            groups[group_name] = [str(item) for item in ids if str(item).strip()]
    return groups


def _extract_model_parameters(raw: Mapping[str, Any]) -> list[ParameterSpec]:
    parameters: list[ParameterSpec] = []
    raw_parameters = raw.get("Parameters", [])
    if isinstance(raw_parameters, list):
        for raw_parameter in raw_parameters:
            if not isinstance(raw_parameter, Mapping):
                continue
            parameter_id = str(raw_parameter.get("Id") or raw_parameter.get("id") or "").strip()
            if parameter_id:
                parameters.append(ParameterSpec(id=parameter_id))
    for ids in _extract_model_groups(raw).values():
        for parameter_id in ids:
            if parameter_id not in {parameter.id for parameter in parameters}:
                parameters.append(ParameterSpec(id=parameter_id))
    return parameters

"""Local bridge from MaiBot Live2D JSON events to the VTube Studio public API.

Run this script while VTube Studio is open and "Allow Plugin API access" is enabled.
The MaiBot plugin connects to this bridge, and this bridge translates parameter frames
to VTS InjectParameterDataRequest calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import argparse
import asyncio
import contextlib
import ctypes
import hashlib
import heapq
import json
import logging
import os
import signal
import string
import time
import uuid
from urllib.parse import urlparse

from aiohttp import ClientConnectionError, ClientSession, ClientTimeout, WSMsgType, web


API_NAME = "VTubeStudioPublicAPI"
API_VERSION = "1.0"
_AUTH_MESSAGE_TYPES = {"APIStateRequest", "AuthenticationTokenRequest", "AuthenticationRequest"}
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 18081
DEFAULT_LISTEN_PATH = "/live2d"
DEFAULT_VTS_URL = "ws://127.0.0.1:8002"
DEFAULT_PLUGIN_NAME = "MaiBot Live2D Bridge"
DEFAULT_PLUGIN_DEVELOPER = "MaiBot"

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKEN_FILE = PLUGIN_ROOT / "data" / "vts_auth_token.json"
DEFAULT_FRAME_DIAGNOSTICS_FILE = PLUGIN_ROOT / "data" / "logs" / "vts_frame_diagnostics.jsonl"

FORBIDDEN_ID_TOKENS = ("hand", "arm", "leg", "foot", "finger", "gesture")
MOTION_HOTKEY_TYPES = {"ChangeIdleAnimation", "TriggerAnimation"}
MOTION_IDLE_TYPE_BONUS = {"ChangeIdleAnimation": 20, "TriggerAnimation": 10}
TIMELINE_INTERPOLATION_INTERVAL_MS = 16
MIN_TIMELINE_KEYFRAME_HZ = 45
TIMELINE_FRAME_HOLD_MS = max((TIMELINE_INTERPOLATION_INTERVAL_MS * 2) + 10, int(1000 / MIN_TIMELINE_KEYFRAME_HZ))
BRIDGE_OUTPUT_REFRESH_INTERVAL_SEC = 0.005
IMMEDIATE_DUPLICATE_PUSH_WINDOW_SEC = 0.004
OUTPUT_INTERPOLATION_DEFAULT_MS = 34.0
OUTPUT_INTERPOLATION_MOUTH_MS = 24.0
OUTPUT_INTERPOLATION_EYE_MS = 28.0
OUTPUT_INTERPOLATION_POSE_MS = 40.0

DEFAULT_PARAMETER_MAPPING: dict[str, list[str]] = {
    "ParamAngleX": ["FaceAngleX"],
    "ParamAngleY": ["FaceAngleY"],
    "ParamAngleZ": ["FaceAngleZ"],
    "ParamBodyAngleX": ["BodyAngleX", "FaceAngleX"],
    "ParamBodyAngleY": ["BodyAngleY", "FaceAngleY"],
    "ParamBodyAngleZ": ["BodyAngleZ", "FaceAngleZ"],
    "ParamEyeLOpen": ["EyeOpenLeft"],
    "ParamEyeROpen": ["EyeOpenRight"],
    "ParamEyeLSmile": [],
    "ParamEyeRSmile": [],
    "ParamEyeBallX": ["EyeLeftX", "EyeRightX"],
    "ParamEyeBallY": ["EyeLeftY", "EyeRightY"],
    "ParamMouthOpenY": ["MouthOpen"],
    "ParamMouthSmile": ["MouthSmile"],
    "ParamMouthForm": ["MouthX"],
    "ParamBrowLY": ["BrowLeftY", "Brows"],
    "ParamBrowRY": ["BrowRightY", "Brows"],
    "ParamCheek": ["CheekPuff"],
    "ParamBreath": ["UseBreathing", "Breathing"],
}

FALLBACK_LIVE2D_PARAMETERS: list[dict[str, float | str]] = [
    {"id": "ParamAngleX", "min": -30.0, "max": 30.0, "default": 0.0, "current": 0.0},
    {"id": "ParamAngleY", "min": -30.0, "max": 30.0, "default": 0.0, "current": 0.0},
    {"id": "ParamAngleZ", "min": -30.0, "max": 30.0, "default": 0.0, "current": 0.0},
    {"id": "ParamBodyAngleX", "min": -10.0, "max": 10.0, "default": 0.0, "current": 0.0},
    {"id": "ParamBodyAngleY", "min": -10.0, "max": 10.0, "default": 0.0, "current": 0.0},
    {"id": "ParamBodyAngleZ", "min": -10.0, "max": 10.0, "default": 0.0, "current": 0.0},
    {"id": "ParamEyeLOpen", "min": 0.0, "max": 1.0, "default": 1.0, "current": 1.0},
    {"id": "ParamEyeROpen", "min": 0.0, "max": 1.0, "default": 1.0, "current": 1.0},
    {"id": "ParamEyeLSmile", "min": 0.0, "max": 1.0, "default": 0.0, "current": 0.0},
    {"id": "ParamEyeRSmile", "min": 0.0, "max": 1.0, "default": 0.0, "current": 0.0},
    {"id": "ParamEyeBallX", "min": -1.0, "max": 1.0, "default": 0.0, "current": 0.0},
    {"id": "ParamEyeBallY", "min": -1.0, "max": 1.0, "default": 0.0, "current": 0.0},
    {"id": "ParamMouthOpenY", "min": 0.0, "max": 1.0, "default": 0.0, "current": 0.0},
    {"id": "ParamMouthForm", "min": -2.0, "max": 1.0, "default": 0.0, "current": 0.0},
    {"id": "ParamMouthSmile", "min": 0.0, "max": 1.0, "default": 0.0, "current": 0.0},
    {"id": "ParamBrowLY", "min": -1.0, "max": 1.0, "default": 0.0, "current": 0.0},
    {"id": "ParamBrowRY", "min": -1.0, "max": 1.0, "default": 0.0, "current": 0.0},
    {"id": "ParamCheek", "min": 0.0, "max": 1.0, "default": 0.0, "current": 0.0},
]


@dataclass
class BridgeConfig:
    listen_host: str = DEFAULT_LISTEN_HOST
    listen_port: int = DEFAULT_LISTEN_PORT
    listen_path: str = DEFAULT_LISTEN_PATH
    vts_url: str = DEFAULT_VTS_URL
    plugin_name: str = DEFAULT_PLUGIN_NAME
    plugin_developer: str = DEFAULT_PLUGIN_DEVELOPER
    token_file: Path = DEFAULT_TOKEN_FILE
    mapping_file: Path | None = None
    create_custom_parameters: bool = True
    dry_run: bool = False
    frame_diagnostics_file: Path = DEFAULT_FRAME_DIAGNOSTICS_FILE


@dataclass(frozen=True)
class ModelInputBinding:
    input_parameter: str
    input_range_lower: float
    input_range_upper: float
    output_range_lower: float
    output_range_upper: float
    clamp_input: bool = False


@dataclass(frozen=True)
class ParameterContribution:
    target_id: str
    value: float
    weight: float
    source: str
    lane: str
    source_kind: str
    combine_mode: str
    priority: int
    order: int
    created_at_monotonic: float
    expires_at_monotonic: float


@dataclass
class ParameterMapper:
    """Resolve MaiBot Live2D parameter IDs to VTS input parameter IDs."""

    mapping: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_PARAMETER_MAPPING))
    input_parameter_names: set[str] = field(default_factory=set)
    create_custom_parameters: bool = True
    custom_name_by_parameter_id: dict[str, str] = field(default_factory=dict)

    def resolve_existing_targets(self, parameter_id: str) -> list[str]:
        if is_forbidden_parameter_id(parameter_id):
            return []
        explicit_targets = self.mapping.get(parameter_id, [])
        existing_targets = [target for target in explicit_targets if target in self.input_parameter_names]
        if existing_targets:
            return existing_targets
        if parameter_id in self.input_parameter_names:
            return [parameter_id]
        custom_name = self.custom_name_by_parameter_id.get(parameter_id)
        if custom_name and custom_name in self.input_parameter_names:
            return [custom_name]
        return []

    def custom_name_for(self, parameter_id: str) -> str:
        existing = self.custom_name_by_parameter_id.get(parameter_id)
        if existing:
            return existing
        candidate = sanitize_vts_parameter_name(parameter_id)
        self.custom_name_by_parameter_id[parameter_id] = candidate
        return candidate

    def update_input_names(self, names: set[str]) -> None:
        self.input_parameter_names = set(names)


class _WindowsTimerResolution:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self._active = False
        self._winmm = None
        if os.name != "nt":
            return
        with contextlib.suppress(Exception):
            self._winmm = ctypes.windll.winmm

    def enable(self) -> None:
        if self._active or self._winmm is None:
            return
        try:
            result = int(self._winmm.timeBeginPeriod(1))
        except Exception as exc:
            self.logger.debug("Failed to enable 1ms Windows timer resolution: %s", exc)
            return
        if result == 0:
            self._active = True
        else:
            self.logger.debug("timeBeginPeriod(1) returned %s", result)

    def disable(self) -> None:
        if not self._active or self._winmm is None:
            return
        with contextlib.suppress(Exception):
            self._winmm.timeEndPeriod(1)
        self._active = False


class VTubeStudioClient:
    """Small async VTube Studio API client."""

    def __init__(self, config: BridgeConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self._session: ClientSession | None = None
        self._ws: Any = None
        self._request_lock = asyncio.Lock()
        self._authenticated = False

    async def connect(self) -> None:
        if self.config.dry_run:
            return
        if self._session is None:
            timeout = ClientTimeout(total=None, connect=10.0, sock_read=10.0)
            self._session = ClientSession(timeout=timeout)
        if self._ws is None or self._ws.closed:
            self.logger.info("Connecting to VTube Studio API at %s", self.config.vts_url)
            try:
                self._ws = await self._session.ws_connect(self.config.vts_url)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(_format_vts_handshake_timeout_message(self.config.vts_url)) from exc
            self._authenticated = False

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None
        self._authenticated = False
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
        self._session = None

    async def ensure_authenticated(self) -> None:
        if self.config.dry_run:
            self.logger.info("Dry-run mode enabled; skipping VTS authentication")
            return
        if self._authenticated and self._ws is not None and not self._ws.closed:
            return
        await self.connect()
        state = await self.request("APIStateRequest", authenticate=False)
        if _response_data(state).get("currentSessionAuthenticated") is True:
            self.logger.info("Current VTS session is already authenticated")
            self._authenticated = True
            return

        token = self._load_token()
        if token and await self._authenticate_with_token(token):
            return

        self.logger.info("Requesting VTS auth token; click Allow in VTube Studio")
        token_response = await self.request(
            "AuthenticationTokenRequest",
            {
                "pluginName": self.config.plugin_name,
                "pluginDeveloper": self.config.plugin_developer,
            },
            authenticate=False,
        )
        token = str(_response_data(token_response).get("authenticationToken") or "").strip()
        if not token:
            raise RuntimeError(f"VTS did not return an authentication token: {token_response}")
        self._save_token(token)
        if not await self._authenticate_with_token(token):
            raise RuntimeError("VTS authentication failed after receiving a token")

    async def request(
        self,
        message_type: str,
        data: Mapping[str, Any] | None = None,
        *,
        authenticate: bool = True,
        retries: int = 1,
    ) -> dict[str, Any]:
        if self.config.dry_run:
            return {"messageType": message_type.replace("Request", "Response"), "data": {}}
        if authenticate and message_type not in _AUTH_MESSAGE_TYPES:
            await self.ensure_authenticated()
        last_error: Exception | None = None
        for attempt in range(max(1, retries + 1)):
            try:
                return await self._request_once(message_type, data)
            except (ClientConnectionError, ConnectionResetError, RuntimeError) as exc:
                last_error = exc
                await self._reset_ws()
                if attempt >= retries:
                    break
                self.logger.warning("VTS request %s failed; reconnecting: %s", message_type, exc)
                if authenticate and message_type not in _AUTH_MESSAGE_TYPES:
                    await self.ensure_authenticated()
        raise last_error or RuntimeError(f"VTS request failed: {message_type}")

    async def _request_once(self, message_type: str, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        request = {
            "apiName": API_NAME,
            "apiVersion": API_VERSION,
            "requestID": request_id,
            "messageType": message_type,
            "data": dict(data or {}),
        }
        async with self._request_lock:
            await self.connect()
            await self._ws.send_str(json.dumps(request, ensure_ascii=False))
            while True:
                response = await self._ws.receive()
                if response.type != WSMsgType.TEXT:
                    if response.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                        raise ClientConnectionError(f"VTS websocket closed while waiting for {message_type}")
                    raise RuntimeError(f"Unexpected VTS websocket message type: {response.type}")
                payload = json.loads(response.data)
                if not isinstance(payload, dict):
                    continue
                if payload.get("requestID") not in {request_id, "", None}:
                    continue
                if payload.get("messageType") == "APIError":
                    raise RuntimeError(json.dumps(payload.get("data") or payload, ensure_ascii=False))
                return payload

    async def _authenticate_with_token(self, token: str) -> bool:
        try:
            response = await self.request(
                "AuthenticationRequest",
                {
                    "pluginName": self.config.plugin_name,
                    "pluginDeveloper": self.config.plugin_developer,
                    "authenticationToken": token,
                },
                authenticate=False,
            )
        except Exception as exc:
            self.logger.warning("VTS authentication token failed: %s", exc)
            return False
        authenticated = bool(_response_data(response).get("authenticated"))
        if authenticated:
            self.logger.info("Authenticated with VTube Studio")
            self._authenticated = True
        else:
            self.logger.warning("VTS authentication returned false: %s", response)
        return authenticated

    async def _reset_ws(self) -> None:
        ws = self._ws
        self._ws = None
        self._authenticated = False
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    def _load_token(self) -> str:
        try:
            raw = json.loads(self.config.token_file.read_text(encoding="utf-8"))
        except Exception:
            return ""
        return str(raw.get("authenticationToken") or "").strip() if isinstance(raw, Mapping) else ""

    def _save_token(self, token: str) -> None:
        self.config.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.token_file.write_text(
            json.dumps({"authenticationToken": token}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.logger.info("Saved VTS auth token to %s", self.config.token_file)


class MaiBotVTubeStudioBridge:
    """Bridge server that accepts MaiBot Live2D events and injects VTS inputs."""

    def __init__(self, config: BridgeConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.vts = VTubeStudioClient(config, logger)
        self.mapper = ParameterMapper(
            mapping=merge_parameter_mapping(load_mapping_file(config.mapping_file)),
            create_custom_parameters=config.create_custom_parameters,
        )
        self.live2d_specs: dict[str, dict[str, Any]] = {}
        self.timeline_origins: dict[str, float] = {}
        self.timeline_keyframes: dict[str, Mapping[str, Any]] = {}
        self._scheduled_payloads: list[tuple[float, int, Mapping[str, Any]]] = []
        self._inject_worker: asyncio.Task[None] | None = None
        self._output_worker: asyncio.Task[None] | None = None
        self._inject_sequence = 0
        self._contribution_sequence = 0
        self._started = False
        self._inject_success_count = 0
        self._last_inject_log_at = 0.0
        self._logged_parameter_targets: set[str] = set()
        self._logged_missing_targets: set[str] = set()
        self._active_parameter_values: dict[str, dict[str, float | str]] = {}
        self._active_parameter_contributions: dict[str, list[ParameterContribution]] = {}
        self._output_wake = asyncio.Event()
        self.input_parameter_specs: dict[str, dict[str, float | str]] = {}
        self.model_input_bindings: dict[str, ModelInputBinding] = {}
        self.current_model_file: Path | None = None
        self._last_push_at_monotonic: float | None = None
        self._last_pushed_parameter_values: dict[str, float] = {}
        self._last_push_signature: tuple[tuple[str, float, float], ...] | None = None
        self._windows_timer_resolution = _WindowsTimerResolution(logger)

    async def start(self) -> None:
        if self._started:
            return
        self._windows_timer_resolution.enable()
        self._start_inject_worker()
        self._started = True
        try:
            await self.vts.ensure_authenticated()
            await self.refresh_input_parameters()
            await self.refresh_model_input_bindings()
        except Exception as exc:
            self.logger.warning("Bridge could not initialize VTube Studio yet; running in degraded mode: %s", exc)

    async def close(self) -> None:
        if self._inject_worker is not None:
            self._inject_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._inject_worker
        self._inject_worker = None
        if self._output_worker is not None:
            self._output_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._output_worker
        self._output_worker = None
        self._active_parameter_values.clear()
        self._active_parameter_contributions.clear()
        self._scheduled_payloads.clear()
        self._output_wake.clear()
        self._windows_timer_resolution.disable()
        await self.vts.close()

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.logger.info("MaiBot Live2D client connected from %s", request.remote)
        try:
            await self.start()
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await self.handle_plugin_payload(json.loads(message.data), ws)
                elif message.type == WSMsgType.ERROR:
                    self.logger.warning("MaiBot websocket error: %s", ws.exception())
        except Exception as exc:
            self.logger.exception("Bridge websocket handler failed: %s", exc)
        finally:
            self.logger.info("MaiBot Live2D client disconnected")
        return ws

    async def handle_plugin_payload(self, payload: Mapping[str, Any], ws: web.WebSocketResponse) -> None:
        event_type = str(payload.get("type") or "").strip()
        if event_type == "live2d.capabilities.request":
            await ws.send_str(json.dumps(await self.build_capabilities_response(), ensure_ascii=False))
            return
        if event_type == "bot_reply.prepare":
            timeline_id = str(payload.get("timeline_id") or "").strip()
            if timeline_id:
                self.timeline_origins[timeline_id] = time.monotonic()
                self.timeline_keyframes.pop(timeline_id, None)
            self.logger.info("Prepared timeline %s text=%r", timeline_id, str(payload.get("text") or "")[:40])
            return
        if event_type in {"bot_reply.start", "bot_reply.end"}:
            self.logger.info("Timeline event %s id=%s", event_type, payload.get("timeline_id"))
            return
        if event_type == "live2d.motion":
            result = await self.trigger_motion(payload)
            if not result.get("success", False):
                self.logger.warning("VTS motion trigger failed: %s", result)
            return
        if event_type in {"live2d.parameters", "live2d.timeline.frame"}:
            self._schedule_or_inject(payload)
            return
        self.logger.debug("Ignoring bridge event type=%s", event_type)

    async def build_capabilities_response(self) -> dict[str, Any]:
        try:
            response = await self.vts.request("Live2DParameterListRequest")
            data = _response_data(response)
            raw_parameters = data.get("parameters") if isinstance(data.get("parameters"), list) else []
            parameters = [convert_vts_live2d_parameter(item) for item in raw_parameters if isinstance(item, Mapping)]
            if not parameters:
                parameters = [dict(item) for item in FALLBACK_LIVE2D_PARAMETERS]
            self.live2d_specs = {str(item["id"]): dict(item) for item in parameters}
            return {
                "type": "live2d.capabilities.response",
                "model_id": str(data.get("modelID") or data.get("modelId") or ""),
                "model_name": str(data.get("modelName") or ""),
                "parameters": parameters,
                "groups": infer_groups(parameters),
            }
        except Exception as exc:
            self.logger.warning("Failed to request VTS Live2D parameters; using fallback profile: %s", exc)
            parameters = [dict(item) for item in FALLBACK_LIVE2D_PARAMETERS]
            self.live2d_specs = {str(item["id"]): dict(item) for item in parameters}
            return {
                "type": "live2d.capabilities.response",
                "model_id": "fallback",
                "model_name": "VTube Studio fallback",
                "parameters": parameters,
                "groups": infer_groups(parameters),
            }

    async def refresh_input_parameters(self) -> None:
        if self.config.dry_run:
            names = set().union(*DEFAULT_PARAMETER_MAPPING.values())
            self.mapper.update_input_names(names)
            self.input_parameter_specs = {name: {"name": name, "min": -1.0, "max": 1.0, "default": 0.0} for name in names}
            self.logger.info("Loaded %d VTS input parameters in dry-run mode", len(names))
            return
        response = await self.vts.request("InputParameterListRequest")
        data = _response_data(response)
        names: set[str] = set()
        specs: dict[str, dict[str, float | str]] = {}
        for key in ("defaultParameters", "customParameters"):
            raw_parameters = data.get(key)
            if not isinstance(raw_parameters, list):
                continue
            for raw_parameter in raw_parameters:
                if isinstance(raw_parameter, Mapping):
                    name = str(raw_parameter.get("name") or "").strip()
                    if name:
                        names.add(name)
                        specs[name] = convert_vts_input_parameter(raw_parameter)
        self.mapper.update_input_names(names)
        self.input_parameter_specs = specs
        sample = ", ".join(sorted(names)[:8])
        self.logger.info("VTS input parameters loaded: %d [%s]", len(names), sample)

    async def refresh_model_input_bindings(self) -> None:
        if self.config.dry_run:
            return
        response = await self.vts.request("CurrentModelRequest")
        data = _response_data(response)
        if not bool(data.get("modelLoaded")):
            self.model_input_bindings = {}
            self.current_model_file = None
            return
        model_name = str(data.get("modelName") or "").strip()
        vts_model_name = str(data.get("vtsModelName") or "").strip()
        if not vts_model_name:
            return
        model_file = locate_vts_model_file(vts_model_name=vts_model_name, model_name=model_name)
        if model_file is None:
            if self.current_model_file is None:
                self.logger.warning(
                    "Could not locate active VTS model config for %s (%s); parameter injection will use generic ranges",
                    model_name or "current model",
                    vts_model_name,
                )
            return
        if model_file == self.current_model_file and self.model_input_bindings:
            return
        self.model_input_bindings = load_model_input_bindings(model_file)
        self.current_model_file = model_file
        self.logger.info("Loaded %d model input bindings from %s", len(self.model_input_bindings), model_file)

    async def trigger_motion(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        motion_name = str(payload.get("motion") or payload.get("name") or "m01").strip()
        motion_file = str(payload.get("motion_file") or payload.get("file") or "").strip()
        model_name = str(payload.get("model") or "").strip()
        if not motion_file and motion_name:
            motion_file = f"{motion_name}.motion3.json"
        if not motion_name and not motion_file:
            return {"success": False, "error": "motion name or file is required"}
        if self.config.dry_run:
            self.logger.info("DRY RUN trigger motion: model=%s motion=%s file=%s", model_name, motion_name, motion_file)
            return {"success": True, "dry_run": True}
        response = await self.vts.request("HotkeysInCurrentModelRequest")
        data = _response_data(response)
        hotkeys = data.get("availableHotkeys") if isinstance(data.get("availableHotkeys"), list) else []
        selected = select_motion_hotkey(hotkeys, motion_name=motion_name, motion_file=motion_file, model_name=model_name)
        if not selected:
            return {
                "success": False,
                "error": "no matching VTS motion hotkey",
                "motion": motion_name,
                "motion_file": motion_file,
                "available_hotkey_count": len(hotkeys),
            }
        hotkey_id = str(selected.get("hotkeyID") or selected.get("id") or selected.get("name") or "").strip()
        if not hotkey_id:
            return {"success": False, "error": "matching VTS motion hotkey has no id", "hotkey": dict(selected)}
        trigger_response = await self.vts.request("HotkeyTriggerRequest", {"hotkeyID": hotkey_id})
        self.logger.info(
            "Triggered VTS motion hotkey: model=%s motion=%s file=%s hotkey=%s",
            model_name or "current",
            motion_name,
            motion_file,
            hotkey_id,
        )
        return {"success": True, "hotkey_id": hotkey_id, "hotkey": dict(selected), "response": trigger_response}

    def _schedule_or_inject(self, payload: Mapping[str, Any]) -> None:
        if str(payload.get("type") or "").strip() == "live2d.timeline.frame":
            self._schedule_timeline_frame(payload)
            return
        self._schedule_payload(payload)

    def _schedule_timeline_frame(self, payload: Mapping[str, Any]) -> None:
        timeline_id = str(payload.get("timeline_id") or "").strip()
        if not timeline_id:
            self._schedule_payload(payload)
            return
        current = dict(payload)
        current["keyframe"] = True
        previous = self.timeline_keyframes.get(timeline_id)
        if previous is None:
            self.timeline_keyframes[timeline_id] = current
            self._schedule_payload(current)
            return
        frames = interpolate_timeline_keyframes(previous, current)
        if not frames:
            frames = [current]
        for frame in frames:
            self._schedule_payload(frame)
        self.timeline_keyframes[timeline_id] = current

    def _schedule_payload(self, payload: Mapping[str, Any]) -> None:
        self._start_inject_worker()
        due_at = time.monotonic() + self._compute_delay_seconds(payload)
        self._inject_sequence += 1
        heapq.heappush(self._scheduled_payloads, (due_at, self._inject_sequence, dict(payload)))
        self._output_wake.set()

    def _start_inject_worker(self) -> None:
        if self._inject_worker is None or self._inject_worker.done():
            self._inject_worker = asyncio.create_task(self._inject_worker_loop(), name="vts_bridge.clock_worker")

    def _start_output_worker(self) -> None:
        return

    async def _inject_worker_loop(self) -> None:
        refresh_interval = BRIDGE_OUTPUT_REFRESH_INTERVAL_SEC
        while True:
            try:
                current_now = time.monotonic()
                cycle_result = await self._run_clock_cycle(now=current_now)
                push_result = cycle_result.get("push_result")
                if isinstance(push_result, Mapping) and not bool(push_result.get("success", False)):
                    self.logger.warning("VTS injection failed: %s", dict(push_result))
                next_deadline = self._next_clock_deadline(now=current_now, refresh_interval=refresh_interval)
                if next_deadline is None:
                    await self._output_wake.wait()
                    self._output_wake.clear()
                    continue
                timeout = max(0.0, next_deadline - time.monotonic())
                try:
                    await asyncio.wait_for(self._output_wake.wait(), timeout=timeout)
                    self._output_wake.clear()
                except asyncio.TimeoutError:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("VTS clock worker failed: %s", exc)

    def _pop_due_payloads(self, *, now: float) -> list[Mapping[str, Any]]:
        due_payloads: list[Mapping[str, Any]] = []
        while self._scheduled_payloads and self._scheduled_payloads[0][0] <= now:
            _due_at, _sequence, payload = heapq.heappop(self._scheduled_payloads)
            due_payloads.append(payload)
        return due_payloads

    def _next_scheduled_due_at(self) -> float | None:
        if not self._scheduled_payloads:
            return None
        return float(self._scheduled_payloads[0][0])

    def _next_active_expiration_at(self) -> float | None:
        earliest: float | None = None
        for contributions in self._active_parameter_contributions.values():
            for contribution in contributions:
                if earliest is None or contribution.expires_at_monotonic < earliest:
                    earliest = contribution.expires_at_monotonic
        return earliest

    def _next_clock_deadline(self, *, now: float, refresh_interval: float) -> float | None:
        candidates: list[float] = []
        next_due = self._next_scheduled_due_at()
        if next_due is not None:
            candidates.append(next_due)
        next_expiration = self._next_active_expiration_at()
        if next_expiration is not None:
            candidates.append(next_expiration)
        if self._active_parameter_contributions:
            candidates.append(now + refresh_interval)
        if not candidates:
            return None
        return min(candidates)

    async def _run_clock_cycle(self, *, now: float | None = None) -> dict[str, Any]:
        current_now = time.monotonic() if now is None else float(now)
        processed = 0
        for payload in self._pop_due_payloads(now=current_now):
            processed += 1
            result = await self.inject_event_parameters(payload, now=current_now, trigger_wake=False)
            if not result.get("success", False):
                self.logger.warning("VTS injection failed: %s", result)
        if processed > 0 or self._active_parameter_contributions:
            push_result = await self._push_active_parameters(now=current_now)
        else:
            push_result = {"success": True, "skipped": True, "reason": "no_active_parameters"}
        return {"processed_count": processed, "push_result": push_result}

    async def inject_event_parameters(
        self,
        payload: Mapping[str, Any],
        *,
        now: float | None = None,
        trigger_wake: bool = True,
    ) -> dict[str, Any]:
        raw_parameters = payload.get("parameters")
        if not isinstance(raw_parameters, list):
            return {"success": False, "error": "parameters must be a list"}
        parameter_values: list[dict[str, float | str]] = []
        for raw_parameter in raw_parameters:
            if not isinstance(raw_parameter, Mapping):
                continue
            parameter_values.extend(await self._to_vts_parameter_values(raw_parameter))
        parameter_values = self._merge_parameter_values(parameter_values)
        if not parameter_values:
            return {"success": True, "skipped": True, "reason": "no mapped VTS inputs"}
        self._store_parameter_contributions(payload, parameter_values, now=now)
        if trigger_wake:
            self._output_wake.set()
        return {"success": True, "parameter_values": parameter_values}

    def _payload_purpose(self, payload: Mapping[str, Any]) -> str:
        purpose = str(payload.get("purpose") or "").strip().lower()
        if purpose:
            return purpose
        timeline_id = str(payload.get("timeline_id") or "").strip().lower()
        if timeline_id.startswith("idle"):
            return timeline_id
        event_type = str(payload.get("type") or "").strip().lower()
        return event_type or "unknown"

    def _resolve_source_name(self, payload: Mapping[str, Any]) -> str:
        purpose = self._payload_purpose(payload)
        timeline_id = str(payload.get("timeline_id") or "").strip().lower()
        if timeline_id:
            return f"{purpose}:{timeline_id}"
        return purpose

    def _resolve_source_kind(self, payload: Mapping[str, Any]) -> str:
        purpose = self._payload_purpose(payload)
        if purpose in {"idle", "idle-mouth", "idle-sway"} or purpose.startswith("idle"):
            return "idle"
        return "active"

    def _resolve_source_lane(self, payload: Mapping[str, Any]) -> str:
        purpose = self._payload_purpose(payload)
        if purpose in {"idle-mouth", "lipsync"}:
            return "mouth"
        if purpose in {"blink", "wink"}:
            return "eye-overlay"
        if purpose == "soullink-initial" or purpose.startswith("soullink-frame-"):
            return "soullink-motion"
        if purpose in {"idle", "idle-sway", "idle-motion"} or purpose.startswith("idle"):
            return "idle"
        return purpose or "default"

    def _resolve_parameter_lane(self, payload: Mapping[str, Any], parameter_id: str) -> str:
        lane = self._resolve_source_lane(payload)
        if lane == "soullink-motion" and "mouth" in parameter_id.strip().lower():
            # SoulLink's original frontend applies lipsync and motion mouth updates
            # onto the same model state. Keeping them in one bridge lane is closer to
            # that behavior than letting separate lanes survive and fight.
            return "mouth"
        return lane

    def _resolve_combine_mode(self, payload: Mapping[str, Any]) -> str:
        normalized = str(payload.get("combine_mode") or "replace").strip().lower()
        return normalized or "replace"

    def _resolve_priority(self, payload: Mapping[str, Any]) -> int:
        try:
            return int(payload.get("priority") or 0)
        except (TypeError, ValueError):
            return 0

    def _resolve_expiration_time(self, payload: Mapping[str, Any], *, now: float | None = None) -> float:
        current_now = time.monotonic() if now is None else float(now)
        duration_ms = max(0, int(_as_float(payload.get("duration_ms"), default=0.0)))
        event_type = str(payload.get("type") or "").strip().lower()
        if duration_ms > 0:
            lifetime_ms = duration_ms
        elif event_type == "live2d.timeline.frame":
            # Timeline frames should only survive for a very short bridge-side
            # window: long enough for two output passes plus scheduling jitter,
            # but short enough that stale poses do not visibly stick between
            # authored keyframes.
            lifetime_ms = TIMELINE_FRAME_HOLD_MS
        else:
            lifetime_ms = TIMELINE_INTERPOLATION_INTERVAL_MS
        return current_now + max(0.01, lifetime_ms / 1000.0)

    def _store_parameter_contributions(
        self,
        payload: Mapping[str, Any],
        parameter_values: list[dict[str, float | str]],
        *,
        now: float | None = None,
    ) -> None:
        current_now = time.monotonic() if now is None else float(now)
        source = self._resolve_source_name(payload)
        source_kind = self._resolve_source_kind(payload)
        combine_mode = self._resolve_combine_mode(payload)
        priority = self._resolve_priority(payload)
        expires_at = self._resolve_expiration_time(payload, now=current_now)
        self._contribution_sequence += 1
        contribution_order = self._contribution_sequence
        for item in parameter_values:
            parameter_id = str(item.get("id") or "").strip()
            if not parameter_id:
                continue
            lane = self._resolve_parameter_lane(payload, parameter_id)
            contributions = [
                existing
                for existing in self._active_parameter_contributions.get(parameter_id, [])
                if existing.source != source and existing.lane != lane
            ]
            contributions.append(
                ParameterContribution(
                    target_id=parameter_id,
                    value=_as_float(item.get("value")),
                    weight=min(1.0, max(0.0, _as_float(item.get("weight"), default=1.0))),
                    source=source,
                    lane=lane,
                    source_kind=source_kind,
                    combine_mode=combine_mode,
                    priority=priority,
                    order=contribution_order,
                    created_at_monotonic=current_now,
                    expires_at_monotonic=expires_at,
                )
            )
            self._active_parameter_contributions[parameter_id] = contributions
        self._refresh_active_parameter_value_cache(now=current_now)

    def _prune_and_select_contributions(
        self,
        target_id: str,
        *,
        now: float,
    ) -> list[ParameterContribution]:
        contributions = self._active_parameter_contributions.get(target_id, [])
        active = [item for item in contributions if item.expires_at_monotonic > now]
        if not active:
            self._active_parameter_contributions.pop(target_id, None)
            return []
        self._active_parameter_contributions[target_id] = active
        if any(item.source_kind != "idle" for item in active):
            return [item for item in active if item.source_kind != "idle"]
        return active

    def _merge_surviving_contributions(
        self,
        target_id: str,
        contributions: list[ParameterContribution],
    ) -> dict[str, float | str] | None:
        if not contributions:
            return None
        if len(contributions) == 1:
            contribution = contributions[0]
            return {"id": target_id, "value": contribution.value, "weight": contribution.weight}
        default_value = self._input_parameter_default(target_id)
        winner = max(
            contributions,
            key=lambda item: (
                item.priority,
                item.order,
                item.weight,
                abs(item.value - default_value),
            ),
        )
        return {"id": target_id, "value": winner.value, "weight": winner.weight}

    def _compose_active_parameter_values(self, *, now: float | None = None) -> list[dict[str, float | str]]:
        current_now = time.monotonic() if now is None else float(now)
        composed: list[dict[str, float | str]] = []
        for target_id in list(self._active_parameter_contributions.keys()):
            survivors = self._prune_and_select_contributions(target_id, now=current_now)
            if not survivors:
                continue
            merged = self._merge_surviving_contributions(target_id, survivors)
            if merged is not None:
                composed.append(merged)
        return composed

    def _refresh_active_parameter_value_cache(self, *, now: float | None = None) -> None:
        self._active_parameter_values = {
            str(item["id"]): dict(item)
            for item in self._compose_active_parameter_values(now=now)
            if str(item.get("id") or "").strip()
        }

    async def _push_active_parameters(self, *, now: float | None = None) -> dict[str, Any]:
        current_now = time.monotonic() if now is None else float(now)
        target_values = self._compose_active_parameter_values(now=current_now)
        self._active_parameter_values = {
            str(item["id"]): dict(item)
            for item in target_values
            if str(item.get("id") or "").strip()
        }
        if not target_values:
            return {"success": True, "skipped": True, "reason": "no_active_parameters"}
        parameter_values = self._interpolate_output_parameter_values(target_values, now=current_now)
        signature = self._build_push_signature(parameter_values)
        if (
            self._last_push_signature is not None
            and signature == self._last_push_signature
            and self._last_push_at_monotonic is not None
            and (current_now - self._last_push_at_monotonic) <= IMMEDIATE_DUPLICATE_PUSH_WINDOW_SEC
        ):
            return {"success": True, "skipped": True, "reason": "duplicate_parameter_batch"}
        self._record_frame_diagnostics(parameter_values, now=current_now)
        if self.config.dry_run:
            self.logger.info("DRY RUN inject: %s", parameter_values)
            self._last_push_signature = signature
            return {"success": True, "dry_run": True}
        response = await self.vts.request(
            "InjectParameterDataRequest",
            {
                "faceFound": True,
                "mode": "set",
                "parameterValues": parameter_values,
            },
        )
        self._last_push_signature = signature
        self._log_injection_success(parameter_values)
        self.logger.debug("Injected %d VTS parameter values", len(parameter_values))
        return {"success": True, "response": response}

    def _interpolate_output_parameter_values(
        self,
        parameter_values: list[Mapping[str, float | str]],
        *,
        now: float,
    ) -> list[dict[str, float | str]]:
        if self._last_push_at_monotonic is None:
            return [dict(item) for item in parameter_values]
        elapsed_ms = max(0.0, (now - self._last_push_at_monotonic) * 1000.0)
        smoothed: list[dict[str, float | str]] = []
        for item in parameter_values:
            parameter_id = str(item.get("id") or "").strip()
            if not parameter_id:
                continue
            current_value = _as_float(item.get("value"))
            previous_value = self._last_pushed_parameter_values.get(parameter_id)
            if previous_value is None:
                smoothed.append(dict(item))
                continue
            window_ms = self._output_interpolation_window_ms(parameter_id)
            alpha = 1.0 if window_ms <= 0.0 else min(1.0, elapsed_ms / window_ms)
            blended_value = previous_value + ((current_value - previous_value) * alpha)
            smoothed.append(
                {
                    "id": parameter_id,
                    "value": blended_value,
                    "weight": _as_float(item.get("weight"), default=1.0),
                }
            )
        return smoothed

    def _output_interpolation_window_ms(self, parameter_id: str) -> float:
        normalized = parameter_id.strip().lower()
        if "mouth" in normalized:
            return OUTPUT_INTERPOLATION_MOUTH_MS
        if "eyeopen" in normalized or "eyeball" in normalized:
            return OUTPUT_INTERPOLATION_EYE_MS
        if "angle" in normalized or "shoulder" in normalized or "breath" in normalized:
            return OUTPUT_INTERPOLATION_POSE_MS
        return OUTPUT_INTERPOLATION_DEFAULT_MS

    def _build_push_signature(
        self,
        parameter_values: list[Mapping[str, float | str]],
    ) -> tuple[tuple[str, float, float], ...]:
        normalized: list[tuple[str, float, float]] = []
        for item in parameter_values:
            parameter_id = str(item.get("id") or "").strip()
            if not parameter_id:
                continue
            normalized.append(
                (
                    parameter_id,
                    round(_as_float(item.get("value")), 6),
                    round(_as_float(item.get("weight"), default=1.0), 6),
                )
            )
        normalized.sort()
        return tuple(normalized)

    def _log_injection_success(self, parameter_values: list[Mapping[str, float | str]]) -> None:
        self._inject_success_count += 1
        now = time.monotonic()
        if self._inject_success_count <= 3 or now - self._last_inject_log_at >= 2.0:
            sample = ", ".join(str(item.get("id") or "") for item in parameter_values[:4])
            self.logger.info(
                "Injected VTS parameter batch #%d: %d values [%s]",
                self._inject_success_count,
                len(parameter_values),
                sample,
            )
            self._last_inject_log_at = now

    def _record_frame_diagnostics(
        self,
        parameter_values: list[Mapping[str, float | str]],
        *,
        now: float,
    ) -> None:
        interval_ms: float | None = None
        if self._last_push_at_monotonic is not None:
            interval_ms = max(0.0, (now - self._last_push_at_monotonic) * 1000.0)
            if interval_ms < 8.0 or interval_ms > 30.0:
                self._append_frame_diagnostic(
                    "interval",
                    {
                        "interval_ms": round(interval_ms, 3),
                        "expected_ms": TIMELINE_INTERPOLATION_INTERVAL_MS,
                        "parameter_count": len(parameter_values),
                    },
                )

        for item in parameter_values:
            target_id = str(item.get("id") or "").strip()
            if not target_id:
                continue
            current_value = _as_float(item.get("value"))
            previous_value = self._last_pushed_parameter_values.get(target_id)
            if previous_value is None:
                continue
            minimum, maximum = self._input_parameter_range(target_id)
            span = max(0.0001, maximum - minimum)
            delta = current_value - previous_value
            normalized_delta = abs(delta) / span
            if normalized_delta >= 0.4:
                self._append_frame_diagnostic(
                    "sudden",
                    {
                        "parameter_id": target_id,
                        "previous_value": round(previous_value, 6),
                        "current_value": round(current_value, 6),
                        "delta": round(delta, 6),
                        "normalized_delta": round(normalized_delta, 6),
                        "interval_ms": None if interval_ms is None else round(interval_ms, 3),
                    },
                )

        for target_id, survivors in self._collect_conflict_survivors(now=now).items():
            if len(survivors) <= 1:
                continue
            merged = self._merge_surviving_contributions(target_id, survivors)
            self._append_frame_diagnostic(
                "conflict",
                {
                    "parameter_id": target_id,
                    "survivor_count": len(survivors),
                    "lanes": sorted({item.lane for item in survivors}),
                    "sources": sorted({item.source for item in survivors}),
                    "winner_value": None if merged is None else round(_as_float(merged.get("value")), 6),
                },
            )

        self._last_push_at_monotonic = now
        self._last_pushed_parameter_values = {
            str(item.get("id") or ""): _as_float(item.get("value"))
            for item in parameter_values
            if str(item.get("id") or "").strip()
        }

    def _collect_conflict_survivors(self, *, now: float) -> dict[str, list[ParameterContribution]]:
        conflicts: dict[str, list[ParameterContribution]] = {}
        for target_id in list(self._active_parameter_contributions.keys()):
            survivors = self._prune_and_select_contributions(target_id, now=now)
            if len(survivors) > 1:
                conflicts[target_id] = survivors
        return conflicts

    def _append_frame_diagnostic(self, kind: str, payload: Mapping[str, Any]) -> None:
        record = {
            "ts": time.time(),
            "kind": str(kind or "").strip(),
            **dict(payload),
        }
        try:
            self.config.frame_diagnostics_file.parent.mkdir(parents=True, exist_ok=True)
            with self.config.frame_diagnostics_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            self.logger.debug("Failed to append VTS frame diagnostic: %s", exc)

    def _input_parameter_range(self, parameter_id: str) -> tuple[float, float]:
        spec = self.input_parameter_specs.get(parameter_id, {})
        minimum = _as_float(spec.get("min"), default=0.0)
        maximum = _as_float(spec.get("max"), default=0.0)
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        return minimum, maximum

    async def _to_vts_parameter_values(self, raw_parameter: Mapping[str, Any]) -> list[dict[str, float | str]]:
        parameter_id = str(raw_parameter.get("id") or "").strip()
        if not parameter_id or is_forbidden_parameter_id(parameter_id):
            return []
        value = _as_float(raw_parameter.get("value"))
        weight = min(1.0, max(0.0, _as_float(raw_parameter.get("weight"), default=1.0)))
        targets = await self._resolve_parameter_targets(parameter_id)
        if not targets and not self.mapper.input_parameter_names and not self.config.dry_run:
            with contextlib.suppress(Exception):
                await self.refresh_input_parameters()
            targets = await self._resolve_parameter_targets(parameter_id)
        if not targets and self.config.create_custom_parameters:
            custom_name = await self.ensure_custom_parameter(parameter_id)
            if custom_name:
                targets = [custom_name]
        if targets and parameter_id not in self._logged_parameter_targets:
            self.logger.info("Mapped Live2D parameter %s -> VTS inputs %s", parameter_id, ", ".join(targets))
            self._logged_parameter_targets.add(parameter_id)
        if not targets and parameter_id not in self._logged_missing_targets:
            self.logger.warning("No VTS input target resolved for Live2D parameter %s", parameter_id)
            self._logged_missing_targets.add(parameter_id)
        return [
            {"id": target, "value": self._translate_parameter_value(parameter_id, target, value), "weight": weight}
            for target in targets
        ]

    async def _resolve_parameter_targets(self, parameter_id: str) -> list[str]:
        binding = self.model_input_bindings.get(parameter_id)
        if binding is not None:
            bound_target = str(binding.input_parameter or "").strip()
            if bound_target in self.mapper.input_parameter_names:
                return [bound_target]
            if self.config.create_custom_parameters and _should_create_bound_custom_target(parameter_id, bound_target):
                custom_name = await self.ensure_custom_parameter(parameter_id, custom_name=bound_target)
                if custom_name:
                    return [custom_name]
        return self.mapper.resolve_existing_targets(parameter_id)

    async def ensure_custom_parameter(self, parameter_id: str, custom_name: str | None = None) -> str:
        custom_name = sanitize_vts_parameter_name(custom_name or self.mapper.custom_name_for(parameter_id))
        if custom_name in self.mapper.input_parameter_names:
            self.mapper.custom_name_by_parameter_id[parameter_id] = custom_name
            return custom_name
        spec = self.live2d_specs.get(parameter_id, default_spec_for(parameter_id))
        if self.config.dry_run:
            self.mapper.input_parameter_names.add(custom_name)
            self.mapper.custom_name_by_parameter_id[parameter_id] = custom_name
            return custom_name
        try:
            await self.vts.request(
                "ParameterCreationRequest",
                {
                    "parameterName": custom_name,
                    "explanation": f"MaiBot bridge input for {parameter_id}",
                    "min": _as_float(spec.get("min"), default=-1.0),
                    "max": _as_float(spec.get("max"), default=1.0),
                    "defaultValue": _as_float(spec.get("default"), default=0.0),
                },
            )
        except Exception as exc:
            self.logger.warning("Failed to create VTS custom parameter %s: %s", custom_name, exc)
            return ""
        self.mapper.input_parameter_names.add(custom_name)
        self.mapper.custom_name_by_parameter_id[parameter_id] = custom_name
        self.logger.info("Created VTS custom parameter %s for %s", custom_name, parameter_id)
        return custom_name

    def _compute_delay_seconds(self, payload: Mapping[str, Any]) -> float:
        timeline_id = str(payload.get("timeline_id") or "").strip()
        if not timeline_id:
            return 0.0
        origin = self.timeline_origins.setdefault(timeline_id, time.monotonic())
        offset_ms = max(0, int(_as_float(payload.get("offset_ms"), default=0.0)))
        elapsed = time.monotonic() - origin
        return max(0.0, offset_ms / 1000.0 - elapsed)

    def _translate_parameter_value(self, parameter_id: str, target: str, value: float) -> float:
        binding = self.model_input_bindings.get(parameter_id)
        if binding is not None and binding.input_parameter == target:
            return _remap_value(
                value,
                source_lower=binding.output_range_lower,
                source_upper=binding.output_range_upper,
                target_lower=binding.input_range_lower,
                target_upper=binding.input_range_upper,
                clamp=True,
            )
        live2d_spec = self.live2d_specs.get(parameter_id)
        input_spec = self.input_parameter_specs.get(target)
        if (
            isinstance(live2d_spec, Mapping)
            and isinstance(input_spec, Mapping)
            and parameter_id != target
        ):
            return _remap_value(
                value,
                source_lower=_as_float(live2d_spec.get("min"), default=-1.0),
                source_upper=_as_float(live2d_spec.get("max"), default=1.0),
                target_lower=_as_float(input_spec.get("min"), default=-1.0),
                target_upper=_as_float(input_spec.get("max"), default=1.0),
                clamp=False,
            )
        return value

    def _input_parameter_default(self, parameter_id: str) -> float:
        spec = self.input_parameter_specs.get(parameter_id)
        if not isinstance(spec, Mapping):
            return 0.0
        minimum = _as_float(spec.get("min"), default=0.0)
        maximum = _as_float(spec.get("max"), default=0.0)
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        if "default" in spec:
            return _as_float(
                spec.get("default"),
                default=0.0 if minimum <= 0.0 <= maximum else (minimum + maximum) / 2.0,
            )
        return 0.0 if minimum <= 0.0 <= maximum else (minimum + maximum) / 2.0

    def _shared_input_should_average(self, parameter_id: str) -> bool:
        spec = self.input_parameter_specs.get(parameter_id)
        if not isinstance(spec, Mapping):
            return False
        minimum = _as_float(spec.get("min"), default=0.0)
        maximum = _as_float(spec.get("max"), default=0.0)
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        return minimum >= 0.0 or maximum <= 0.0

    def _merge_parameter_values(self, parameter_values: list[dict[str, float | str]]) -> list[dict[str, float | str]]:
        grouped: dict[str, list[dict[str, float | str]]] = {}
        for item in parameter_values:
            parameter_id = str(item.get("id") or "").strip()
            if not parameter_id:
                continue
            grouped.setdefault(parameter_id, []).append(dict(item))

        merged: list[dict[str, float | str]] = []
        for parameter_id, items in grouped.items():
            if len(items) == 1:
                merged.append(items[0])
                continue
            if self._shared_input_should_average(parameter_id):
                total_weight = 0.0
                weighted_value = 0.0
                peak_weight = 0.0
                for item in items:
                    weight = min(1.0, max(0.0, _as_float(item.get("weight"), default=1.0)))
                    value = _as_float(item.get("value"))
                    total_weight += weight
                    weighted_value += value * weight
                    peak_weight = max(peak_weight, weight)
                if total_weight <= 0.0:
                    total_weight = float(len(items))
                    weighted_value = sum(_as_float(item.get("value")) for item in items)
                merged_value = weighted_value / total_weight
                input_spec = self.input_parameter_specs.get(parameter_id)
                if isinstance(input_spec, Mapping):
                    minimum = _as_float(input_spec.get("min"), default=merged_value)
                    maximum = _as_float(input_spec.get("max"), default=merged_value)
                    if minimum > maximum:
                        minimum, maximum = maximum, minimum
                    merged_value = min(maximum, max(minimum, merged_value))
                merged.append({"id": parameter_id, "value": merged_value, "weight": peak_weight})
                continue
            default_value = self._input_parameter_default(parameter_id)
            strongest = max(
                items,
                key=lambda item: abs(_as_float(item.get("value")) - default_value),
            )
            merged.append(dict(strongest))
        return merged


def build_app(bridge: MaiBotVTubeStudioBridge) -> web.Application:
    app = web.Application()
    app.router.add_get(bridge.config.listen_path, bridge.handle_ws)
    return app


async def run_bridge(config: BridgeConfig) -> None:
    logger = logging.getLogger("vtube_studio_bridge")
    bridge = MaiBotVTubeStudioBridge(config, logger)
    app = build_app(bridge)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.listen_host, config.listen_port)
    try:
        await site.start()
    except OSError as exc:
        await runner.cleanup()
        await bridge.close()
        if _is_address_in_use_error(exc):
            if await _existing_bridge_reachable(config, logger):
                logger.info(
                    "MaiBot VTS bridge is already running at %s; reusing the existing bridge instance.",
                    _bridge_public_url(config),
                )
                return
            raise RuntimeError(_format_bridge_listen_in_use_message(config)) from exc
        raise
    client_host = "127.0.0.1" if config.listen_host in {"0.0.0.0", "::"} else config.listen_host
    logger.info(
        "MaiBot VTS bridge listening at ws://%s:%s%s; configure MaiBot to connect to ws://%s:%s%s",
        config.listen_host,
        config.listen_port,
        config.listen_path,
        client_host,
        config.listen_port,
        config.listen_path,
    )
    _warn_if_ports_overlap(config, logger)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(getattr(signal, signal_name), stop_event.set)
    try:
        await stop_event.wait()
    finally:
        await bridge.close()
        await runner.cleanup()


def parse_args() -> BridgeConfig:
    parser = argparse.ArgumentParser(description="Bridge MaiBot Live2D JSON events to VTube Studio.")
    parser.add_argument("--listen-host", default=DEFAULT_LISTEN_HOST)
    parser.add_argument("--listen-port", type=int, default=DEFAULT_LISTEN_PORT)
    parser.add_argument("--listen-path", default=DEFAULT_LISTEN_PATH)
    parser.add_argument("--vts-url", default=DEFAULT_VTS_URL)
    parser.add_argument("--plugin-name", default=DEFAULT_PLUGIN_NAME)
    parser.add_argument("--plugin-developer", default=DEFAULT_PLUGIN_DEVELOPER)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--mapping-file", type=Path, default=None)
    parser.add_argument("--no-create-custom-parameters", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    return BridgeConfig(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        listen_path=args.listen_path,
        vts_url=args.vts_url,
        plugin_name=args.plugin_name,
        plugin_developer=args.plugin_developer,
        token_file=args.token_file,
        mapping_file=args.mapping_file,
        create_custom_parameters=not args.no_create_custom_parameters,
        dry_run=args.dry_run,
    )


def _warn_if_ports_overlap(config: BridgeConfig, logger: logging.Logger) -> None:
    parsed = urlparse(config.vts_url)
    vts_port = parsed.port
    if vts_port == config.listen_port:
        logger.warning(
            "VTS API URL (%s) uses the same port as this bridge listener (%s). "
            "Use different ports, for example VTS on 8002 and bridge on 18081.",
            config.vts_url,
            config.listen_port,
        )


def _bridge_public_url(config: BridgeConfig) -> str:
    host = "127.0.0.1" if config.listen_host in {"0.0.0.0", "::"} else config.listen_host
    return f"ws://{host}:{config.listen_port}{config.listen_path}"


def _is_address_in_use_error(exc: OSError) -> bool:
    return int(getattr(exc, "errno", 0) or 0) in {48, 98, 10048}


def _format_bridge_listen_in_use_message(config: BridgeConfig) -> str:
    return (
        f"MaiBot VTS bridge listen port {config.listen_port} is already in use at {_bridge_public_url(config)}. "
        "If MaiBot is already running, reuse the existing bridge instance instead of starting a second copy. "
        "If another service owns this port, stop it or change the bridge listen port."
    )


async def _existing_bridge_reachable(config: BridgeConfig, logger: logging.Logger) -> bool:
    probe_url = _bridge_public_url(config)
    timeout = ClientTimeout(total=2.0, connect=1.0, sock_read=1.0)
    try:
        async with ClientSession(timeout=timeout) as session:
            async with session.ws_connect(probe_url) as ws:
                await ws.send_str(json.dumps({"type": "live2d.capabilities.request"}, ensure_ascii=False))
                response = await ws.receive()
    except Exception as exc:
        if hasattr(logger, "debug"):
            logger.debug("Existing bridge probe failed at %s: %s", probe_url, exc)
        return False
    if response.type != WSMsgType.TEXT:
        return False
    try:
        payload = json.loads(response.data)
    except Exception:
        return False
    return isinstance(payload, Mapping) and str(payload.get("type") or "") == "live2d.capabilities.response"


def _format_vts_handshake_timeout_message(vts_url: str) -> str:
    return (
        f"VTube Studio API websocket handshake timed out at {vts_url}. "
        "The TCP port is reachable, but VTube Studio did not complete the websocket upgrade. "
        "Check that VTube Studio is fully responsive, Plugin API access is enabled, and then restart VTube Studio if needed."
    )


def load_mapping_file(path: Path | None) -> dict[str, list[str]]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("mapping file must contain a JSON object")
    mapping: dict[str, list[str]] = {}
    for key, value in raw.items():
        parameter_id = str(key).strip()
        if isinstance(value, str):
            targets = [value.strip()]
        elif isinstance(value, list):
            targets = [str(item).strip() for item in value]
        else:
            targets = []
        mapping[parameter_id] = [target for target in targets if target]
    return mapping


def merge_parameter_mapping(overrides: Mapping[str, list[str]] | None = None) -> dict[str, list[str]]:
    merged = {key: list(value) for key, value in DEFAULT_PARAMETER_MAPPING.items()}
    for key, value in dict(overrides or {}).items():
        merged[str(key)] = [str(item) for item in value]
    return merged


def convert_vts_live2d_parameter(raw_parameter: Mapping[str, Any]) -> dict[str, float | str]:
    return {
        "id": str(raw_parameter.get("name") or raw_parameter.get("id") or ""),
        "min": _as_float(raw_parameter.get("min"), default=-1.0),
        "max": _as_float(raw_parameter.get("max"), default=1.0),
        "default": _as_float(raw_parameter.get("defaultValue", raw_parameter.get("default")), default=0.0),
        "current": _as_float(raw_parameter.get("value", raw_parameter.get("current")), default=0.0),
    }


def convert_vts_input_parameter(raw_parameter: Mapping[str, Any]) -> dict[str, float | str]:
    return {
        "name": str(raw_parameter.get("name") or raw_parameter.get("id") or ""),
        "min": _as_float(raw_parameter.get("min"), default=-1.0),
        "max": _as_float(raw_parameter.get("max"), default=1.0),
        "default": _as_float(raw_parameter.get("defaultValue", raw_parameter.get("default")), default=0.0),
        "current": _as_float(raw_parameter.get("value", raw_parameter.get("current")), default=0.0),
    }


def load_model_input_bindings(path: Path) -> dict[str, ModelInputBinding]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        return {}
    settings = raw.get("ParameterSettings")
    if not isinstance(settings, list):
        return {}
    bindings: dict[str, ModelInputBinding] = {}
    for raw_setting in settings:
        if not isinstance(raw_setting, Mapping):
            continue
        output_parameter = str(raw_setting.get("OutputLive2D") or "").strip()
        input_parameter = str(raw_setting.get("Input") or "").strip()
        if not output_parameter or not input_parameter:
            continue
        bindings[output_parameter] = ModelInputBinding(
            input_parameter=input_parameter,
            input_range_lower=_as_float(raw_setting.get("InputRangeLower"), default=-1.0),
            input_range_upper=_as_float(raw_setting.get("InputRangeUpper"), default=1.0),
            output_range_lower=_as_float(raw_setting.get("OutputRangeLower"), default=-1.0),
            output_range_upper=_as_float(raw_setting.get("OutputRangeUpper"), default=1.0),
            clamp_input=bool(raw_setting.get("ClampInput")),
        )
    return bindings


def locate_vts_model_file(*, vts_model_name: str, model_name: str = "") -> Path | None:
    normalized_model = _normalize_match_token(model_name)
    normalized_vts_model = _normalize_match_token(vts_model_name)
    best_path: Path | None = None
    best_score = -1
    for root in _candidate_vts_model_roots():
        for candidate in root.rglob("*.vtube.json"):
            if not candidate.is_file():
                continue
            score = 0
            candidate_text = _normalize_match_text(candidate)
            candidate_token = _normalize_match_token(candidate)
            candidate_stem_token = _normalize_match_token(candidate.stem)
            if normalized_vts_model and candidate_stem_token == normalized_vts_model:
                score += 20
            elif normalized_vts_model and normalized_vts_model in candidate_token:
                score += 12
            if normalized_model and normalized_model in candidate_token:
                score += 10
            if "runtime" in {part.lower() for part in candidate.parts}:
                score += 3
            if candidate_text == _normalize_match_text(vts_model_name):
                score += 1
            if candidate_text == _normalize_match_text(f"{vts_model_name}.vtube.json"):
                score += 1
            if score > best_score:
                best_score = score
                best_path = candidate
    return best_path


def _candidate_vts_model_roots() -> list[Path]:
    relative_roots = (
        Path("SteamLibrary/steamapps/common/VTube Studio/VTube Studio_Data/StreamingAssets/Live2DModels"),
        Path("Program Files (x86)/Steam/steamapps/common/VTube Studio/VTube Studio_Data/StreamingAssets/Live2DModels"),
        Path("Program Files/Steam/steamapps/common/VTube Studio/VTube Studio_Data/StreamingAssets/Live2DModels"),
    )
    roots: list[Path] = []
    seen: set[str] = set()
    for drive_letter in string.ascii_uppercase:
        drive_root = Path(f"{drive_letter}:/")
        for relative_root in relative_roots:
            candidate = drive_root / relative_root
            if not candidate.exists():
                continue
            normalized = str(candidate).lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            roots.append(candidate)
    return roots


def infer_groups(parameters: list[Mapping[str, Any]]) -> dict[str, list[str]]:
    ids = {str(item.get("id") or "") for item in parameters}
    groups: dict[str, list[str]] = {}
    eye_blink = [parameter_id for parameter_id in ("ParamEyeLOpen", "ParamEyeROpen") if parameter_id in ids]
    lip_sync = [parameter_id for parameter_id in ("ParamMouthOpenY",) if parameter_id in ids]
    if eye_blink:
        groups["EyeBlink"] = eye_blink
    if lip_sync:
        groups["LipSync"] = lip_sync
    return groups


def _should_create_bound_custom_target(parameter_id: str, bound_target: str) -> bool:
    normalized_target = str(bound_target or "").strip()
    if not normalized_target:
        return False
    if normalized_target == parameter_id:
        return True
    return normalized_target.startswith("Param")


def select_motion_hotkey(
    hotkeys: list[Any],
    *,
    motion_name: str,
    motion_file: str,
    model_name: str = "",
) -> Mapping[str, Any] | None:
    """Select the best VTS hotkey for a Live2D motion request."""

    best_hotkey: Mapping[str, Any] | None = None
    best_score = 0
    normalized_motion = _normalize_match_token(motion_name)
    normalized_file = _normalize_match_text(motion_file)
    normalized_model = _normalize_match_token(model_name)
    for raw_hotkey in hotkeys:
        if not isinstance(raw_hotkey, Mapping):
            continue
        hotkey_type = str(raw_hotkey.get("type") or "").strip()
        if hotkey_type not in MOTION_HOTKEY_TYPES:
            continue
        score = MOTION_IDLE_TYPE_BONUS.get(hotkey_type, 0)
        hotkey_file = _normalize_match_text(raw_hotkey.get("file"))
        hotkey_name = _normalize_match_text(raw_hotkey.get("name"))
        hotkey_id = _normalize_match_text(raw_hotkey.get("hotkeyID") or raw_hotkey.get("id"))
        matched = False
        if normalized_file and hotkey_file == normalized_file:
            score += 100
            matched = True
        elif normalized_file and hotkey_file.endswith(normalized_file):
            score += 80
            matched = True
        if normalized_motion:
            motion_token = _normalize_match_token(normalized_motion)
            if motion_token and motion_token in _normalize_match_token(hotkey_file):
                score += 35
                matched = True
            if motion_token and motion_token in _normalize_match_token(hotkey_name):
                score += 25
                matched = True
            if motion_token and motion_token in _normalize_match_token(hotkey_id):
                score += 20
                matched = True
        if not matched:
            continue
        if normalized_model:
            searchable = _normalize_match_token(f"{hotkey_name} {hotkey_file} {hotkey_id}")
            if normalized_model in searchable:
                score += 5
        if score > best_score:
            best_score = score
            best_hotkey = raw_hotkey
    return best_hotkey


def interpolate_timeline_keyframes(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    interval_ms: int = TIMELINE_INTERPOLATION_INTERVAL_MS,
) -> list[dict[str, Any]]:
    """Create bridge-side transition frames between two Live2D timeline keyframes."""

    previous_offset = max(0, int(_as_float(previous.get("offset_ms"), default=0.0)))
    current_offset = max(0, int(_as_float(current.get("offset_ms"), default=0.0)))
    if current_offset <= previous_offset:
        payload = dict(current)
        payload["keyframe"] = True
        return [payload]
    interval = max(10, int(interval_ms))
    previous_parameters = _parameter_map(previous.get("parameters"))
    current_parameters = _parameter_map(current.get("parameters"))
    if not current_parameters:
        payload = dict(current)
        payload["keyframe"] = True
        return [payload]
    frames: list[dict[str, Any]] = []
    for offset_ms in range(previous_offset + interval, current_offset, interval):
        progress = (offset_ms - previous_offset) / max(1, current_offset - previous_offset)
        frames.append(
            _interpolated_timeline_payload(
                current,
                previous_parameters,
                current_parameters,
                offset_ms=offset_ms,
                factor=_smoothstep(progress),
                keyframe=False,
            )
        )
    frames.append(
        _interpolated_timeline_payload(
            current,
            previous_parameters,
            current_parameters,
            offset_ms=current_offset,
            factor=1.0,
            keyframe=True,
        )
    )
    return frames


def _interpolated_timeline_payload(
    template: Mapping[str, Any],
    previous_parameters: Mapping[str, Mapping[str, Any]],
    current_parameters: Mapping[str, Mapping[str, Any]],
    *,
    offset_ms: int,
    factor: float,
    keyframe: bool,
) -> dict[str, Any]:
    payload = dict(template)
    payload["offset_ms"] = max(0, int(offset_ms))
    payload["interpolated"] = True
    payload["keyframe"] = bool(keyframe)
    parameters: list[dict[str, float | str]] = []
    for parameter_id, current in current_parameters.items():
        previous = previous_parameters.get(parameter_id)
        start_value = _as_float(previous.get("value"), default=_as_float(current.get("value"))) if previous else _as_float(current.get("value"))
        end_value = _as_float(current.get("value"))
        start_weight = _as_float(previous.get("weight"), default=_as_float(current.get("weight"), default=1.0)) if previous else _as_float(current.get("weight"), default=1.0)
        end_weight = _as_float(current.get("weight"), default=1.0)
        parameters.append(
            {
                "id": parameter_id,
                "value": start_value + (end_value - start_value) * factor,
                "weight": min(1.0, max(0.0, start_weight + (end_weight - start_weight) * factor)),
            }
        )
    payload["parameters"] = parameters
    return payload


def _parameter_map(raw_parameters: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(raw_parameters, list):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for raw_parameter in raw_parameters:
        if not isinstance(raw_parameter, Mapping):
            continue
        parameter_id = str(raw_parameter.get("id") or "").strip()
        if parameter_id:
            result[parameter_id] = dict(raw_parameter)
    return result


def _smoothstep(value: float) -> float:
    clamped = min(1.0, max(0.0, float(value)))
    return clamped * clamped * (3.0 - 2.0 * clamped)


def _normalize_match_text(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/").lower()
    return text.rsplit("/", 1)[-1]


def _normalize_match_token(value: Any) -> str:
    return "".join(char for char in _normalize_match_text(value) if char.isalnum())


def sanitize_vts_parameter_name(parameter_id: str) -> str:
    allowed = set(string.ascii_letters + string.digits)
    sanitized = "".join(char for char in str(parameter_id) if char in allowed)
    if 4 <= len(sanitized) <= 32:
        return sanitized
    digest = hashlib.sha1(str(parameter_id).encode("utf-8")).hexdigest()[:8]
    prefix = "MB"
    trimmed = sanitized[: 32 - len(prefix) - len(digest)]
    return f"{prefix}{trimmed}{digest}"[:32]


def is_forbidden_parameter_id(parameter_id: str) -> bool:
    lowered = parameter_id.lower()
    return any(token in lowered for token in FORBIDDEN_ID_TOKENS)


def default_spec_for(parameter_id: str) -> dict[str, float | str]:
    for spec in FALLBACK_LIVE2D_PARAMETERS:
        if spec["id"] == parameter_id:
            return dict(spec)
    return {"id": parameter_id, "min": -1.0, "max": 1.0, "default": 0.0, "current": 0.0}


def _response_data(response: Mapping[str, Any]) -> Mapping[str, Any]:
    data = response.get("data")
    return data if isinstance(data, Mapping) else {}


def _as_float(value: Any, *, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _remap_value(
    value: float,
    *,
    source_lower: float,
    source_upper: float,
    target_lower: float,
    target_upper: float,
    clamp: bool,
) -> float:
    if source_upper == source_lower:
        return target_lower
    ratio = (float(value) - source_lower) / (source_upper - source_lower)
    if clamp:
        ratio = min(1.0, max(0.0, ratio))
    return target_lower + ratio * (target_upper - target_lower)


def main() -> None:
    config = parse_args()
    logger = logging.getLogger("vtube_studio_bridge")
    try:
        asyncio.run(run_bridge(config))
    except KeyboardInterrupt:
        return
    except RuntimeError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

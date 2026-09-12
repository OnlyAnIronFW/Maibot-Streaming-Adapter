"""SoulLink-style Live2D adapter that reuses the current VTS bridge output path."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import math
import random
import time
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Mapping, Protocol

from src.config.config import config_manager
from src.llm_models.openai_compat import build_openai_compatible_client_config

from ..config import Live2DConfig, Live2DSoulLinkConfig
from ..live2d_shell_protocol import (
    ShellExpressionMessage,
    ShellLoadModelMessage,
    ShellTtsMotionFrameMessage,
    message_to_payload,
)
from ..live2d_soullink_vendor.src.config.models import APIConfig as SoulLinkAPIConfig
from ..live2d_soullink_vendor.src.generators.expression import ExpressionGenerator as SoulLinkExpressionGenerator
from .controller import Live2DController, _timeline_prepare_offset
from .profile import ParameterProfile, ParameterSpec
from .speech_timeline import SpeechTimeline

SOULLINK_TRANSITION_SAMPLE_INTERVAL_MS = 16
SOULLINK_REALTIME_TRANSITION_LEAD_MS = 48
SOULLINK_MOTION_PREFETCH_FRAMES = 8
SOULLINK_GENERATOR_RETRY_ATTEMPTS = 2


@dataclass(frozen=True)
class ResolvedSoulLinkModel:
    """Resolved OpenAI-compatible endpoint data for the SoulLink generator."""

    provider_name: str
    model_identifier: str
    base_url: str
    api_key: str
    has_extra_request_metadata: bool


class SoulLinkOutputSink(Protocol):
    """Dispatch SoulLink output to a concrete runtime target."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send_expression(
        self,
        parameters: list[dict[str, float | str]],
        *,
        duration_ms: int,
        timeline_id: str,
        purpose: str,
    ) -> dict[str, Any]: ...

    async def send_timeline_clip(
        self,
        keyframes: list[dict[str, Any]],
        *,
        timeline_id: str,
        easing: str,
        blend: str,
        priority: int,
        purpose: str,
    ) -> dict[str, Any]: ...


class VtsSoulLinkSink:
    """Dispatch SoulLink output to the existing VTS-backed controller path."""

    def __init__(self, *, base_controller: Live2DController, profile: ParameterProfile) -> None:
        self.base_controller = base_controller
        self.profile = profile

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_expression(
        self,
        parameters: list[dict[str, float | str]],
        *,
        duration_ms: int,
        timeline_id: str,
        purpose: str,
    ) -> dict[str, Any]:
        return await self.base_controller.send_parameters(
            parameters,
            duration_ms=duration_ms,
            easing="easeInOutCubic",
            blend="replace",
            priority=5,
            timeline_id=timeline_id,
            purpose=purpose,
        )

    async def send_timeline_clip(
        self,
        keyframes: list[dict[str, Any]],
        *,
        timeline_id: str,
        easing: str,
        blend: str,
        priority: int,
        purpose: str,
    ) -> dict[str, Any]:
        return await self.base_controller.send_timeline_clip(
            keyframes,
            timeline_id=timeline_id,
            easing=easing,
            blend=blend,
            priority=priority,
            purpose=purpose,
        )


class ShellSoulLinkSink:
    """Dispatch SoulLink output to the transparent shell runtime."""

    def __init__(self, *, runtime: Any, profile: ParameterProfile, model_path: str = "") -> None:
        self.runtime = runtime
        self.profile = profile
        self.model_path = str(model_path or "").strip()
        self._clip_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        model = _resolve_shell_model_payload(self.profile, self.model_path)
        register_model_file = getattr(self.runtime, "register_model_file", None)
        if callable(register_model_file) and str(self.model_path or "").strip():
            with contextlib.suppress(Exception):
                registered_url = register_model_file(self.model_path)
                if registered_url:
                    model["path"] = str(registered_url)
        if model:
            await self.runtime.broadcast(message_to_payload(ShellLoadModelMessage(model=model)))
        await self.runtime.broadcast({"type": "reset", "duration_ms": 0})

    async def stop(self) -> None:
        await self._cancel_clip_tasks()
        await self.runtime.broadcast({"type": "reset", "duration_ms": 0})

    async def send_expression(
        self,
        parameters: list[dict[str, float | str]],
        *,
        duration_ms: int,
        timeline_id: str,
        purpose: str,
    ) -> dict[str, Any]:
        shell_parameters = _parameter_entries_to_shell_map(parameters)
        if not shell_parameters:
            return {"success": False, "error": "no valid Live2D parameters"}
        await self.runtime.broadcast(
            message_to_payload(
                ShellExpressionMessage(
                    parameters=shell_parameters,
                    duration_ms=max(0, int(duration_ms)),
                )
            )
        )
        return {
            "success": True,
            "timeline_id": str(timeline_id or ""),
            "parameters": dict(shell_parameters),
            "purpose": str(purpose or ""),
        }

    async def send_timeline_clip(
        self,
        keyframes: list[dict[str, Any]],
        *,
        timeline_id: str,
        easing: str,
        blend: str,
        priority: int,
        purpose: str,
    ) -> dict[str, Any]:
        del easing, blend, priority
        ordered_frames = sorted(
            (frame for frame in keyframes if isinstance(frame, Mapping)),
            key=lambda item: max(0, int(item.get("offset_ms") or 0)),
        )
        if not ordered_frames:
            return {"success": False, "error": "no valid Live2D timeline frames"}
        clip_task = asyncio.create_task(
            self._broadcast_timeline_frames(
                ordered_frames,
                timeline_id=str(timeline_id or ""),
            ),
            name=f"soullink_shell.clip.{timeline_id or 'anonymous'}",
        )
        self._clip_tasks.add(clip_task)
        clip_task.add_done_callback(self._clip_tasks.discard)
        return {
            "success": True,
            "timeline_id": str(timeline_id or ""),
            "frames": len(ordered_frames),
            "purpose": str(purpose or ""),
        }

    async def _broadcast_timeline_frames(
        self,
        keyframes: list[Mapping[str, Any]],
        *,
        timeline_id: str,
    ) -> None:
        if not keyframes:
            return
        baseline_offset_ms = max(0, int(keyframes[0].get("offset_ms") or 0))
        clip_started_at = time.monotonic()
        for frame in keyframes:
            parameters = frame.get("parameters")
            if not isinstance(parameters, list):
                continue
            shell_parameters = _parameter_entries_to_shell_map(parameters)
            if not shell_parameters:
                continue
            offset_ms = max(0, int(frame.get("offset_ms") or 0))
            target_delay_sec = max(0.0, (offset_ms - baseline_offset_ms) / 1000.0)
            remaining = (clip_started_at + target_delay_sec) - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            await self.runtime.broadcast(
                message_to_payload(
                    ShellTtsMotionFrameMessage(
                        timeline_id=timeline_id,
                        parameters=shell_parameters,
                        duration_ms=max(0, int(frame.get("duration_ms") or 0)),
                        offset_ms=offset_ms,
                    )
                )
            )

    async def _cancel_clip_tasks(self) -> None:
        tasks = list(self._clip_tasks)
        self._clip_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def resolve_live2d_scheme(config: Live2DConfig) -> str:
    """Resolve the effective Live2D runtime scheme while preserving old defaults."""

    scheme = str(getattr(config, "scheme", "auto") or "auto").strip().lower()
    if scheme in {"legacy", "embodied", "soullink", "soullink_shell"}:
        return scheme
    if bool(getattr(config.embodied, "enabled", False)) and str(getattr(config.embodied, "source_session_id", "") or "").strip():
        return "embodied"
    return "legacy"


def build_soullink_available_parameters(profile: ParameterProfile) -> dict[str, dict[str, float | str]]:
    """Convert the current parameter profile into SoulLink's expected metadata shape."""

    payload: dict[str, dict[str, float | str]] = {}
    for spec in profile.parameters.values():
        if not spec.enabled:
            continue
        payload[spec.id] = {
            "name": spec.role or spec.id,
            "min": float(spec.minimum),
            "max": float(spec.maximum),
            "default": float(spec.default),
        }
    return payload


def _parameter_entries_to_shell_map(parameters: list[Mapping[str, Any]]) -> dict[str, float]:
    payload: dict[str, float] = {}
    for item in parameters:
        if not isinstance(item, Mapping):
            continue
        parameter_id = str(item.get("id") or "").strip()
        if not parameter_id:
            continue
        try:
            payload[parameter_id] = float(item.get("value") or 0.0)
        except (TypeError, ValueError):
            continue
    return payload


def _resolve_shell_model_payload(profile: ParameterProfile, model_path: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    model_id = str(getattr(profile, "model_id", "") or "").strip()
    model_name = str(getattr(profile, "model_name", "") or "").strip()
    if model_id:
        payload["id"] = model_id
    if model_name:
        payload["name"] = model_name
    normalized_model_path = str(model_path or "").strip()
    if normalized_model_path:
        path = Path(normalized_model_path)
        if path.is_dir():
            model_files = list(path.glob("*.model3.json"))
            if not model_files:
                model_files = list(path.glob("*.json"))
            if model_files:
                path = model_files[0]
        if path.exists() and path.is_file():
            payload["path"] = path.resolve().as_uri()
    return payload


def _is_mouth_open_parameter(spec: ParameterSpec | None, parameter_id: str) -> bool:
    role = str(spec.role if spec is not None else "").strip().lower()
    if role == "mouth.open":
        return True
    pid = str(parameter_id or "").replace("_", "").lower()
    return "mouth" in pid and "open" in pid


def _is_mouth_form_parameter(spec: ParameterSpec | None, parameter_id: str) -> bool:
    role = str(spec.role if spec is not None else "").strip().lower()
    if role == "mouth.form":
        return True
    pid = str(parameter_id or "").replace("_", "").lower()
    return "mouth" in pid and "form" in pid


def _sanitize_generator_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return "".join(char for char in text if ord(char) <= 0xFFFF)


def _sanitize_frame_plan(frame_plan: Mapping[str, Any], *, fallback_action: str = "自然动作") -> dict[str, Any]:
    sanitized = dict(frame_plan)
    action = _sanitize_generator_text(sanitized.get("action") or fallback_action)
    sanitized["action"] = action if action.strip() else fallback_action
    emphasis = _sanitize_generator_text(sanitized.get("emphasis") or "")
    sanitized["emphasis"] = emphasis
    return sanitized


@contextlib.contextmanager
def _suppress_generator_console_output():
    original_print = builtins.print
    try:
        builtins.print = lambda *args, **kwargs: None
        with contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()):
            yield
    finally:
        builtins.print = original_print


def resolve_soullink_model_config(config: Live2DSoulLinkConfig) -> ResolvedSoulLinkModel:
    """Resolve provider/model settings from MaiBot model_config for the SoulLink adapter."""

    model_config = config_manager.get_model_config()
    models_by_name = {model.name: model for model in model_config.models}
    providers_by_name = {provider.name: provider for provider in model_config.api_providers}

    provider_name = str(config.api_provider or "").strip()
    model_identifier = str(config.model_identifier or "").strip()
    if provider_name and model_identifier:
        provider = providers_by_name.get(provider_name)
        if provider is None:
            raise RuntimeError(f"SoulLink live2d api_provider not found in model_config: {provider_name}")
        resolved = build_openai_compatible_client_config(provider)
        has_extra_request_metadata = bool(resolved.default_headers or resolved.default_query)
        return ResolvedSoulLinkModel(
            provider_name=provider_name,
            model_identifier=model_identifier,
            base_url=str(resolved.base_url or ""),
            api_key=str(resolved.api_key or ""),
            has_extra_request_metadata=has_extra_request_metadata,
        )

    if str(config.model_name or "").strip():
        model_info = models_by_name.get(config.model_name)
        if model_info is None:
            raise RuntimeError(f"SoulLink live2d model_name not found in model_config: {config.model_name}")
        provider = providers_by_name.get(model_info.api_provider)
        if provider is None:
            raise RuntimeError(f"SoulLink live2d provider not found in model_config: {model_info.api_provider}")
        resolved = build_openai_compatible_client_config(provider)
        has_extra_request_metadata = bool(resolved.default_headers or resolved.default_query)
        return ResolvedSoulLinkModel(
            provider_name=str(provider.name or ""),
            model_identifier=str(model_info.model_identifier or ""),
            base_url=str(resolved.base_url or ""),
            api_key=str(resolved.api_key or ""),
            has_extra_request_metadata=has_extra_request_metadata,
        )

    raise RuntimeError(
        "SoulLink live2d backend requires either both live2d.soullink.api_provider "
        "and live2d.soullink.model_identifier, or a fallback live2d.soullink.model_name."
    )


class SoulLinkLive2DController:
    """Controller-compatible wrapper that uses SoulLink generation and the current VTS bridge path."""

    def __init__(
        self,
        *,
        base_controller: Live2DController,
        profile: ParameterProfile,
        config: Live2DSoulLinkConfig,
        sink: SoulLinkOutputSink | None = None,
        logger: Any = None,
    ) -> None:
        self.base_controller = base_controller
        self.profile = profile
        self.config = config
        self.sink = sink if sink is not None else VtsSoulLinkSink(base_controller=base_controller, profile=profile)
        self.logger = logger
        resolved_model = resolve_soullink_model_config(config)
        self._resolved_model = resolved_model
        self._generator = SoulLinkExpressionGenerator(
            SoulLinkAPIConfig(
                provider=resolved_model.provider_name or "openai",
                api_key=resolved_model.api_key,
                base_url=resolved_model.base_url,
                model=resolved_model.model_identifier,
                temperature=float(config.temperature),
                max_tokens=int(config.max_tokens),
                enable_thinking=bool(config.enable_thinking),
                response_format_json_object=bool(config.response_format_json_object),
            ),
            eye_open_binary=bool(config.eye_open_binary),
            joint_motion_boost=float(config.joint_motion_boost),
            tts_motion_keep_lip_sync=bool(config.tts_motion_keep_lip_sync),
        )
        self._generator.custom_prompt = str(config.custom_prompt or "").strip()
        self._reply_motion_tasks: set[asyncio.Task[None]] = set()
        self._timeline_states: dict[str, dict[str, Any]] = {}

    @property
    def is_speaking(self) -> bool:
        return bool(getattr(self.base_controller, "is_speaking", False))

    @property
    def active_timeline_id(self) -> str:
        return str(getattr(self.base_controller, "active_timeline_id", "") or "")

    async def start(self) -> None:
        self._safe_update_parameters(build_soullink_available_parameters(self.profile))
        self._log_info(
            "SoulLink Live2D adapter starting: "
            f"provider={self._resolved_model.provider_name} "
            f"model={self._resolved_model.model_identifier} "
            f"source_model_name={self.config.model_name or '<direct>'}"
        )
        if self._resolved_model.has_extra_request_metadata:
            self._log_info(
                "SoulLink adapter resolved a provider with extra headers/query metadata; "
                "the vendored generator only uses api_key/base_url, so provider-specific extras are not forwarded."
            )
        await self.base_controller.start()
        await self.sink.start()

    async def stop(self) -> None:
        tasks = list(self._reply_motion_tasks)
        self._reply_motion_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._timeline_state_store().clear()
        await self.sink.stop()
        await self.base_controller.stop()

    async def play_reply(
        self,
        text: str,
        *,
        audio_timeline: Mapping[str, Any] | None = None,
        emotion_intent: str = "",
        motion_intensity: float | None = None,
        timeline_prepare_ms: int | None = None,
    ) -> SpeechTimeline:
        timeline = await self.base_controller.play_reply(
            text,
            audio_timeline=audio_timeline,
            emotion_intent=emotion_intent,
            motion_intensity=motion_intensity,
            timeline_prepare_ms=timeline_prepare_ms,
            suppress_lipsync=True,
            suppress_expression_overlay=True,
        )
        if str(text or "").strip() and bool(getattr(self.config, "tts_motion_keep_lip_sync", True)):
            prepare_ms = _timeline_prepare_offset(timeline)
            lipsync_keyframes = self._build_soullink_lipsync_keyframes(
                timeline_id=timeline.timeline_id,
                duration_ms=timeline.estimated_duration_ms,
                prepare_ms=prepare_ms,
            )
            if lipsync_keyframes:
                await self.base_controller.send_timeline_clip(
                    lipsync_keyframes,
                    timeline_id=timeline.timeline_id,
                    easing="easeInOutCubic",
                    blend="replace",
                    priority=6,
                    purpose="lipsync",
                )
        if str(text or "").strip():
            task = asyncio.create_task(
                self._run_reply_motion(
                    timeline=timeline,
                    text=text,
                    audio_timeline=audio_timeline,
                    emotion_intent=emotion_intent,
                ),
                name=f"live2d_soullink.reply.{timeline.timeline_id}",
            )
            self._reply_motion_tasks.add(task)
            task.add_done_callback(self._reply_motion_tasks.discard)
        return timeline

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base_controller, name)

    async def _run_reply_motion(
        self,
        *,
        timeline: SpeechTimeline,
        text: str,
        audio_timeline: Mapping[str, Any] | None,
        emotion_intent: str,
    ) -> None:
        context = self._build_reply_context(emotion_intent)
        sanitized_text = _sanitize_generator_text(text)
        sanitized_context = _sanitize_generator_text(context)
        motion_started_at_monotonic = time.monotonic()
        try:
            initial_expression: Mapping[str, Any] | None
            try:
                initial_expression = await self._safe_generate(sanitized_text, sanitized_context)
            except Exception as exc:
                self._log_warning(
                    f"SoulLink initial expression generation failed for timeline={timeline.timeline_id}: {exc}"
                )
                initial_expression = None
            if bool(self.config.tts_motion_enabled):
                if initial_expression is not None:
                    await self._dispatch_timeline_expression_result(
                        initial_expression,
                        timeline_id=timeline.timeline_id,
                        purpose="soullink-initial",
                        offset_ms=0,
                        fallback_duration_ms=240,
                    )
                await self._dispatch_tts_motion(
                    timeline=timeline,
                    text=sanitized_text,
                    audio_timeline=audio_timeline,
                    context=sanitized_context,
                    motion_started_at_monotonic=motion_started_at_monotonic,
                )
            else:
                if initial_expression is not None:
                    await self._dispatch_expression_result(
                        initial_expression,
                        timeline_id=timeline.timeline_id,
                        purpose="soullink-initial",
                        default_duration_ms=240,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_warning(f"SoulLink reply motion failed for timeline={timeline.timeline_id}: {exc}")

    def _build_reply_context(self, emotion_intent: str) -> str:
        normalized_emotion = str(emotion_intent or "").strip()
        if not normalized_emotion:
            return "MaiBot livestream reply. Generate expressive but VTS-safe non-mouth Live2D parameters."
        return (
            "MaiBot livestream reply. Generate expressive but VTS-safe non-mouth Live2D parameters. "
            f"Emotion hint: {normalized_emotion}."
        )

    async def _dispatch_tts_motion(
        self,
        *,
        timeline: SpeechTimeline,
        text: str,
        audio_timeline: Mapping[str, Any] | None,
        context: str,
        motion_started_at_monotonic: float | None = None,
    ) -> None:
        sanitized_text = _sanitize_generator_text(text)
        sanitized_context = _sanitize_generator_text(context)
        frame_duration_ms = max(240, int(self.config.tts_motion_frame_duration_ms))
        total_duration_ms = self._resolve_total_motion_duration_ms(
            text=sanitized_text,
            audio_timeline=audio_timeline,
            fallback_duration_ms=timeline.estimated_duration_ms,
        )
        total_frames = self._resolve_total_frames(
            text=sanitized_text,
            audio_timeline=audio_timeline,
            fallback_duration_ms=total_duration_ms,
            frame_duration_ms=frame_duration_ms,
        )
        try:
            motion_plan = await self._safe_generate_motion_plan(
                speech_text=sanitized_text,
                total_frames=total_frames,
                context=sanitized_context,
            )
        except Exception as exc:
            self._log_warning(
                f"SoulLink motion plan generation failed for timeline={timeline.timeline_id}: {exc}; "
                "falling back to default motion plan."
            )
            motion_plan = [
                {"frameIndex": frame_index, "action": "自然动作", "emphasis": ""}
                for frame_index in range(total_frames)
            ]
        frame_offsets_ms = self._build_motion_frame_offsets_ms(
            total_frames=total_frames,
            total_duration_ms=total_duration_ms,
        )
        pending_results: dict[int, asyncio.Task[dict[str, Any]]] = {}
        next_frame_to_schedule = 0
        prefetch_window = self._resolve_motion_prefetch_frames(total_frames=total_frames)
        def _schedule_generation(frame_index: int) -> None:
            nonlocal next_frame_to_schedule
            if frame_index in pending_results:
                return
            frame_plan = motion_plan[frame_index] if frame_index < len(motion_plan) else {
                "frameIndex": frame_index,
                "action": "鑷劧鍔ㄤ綔",
                "emphasis": "",
            }
            sanitized_frame_plan = _sanitize_frame_plan(frame_plan)
            pending_results[frame_index] = asyncio.create_task(
                self._safe_generate_tts_motion_frame_with_plan(
                    frame_index=frame_index,
                    total_frames=total_frames,
                    frame_plan=sanitized_frame_plan,
                    context=sanitized_context,
                    frame_duration_ms=frame_duration_ms,
                ),
                name=f"soullink.motion_frame.{timeline.timeline_id}.{frame_index}",
            )
            next_frame_to_schedule = max(next_frame_to_schedule, frame_index + 1)

        while next_frame_to_schedule < min(total_frames, prefetch_window):
            _schedule_generation(next_frame_to_schedule)

        for frame_index in range(total_frames):
            while next_frame_to_schedule < total_frames and len(pending_results) < prefetch_window:
                _schedule_generation(next_frame_to_schedule)
            result_task = pending_results.pop(frame_index, None)
            if result_task is None:
                _schedule_generation(frame_index)
                result_task = pending_results.pop(frame_index)
            try:
                result = await result_task
            except Exception as exc:
                self._log_warning(
                    f"SoulLink motion frame generation failed: timeline={timeline.timeline_id} frame={frame_index} {exc}"
                )
                continue
            duration_ms = max(1, int(result.get("duration", frame_duration_ms) or frame_duration_ms))
            await self._dispatch_timeline_expression_result(
                result,
                timeline_id=timeline.timeline_id,
                purpose=f"soullink-frame-{frame_index}",
                offset_ms=frame_offsets_ms[frame_index],
                fallback_duration_ms=duration_ms,
                motion_started_at_monotonic=motion_started_at_monotonic,
            )
            continue
            frame_plan = motion_plan[frame_index] if frame_index < len(motion_plan) else {
                "frameIndex": frame_index,
                "action": "自然动作",
                "emphasis": "",
            }
            sanitized_frame_plan = _sanitize_frame_plan(frame_plan)
            try:
                result = await self._safe_generate_tts_motion_frame_with_plan(
                    frame_index=frame_index,
                    total_frames=total_frames,
                    frame_plan=sanitized_frame_plan,
                    context=sanitized_context,
                    frame_duration_ms=frame_duration_ms,
                )
            except Exception as exc:
                self._log_warning(
                    f"SoulLink motion frame generation failed: timeline={timeline.timeline_id} frame={frame_index} {exc}"
                )
                continue
            duration_ms = max(1, int(result.get("duration", frame_duration_ms) or frame_duration_ms))
            accumulated_offset_ms += duration_ms
            await self._dispatch_timeline_expression_result(
                result,
                timeline_id=timeline.timeline_id,
                purpose=f"soullink-frame-{frame_index}",
                offset_ms=accumulated_offset_ms,
                fallback_duration_ms=duration_ms,
                motion_started_at_monotonic=motion_started_at_monotonic,
            )
        for task in pending_results.values():
            task.cancel()
        for task in pending_results.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _dispatch_expression_result(
        self,
        result: Mapping[str, Any],
        *,
        timeline_id: str,
        purpose: str,
        default_duration_ms: int,
    ) -> None:
        parameters = self._convert_parameters(result.get("parameters", {}))
        if not parameters:
            return
        duration_ms = max(0, int(result.get("duration", default_duration_ms) or default_duration_ms))
        await self.sink.send_expression(
            parameters,
            duration_ms=duration_ms,
            timeline_id=timeline_id,
            purpose=purpose,
        )

    async def _dispatch_timeline_expression_result(
        self,
        result: Mapping[str, Any],
        *,
        timeline_id: str,
        purpose: str,
        offset_ms: int,
        fallback_duration_ms: int,
        motion_started_at_monotonic: float | None = None,
    ) -> None:
        parameters = self._convert_parameters(result.get("parameters", {}))
        if not parameters:
            return
        duration_ms = max(0, int(result.get("duration", fallback_duration_ms) or fallback_duration_ms))
        offset_ms = max(0, int(offset_ms))
        effective_parameters = self._merge_with_previous_timeline_state(timeline_id, parameters)
        start_offset_ms, end_offset_ms = self._resolve_realtime_timeline_window(
            timeline_id=timeline_id,
            offset_ms=offset_ms,
            duration_ms=duration_ms,
            motion_started_at_monotonic=motion_started_at_monotonic,
        )
        keyframes = self._build_eased_timeline_keyframes(
            timeline_id=timeline_id,
            parameters=effective_parameters,
            offset_ms=end_offset_ms,
            duration_ms=duration_ms,
            purpose=purpose,
            start_offset_ms=start_offset_ms,
        )
        if not keyframes:
            return
        await self.sink.send_timeline_clip(
            keyframes,
            timeline_id=timeline_id,
            easing="easeInOutCubic",
            blend="replace",
            priority=5,
            purpose=purpose,
        )
        self._timeline_state_store()[timeline_id] = {
            "offset_ms": end_offset_ms,
            "parameters": [dict(item) for item in effective_parameters],
        }

    def _convert_parameters(self, raw_parameters: Any) -> list[dict[str, float | str]]:
        if not isinstance(raw_parameters, Mapping):
            return []
        converted: list[dict[str, float | str]] = []
        weight = float(self.config.parameter_weight)
        keep_lip_sync = bool(getattr(self.config, "tts_motion_keep_lip_sync", True))
        for parameter_id, raw_value in raw_parameters.items():
            pid = str(parameter_id or "").strip()
            if not pid:
                continue
            spec = self.profile.parameters.get(pid)
            if spec is None or not spec.enabled:
                continue
            if keep_lip_sync and _is_mouth_open_parameter(spec, pid):
                continue
            if keep_lip_sync and _is_mouth_form_parameter(spec, pid) and not bool(self.config.keep_mouth_form):
                continue
            try:
                value = spec.clamp(float(raw_value))
            except (TypeError, ValueError):
                continue
            converted.append({"id": pid, "value": value, "weight": weight})
        return converted

    def _build_soullink_lipsync_keyframes(
        self,
        *,
        timeline_id: str,
        duration_ms: int,
        prepare_ms: int = 0,
    ) -> list[dict[str, Any]]:
        mouth_open_spec = self.profile.find_by_role("mouth.open")
        if mouth_open_spec is None or not mouth_open_spec.enabled:
            return []
        mouth_form_spec = self.profile.find_by_role("mouth.form")
        open_range = max(0.0001, float(mouth_open_spec.maximum) - float(mouth_open_spec.minimum))
        open_floor = float(mouth_open_spec.minimum) + open_range * 0.32
        open_ceil = float(mouth_open_spec.minimum) + open_range * 0.98
        form_default = 0.0
        form_min = -1.0
        form_max = 1.0
        form_amplitude = 0.0
        if mouth_form_spec is not None and mouth_form_spec.enabled:
            form_default = float(mouth_form_spec.default)
            form_min = float(mouth_form_spec.minimum)
            form_max = float(mouth_form_spec.maximum)
            form_amplitude = max((form_max - form_min) * 0.42, 0.2)
        time_sec = 0.0
        rng = random.Random()
        keyframes: list[dict[str, Any]] = []
        interval_ms = 50
        for offset_ms in range(0, max(1, int(duration_ms)), interval_ms):
            time_sec += 0.05
            wave1 = math.sin(time_sec * 9.5) * 0.6 + 0.6
            wave2 = math.sin(time_sec * 9.5 * 1.9) * 0.32
            wave3 = math.sin(time_sec * 9.5 * 0.73) * 0.2
            jitter = (rng.random() - 0.5) * 0.16
            normalized = max(0.0, min(1.0, wave1 + wave2 + wave3 + jitter))
            mouth_open_value = mouth_open_spec.clamp(open_floor + (open_ceil - open_floor) * normalized)
            parameters: list[dict[str, float | str]] = [
                {"id": mouth_open_spec.id, "value": mouth_open_value, "weight": 1.0}
            ]
            if mouth_form_spec is not None and mouth_form_spec.enabled:
                form_wave = math.sin(time_sec * 2.6) + math.sin(time_sec * 4.9) * 0.25
                mouth_form_value = max(form_min, min(form_max, form_default + form_wave * form_amplitude))
                parameters.append({"id": mouth_form_spec.id, "value": mouth_form_value, "weight": 0.88})
            keyframes.append(
                self._build_timeline_keyframe(
                    offset_ms=max(0, int(prepare_ms)) + offset_ms,
                    parameters=parameters,
                    duration_ms=0,
                    purpose="lipsync",
                    keyframe=True,
                )
            )
        closing_parameters: list[dict[str, float | str]] = [
            {"id": mouth_open_spec.id, "value": mouth_open_spec.clamp(float(mouth_open_spec.default)), "weight": 1.0}
        ]
        if mouth_form_spec is not None and mouth_form_spec.enabled:
            closing_parameters.append(
                {
                    "id": mouth_form_spec.id,
                    "value": mouth_form_spec.clamp(float(mouth_form_spec.default)),
                    "weight": 0.88,
                }
            )
        keyframes.append(
            self._build_timeline_keyframe(
                offset_ms=max(0, int(prepare_ms)) + max(0, int(duration_ms)),
                parameters=closing_parameters,
                duration_ms=0,
                purpose="lipsync",
                keyframe=True,
            )
        )
        return keyframes

    def _merge_with_previous_timeline_state(
        self,
        timeline_id: str,
        parameters: list[dict[str, float | str]],
    ) -> list[dict[str, float | str]]:
        previous_state = self._timeline_state_store().get(timeline_id)
        merged: dict[str, dict[str, float | str]] = {}
        if isinstance(previous_state, Mapping):
            for item in previous_state.get("parameters", []):
                if not isinstance(item, Mapping):
                    continue
                parameter_id = str(item.get("id") or "").strip()
                if not parameter_id:
                    continue
                merged[parameter_id] = {
                    "id": parameter_id,
                    "value": float(item.get("value", 0.0)),
                    "weight": float(item.get("weight", 1.0)),
                }
        for item in parameters:
            parameter_id = str(item.get("id") or "").strip()
            if not parameter_id:
                continue
            merged[parameter_id] = {
                "id": parameter_id,
                "value": float(item.get("value", 0.0)),
                "weight": float(item.get("weight", 1.0)),
            }
        return list(merged.values())

    def _build_eased_timeline_keyframes(
        self,
        *,
        timeline_id: str,
        parameters: list[dict[str, float | str]],
        offset_ms: int,
        duration_ms: int,
        purpose: str,
        start_offset_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        if not parameters:
            return []
        previous_state = self._timeline_state_store().get(timeline_id)
        current_frame = self._build_timeline_keyframe(
            offset_ms=offset_ms,
            parameters=parameters,
            duration_ms=duration_ms,
            purpose=purpose,
            keyframe=True,
        )
        if not isinstance(previous_state, Mapping):
            return [current_frame]
        previous_offset_ms = max(
            0,
            int(start_offset_ms if start_offset_ms is not None else previous_state.get("offset_ms") or 0),
        )
        if offset_ms <= previous_offset_ms:
            return [current_frame]
        previous_parameters = self._parameter_state_map(previous_state.get("parameters", []))
        current_parameters = self._parameter_state_map(parameters)
        if not current_parameters:
            return [current_frame]
        interval_ms = self._resolve_transition_sample_interval_ms()
        span_ms = max(1, offset_ms - previous_offset_ms)
        keyframes: list[dict[str, Any]] = []
        for sample_offset_ms in range(previous_offset_ms + interval_ms, offset_ms, interval_ms):
            progress = (sample_offset_ms - previous_offset_ms) / span_ms
            factor = _ease_in_out_cubic(progress)
            keyframes.append(
                self._build_timeline_keyframe(
                    offset_ms=sample_offset_ms,
                    parameters=self._interpolate_parameter_state(previous_parameters, current_parameters, factor),
                    duration_ms=0,
                    purpose=purpose,
                    keyframe=False,
                )
            )
        keyframes.append(current_frame)
        return keyframes

    def _resolve_realtime_timeline_window(
        self,
        *,
        timeline_id: str,
        offset_ms: int,
        duration_ms: int,
        motion_started_at_monotonic: float | None,
    ) -> tuple[int | None, int]:
        previous_state = self._timeline_state_store().get(timeline_id)
        previous_offset_ms = 0
        if isinstance(previous_state, Mapping):
            previous_offset_ms = max(0, int(previous_state.get("offset_ms") or 0))
        if motion_started_at_monotonic is None:
            return previous_offset_ms or None, offset_ms
        elapsed_ms = max(0, int((time.monotonic() - motion_started_at_monotonic) * 1000.0))
        lead_ms = max(SOULLINK_REALTIME_TRANSITION_LEAD_MS, self._resolve_transition_sample_interval_ms() * 2)
        if offset_ms >= elapsed_ms + lead_ms:
            return previous_offset_ms or None, offset_ms
        start_offset_ms = max(previous_offset_ms, elapsed_ms + lead_ms)
        effective_duration_ms = max(1, int(duration_ms))
        end_offset_ms = max(offset_ms, start_offset_ms + effective_duration_ms)
        return start_offset_ms, end_offset_ms

    @staticmethod
    def _parameter_state_map(raw_parameters: Any) -> dict[str, dict[str, float | str]]:
        result: dict[str, dict[str, float | str]] = {}
        if not isinstance(raw_parameters, list):
            return result
        for item in raw_parameters:
            if not isinstance(item, Mapping):
                continue
            parameter_id = str(item.get("id") or "").strip()
            if not parameter_id:
                continue
            result[parameter_id] = {
                "id": parameter_id,
                "value": float(item.get("value", 0.0)),
                "weight": float(item.get("weight", 1.0)),
            }
        return result

    @staticmethod
    def _interpolate_parameter_state(
        previous_parameters: Mapping[str, Mapping[str, float | str]],
        current_parameters: Mapping[str, Mapping[str, float | str]],
        factor: float,
    ) -> list[dict[str, float | str]]:
        interpolated: list[dict[str, float | str]] = []
        for parameter_id, current in current_parameters.items():
            previous = previous_parameters.get(parameter_id, current)
            start_value = float(previous.get("value", current.get("value", 0.0)))
            end_value = float(current.get("value", start_value))
            start_weight = float(previous.get("weight", current.get("weight", 1.0)))
            end_weight = float(current.get("weight", start_weight))
            interpolated.append(
                {
                    "id": parameter_id,
                    "value": start_value + (end_value - start_value) * factor,
                    "weight": start_weight + (end_weight - start_weight) * factor,
                }
            )
        return interpolated

    @staticmethod
    def _build_timeline_keyframe(
        *,
        offset_ms: int,
        parameters: list[dict[str, float | str]],
        duration_ms: int,
        purpose: str,
        keyframe: bool,
    ) -> dict[str, Any]:
        return {
            "offset_ms": max(0, int(offset_ms)),
            "parameters": parameters,
            "duration_ms": max(0, int(duration_ms)),
            "easing": "easeInOutCubic",
            "blend": "replace",
            "priority": 5,
            "purpose": purpose,
            "keyframe": bool(keyframe),
        }

    def _resolve_transition_sample_interval_ms(self) -> int:
        raw_value = getattr(self.config, "transition_sample_interval_ms", SOULLINK_TRANSITION_SAMPLE_INTERVAL_MS)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = SOULLINK_TRANSITION_SAMPLE_INTERVAL_MS
        return max(8, min(100, value))

    def _resolve_motion_prefetch_frames(self, *, total_frames: int) -> int:
        raw_value = getattr(self.config, "tts_motion_prefetch_frames", SOULLINK_MOTION_PREFETCH_FRAMES)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = SOULLINK_MOTION_PREFETCH_FRAMES
        return max(1, min(int(total_frames), max(1, value)))

    @staticmethod
    def _should_retry_generator_exception(exc: Exception) -> bool:
        message = str(exc or "")
        if "无法解析 LLM 返回的 JSON" in message:
            return True
        return isinstance(exc, UnicodeEncodeError)

    async def _run_generator_call(self, operation: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, SOULLINK_GENERATOR_RETRY_ATTEMPTS + 1):
            try:
                with _suppress_generator_console_output():
                    return await operation()
            except Exception as exc:
                last_exc = exc
                if attempt >= SOULLINK_GENERATOR_RETRY_ATTEMPTS or not self._should_retry_generator_exception(exc):
                    raise
                self._log_warning(f"SoulLink generator retry {attempt}/{SOULLINK_GENERATOR_RETRY_ATTEMPTS - 1}: {exc}")
                await asyncio.sleep(0.05)
        if last_exc is not None:
            raise last_exc

    @staticmethod
    def _resolve_total_motion_duration_ms(
        *,
        text: str,
        audio_timeline: Mapping[str, Any] | None,
        fallback_duration_ms: int,
    ) -> int:
        duration_ms = 0
        if isinstance(audio_timeline, Mapping):
            try:
                duration_ms = max(0, int(audio_timeline.get("audio_duration_ms") or 0))
            except (TypeError, ValueError):
                duration_ms = 0
        if duration_ms <= 0:
            duration_ms = max(0, int(fallback_duration_ms or 0))
        if duration_ms <= 0:
            duration_ms = max(1000, int(math.ceil(len(str(text or "").strip()) * 160)))
        return duration_ms

    @staticmethod
    def _build_motion_frame_offsets_ms(*, total_frames: int, total_duration_ms: int) -> list[int]:
        resolved_total_frames = max(1, int(total_frames))
        resolved_total_duration_ms = max(0, int(total_duration_ms))
        if resolved_total_frames == 1:
            return [max(0, resolved_total_duration_ms)]
        frame_stride_ms = max(1, int(math.ceil(resolved_total_duration_ms / resolved_total_frames)))
        offsets: list[int] = []
        for frame_index in range(resolved_total_frames):
            target_offset_ms = min(
                resolved_total_duration_ms,
                frame_stride_ms * (frame_index + 1),
            )
            offsets.append(max(0, int(target_offset_ms)))
        if offsets:
            offsets[-1] = max(offsets[-1], resolved_total_duration_ms)
        return offsets

    def _timeline_state_store(self) -> dict[str, dict[str, Any]]:
        store = self.__dict__.get("_timeline_states")
        if not isinstance(store, dict):
            store = {}
            self.__dict__["_timeline_states"] = store
        return store

    @staticmethod
    def _resolve_total_frames(
        *,
        text: str,
        audio_timeline: Mapping[str, Any] | None,
        fallback_duration_ms: int,
        frame_duration_ms: int,
    ) -> int:
        duration_ms = SoulLinkLive2DController._resolve_total_motion_duration_ms(
            text=text,
            audio_timeline=audio_timeline,
            fallback_duration_ms=fallback_duration_ms,
        )
        return max(1, min(120, int(math.ceil(duration_ms / max(1, frame_duration_ms)))))

    def _safe_update_parameters(self, parameters: Mapping[str, dict[str, float | str]]) -> None:
        with _suppress_generator_console_output():
            self._generator.update_parameters(dict(parameters))

    async def _safe_generate(self, input_text: str, context: str = "") -> dict[str, Any]:
        return await self._run_generator_call(lambda: self._generator.generate(input_text, context))

    async def _safe_generate_motion_plan(self, *, speech_text: str, total_frames: int, context: str = "") -> list[dict[str, Any]]:
        return await self._run_generator_call(
            lambda: self._generator.generate_motion_plan(
                speech_text=speech_text,
                total_frames=total_frames,
                context=context,
            )
        )

    async def _safe_generate_tts_motion_frame_with_plan(
        self,
        *,
        frame_index: int,
        total_frames: int,
        frame_plan: Mapping[str, Any],
        context: str = "",
        frame_duration_ms: int = 1000,
    ) -> dict[str, Any]:
        return await self._run_generator_call(
            lambda: self._generator.generate_tts_motion_frame_with_plan(
                frame_index=frame_index,
                total_frames=total_frames,
                frame_plan=dict(frame_plan),
                context=context,
                frame_duration_ms=frame_duration_ms,
            )
        )

    def _log_info(self, message: str) -> None:
        if self.logger is not None and hasattr(self.logger, "info"):
            self.logger.info(message)

    def _log_warning(self, message: str) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(message)


def _ease_in_out_cubic(progress: float) -> float:
    clamped = max(0.0, min(1.0, float(progress)))
    if clamped < 0.5:
        return 4.0 * clamped * clamped * clamped
    shifted = (2.0 * clamped) - 2.0
    return 0.5 * shifted * shifted * shifted + 1.0

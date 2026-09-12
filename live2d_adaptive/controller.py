"""Adaptive Live2D controller with ordered delivery and synchronized speech timelines."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

import asyncio
import contextlib
from uuid import uuid4

from .bridge import Live2DBridgeProtocol
from .local_lipsync import PluginLocalLipSyncEngine
from .profile import ParameterProfile
from .semantic_mapper import Live2DSemanticMapper
from .speech_timeline import SpeechTimeline, SpeechTimelineBuilder

LIP_SYNC_ONLY_ROLES = frozenset({"mouth.open", "mouth.form"})
DEFAULT_IDLE_MOTION_MODEL = "hiyori"
DEFAULT_IDLE_MOTION_NAME = "m01"
DEFAULT_IDLE_MOTION_FILE = "m01.motion3.json"
SpeechEnvelopeSink = Callable[..., Awaitable[None] | None]
ParameterOutputModifier = Callable[[list[dict[str, float | str]], Mapping[str, Any]], list[Mapping[str, Any]] | None]


class Live2DController:
    """High-level Live2D controller used by the Bilibili live adapter."""

    def __init__(
        self,
        *,
        bridge: Live2DBridgeProtocol,
        profile: ParameterProfile,
        chars_per_second: float = 7.5,
        prepare_ms: int = 180,
        release_ms: int = 600,
        mouth_update_interval_ms: int = 40,
        mouth_closed_value: float = 0.0,
        mouth_open_threshold: float = 0.02,
        mouth_open_gamma: float = 0.65,
        mouth_open_gain: float = 2.9,
        mouth_open_max: float = 1.0,
        mouth_sync_mode: str = "hybrid",
        mouth_amplitude_mix: float = 0.80,
        mouth_viseme_lead_ms: int = 10,
        mouth_open_smoothing: float = 0.40,
        mouth_open_attack_smoothing: float | None = 0.06,
        mouth_open_release_smoothing: float | None = 0.45,
        mouth_open_min_delta: float = 0.015,
        mouth_form_smoothing: float = 0.18,
        mouth_form_min_delta: float = 0.015,
        mouth_keyframe_transition_ms: int = 85,
        mouth_vowel_shapes: Mapping[str, Any] | None = None,
        parameter_keepalive_ms: int = 650,
        lip_sync_only_mode: bool = False,
        idle_motion_enabled: bool = False,
        idle_motion_model: str = DEFAULT_IDLE_MOTION_MODEL,
        idle_motion_name: str = DEFAULT_IDLE_MOTION_NAME,
        idle_motion_file: str = DEFAULT_IDLE_MOTION_FILE,
        idle_motion_interval_ms: int = 9000,
        idle_sway_enabled: bool = True,
        idle_sway_interval_ms: int = 900,
        idle_sway_intensity: float = 0.25,
        speech_sway_enabled: bool = True,
        speech_sway_intensity: float = 0.45,
        speech_sway_update_interval_ms: int = 160,
        embodied_mode: bool = False,
        logger: Any = None,
    ) -> None:
        self.bridge = bridge
        self.profile = profile
        self.mapper = Live2DSemanticMapper(profile)
        self.lip_sync_only_mode = bool(lip_sync_only_mode)
        self.mouth_sync_mode = _normalize_runtime_mouth_sync_mode(mouth_sync_mode)
        self.use_vts_native_lip_sync = False
        self._embodied_mode = bool(embodied_mode)
        self._base_idle_motion_enabled = bool(idle_motion_enabled)
        self._base_idle_sway_enabled = bool(idle_sway_enabled)
        self._base_speech_sway_enabled = bool(speech_sway_enabled)
        self.idle_motion_enabled = bool(idle_motion_enabled)
        self.idle_motion_model = _normalize_motion_text(idle_motion_model, DEFAULT_IDLE_MOTION_MODEL)
        self.idle_motion_name = _normalize_motion_text(idle_motion_name, DEFAULT_IDLE_MOTION_NAME)
        self.idle_motion_file = _normalize_motion_file(idle_motion_file, self.idle_motion_name)
        self.idle_motion_interval_ms = max(120, int(idle_motion_interval_ms))
        self._timeline_builder_config = {
            "chars_per_second": chars_per_second,
            "prepare_ms": prepare_ms,
            "release_ms": release_ms,
            "speech_sway_update_interval_ms": speech_sway_update_interval_ms,
        }
        self.lip_sync_engine = PluginLocalLipSyncEngine(
            self.profile,
            update_interval_ms=mouth_update_interval_ms,
            mouth_closed_value=mouth_closed_value,
            mouth_open_threshold=mouth_open_threshold,
            mouth_open_gamma=mouth_open_gamma,
            mouth_open_gain=mouth_open_gain,
            mouth_open_max=mouth_open_max,
            mouth_amplitude_mix=mouth_amplitude_mix,
            mouth_viseme_lead_ms=mouth_viseme_lead_ms,
            mouth_open_attack_smoothing=(
                mouth_open_attack_smoothing if mouth_open_attack_smoothing is not None else mouth_open_smoothing
            ),
            mouth_open_release_smoothing=(
                mouth_open_release_smoothing if mouth_open_release_smoothing is not None else mouth_open_smoothing
            ),
            mouth_open_min_delta=mouth_open_min_delta,
            mouth_form_smoothing=mouth_form_smoothing,
            mouth_form_min_delta=mouth_form_min_delta,
            mouth_keyframe_transition_ms=mouth_keyframe_transition_ms,
        )
        self.parameter_keepalive_ms = max(100, int(parameter_keepalive_ms))
        self.idle_sway_interval_ms = max(120, int(idle_sway_interval_ms))
        self.idle_sway_intensity = min(1.0, max(0.0, float(idle_sway_intensity)))
        self.speech_sway_intensity = min(1.0, max(0.0, float(speech_sway_intensity)))
        self.idle_sway_enabled = False
        self.timeline_builder = None
        self.logger = logger
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._idle_motion_task: asyncio.Task[None] | None = None
        self._idle_sway_task: asyncio.Task[None] | None = None
        self._speaking_release_task: asyncio.Task[None] | None = None
        self._speech_envelope_task: asyncio.Task[None] | None = None
        self._speech_envelope_tasks: set[asyncio.Task[None]] = set()
        self._speech_envelope_sink: SpeechEnvelopeSink | None = None
        self._parameter_modifiers: list[ParameterOutputModifier] = []
        self._speaking = False
        self._active_timeline_id = ""
        self._idle_phase = 0.0
        self._sync_runtime_mode()

    @property
    def is_speaking(self) -> bool:
        """Return whether a synchronized reply timeline is active."""

        return self._speaking

    @property
    def active_timeline_id(self) -> str:
        """Return the current active speech timeline id."""

        return self._active_timeline_id

    @property
    def embodied_mode_enabled(self) -> bool:
        return self._embodied_mode

    def _sync_runtime_mode(self) -> None:
        self.idle_motion_enabled = self._base_idle_motion_enabled and not self._embodied_mode
        speech_sway_enabled = (
            self._base_speech_sway_enabled and not self.lip_sync_only_mode and not self.idle_motion_enabled and not self._embodied_mode
        )
        self.idle_sway_enabled = (
            self._base_idle_sway_enabled and not self.lip_sync_only_mode and not self.idle_motion_enabled and not self._embodied_mode
        )
        self.timeline_builder = SpeechTimelineBuilder(
            self.mapper,
            chars_per_second=self._timeline_builder_config["chars_per_second"],
            prepare_ms=self._timeline_builder_config["prepare_ms"],
            release_ms=self._timeline_builder_config["release_ms"],
            mouth_sync_mode="vts_native",
            speech_sway_enabled=speech_sway_enabled,
            speech_sway_intensity=self.speech_sway_intensity,
            speech_sway_update_interval_ms=self._timeline_builder_config["speech_sway_update_interval_ms"],
        )

    async def set_embodied_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._embodied_mode:
            return
        previous_idle_motion = self.idle_motion_enabled
        previous_idle_sway = self.idle_sway_enabled
        self._embodied_mode = enabled
        self._sync_runtime_mode()
        if previous_idle_motion and not self.idle_motion_enabled:
            await self._cancel_idle_motion_task()
        elif not previous_idle_motion and self.idle_motion_enabled and self._worker is not None:
            await self._ensure_idle_motion_started()
        if previous_idle_sway and not self.idle_sway_enabled:
            await self._cancel_idle_sway_task()
        elif not previous_idle_sway and self.idle_sway_enabled and self._worker is not None:
            await self._ensure_idle_sway_started()

    async def start(self) -> None:
        """Start bridge and ordered delivery worker."""

        await self.bridge.start()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop(), name="live2d_adaptive.controller")
        await self._ensure_idle_motion_started()
        await self._ensure_idle_sway_started()
        await self._queue_idle_mouth_pose()

    async def stop(self) -> None:
        """Stop worker and bridge."""

        await self._cancel_idle_motion_task()
        await self._cancel_idle_sway_task()
        await self._queue.put(None)
        worker = self._worker
        self._worker = None
        if worker is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        if self._speaking_release_task is not None:
            self._speaking_release_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._speaking_release_task
        self._speaking_release_task = None
        speech_envelope_tasks = list(self._speech_envelope_tasks)
        self._speech_envelope_tasks.clear()
        if self._speech_envelope_task is not None and self._speech_envelope_task not in speech_envelope_tasks:
            speech_envelope_tasks.append(self._speech_envelope_task)
        for speech_task in speech_envelope_tasks:
            speech_task.cancel()
        for speech_task in speech_envelope_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await speech_task
        self._speech_envelope_task = None
        await self.bridge.stop()
        self._speaking = False
        self._active_timeline_id = ""

    def set_speech_envelope_sink(self, sink: SpeechEnvelopeSink | None) -> None:
        self._speech_envelope_sink = sink

    def register_parameter_modifier(self, modifier: ParameterOutputModifier) -> None:
        if modifier not in self._parameter_modifiers:
            self._parameter_modifiers.append(modifier)

    def unregister_parameter_modifier(self, modifier: ParameterOutputModifier) -> None:
        with contextlib.suppress(ValueError):
            self._parameter_modifiers.remove(modifier)

    async def send_parameters(
        self,
        parameters: list[Mapping[str, Any]],
        *,
        duration_ms: int = 300,
        easing: str = "easeOutQuad",
        blend: str = "replace",
        priority: int = 5,
        timeline_id: str = "",
        purpose: str = "",
    ) -> dict[str, Any]:
        """Queue a raw parameter batch after validation and clamping."""

        normalized_parameters = self._normalize_parameters(parameters)
        if not normalized_parameters:
            return {"success": False, "error": "no valid Live2D parameters"}
        event = {
            "type": "live2d.parameters",
            "timeline_id": timeline_id or uuid4().hex,
            "parameters": normalized_parameters,
            "duration_ms": max(0, int(duration_ms)),
            "easing": _normalize_easing(easing),
            "blend": _normalize_blend(blend),
            "priority": int(priority),
        }
        normalized_purpose = str(purpose or "").strip()
        if normalized_purpose:
            event["purpose"] = normalized_purpose
        await self._queue_parameter_with_keepalive(event)
        return {"success": True, "parameters": normalized_parameters, "event": event}

    async def send_timeline_clip(
        self,
        keyframes: list[Mapping[str, Any]],
        *,
        timeline_id: str = "",
        easing: str = "easeInOut",
        blend: str = "replace",
        priority: int = 4,
        purpose: str = "",
    ) -> dict[str, Any]:
        """Queue a short Live2D keyframe clip for bridge-side interpolation."""

        clip_id = timeline_id or uuid4().hex
        queued_events: list[dict[str, Any]] = []
        sample_parameters: list[dict[str, float | str]] = []
        ordered_frames = sorted(
            (frame for frame in keyframes if isinstance(frame, Mapping)),
            key=lambda item: max(0, int(item.get("offset_ms") or 0)),
        )
        for raw_frame in ordered_frames:
            raw_parameters = raw_frame.get("parameters")
            if not isinstance(raw_parameters, list):
                continue
            normalized_parameters = self._normalize_parameters(raw_parameters)
            if not normalized_parameters:
                continue
            event = {
                "type": "live2d.timeline.frame",
                "timeline_id": clip_id,
                "offset_ms": max(0, int(raw_frame.get("offset_ms") or 0)),
                "parameters": normalized_parameters,
                "easing": _normalize_easing(str(raw_frame.get("easing") or easing)),
                "blend": _normalize_blend(str(raw_frame.get("blend") or blend)),
                "priority": int(raw_frame.get("priority") or priority),
            }
            frame_purpose = str(raw_frame.get("purpose") or purpose or "").strip()
            if frame_purpose:
                event["purpose"] = frame_purpose
            sanitized_event = self._sanitize_bridge_event(event)
            if sanitized_event is None:
                continue
            await self._queue.put(sanitized_event)
            queued_events.append(sanitized_event)
            if not sample_parameters:
                sample_parameters = list(sanitized_event.get("parameters") or [])
        if not queued_events:
            return {"success": False, "error": "no valid Live2D timeline frames"}
        return {
            "success": True,
            "timeline_id": clip_id,
            "parameters": sample_parameters,
            "events": queued_events,
        }

    async def play_reply(
        self,
        text: str,
        *,
        audio_timeline: Mapping[str, Any] | None = None,
        emotion_intent: str = "",
        motion_intensity: float | None = None,
        timeline_prepare_ms: int | None = None,
        suppress_lipsync: bool = False,
        suppress_expression_overlay: bool = False,
    ) -> SpeechTimeline:
        """Queue a reply timeline synchronized with text or future TTS metadata."""

        speech_emotion_intent = str(emotion_intent or "").strip()
        if self._embodied_mode:
            emotion_intent = ""
        resolved_motion_intensity = self._resolve_speech_motion_intensity(
            text,
            emotion_intent=speech_emotion_intent,
            motion_intensity=motion_intensity,
        )
        timeline = self.timeline_builder.build_reply_timeline(
            text,
            audio_timeline=audio_timeline,
            motion_intensity=resolved_motion_intensity,
            prepare_ms_override=timeline_prepare_ms,
        )
        prepare_ms = _timeline_prepare_offset(timeline)
        lipsync_frames = (
            []
            if suppress_lipsync
            else self.lip_sync_engine.build_timeline_frames(
                timeline_id=timeline.timeline_id,
                text=text,
                duration_ms=timeline.estimated_duration_ms,
                audio_timeline=audio_timeline,
                prepare_ms=prepare_ms,
            )
        )
        self._speaking = True
        self._active_timeline_id = timeline.timeline_id
        self._schedule_speaking_release(timeline)
        self._dispatch_speech_envelope(
            text=text,
            audio_timeline=audio_timeline,
            emotion_intent=speech_emotion_intent,
            emotion_gain=resolved_motion_intensity,
            timeline_id=timeline.timeline_id,
        )
        if (
            emotion_intent
            and not suppress_expression_overlay
            and not self.lip_sync_only_mode
            and not self.idle_motion_enabled
        ):
            emotion_parameters = self._filter_non_mouth_parameters(
                self.mapper.build_intent(emotion_intent, intensity=0.55, target={"text": text})
            )
            if emotion_parameters:
                await self._queue_parameter_with_keepalive(
                    {
                        "type": "live2d.parameters",
                        "timeline_id": timeline.timeline_id,
                        "parameters": emotion_parameters,
                        "duration_ms": 240,
                        "easing": "easeOutQuad",
                        "blend": "replace",
                        "priority": 4,
                    }
                )
        ordered_events = [
            *[dict(frame) for frame in lipsync_frames],
            *[
                event.to_bridge_payload()
                for event in timeline.events
                if not self._should_skip_timeline_event_in_embodied_mode(event.type)
            ],
        ]
        ordered_events.sort(key=_bridge_event_order)
        for event in ordered_events:
            sanitized_event = self._sanitize_bridge_event(event)
            if sanitized_event is not None:
                await self._queue.put(sanitized_event)
        return timeline

    async def _ensure_idle_motion_started(self) -> None:
        if not self.idle_motion_enabled:
            return
        await self._queue_idle_motion()
        if self._idle_motion_task is None or self._idle_motion_task.done():
            self._idle_motion_task = asyncio.create_task(
                self._idle_motion_loop(),
                name="live2d_adaptive.idle_motion",
            )

    async def _ensure_idle_sway_started(self) -> None:
        if not self.idle_sway_enabled:
            return
        if self._idle_sway_task is None or self._idle_sway_task.done():
            self._idle_sway_task = asyncio.create_task(
                self._idle_sway_loop(),
                name="live2d_adaptive.idle_sway",
            )

    async def _cancel_idle_motion_task(self) -> None:
        if self._idle_motion_task is not None:
            self._idle_motion_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_motion_task
        self._idle_motion_task = None

    async def _cancel_idle_sway_task(self) -> None:
        if self._idle_sway_task is not None:
            self._idle_sway_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_sway_task
        self._idle_sway_task = None

    async def _idle_motion_loop(self) -> None:
        interval_sec = self.idle_motion_interval_ms / 1000.0
        while True:
            await asyncio.sleep(interval_sec)
            await self._queue_idle_motion()

    async def _queue_idle_motion(self) -> None:
        await self._queue.put(
            {
                "type": "live2d.motion",
                "timeline_id": "idle-motion",
                "model": self.idle_motion_model,
                "motion": self.idle_motion_name,
                "motion_file": self.idle_motion_file,
                "loop": True,
                "priority": 1,
                "purpose": "idle",
            }
        )

    async def _idle_sway_loop(self) -> None:
        interval_sec = self.idle_sway_interval_ms / 1000.0
        while True:
            await asyncio.sleep(interval_sec)
            if self._speaking:
                continue
            parameters = self.mapper.build_sway(
                self._idle_phase,
                intensity=self.idle_sway_intensity,
                speaking=False,
            )
            self._idle_phase = (self._idle_phase + 0.58) % 6.283185307179586
            normalized_parameters = self._normalize_parameters(parameters)
            if not normalized_parameters:
                continue
            await self._queue_parameter_with_keepalive(
                {
                    "type": "live2d.parameters",
                    "timeline_id": "idle-sway",
                    "parameters": normalized_parameters,
                    "duration_ms": self.idle_sway_interval_ms + 180,
                    "easing": "easeInOut",
                    "blend": "replace",
                    "priority": 1,
                    "idle": True,
                }
            )

    async def _queue_idle_mouth_pose(self) -> None:
        if self._speaking or not self._can_drive_idle_mouth():
            return
        parameters = self.lip_sync_engine.build_idle_parameters()
        normalized_parameters = self._normalize_parameters(parameters)
        if not normalized_parameters:
            return
        sanitized_event = self._sanitize_bridge_event(
            {
                "type": "live2d.timeline.frame",
                "timeline_id": "idle-mouth",
                "offset_ms": 0,
                "parameters": normalized_parameters,
                "easing": "easeOutQuad",
                "blend": "replace",
                "priority": 2,
                "purpose": "idle-mouth",
                "idle": True,
            }
        )
        if sanitized_event is not None:
            await self._queue.put(sanitized_event)

    async def _worker_loop(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                break
            try:
                sanitized_event = self._prepare_bridge_event(event, apply_modifiers=True)
                if sanitized_event is None:
                    continue
                response = await self.bridge.send_event(sanitized_event)
                self._handle_bridge_response(sanitized_event, response)
            except Exception as exc:
                self._log_warning(f"Live2D bridge event failed: {exc}")
            finally:
                self._queue.task_done()

    async def _queue_parameter_with_keepalive(self, event: dict[str, Any]) -> None:
        sanitized_event = self._sanitize_bridge_event(event)
        if sanitized_event is None:
            return
        await self._queue.put(sanitized_event)
        duration_ms = int(sanitized_event.get("duration_ms") or 0)
        if duration_ms <= self.parameter_keepalive_ms:
            return
        offset = self.parameter_keepalive_ms
        while offset < duration_ms:
            keepalive_event = dict(sanitized_event)
            keepalive_event["offset_ms"] = offset
            keepalive_event["keepalive"] = True
            keepalive_event["duration_ms"] = min(self.parameter_keepalive_ms, duration_ms - offset)
            await self._queue.put(keepalive_event)
            offset += self.parameter_keepalive_ms

    def _schedule_speaking_release(self, timeline: SpeechTimeline) -> None:
        if self._speaking_release_task is not None:
            self._speaking_release_task.cancel()
        self._speaking_release_task = asyncio.create_task(
            self._release_speaking_after(timeline.timeline_id, timeline.estimated_duration_ms + timeline.release_ms),
            name="live2d_adaptive.speaking_release",
        )

    async def _release_speaking_after(self, timeline_id: str, delay_ms: int) -> None:
        await asyncio.sleep(max(0.0, delay_ms / 1000.0))
        if self._active_timeline_id == timeline_id:
            self._speaking = False
            self._active_timeline_id = ""
            await self._queue_idle_mouth_pose()

    def _dispatch_speech_envelope(
        self,
        *,
        text: str,
        audio_timeline: Mapping[str, Any] | None,
        emotion_intent: str,
        emotion_gain: float,
        timeline_id: str,
    ) -> None:
        if self._speech_envelope_sink is None or not isinstance(audio_timeline, Mapping):
            return
        amplitudes = audio_timeline.get("amplitudes")
        if not isinstance(amplitudes, list) or not amplitudes:
            return
        task = asyncio.create_task(
            self._run_speech_envelope_sink(
                text=text,
                audio_timeline=audio_timeline,
                emotion_intent=emotion_intent,
                emotion_gain=emotion_gain,
                timeline_id=timeline_id,
            ),
            name="live2d_adaptive.speech_envelope",
        )
        self._speech_envelope_task = task
        self._speech_envelope_tasks.add(task)
        task.add_done_callback(self._discard_speech_envelope_task)

    async def _run_speech_envelope_sink(
        self,
        *,
        text: str,
        audio_timeline: Mapping[str, Any],
        emotion_intent: str,
        emotion_gain: float,
        timeline_id: str,
    ) -> None:
        sink = self._speech_envelope_sink
        if sink is None:
            return
        try:
            result = sink(
                text=text,
                audio_timeline=audio_timeline,
                emotion_intent=emotion_intent,
                emotion_gain=emotion_gain,
                timeline_id=timeline_id,
            )
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_warning(f"Live2D speech envelope sink failed: {exc}")

    def _resolve_speech_motion_intensity(
        self,
        text: str,
        *,
        emotion_intent: str,
        motion_intensity: float | None,
    ) -> float:
        base = self.speech_sway_intensity if motion_intensity is None else float(motion_intensity)
        strong_marks = {"!", "?", "\uff01", "\uff1f"}
        punctuation_boost = min(0.24, sum(1 for char in text if char in strong_marks) * 0.06)
        emotion_boosts = {
            "react_happy": 0.10,
            "react_surprised": 0.24,
            "react_confused": 0.12,
            "react_emphasis": 0.18,
            "react_shy": 0.04,
        }
        emotion_boost = emotion_boosts.get(str(emotion_intent or "").strip(), 0.0)
        return min(1.0, max(0.0, base + punctuation_boost + emotion_boost))

    def _normalize_parameters(self, parameters: list[Mapping[str, Any]]) -> list[dict[str, float | str]]:
        normalized: list[dict[str, float | str]] = []
        for raw_parameter in parameters:
            if not isinstance(raw_parameter, Mapping):
                continue
            parameter_id = str(raw_parameter.get("id") or "").strip()
            spec = self.profile.parameters.get(parameter_id)
            if spec is None or not spec.enabled or not self._is_parameter_allowed(spec.role):
                continue
            try:
                value = float(raw_parameter.get("value"))
            except (TypeError, ValueError):
                continue
            weight = raw_parameter.get("weight", 0.7)
            try:
                normalized_weight = min(1.0, max(0.0, float(weight)))
            except (TypeError, ValueError):
                normalized_weight = 0.7
            normalized.append(
                {
                    "id": parameter_id,
                    "value": spec.clamp(value),
                    "weight": normalized_weight,
                }
            )
        return normalized

    def _sanitize_bridge_event(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        return self._prepare_bridge_event(event, apply_modifiers=False)

    def _prepare_bridge_event(self, event: Mapping[str, Any], *, apply_modifiers: bool) -> dict[str, Any] | None:
        event_type = str(event.get("type") or "").strip()
        if event_type not in {"live2d.parameters", "live2d.timeline.frame"}:
            return dict(event)
        raw_parameters = event.get("parameters")
        if not isinstance(raw_parameters, list):
            return None
        normalized_parameters = self._normalize_parameters(raw_parameters)
        if not normalized_parameters:
            return None
        if apply_modifiers and self._parameter_modifiers:
            normalized_parameters = self._apply_parameter_modifiers(normalized_parameters, event)
            if not normalized_parameters:
                return None
            normalized_parameters = self._normalize_parameters(normalized_parameters)
            if not normalized_parameters:
                return None
        sanitized_event = dict(event)
        sanitized_event["parameters"] = normalized_parameters
        return sanitized_event

    def _apply_parameter_modifiers(
        self,
        parameters: list[dict[str, float | str]],
        event: Mapping[str, Any],
    ) -> list[dict[str, float | str]]:
        modified: list[dict[str, float | str]] = [dict(parameter) for parameter in parameters]
        for modifier in list(self._parameter_modifiers):
            try:
                result = modifier([dict(parameter) for parameter in modified], event)
            except Exception as exc:
                self._log_warning(f"Live2D parameter modifier failed: {exc}")
                continue
            if result is None:
                continue
            modified = [dict(parameter) for parameter in result if isinstance(parameter, Mapping)]
        return modified

    def _is_parameter_allowed(self, role: str) -> bool:
        normalized_role = str(role or "").strip()
        if not self.lip_sync_only_mode:
            return True
        return normalized_role in LIP_SYNC_ONLY_ROLES

    def _can_drive_idle_mouth(self) -> bool:
        return bool(self.lip_sync_engine.build_idle_parameters())

    def _should_skip_timeline_event_in_embodied_mode(self, event_type: str) -> bool:
        if not self._embodied_mode:
            return False
        normalized_type = str(event_type or "").strip()
        return normalized_type in {"live2d.parameters", "live2d.timeline.frame"}

    def _filter_non_mouth_parameters(self, parameters: list[Mapping[str, Any]]) -> list[dict[str, float | str]]:
        filtered: list[dict[str, float | str]] = []
        for raw_parameter in parameters:
            if not isinstance(raw_parameter, Mapping):
                continue
            parameter_id = str(raw_parameter.get("id") or "").strip()
            if not parameter_id:
                continue
            spec = self.profile.parameters.get(parameter_id)
            if spec is not None and str(spec.role or "").startswith("mouth."):
                continue
            try:
                value = float(raw_parameter.get("value"))
            except (TypeError, ValueError):
                continue
            try:
                weight = min(1.0, max(0.0, float(raw_parameter.get("weight", 0.7))))
            except (TypeError, ValueError):
                weight = 0.7
            filtered.append({"id": parameter_id, "value": value, "weight": weight})
        return filtered

    def _handle_bridge_response(self, event: Mapping[str, Any], response: Any) -> None:
        if not isinstance(response, Mapping):
            return
        error = str(response.get("error") or response.get("code") or "").lower()
        if "unknown_parameter" not in error and "out_of_range" not in error:
            return
        for raw_parameter in event.get("parameters", []):
            if isinstance(raw_parameter, Mapping):
                parameter_id = str(raw_parameter.get("id") or "").strip()
                if parameter_id:
                    self.profile.mark_failed(parameter_id)

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)

    def _discard_speech_envelope_task(self, task: asyncio.Task[None]) -> None:
        self._speech_envelope_tasks.discard(task)
        if self._speech_envelope_task is task:
            self._speech_envelope_task = None


def _normalize_easing(value: str) -> str:
    normalized = str(value or "").strip()
    allowed = {"linear", "easeIn", "easeOut", "easeInOut", "easeOutQuad"}
    return normalized if normalized in allowed else "linear"


def _normalize_blend(value: str) -> str:
    normalized = str(value or "").strip()
    allowed = {"replace", "additive", "multiply"}
    return normalized if normalized in allowed else "replace"


def _normalize_motion_text(value: str, fallback: str) -> str:
    normalized = str(value or "").strip()
    return normalized or fallback


def _normalize_motion_file(value: str, motion_name: str) -> str:
    normalized = _normalize_motion_text(value, "")
    if normalized:
        return normalized
    return f"{motion_name}.motion3.json" if motion_name else DEFAULT_IDLE_MOTION_FILE


def _normalize_runtime_mouth_sync_mode(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"plugin_local", "plugin", "local", "local_clip", "timeline", "clip"}:
        return "plugin_local"
    return "plugin_local"


def _timeline_prepare_offset(timeline: SpeechTimeline) -> int:
    for event in timeline.events:
        if event.type == "bot_reply.start":
            return max(0, int(event.offset_ms))
    return 0


def _bridge_event_order(event: Mapping[str, Any]) -> tuple[int, int]:
    event_type = str(event.get("type") or "").strip()
    offset_ms = max(0, int(event.get("offset_ms") or 0))
    priority = 1 if event_type == "bot_reply.prepare" else 2 if event_type == "bot_reply.start" else 3
    return (offset_ms, priority)

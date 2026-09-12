"""Embodied Live2D state subscription and parameter driving."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

import asyncio
import contextlib
import json
import math
import random
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

try:
    from aiohttp import ClientSession, ClientTimeout, WSMsgType

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from .controller import Live2DController
from .mouse_follow import GlobalMouseFollowRuntime, MouseFollowSnapshot

AVATAR_STATE_DOMAIN = "avatar_state"
AVATAR_STATE_TOPIC = "main"
_LOW_PASS_SECONDS = 0.12
_MOUSE_FOLLOW_SMOOTHING_FALLBACK_STEP_SECONDS = 1.0 / 60.0
_CLIP_FRAME_INTERVAL_MS = 16
_EXPRESSIVE_CLIP_FRAME_COUNT = 12
_IDLE_CLIP_FRAME_COUNT = 20
_SCHEDULER_TICK_SECONDS = 0.016
_SPEECH_RESAMPLE_INTERVAL_MS = 16
_EYE_OVERLAY_FRAME_INTERVAL_MS = 16
_REPLY_EMOTION_HOLD_SECONDS = 2.4
_REPLY_EMOTION_FADE_SECONDS = 1.2
_MAX_REPLY_EMOTION_GAIN = 1.5
_SPEECH_TO_IDLE_GRACE_SECONDS = 0.28
_SPECIAL_MOVE_FRAME_INTERVAL_MS = 16
_SPECIAL_MOVE_AHOGE_SWAY_CYCLE_SECONDS = 0.18
_SPECIAL_MOVE_AHOGE_SWAY_PEAK_RATIO = 1.0
_SPECIAL_MOVE_NAME_ALIASES: dict[str, str] = {
    "ahoge spin": "ahoge_spin",
}
_SPECIAL_MOVE_HAIR_COMPANIONS: tuple[tuple[str, float, float], ...] = (
    ("ParamHairFront", 0.95, 0.12),
    ("ParamHairBack", -0.90, 0.44),
    ("ParamRibbon", 1.00, 0.20),
    ("ParamSideupRibbon", -1.00, 0.58),
)
_REPLY_EMOTION_BY_INTENT = {
    "react_happy": (0.72, 0.28),
    "react_surprised": (0.34, 0.68),
    "react_shy": (0.26, 0.24),
    "react_confused": (-0.16, 0.34),
    "react_emphasis": (0.24, 0.46),
    "react_sad": (-0.56, 0.18),
    "react_angry": (-0.74, 0.66),
}
_REPLY_EMOTION_TEXT_HINTS = (
    ("react_happy", ("happy", "amazing", "great", "nice", "love", "lol", "haha", "哈哈", "开心", "太好", "太棒", "好耶")),
    ("react_surprised", ("wow", "surprise", "surprised", "really", "unbelievable", "哇", "惊讶", "震惊", "真的吗")),
    ("react_shy", ("shy", "blush", "sorry", "害羞", "脸红", "不好意思")),
    ("react_angry", ("angry", "mad", "furious", "生气", "气死")),
    ("react_sad", ("sad", "sorry", "cry", "upset", "难过", "伤心", "哭")),
    ("react_confused", ("confused", "why", "how", "what", "为什么", "怎么", "啥", "?", "？")),
)

DisableCallback = Callable[[str], Awaitable[None] | None]
SubscriberConnectedCallback = Callable[[], Awaitable[None] | None]
SubscriberDisconnectedCallback = Callable[[str], Awaitable[None] | None]


def normalize_special_move_name(move: str) -> str:
    normalized = " ".join(
        str(move or "").strip().lower().replace("_", " ").replace("-", " ").split()
    )
    if not normalized:
        return ""
    return _SPECIAL_MOVE_NAME_ALIASES.get(normalized, normalized.replace(" ", "_"))


@dataclass(slots=True)
class EmbodiedStateSnapshot:
    session_id: str
    ts: float
    seq: int
    agent_state: str
    valence: float
    arousal: float
    attention: float
    cognitive_load: float
    confidence: float
    social_approach: float
    energy: float
    gaze_x: float
    gaze_y: float

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "EmbodiedStateSnapshot":
        return cls(
            session_id=str(payload.get("session_id") or "").strip(),
            ts=float(payload.get("ts") or 0.0),
            seq=int(payload.get("seq") or 0),
            agent_state=str(payload.get("agent_state") or "stop"),
            valence=float(payload.get("valence") or 0.0),
            arousal=float(payload.get("arousal") or 0.0),
            attention=float(payload.get("attention") or 0.0),
            cognitive_load=float(payload.get("cognitive_load") or 0.0),
            confidence=float(payload.get("confidence") or 0.0),
            social_approach=float(payload.get("social_approach") or 0.0),
            energy=float(payload.get("energy") or 0.0),
            gaze_x=float(payload.get("gaze_x") or 0.0),
            gaze_y=float(payload.get("gaze_y") or 0.0),
        )


@dataclass(slots=True)
class EmbodiedMotionClip:
    purpose: str
    frames: list[dict[str, Any]]
    end_targets: dict[str, float]
    duration_sec: float


@dataclass(slots=True)
class BlinkTiming:
    enabled: bool
    interval_min_sec: float
    interval_max_sec: float
    double_blink_chance: float
    close_sec: float
    hold_sec: float
    open_sec: float
    double_gap_sec: float


@dataclass(slots=True)
class WinkTiming:
    enabled: bool
    close_sec: float
    hold_sec: float
    open_sec: float
    cooldown_sec: float
    non_target_eye_drop: float


class EmbodiedParamDriver:
    """Project continuous avatar_state snapshots onto model parameters."""

    def __init__(
        self,
        *,
        controller: Live2DController,
        logger: Any = None,
        fallback_to_legacy: bool = True,
        on_disable: DisableCallback | None = None,
        stale_after_sec: float = 1.2,
        mouse_follow_enabled: bool = False,
        mouse_follow_smoothing_ms: int = 45,
        mouse_follow_return_after_sec: float = 1.2,
        mouse_follow_cooldown_sec: float = 0.45,
        mouse_follow_eye_gain: float = 0.55,
        mouse_follow_head_gain: float = 0.28,
        mouse_follow_body_gain: float = 0.12,
        blink_enabled: bool = False,
        blink_interval_min_sec: float = 2.8,
        blink_interval_max_sec: float = 5.5,
        blink_double_blink_chance: float = 0.03,
        blink_close_ms: int = 60,
        blink_hold_ms: int = 28,
        blink_open_ms: int = 110,
        blink_double_blink_gap_ms: int = 140,
        wink_enabled: bool = False,
        wink_close_ms: int = 65,
        wink_hold_ms: int = 90,
        wink_open_ms: int = 110,
        wink_request_cooldown_sec: float = 1.8,
        wink_non_target_eye_drop: float = 0.08,
    ) -> None:
        self.controller = controller
        self.logger = logger
        self.fallback_to_legacy = bool(fallback_to_legacy)
        self.on_disable = on_disable
        self.stale_after_sec = max(0.2, float(stale_after_sec))
        self._active = True
        self._consecutive_errors = 0
        self._last_snapshot_monotonic = 0.0
        self._started_monotonic = 0.0
        self._received_snapshot = False
        self._last_smoothing_monotonic = 0.0
        self._smoothed_values: dict[str, float] = {}
        self._watchdog_task: asyncio.Task[None] | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._eye_overlay_task: asyncio.Task[None] | None = None
        self._eye_overlay_wake = asyncio.Event()
        self._snapshot_log_count = 0
        self._last_snapshot_log_monotonic = 0.0
        self._dispatch_lock = asyncio.Lock()
        self._motion_queue: deque[EmbodiedMotionClip] = deque()
        self._clip_busy_until = 0.0
        self.mouse_follow_enabled = bool(mouse_follow_enabled)
        self._latest_mouse_snapshot = MouseFollowSnapshot()
        self._mouse_follow_smoothing_sec = max(0.0, float(mouse_follow_smoothing_ms) / 1000.0)
        self._mouse_follow_return_after_sec = max(0.0, float(mouse_follow_return_after_sec))
        self._mouse_follow_cooldown_sec = max(0.0, float(mouse_follow_cooldown_sec))
        self._mouse_follow_eye_gain = max(0.0, float(mouse_follow_eye_gain))
        self._mouse_follow_head_gain = max(0.0, float(mouse_follow_head_gain))
        self._mouse_follow_body_gain = max(0.0, float(mouse_follow_body_gain))
        self._mouse_follow_lease_until = 0.0
        self._mouse_follow_smoothed_x = 0.0
        self._mouse_follow_smoothed_y = 0.0
        self._mouse_follow_last_update_monotonic = 0.0
        self._latest_snapshot: EmbodiedStateSnapshot | None = None
        self._latest_targets: dict[str, float] = {}
        self._playback_anchor_targets: dict[str, float] = {}
        self._speech_transition_hold_until = 0.0
        self._reply_emotion_valence = 0.0
        self._reply_emotion_arousal = 0.0
        self._reply_emotion_until = 0.0
        self._rng = random.Random(0xEBD0)
        self._idle_motion_cursor = 0.0
        self._stale_warning_emitted = False
        self._idle_phase_offsets = {
            "yaw_primary": self._rng.uniform(0.0, math.tau),
            "yaw_secondary": self._rng.uniform(0.0, math.tau),
            "pitch_primary": self._rng.uniform(0.0, math.tau),
            "pitch_secondary": self._rng.uniform(0.0, math.tau),
            "roll_primary": self._rng.uniform(0.0, math.tau),
            "roll_secondary": self._rng.uniform(0.0, math.tau),
            "amplitude": self._rng.uniform(0.0, math.tau),
            "shoulder": self._rng.uniform(0.0, math.tau),
            "breath": self._rng.uniform(0.0, math.tau),
        }
        self._idle_follow_offsets = {
            "yaw_follow": self._rng.uniform(0.0, math.tau),
            "pitch_follow": self._rng.uniform(0.0, math.tau),
            "roll_follow": self._rng.uniform(0.0, math.tau),
            "shoulder_follow": self._rng.uniform(0.0, math.tau),
        }
        self._mouse_follow_modifier_registered = False
        self._eye_overlay_modifier_registered = False
        self._blink_timing = BlinkTiming(
            enabled=bool(blink_enabled),
            interval_min_sec=max(0.1, float(blink_interval_min_sec)),
            interval_max_sec=max(0.1, float(blink_interval_max_sec)),
            double_blink_chance=_clamp_unit(blink_double_blink_chance),
            close_sec=max(0.001, float(blink_close_ms) / 1000.0),
            hold_sec=max(0.001, float(blink_hold_ms) / 1000.0),
            open_sec=max(0.001, float(blink_open_ms) / 1000.0),
            double_gap_sec=max(0.001, float(blink_double_blink_gap_ms) / 1000.0),
        )
        self._wink_timing = WinkTiming(
            enabled=bool(wink_enabled),
            close_sec=max(0.001, float(wink_close_ms) / 1000.0),
            hold_sec=max(0.001, float(wink_hold_ms) / 1000.0),
            open_sec=max(0.001, float(wink_open_ms) / 1000.0),
            cooldown_sec=max(0.0, float(wink_request_cooldown_sec)),
            non_target_eye_drop=_clamp_unit(wink_non_target_eye_drop),
        )
        if self._blink_timing.interval_max_sec < self._blink_timing.interval_min_sec:
            self._blink_timing.interval_max_sec = self._blink_timing.interval_min_sec
        self._blink_active = False
        self._blink_started_at_monotonic = 0.0
        self._blink_next_at_monotonic = 0.0
        self._blink_is_double = False
        self._wink_active = False
        self._wink_side = "right"
        self._wink_started_at_monotonic = 0.0
        self._wink_last_accepted_at = -1e9
        self._last_wink_side = "left"
        self._eye_open_base_raw: dict[str, float] = {}
        self._eye_open_ceiling_raw: dict[str, float] = {}
        self._special_move_active = False
        self._special_move_name = ""
        self._special_move_until = 0.0

    @property
    def active(self) -> bool:
        return self._active

    async def start(self) -> None:
        self._active = True
        self._reset_mouse_follow_smoothing()
        if not self._mouse_follow_modifier_registered:
            self.controller.register_parameter_modifier(self._apply_mouse_follow_output_modifier)
            self._mouse_follow_modifier_registered = True
        self._started_monotonic = time.monotonic()
        self._last_snapshot_monotonic = 0.0
        self._received_snapshot = False
        self._stale_warning_emitted = False
        self._clip_busy_until = 0.0
        self._idle_motion_cursor = 0.0
        self._speech_transition_hold_until = 0.0
        self._motion_queue.clear()
        self._reset_special_move_state()
        self._reset_eye_overlay_state(schedule_next=True, now=self._started_monotonic)
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(
                self._watchdog_loop(),
                name="live2d_adaptive.embodied_watchdog",
            )
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(
                self._motion_scheduler_loop(),
                name="live2d_adaptive.embodied_motion_scheduler",
            )
        if self._eye_overlay_enabled() and (self._eye_overlay_task is None or self._eye_overlay_task.done()):
            self._eye_overlay_task = asyncio.create_task(
                self._eye_overlay_loop(),
                name="live2d_adaptive.embodied_eye_overlay",
            )

    async def stop(self) -> None:
        self._motion_queue.clear()
        self._clip_busy_until = 0.0
        self._speech_transition_hold_until = 0.0
        self._reset_mouse_follow_smoothing()
        self._reset_special_move_state()
        self._reset_eye_overlay_state(schedule_next=False)
        if self._mouse_follow_modifier_registered:
            self.controller.unregister_parameter_modifier(self._apply_mouse_follow_output_modifier)
            self._mouse_follow_modifier_registered = False
        self._eye_overlay_wake.set()
        eye_overlay = self._eye_overlay_task
        self._eye_overlay_task = None
        if eye_overlay is not None:
            eye_overlay.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await eye_overlay
        scheduler = self._scheduler_task
        self._scheduler_task = None
        if scheduler is not None:
            scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler
        watchdog = self._watchdog_task
        self._watchdog_task = None
        if watchdog is not None:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog

    def set_mouse_follow_enabled(self, enabled: bool) -> None:
        self.mouse_follow_enabled = bool(enabled)
        if not self.mouse_follow_enabled:
            self._mouse_follow_lease_until = 0.0
            self._reset_mouse_follow_smoothing()

    def update_mouse_follow_snapshot(self, snapshot: MouseFollowSnapshot) -> None:
        now = time.monotonic()
        self._latest_mouse_snapshot = MouseFollowSnapshot(
            x_norm=_clamp_signed(snapshot.x_norm),
            y_norm=_clamp_signed(snapshot.y_norm),
            active=bool(snapshot.active),
            activity_ts=float(snapshot.activity_ts),
        )
        if self.mouse_follow_enabled and self._latest_mouse_snapshot.active:
            self._mouse_follow_lease_until = now + self._mouse_follow_return_after_sec
        self._update_mouse_follow_smoothing(now=now)

    def debug_latest_targets(self) -> dict[str, float]:
        return dict(self._latest_targets)

    def debug_latest_snapshot(self) -> EmbodiedStateSnapshot | None:
        return self._latest_snapshot

    def _reset_special_move_state(self) -> None:
        self._special_move_active = False
        self._special_move_name = ""
        self._special_move_until = 0.0

    async def debug_apply_emotion_preview(
        self,
        *,
        emotion_intent: str,
        emotion_gain: float = 1.0,
    ) -> dict[str, Any]:
        if not self._active:
            return {"success": False, "reason": "driver_disabled"}
        now = time.monotonic()
        normalized_intent = str(emotion_intent or "").strip().lower()
        if normalized_intent in {"", "neutral", "none", "clear"}:
            self._reply_emotion_valence = 0.0
            self._reply_emotion_arousal = 0.0
            self._reply_emotion_until = 0.0
        else:
            self._remember_reply_emotion(
                emotion_intent=normalized_intent,
                text="",
                emotion_gain=emotion_gain,
                now=now,
            )
        return await self.handle_snapshot(self._latest_snapshot or _build_debug_snapshot())

    async def handle_snapshot(self, snapshot: EmbodiedStateSnapshot) -> dict[str, Any]:
        if not self._active:
            return {"success": False, "reason": "driver_disabled"}
        if self._started_monotonic <= 0.0:
            await self.start()

        now = time.monotonic()
        self._last_snapshot_monotonic = now
        self._received_snapshot = True
        self._stale_warning_emitted = False
        self._latest_snapshot = snapshot
        semantic_targets = self._build_semantic_targets(snapshot, now=now)
        smoothed_targets = self._smooth_targets(semantic_targets, now=now)
        self._latest_targets = dict(smoothed_targets)
        if self._eye_overlay_enabled():
            self._eye_overlay_wake.set()
        parameters = self._resolve_parameters(smoothed_targets)
        self._log_snapshot(snapshot, parameters)
        if not parameters:
            return await self._handle_failure("no_valid_parameters")

        clip = self._build_expressive_clip(snapshot, current_targets=smoothed_targets, now=now)
        if clip is None:
            return await self._handle_failure("no_valid_parameters")
        async with self._dispatch_lock:
            dispatch_now = (not self._speech_transition_active(now)) and now >= self._clip_busy_until and not self._motion_queue
            if dispatch_now:
                result = await self._dispatch_clip(clip, now=now)
                if not bool(result.get("success")):
                    return result
                return {
                    "success": True,
                    "parameters": list(result.get("parameters") or parameters),
                    "timeline_id": result.get("timeline_id") or "",
                    "frame_count": len(clip.frames),
                    "purpose": "expressive",
                }
            self._enqueue_motion_clip(clip)
        return {
            "success": True,
            "queued": True,
            "parameters": parameters,
            "frame_count": len(clip.frames),
            "purpose": "expressive",
        }

    async def handle_speech_envelope(
        self,
        audio_timeline: Mapping[str, Any] | None,
        *,
        snapshot: EmbodiedStateSnapshot | None = None,
        emotion_gain: float = 1.0,
        text: str = "",
        timeline_id: str = "",
        emotion_intent: str = "",
    ) -> dict[str, Any]:
        if not self._active:
            return {"success": False, "reason": "driver_disabled"}
        if not isinstance(audio_timeline, Mapping):
            return {"success": False, "reason": "no_audio_timeline"}
        if self._started_monotonic <= 0.0:
            await self.start()
        active_snapshot = snapshot or self._latest_snapshot
        if active_snapshot is None:
            return {"success": False, "reason": "no_snapshot"}

        now = time.monotonic()
        self._remember_reply_emotion(
            emotion_intent=emotion_intent,
            text=text,
            emotion_gain=emotion_gain,
            now=now,
        )
        semantic_targets = self._build_semantic_targets(active_snapshot, now=now)
        smoothed_targets = self._smooth_targets(semantic_targets, now=now)
        clip = self._build_speech_clip(
            audio_timeline,
            snapshot=active_snapshot,
            base_targets=smoothed_targets,
            emotion_gain=emotion_gain,
            timeline_id=timeline_id,
        )
        if clip is None:
            return {"success": False, "reason": "no_speech_motion"}
        self._speech_transition_hold_until = max(
            self._speech_transition_hold_until,
            now + clip.duration_sec + _SPEECH_TO_IDLE_GRACE_SECONDS,
        )

        async with self._dispatch_lock:
            dispatch_now = now >= self._clip_busy_until and not self._motion_queue
            if dispatch_now:
                result = await self._dispatch_clip(clip, now=now)
                if not bool(result.get("success")):
                    return result
                self._latest_targets = dict(clip.end_targets)
                return {
                    "success": True,
                    "parameters": list(result.get("parameters") or []),
                    "frame_count": len(clip.frames),
                    "purpose": "speech",
                    "timeline_id": result.get("timeline_id") or "",
                    "end_targets": dict(clip.end_targets),
                }
            self._enqueue_motion_clip(clip)
        self._latest_targets = dict(clip.end_targets)
        return {
            "success": True,
            "queued": True,
            "parameters": list(clip.frames[-1].get("parameters") or []),
            "frame_count": len(clip.frames),
            "purpose": "speech",
            "timeline_id": timeline_id,
            "end_targets": dict(clip.end_targets),
        }

    async def handle_reset(self) -> dict[str, Any]:
        neutral = self._resolve_neutral_parameters()
        if not neutral:
            return {"success": False, "reason": "no_valid_parameters"}
        self._smoothed_values = {}
        self._latest_snapshot = None
        self._latest_targets = {}
        self._playback_anchor_targets = {}
        self._speech_transition_hold_until = 0.0
        self._reply_emotion_valence = 0.0
        self._reply_emotion_arousal = 0.0
        self._reply_emotion_until = 0.0
        self._last_smoothing_monotonic = 0.0
        self._reset_mouse_follow_smoothing()
        self._last_snapshot_monotonic = time.monotonic()
        self._received_snapshot = False
        self._stale_warning_emitted = False
        self._clip_busy_until = 0.0
        self._idle_motion_cursor = 0.0
        self._motion_queue.clear()
        self._reset_eye_overlay_state(schedule_next=True, now=self._last_snapshot_monotonic)
        self._log_info("Live2D embodied reset -> %d neutral parameters", len(neutral))
        result = await self.controller.send_parameters(
            neutral,
            duration_ms=300,
            easing="easeOutQuad",
            blend="replace",
            priority=4,
            purpose="reset",
        )
        return {
            "success": bool(result.get("success")),
            "parameters": list(result.get("parameters") or neutral),
        }

    def _log_snapshot(self, snapshot: EmbodiedStateSnapshot, parameters: list[dict[str, float | str]]) -> None:
        now = time.monotonic()
        self._snapshot_log_count += 1
        if self._snapshot_log_count <= 3 or now - self._last_snapshot_log_monotonic >= 2.0:
            sample_ids = ", ".join(str(item.get("id") or "") for item in parameters[:6])
            self._log_info(
                "Live2D embodied snapshot seq=%s state=%s attention=%.2f load=%.2f valence=%.2f -> %d params [%s]",
                snapshot.seq,
                snapshot.agent_state,
                snapshot.attention,
                snapshot.cognitive_load,
                snapshot.valence,
                len(parameters),
                sample_ids,
            )
            self._last_snapshot_log_monotonic = now

    _reply_emotion_text_hints: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "react_happy",
            (
                "happy",
                "amazing",
                "great",
                "nice",
                "love",
                "lol",
                "haha",
                "\u54c8\u54c8",
                "\u5f00\u5fc3",
                "\u592a\u597d",
                "\u592a\u68d2",
                "\u597d\u8036",
                "\u559c\u6b22",
                "鍝堝搱",
                "寮€蹇?",
                "澶ソ",
                "澶",
                "濂借€?",
            ),
        ),
        (
            "react_surprised",
            (
                "wow",
                "surprise",
                "surprised",
                "really",
                "unbelievable",
                "\u54c7",
                "\u60ca\u8bb6",
                "\u9707\u60ca",
                "\u771f\u7684\u5417",
                "\u592a\u79bb\u8c31",
                "鍝?",
                "鎯婅",
                "闇囨儕",
                "鐪熺殑鍚?",
            ),
        ),
        (
            "react_shy",
            (
                "shy",
                "blush",
                "\u5bb3\u7f9e",
                "\u8138\u7ea2",
                "\u817c\u7ea2",
                "\u4e0d\u597d\u610f\u601d",
                "瀹崇緸",
                "鑴哥孩",
                "涓嶅ソ鎰忔€?",
            ),
        ),
        (
            "react_angry",
            (
                "angry",
                "mad",
                "furious",
                "\u751f\u6c14",
                "\u6c14\u6b7b",
                "\u6c14\u70b8",
                "\u706b\u5927",
                "鐢熸皵",
                "姘旀",
            ),
        ),
        (
            "react_sad",
            (
                "sad",
                "cry",
                "upset",
                "\u96be\u8fc7",
                "\u4f24\u5fc3",
                "\u60b2\u4f24",
                "\u60f3\u54ed",
                "闅捐繃",
                "浼ゅ績",
                "鍝?",
            ),
        ),
        (
            "react_confused",
            (
                "confused",
                "why",
                "how",
                "what",
                "\u4e3a\u4ec0\u4e48",
                "\u600e\u4e48",
                "\u600e\u4e48\u56de\u4e8b",
                "\u5565",
                "?",
                "\uff1f",
                "涓轰粈涔?",
                "鎬庝箞",
                "鍟?",
                "锛?",
            ),
        ),
    )

    def _remember_reply_emotion(
        self,
        *,
        emotion_intent: str,
        text: str,
        emotion_gain: float,
        now: float,
    ) -> None:
        intent = str(emotion_intent or "").strip()
        if not intent:
            intent = self._infer_reply_emotion_intent(text)
        base = _REPLY_EMOTION_BY_INTENT.get(intent)
        if base is None:
            return
        normalized_gain = min(_MAX_REPLY_EMOTION_GAIN, max(0.0, float(emotion_gain)))
        gain = 0.85 + (0.70 * normalized_gain)
        self._reply_emotion_valence = _clamp_signed(base[0] * gain)
        self._reply_emotion_arousal = _clamp_unit(base[1] * gain)
        self._reply_emotion_until = now + _REPLY_EMOTION_HOLD_SECONDS

    def _infer_reply_emotion_intent(self, text: str) -> str:
        lowered = str(text or "").lower()
        if not lowered:
            return ""
        for intent, hints in self._reply_emotion_text_hints:
            if any(hint in lowered for hint in hints):
                return intent
        strong_marks = lowered.count("!") + lowered.count("\uff01")
        if strong_marks > 0:
            return "react_emphasis"
        return ""

    def _reply_emotion_overlay(self, now: float) -> tuple[float, float]:
        if self._reply_emotion_until <= 0.0 or now >= (self._reply_emotion_until + _REPLY_EMOTION_FADE_SECONDS):
            return 0.0, 0.0
        if now <= self._reply_emotion_until:
            scale = 1.0
        else:
            scale = 1.0 - ((now - self._reply_emotion_until) / _REPLY_EMOTION_FADE_SECONDS)
        scale = _clamp_unit(scale)
        return self._reply_emotion_valence * scale, self._reply_emotion_arousal * scale

    def _build_semantic_targets(self, snapshot: EmbodiedStateSnapshot, *, now: float) -> dict[str, float]:
        overlay_valence, overlay_arousal = self._reply_emotion_overlay(now)
        valence = _clamp_signed(snapshot.valence + overlay_valence)
        arousal = _clamp_unit(snapshot.arousal + overlay_arousal)
        positive_social = max(snapshot.social_approach, 0.0)
        face_positive_baseline = _clamp_unit(0.18 + (0.18 * snapshot.confidence) + (0.10 * positive_social))
        face_valence = valence if valence < -0.05 else max(valence, face_positive_baseline)
        emotion_drive = _clamp_unit((0.90 * abs(valence)) + (0.62 * arousal) + (0.30 * positive_social))
        face_gain = 1.55 + (0.70 * emotion_drive)
        pose_gain = 1.40 + (0.62 * emotion_drive)
        activity_gain = {
            "running": 0.98,
            "wait": 0.82,
            "stop": 0.56,
        }.get(str(snapshot.agent_state or "").strip().lower(), 0.72)
        engagement = _clamp_unit((0.42 * snapshot.attention) + (0.36 * snapshot.energy) + (0.22 * snapshot.confidence))
        breath_wave = math.sin(2.0 * math.pi * 0.22 * now)
        lively_wave = math.sin(2.0 * math.pi * 0.44 * now)
        head_drift = math.sin(2.0 * math.pi * 0.12 * now)
        torso_wave = math.sin(2.0 * math.pi * 0.16 * now + 0.85)
        torso_roll_wave = math.sin(2.0 * math.pi * 0.11 * now + 1.7)

        head_yaw = (
            (1.12 * snapshot.gaze_x)
            + pose_gain
            * (
                (0.52 * snapshot.social_approach)
                + (0.28 * head_drift * (0.44 + (0.92 * engagement)) * activity_gain)
            )
        )
        head_pitch = (
            pose_gain
            * (
                (-0.48 * snapshot.cognitive_load)
                + (0.38 * snapshot.confidence)
                + (0.22 * (snapshot.attention - 0.5))
                + (0.18 * torso_wave * activity_gain)
            )
        )
        head_roll = (
            pose_gain
            * (
                (0.36 * valence)
                + (0.42 * snapshot.social_approach)
                + (0.24 * torso_roll_wave * (0.34 + (0.92 * engagement)) * activity_gain)
            )
        )
        positive_valence = max(face_valence, 0.0)
        negative_valence = max(-face_valence, 0.0)
        expressive_positive_valence = max(positive_valence - 0.18, 0.0)
        expressive_negative_valence = max(negative_valence - 0.04, 0.0)
        positive_brow_shape = expressive_positive_valence * (1.05 + (0.35 * snapshot.confidence))
        surprise_brow_gain = _clamp_unit(
            max(arousal - 0.38, 0.0)
            * (0.65 + (0.35 * snapshot.attention))
            * max(0.0, 1.0 - (1.35 * negative_valence))
        )
        focus_load = _clamp_unit(snapshot.cognitive_load) * (1.0 - (0.55 * positive_valence))
        brow_up = (
            0.12
            + face_gain
            * (
                (0.24 * positive_valence)
                + (0.12 * snapshot.confidence)
                + (0.22 * surprise_brow_gain)
            )
            - ((0.05 + (0.03 * emotion_drive)) * focus_load)
        )
        brow_down = (face_gain * (0.26 * expressive_negative_valence)) + ((0.05 + (0.03 * emotion_drive)) * focus_load)
        breath = (0.14 + (0.14 * breath_wave)) + (0.26 * snapshot.energy)
        eye_smile = 0.08 + face_gain * (
            (0.44 * positive_valence) + (0.12 * snapshot.confidence) + (0.10 * positive_social)
        )
        blush = 0.03 + face_gain * (
            (0.32 * positive_valence) + (0.12 * arousal) + (0.08 * positive_social)
        )
        brow_angle = (
            face_gain
            * (
                (0.26 * positive_brow_shape)
                + (0.14 * surprise_brow_gain)
                - (0.56 * expressive_negative_valence)
                + (0.02 * snapshot.confidence)
                - (0.02 * snapshot.cognitive_load)
            )
        )
        brow_form = (
            face_gain
            * (
                (0.18 * positive_brow_shape)
                + (0.10 * surprise_brow_gain)
                - (0.48 * expressive_negative_valence)
                + (0.04 * positive_social)
                - (0.02 * snapshot.cognitive_load)
            )
        )
        body_pitch = (
            pose_gain
            * (
                (0.56 * snapshot.social_approach)
                + (0.34 * snapshot.confidence)
                + (0.30 * snapshot.energy)
                - (0.16 * snapshot.cognitive_load)
                + (0.28 * torso_wave * (0.42 + (0.92 * engagement)) * activity_gain)
            )
        )
        body_roll = (
            pose_gain
            * (
                (0.34 * valence)
                + (0.28 * snapshot.social_approach)
                + (0.34 * torso_roll_wave * (0.34 + (0.88 * engagement)) * activity_gain)
            )
        )
        body_yaw = (
            (1.18 * head_yaw)
            + pose_gain * (0.42 * head_drift * (0.34 + (0.98 * engagement)) * activity_gain)
        )
        shoulder = (
            pose_gain
            * (
                (0.62 * snapshot.social_approach)
                + (0.48 * breath_wave * (0.56 + (0.78 * snapshot.energy)))
                + (0.28 * torso_roll_wave * activity_gain)
                - (0.12 * snapshot.cognitive_load)
            )
        )
        ahoge = lively_wave * (0.12 + (0.24 * snapshot.arousal))
        targets = {
            "eye.gaze.x": _clamp_signed(1.00 * snapshot.gaze_x),
            "eye.gaze.y": _clamp_signed(1.00 * snapshot.gaze_y),
            "head.yaw": _clamp_signed(head_yaw),
            "head.pitch": _clamp_signed(head_pitch),
            "head.roll": _clamp_signed(head_roll),
            "body.yaw": _clamp_signed(body_yaw),
            "body.pitch": _clamp_signed(body_pitch),
            "body.roll": _clamp_signed(body_roll),
            "brow.left.y": _clamp_signed(brow_up - brow_down),
            "brow.right.y": _clamp_signed(brow_up - brow_down),
            "brow.left.angle": _clamp_signed(brow_angle),
            "brow.right.angle": _clamp_signed(brow_angle),
            "brow.left.form": _clamp_signed(brow_form),
            "brow.right.form": _clamp_signed(brow_form),
            "eye.left.smile": _clamp_unit(eye_smile),
            "eye.right.smile": _clamp_unit(eye_smile),
            "eye.open": _clamp_unit(
                0.82
                - (0.05 * snapshot.cognitive_load)
                - (0.05 * arousal)
                + (0.14 * positive_valence)
                + (0.04 * snapshot.confidence)
            ),
            "face.blush": _clamp_unit(blush),
            "breath": _clamp_unit(breath),
            "body.shoulder": _clamp_signed(shoulder),
            "accessory.ahoge": _clamp_signed(ahoge),
        }
        return targets

    def _smooth_targets(self, targets: Mapping[str, float], *, now: float) -> dict[str, float]:
        if not self._smoothed_values:
            self._smoothed_values = dict(targets)
            self._last_smoothing_monotonic = now
            return dict(self._smoothed_values)
        dt = max(0.0, now - self._last_smoothing_monotonic)
        self._last_smoothing_monotonic = now
        alpha = 1.0 if dt <= 0 else min(1.0, 1.0 - math.exp(-dt / _LOW_PASS_SECONDS))
        for key, value in targets.items():
            current = self._smoothed_values.get(key, value)
            self._smoothed_values[key] = current + ((float(value) - current) * alpha)
        return dict(self._smoothed_values)

    def _resolve_parameters(
        self,
        semantic_values: Mapping[str, float],
        *,
        remember_eye_base: bool = True,
    ) -> list[dict[str, float | str]]:
        parameters: list[dict[str, float | str]] = []
        for role in (
            "eye.gaze.x",
            "eye.gaze.y",
            "head.yaw",
            "head.pitch",
            "head.roll",
            "body.yaw",
            "body.pitch",
            "body.roll",
            "brow.left.y",
            "brow.right.y",
            "brow.left.angle",
            "brow.right.angle",
            "brow.left.form",
            "brow.right.form",
            "eye.left.smile",
            "eye.right.smile",
            "face.blush",
            "breath",
            "body.shoulder",
            "accessory.ahoge",
        ):
            resolved = self.controller.profile.resolve(role, semantic_values.get(role, 0.0), weight=0.75)
            if resolved is not None:
                parameters.append(resolved)

        left_eye_value = float(semantic_values.get("eye.left.open", semantic_values.get("eye.open", 0.75)))
        right_eye_value = float(semantic_values.get("eye.right.open", semantic_values.get("eye.open", 0.75)))
        left_eye = self.controller.profile.resolve("eye.left.open", left_eye_value, weight=0.70)
        right_eye = self.controller.profile.resolve("eye.right.open", right_eye_value, weight=0.70)
        if left_eye is not None:
            left_eye = self._clamp_resolved_eye_open_parameter("eye.left.open", left_eye)
        if right_eye is not None:
            right_eye = self._clamp_resolved_eye_open_parameter("eye.right.open", right_eye)
        if left_eye is not None:
            parameters.append(left_eye)
        if right_eye is not None:
            parameters.append(right_eye)
        if left_eye is None and right_eye is None:
            fallback_eye_value = float(semantic_values.get("eye.open", (left_eye_value + right_eye_value) * 0.5))
            resolved_eye = self.controller.profile.resolve("eye.open", fallback_eye_value, weight=0.70)
            if resolved_eye is not None:
                resolved_eye = self._clamp_resolved_eye_open_parameter("eye.open", resolved_eye)
                parameters.append(resolved_eye)
        if remember_eye_base:
            self._remember_eye_open_base(parameters)
        return parameters

    def _clamp_resolved_eye_open_parameter(
        self,
        role: str,
        parameter: dict[str, float | str],
    ) -> dict[str, float | str]:
        if not self._use_default_eye_open_ceiling():
            return parameter
        spec = self.controller.profile.find_by_role(role)
        if spec is None:
            return parameter
        try:
            value = float(parameter.get("value"))
        except (TypeError, ValueError):
            return parameter
        ceiling = spec.clamp(spec.default)
        if value <= ceiling:
            return parameter
        updated = dict(parameter)
        updated["value"] = ceiling
        return updated

    def _use_default_eye_open_ceiling(self) -> bool:
        model_identity = f"{self.controller.profile.model_id} {self.controller.profile.model_name}".lower()
        return "hiyori" in model_identity

    def _resolve_speech_parameters(self, speech_values: Mapping[str, float]) -> list[dict[str, float | str]]:
        parameters: list[dict[str, float | str]] = []
        for role in (
            "head.yaw",
            "head.pitch",
            "head.roll",
            "body.yaw",
            "body.pitch",
            "body.shoulder",
            "brow.left.y",
            "brow.right.y",
            "brow.left.angle",
            "brow.right.angle",
            "brow.left.form",
            "brow.right.form",
            "eye.left.smile",
            "eye.right.smile",
            "face.blush",
        ):
            resolved = self.controller.profile.resolve(role, speech_values.get(role, 0.0), weight=0.78)
            if resolved is not None:
                parameters.append(resolved)
        return parameters

    def _resolve_neutral_parameters(self) -> list[dict[str, float | str]]:
        parameters: list[dict[str, float | str]] = []
        for role, weight in (
            ("eye.gaze.x", 0.75),
            ("eye.gaze.y", 0.75),
            ("head.yaw", 0.75),
            ("head.pitch", 0.75),
            ("head.roll", 0.75),
            ("body.yaw", 0.75),
            ("body.pitch", 0.75),
            ("body.roll", 0.75),
            ("brow.left.y", 0.75),
            ("brow.right.y", 0.75),
            ("brow.left.angle", 0.75),
            ("brow.right.angle", 0.75),
            ("brow.left.form", 0.75),
            ("brow.right.form", 0.75),
            ("eye.left.smile", 0.75),
            ("eye.right.smile", 0.75),
            ("face.blush", 0.75),
            ("breath", 0.75),
            ("body.shoulder", 0.75),
            ("accessory.ahoge", 0.75),
        ):
            default_parameter = self._build_default_parameter(role, weight=weight)
            if default_parameter is not None:
                parameters.append(default_parameter)

        left_eye = self._build_default_parameter("eye.left.open", weight=0.70)
        right_eye = self._build_default_parameter("eye.right.open", weight=0.70)
        if left_eye is not None:
            parameters.append(left_eye)
        if right_eye is not None:
            parameters.append(right_eye)
        if left_eye is None and right_eye is None:
            fallback_eye = self._build_default_parameter("eye.open", weight=0.70)
            if fallback_eye is not None:
                parameters.append(fallback_eye)
        self._remember_eye_open_base(parameters)
        return parameters

    def request_wink(self, *, side: str = "", now: float | None = None) -> bool:
        if not self._wink_timing.enabled or not self._active:
            return False
        current_now = time.monotonic() if now is None else float(now)
        if self._wink_active:
            return False
        if current_now - self._wink_last_accepted_at < self._wink_timing.cooldown_sec:
            return False
        normalized_side = self._normalize_wink_side(side)
        self._wink_active = True
        self._wink_side = normalized_side
        self._wink_started_at_monotonic = current_now
        self._wink_last_accepted_at = current_now
        self._last_wink_side = normalized_side
        self._blink_active = False
        self._blink_is_double = False
        self._blink_next_at_monotonic = 0.0
        self._eye_overlay_wake.set()
        return True

    def request_special_move(
        self,
        *,
        action: str,
        move: str,
        duration_sec: float,
        now: float | None = None,
    ) -> bool:
        if not self._active:
            return False
        if str(action or "").strip() != "Special_move":
            return False
        normalized_move = normalize_special_move_name(move)
        if normalized_move != "ahoge_spin":
            return False
        if self.controller.profile.find_by_role("accessory.ahoge") is None:
            return False
        current_now = time.monotonic() if now is None else float(now)
        self._special_move_active = True
        self._special_move_name = normalized_move
        self._special_move_until = current_now + max(0.1, float(duration_sec))
        return True

    def _special_move_frame_offsets(self, *, duration_sec: float) -> list[int]:
        total_ms = max(
            _SPECIAL_MOVE_FRAME_INTERVAL_MS,
            int(math.ceil(max(0.0, float(duration_sec)) * 1000.0)),
        )
        offsets = list(range(0, total_ms + 1, _SPECIAL_MOVE_FRAME_INTERVAL_MS))
        if not offsets or offsets[-1] != total_ms:
            offsets.append(total_ms)
        return offsets

    def _ahoge_special_move_raw_value(self, *, t: float, parameter: Any) -> float:
        cycle = max(0.12, float(_SPECIAL_MOVE_AHOGE_SWAY_CYCLE_SECONDS))
        phase = (max(0.0, float(t)) % cycle) / cycle
        if phase < 0.16:
            normalized = _smoothstep_unit(phase / 0.16)
        elif phase < 0.30:
            normalized = 1.0
        elif phase < 0.62:
            blend = _smoothstep_unit((phase - 0.30) / 0.32)
            normalized = 1.0 - (2.0 * blend)
        elif phase < 0.78:
            normalized = -1.0
        else:
            blend = _smoothstep_unit((phase - 0.78) / 0.22)
            normalized = -1.0 + blend
        peak = min(
            max(abs(float(parameter.minimum)), abs(float(parameter.maximum))),
            max(abs(float(parameter.minimum)), abs(float(parameter.maximum))) * _SPECIAL_MOVE_AHOGE_SWAY_PEAK_RATIO,
        )
        return parameter.clamp(normalized * peak)

    def _special_move_parameter_value(self, *, t: float, parameter: Any, scale: float = 1.0, phase_offset: float = 0.0) -> float:
        cycle = max(0.12, float(_SPECIAL_MOVE_AHOGE_SWAY_CYCLE_SECONDS))
        normalized_t = max(0.0, float(t)) + (cycle * float(phase_offset))
        base_value = self._ahoge_special_move_raw_value(t=normalized_t, parameter=parameter)
        return parameter.clamp(base_value * float(scale))

    def _build_special_move_clip(self, *, now: float) -> EmbodiedMotionClip | None:
        if not self._special_move_active or self._special_move_name != "ahoge_spin":
            return None
        duration_sec = max(0.0, self._special_move_until - float(now))
        if duration_sec <= 0.0:
            self._reset_special_move_state()
            return None
        ahoge_parameter = self.controller.profile.find_by_role("accessory.ahoge")
        if ahoge_parameter is None:
            return None
        frames: list[dict[str, Any]] = []
        for offset_ms in self._special_move_frame_offsets(duration_sec=duration_sec):
            parameters = [
                {
                    "id": ahoge_parameter.id,
                    "value": self._ahoge_special_move_raw_value(
                        t=(offset_ms / 1000.0),
                        parameter=ahoge_parameter,
                    ),
                    "weight": 1.0,
                }
            ]
            for parameter_id, scale, phase_offset in _SPECIAL_MOVE_HAIR_COMPANIONS:
                companion = self.controller.profile.parameters.get(parameter_id)
                if companion is None:
                    continue
                parameters.append(
                    {
                        "id": companion.id,
                        "value": self._special_move_parameter_value(
                            t=(offset_ms / 1000.0),
                            parameter=companion,
                            scale=scale,
                            phase_offset=phase_offset,
                        ),
                        "weight": 1.0,
                    }
                )
            frames.append(
                {
                    "offset_ms": offset_ms,
                    "parameters": parameters,
                    "purpose": "special-move",
                }
            )
        if not frames:
            return None
        return EmbodiedMotionClip(
            purpose="special-move",
            frames=frames,
            end_targets={},
            duration_sec=duration_sec,
        )

    async def dispatch_special_move(self, *, now: float | None = None) -> dict[str, Any]:
        current_now = time.monotonic() if now is None else float(now)
        clip = self._build_special_move_clip(now=current_now)
        if clip is None:
            return {"success": False, "reason": "no_special_move_clip"}
        result = await self.controller.send_timeline_clip(
            clip.frames,
            easing="linear",
            blend="replace",
            priority=8,
            purpose=clip.purpose,
        )
        if not bool(result.get("success")):
            self._reset_special_move_state()
        return result

    def _reset_eye_overlay_state(self, *, schedule_next: bool, now: float | None = None) -> None:
        current_now = time.monotonic() if now is None else float(now)
        self._blink_active = False
        self._blink_started_at_monotonic = 0.0
        self._blink_is_double = False
        self._wink_active = False
        self._wink_started_at_monotonic = 0.0
        self._eye_open_base_raw = {}
        self._eye_open_ceiling_raw = {}
        if schedule_next and self._blink_timing.enabled:
            self._schedule_next_blink(now=current_now)
        else:
            self._blink_next_at_monotonic = 0.0
        self._eye_overlay_wake.set()

    def _schedule_next_blink(self, *, now: float) -> None:
        if not self._blink_timing.enabled:
            self._blink_next_at_monotonic = 0.0
            return
        interval = self._rng.uniform(self._blink_timing.interval_min_sec, self._blink_timing.interval_max_sec)
        if self._latest_snapshot is not None:
            load_factor = 1.0 - (0.18 * _clamp_unit(self._latest_snapshot.cognitive_load))
            interval *= max(0.55, load_factor)
        self._blink_next_at_monotonic = float(now) + max(0.4, interval)

    def _eye_overlay_enabled(self) -> bool:
        return self._blink_timing.enabled or self._wink_timing.enabled

    def _next_eye_overlay_delay(self, *, now: float) -> float | None:
        if self._wink_active:
            return 0.0
        if not self._blink_timing.enabled:
            return None
        if self._blink_next_at_monotonic <= 0.0:
            return 0.0
        return max(0.0, self._blink_next_at_monotonic - float(now))

    async def _eye_overlay_loop(self) -> None:
        while self._active and self._eye_overlay_enabled():
            try:
                now = time.monotonic()
                if self._wink_active:
                    clip = self._build_wink_eye_clip(now=max(now, self._wink_started_at_monotonic))
                    self._wink_active = False
                    self._wink_started_at_monotonic = 0.0
                    if clip is not None:
                        await self._dispatch_eye_overlay_clip(clip)
                        now = max(now, time.monotonic() + clip.duration_sec)
                    if self._blink_timing.enabled:
                        self._schedule_next_blink(now=now)
                    continue
                if self._blink_timing.enabled:
                    if self._blink_next_at_monotonic <= 0.0:
                        self._schedule_next_blink(now=now)
                    if now >= self._blink_next_at_monotonic > 0.0:
                        self._blink_active = True
                        self._blink_started_at_monotonic = now
                        self._blink_is_double = self._rng.random() < self._blink_timing.double_blink_chance
                        clip = self._build_blink_eye_clip(now=now, is_double=self._blink_is_double)
                        self._blink_active = False
                        self._blink_started_at_monotonic = 0.0
                        self._blink_is_double = False
                        if clip is not None:
                            await self._dispatch_eye_overlay_clip(clip)
                            now = max(now, time.monotonic() + clip.duration_sec)
                        self._schedule_next_blink(now=now)
                        continue
                timeout = self._next_eye_overlay_delay(now=now)
                if timeout is None:
                    await self._eye_overlay_wake.wait()
                    self._eye_overlay_wake.clear()
                    continue
                try:
                    await asyncio.wait_for(self._eye_overlay_wake.wait(), timeout=max(0.001, timeout))
                    self._eye_overlay_wake.clear()
                except asyncio.TimeoutError:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_warning("Live2D eye overlay loop failed: %s", exc)

    def _normalize_wink_side(self, raw_side: str) -> str:
        normalized = str(raw_side or "").strip().lower()
        if normalized in {"left", "l", "\u5de6", "\u5de6\u773c"}:
            return "left"
        if normalized in {"right", "r", "\u53f3", "\u53f3\u773c"}:
            return "right"
        return "right" if self._last_wink_side != "right" else "left"

    def _remember_eye_open_base(
        self,
        parameters: list[dict[str, float | str]],
        *,
        update_ceiling: bool = True,
    ) -> None:
        left_spec = self.controller.profile.find_by_role("eye.left.open")
        right_spec = self.controller.profile.find_by_role("eye.right.open")
        fallback_spec = self.controller.profile.find_by_role("eye.open")
        for parameter in parameters:
            parameter_id = str(parameter.get("id") or "")
            try:
                value = float(parameter.get("value"))
            except (TypeError, ValueError):
                continue
            if left_spec is not None and parameter_id == left_spec.id:
                self._eye_open_base_raw["left"] = value
                if update_ceiling:
                    self._eye_open_ceiling_raw["left"] = value
            elif right_spec is not None and parameter_id == right_spec.id:
                self._eye_open_base_raw["right"] = value
                if update_ceiling:
                    self._eye_open_ceiling_raw["right"] = value
            elif fallback_spec is not None and parameter_id == fallback_spec.id:
                self._eye_open_base_raw["fallback"] = value
                if update_ceiling:
                    self._eye_open_ceiling_raw["fallback"] = value

    def _current_blink_amount(self, *, now: float) -> float:
        if not self._blink_timing.enabled:
            return 0.0
        if self._wink_active:
            return 0.0
        if not self._blink_active:
            if self._blink_next_at_monotonic <= 0.0:
                self._schedule_next_blink(now=now)
                return 0.0
            if now < self._blink_next_at_monotonic:
                return 0.0
            self._blink_active = True
            self._blink_started_at_monotonic = now
            self._blink_is_double = self._rng.random() < self._blink_timing.double_blink_chance
        close_sec = self._blink_timing.close_sec
        hold_sec = self._blink_timing.hold_sec
        open_sec = self._blink_timing.open_sec
        total_single = close_sec + hold_sec + open_sec
        gap_sec = self._blink_timing.double_gap_sec
        elapsed = max(0.0, now - self._blink_started_at_monotonic)
        total_duration = total_single + (gap_sec + total_single if self._blink_is_double else 0.0)
        if elapsed >= total_duration:
            self._blink_active = False
            self._blink_is_double = False
            self._schedule_next_blink(now=now)
            return 0.0
        if self._blink_is_double and elapsed >= total_single:
            if elapsed < total_single + gap_sec:
                return 0.0
            elapsed -= total_single + gap_sec
        if elapsed < close_sec:
            return _clamp_unit(elapsed / close_sec)
        elapsed -= close_sec
        if elapsed < hold_sec:
            return 1.0
        elapsed -= hold_sec
        if elapsed < open_sec:
            return _clamp_unit(1.0 - (elapsed / open_sec))
        return 0.0

    def _current_wink_amounts(self, *, now: float) -> tuple[float, float]:
        if not self._wink_active or not self._wink_timing.enabled:
            return 0.0, 0.0
        close_sec = self._wink_timing.close_sec
        hold_sec = self._wink_timing.hold_sec
        open_sec = self._wink_timing.open_sec
        elapsed = max(0.0, now - self._wink_started_at_monotonic)
        total_duration = close_sec + hold_sec + open_sec
        if elapsed >= total_duration:
            self._wink_active = False
            self._schedule_next_blink(now=now)
            return 0.0, 0.0
        if elapsed < close_sec:
            target_amount = _clamp_unit(elapsed / close_sec)
        else:
            elapsed -= close_sec
            if elapsed < hold_sec:
                target_amount = 1.0
            else:
                elapsed -= hold_sec
                target_amount = _clamp_unit(1.0 - (elapsed / open_sec))
        non_target_amount = target_amount * self._wink_timing.non_target_eye_drop
        if self._wink_side == "left":
            return target_amount, non_target_amount
        return non_target_amount, target_amount

    def _blink_amount_at_elapsed(self, elapsed: float, *, is_double: bool) -> float:
        close_sec = self._blink_timing.close_sec
        hold_sec = self._blink_timing.hold_sec
        open_sec = self._blink_timing.open_sec
        total_single = close_sec + hold_sec + open_sec
        gap_sec = self._blink_timing.double_gap_sec
        current = max(0.0, float(elapsed))
        total_duration = total_single + (gap_sec + total_single if is_double else 0.0)
        if current >= total_duration:
            return 0.0
        if is_double and current >= total_single:
            if current < total_single + gap_sec:
                return 0.0
            current -= total_single + gap_sec
        if current < close_sec:
            return _smoothstep_unit(current / close_sec)
        current -= close_sec
        if current < hold_sec:
            return 1.0
        current -= hold_sec
        if current < open_sec:
            return 1.0 - _smoothstep_unit(current / open_sec)
        return 0.0

    def _blink_total_duration(self, *, is_double: bool) -> float:
        total_single = self._blink_timing.close_sec + self._blink_timing.hold_sec + self._blink_timing.open_sec
        if not is_double:
            return total_single
        return total_single + self._blink_timing.double_gap_sec + total_single

    def _wink_amounts_at_elapsed(self, elapsed: float, *, side: str) -> tuple[float, float]:
        close_sec = self._wink_timing.close_sec
        hold_sec = self._wink_timing.hold_sec
        open_sec = self._wink_timing.open_sec
        current = max(0.0, float(elapsed))
        total_duration = close_sec + hold_sec + open_sec
        if current >= total_duration:
            return 0.0, 0.0
        if current < close_sec:
            target_amount = _clamp_unit(current / close_sec)
        else:
            current -= close_sec
            if current < hold_sec:
                target_amount = 1.0
            else:
                current -= hold_sec
                target_amount = _clamp_unit(1.0 - (current / open_sec))
        non_target_amount = target_amount * self._wink_timing.non_target_eye_drop
        if side == "left":
            return target_amount, non_target_amount
        return non_target_amount, target_amount

    def _wink_total_duration(self) -> float:
        return self._wink_timing.close_sec + self._wink_timing.hold_sec + self._wink_timing.open_sec

    def _eye_overlay_frame_offsets(self, *, duration_sec: float) -> list[int]:
        total_ms = max(
            _EYE_OVERLAY_FRAME_INTERVAL_MS,
            int(math.ceil(max(0.0, float(duration_sec)) * 1000.0)),
        )
        offsets = list(range(0, total_ms + 1, _EYE_OVERLAY_FRAME_INTERVAL_MS))
        if not offsets or offsets[-1] != total_ms:
            offsets.append(total_ms)
        return offsets

    def _eye_overlay_parameters_for_amounts(
        self,
        *,
        blink_amount: float = 0.0,
        wink_left_amount: float = 0.0,
        wink_right_amount: float = 0.0,
        weight: float = 0.92,
    ) -> list[dict[str, float | str]]:
        left_spec = self.controller.profile.find_by_role("eye.left.open")
        right_spec = self.controller.profile.find_by_role("eye.right.open")
        fallback_spec = self.controller.profile.find_by_role("eye.open")
        parameters: list[dict[str, float | str]] = []
        if left_spec is None and right_spec is None and fallback_spec is None:
            return parameters

        if left_spec is not None or right_spec is not None:
            if left_spec is not None:
                left_base = self._eye_open_base_raw.get("left", left_spec.default)
                left_base = min(left_base, self._eye_open_ceiling_raw.get("left", left_base))
                left_multiplier = 1.0 - max(blink_amount, wink_left_amount)
                parameters.append(
                    {
                        "id": left_spec.id,
                        "value": left_spec.clamp(float(left_base) * max(0.0, left_multiplier)),
                        "weight": weight,
                    }
                )
            if right_spec is not None:
                right_base = self._eye_open_base_raw.get("right", right_spec.default)
                right_base = min(right_base, self._eye_open_ceiling_raw.get("right", right_base))
                right_multiplier = 1.0 - max(blink_amount, wink_right_amount)
                parameters.append(
                    {
                        "id": right_spec.id,
                        "value": right_spec.clamp(float(right_base) * max(0.0, right_multiplier)),
                        "weight": weight,
                    }
                )
            return parameters

        fallback_base = self._eye_open_base_raw.get("fallback", fallback_spec.default)
        fallback_base = min(fallback_base, self._eye_open_ceiling_raw.get("fallback", fallback_base))
        average_multiplier = (
            (1.0 - max(blink_amount, wink_left_amount)) + (1.0 - max(blink_amount, wink_right_amount))
        ) * 0.5
        parameters.append(
            {
                "id": fallback_spec.id,
                "value": fallback_spec.clamp(float(fallback_base) * max(0.0, average_multiplier)),
                "weight": weight,
            }
        )
        return parameters

    def _build_blink_eye_clip(self, *, now: float, is_double: bool) -> EmbodiedMotionClip | None:
        duration_sec = self._blink_total_duration(is_double=is_double)
        frames: list[dict[str, Any]] = []
        for offset_ms in self._eye_overlay_frame_offsets(duration_sec=duration_sec):
            parameters = self._eye_overlay_parameters_for_amounts(
                blink_amount=self._blink_amount_at_elapsed(offset_ms / 1000.0, is_double=is_double),
            )
            if not parameters:
                continue
            frames.append(
                {
                    "offset_ms": offset_ms,
                    "parameters": parameters,
                    "purpose": "blink",
                }
            )
        if not frames:
            return None
        return EmbodiedMotionClip(
            purpose="blink",
            frames=frames,
            end_targets={},
            duration_sec=duration_sec,
        )

    def _build_wink_eye_clip(self, *, now: float) -> EmbodiedMotionClip | None:
        del now
        duration_sec = self._wink_total_duration()
        frames: list[dict[str, Any]] = []
        for offset_ms in self._eye_overlay_frame_offsets(duration_sec=duration_sec):
            wink_left_amount, wink_right_amount = self._wink_amounts_at_elapsed(
                offset_ms / 1000.0,
                side=self._wink_side,
            )
            parameters = self._eye_overlay_parameters_for_amounts(
                wink_left_amount=wink_left_amount,
                wink_right_amount=wink_right_amount,
            )
            if not parameters:
                continue
            frames.append(
                {
                    "offset_ms": offset_ms,
                    "parameters": parameters,
                    "purpose": "wink",
                }
            )
        if not frames:
            return None
        return EmbodiedMotionClip(
            purpose="wink",
            frames=frames,
            end_targets={},
            duration_sec=duration_sec,
        )

    async def _dispatch_eye_overlay_clip(self, clip: EmbodiedMotionClip) -> dict[str, Any]:
        result = await self.controller.send_timeline_clip(
            clip.frames,
            easing="linear",
            blend="replace",
            priority=7,
            purpose=clip.purpose,
        )
        if not bool(result.get("success")):
            self._log_warning("Live2D %s eye overlay dispatch failed: %s", clip.purpose, result)
        return result

    def _apply_eye_overlay(
        self,
        parameters: list[dict[str, float | str]],
        event: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> list[dict[str, float | str]]:
        del event
        current_now = time.monotonic() if now is None else float(now)
        left_spec = self.controller.profile.find_by_role("eye.left.open")
        right_spec = self.controller.profile.find_by_role("eye.right.open")
        fallback_spec = self.controller.profile.find_by_role("eye.open")
        if left_spec is None and right_spec is None and fallback_spec is None:
            return [dict(parameter) for parameter in parameters]

        modified: dict[str, dict[str, float | str]] = {
            str(parameter.get("id") or ""): dict(parameter) for parameter in parameters if isinstance(parameter, Mapping)
        }
        self._remember_eye_open_base(list(modified.values()), update_ceiling=False)

        blink_amount = self._current_blink_amount(now=current_now)
        wink_left_amount, wink_right_amount = self._current_wink_amounts(now=current_now)
        if blink_amount <= 0.0 and wink_left_amount <= 0.0 and wink_right_amount <= 0.0:
            return list(modified.values())

        if left_spec is not None or right_spec is not None:
            left_base = float(
                modified.get(left_spec.id, {}).get("value") if left_spec is not None and left_spec.id in modified else
                self._eye_open_base_raw.get("left", left_spec.default if left_spec is not None else 1.0)
            )
            right_base = float(
                modified.get(right_spec.id, {}).get("value") if right_spec is not None and right_spec.id in modified else
                self._eye_open_base_raw.get("right", right_spec.default if right_spec is not None else 1.0)
            )
            if left_spec is not None:
                left_base = min(left_base, self._eye_open_ceiling_raw.get("left", left_base))
            if right_spec is not None:
                right_base = min(right_base, self._eye_open_ceiling_raw.get("right", right_base))
            left_multiplier = 1.0 - max(blink_amount, wink_left_amount)
            right_multiplier = 1.0 - max(blink_amount, wink_right_amount)
            if left_spec is not None:
                modified[left_spec.id] = {
                    "id": left_spec.id,
                    "value": left_spec.clamp(left_base * max(0.0, left_multiplier)),
                    "weight": float(modified.get(left_spec.id, {}).get("weight", 0.70)),
                }
            if right_spec is not None:
                modified[right_spec.id] = {
                    "id": right_spec.id,
                    "value": right_spec.clamp(right_base * max(0.0, right_multiplier)),
                    "weight": float(modified.get(right_spec.id, {}).get("weight", 0.70)),
                }
            return list(modified.values())

        if fallback_spec is not None:
            fallback_base = float(
                modified.get(fallback_spec.id, {}).get("value")
                if fallback_spec.id in modified
                else self._eye_open_base_raw.get("fallback", fallback_spec.default)
            )
            fallback_base = min(fallback_base, self._eye_open_ceiling_raw.get("fallback", fallback_base))
            average_multiplier = (
                (1.0 - max(blink_amount, wink_left_amount)) + (1.0 - max(blink_amount, wink_right_amount))
            ) * 0.5
            modified[fallback_spec.id] = {
                "id": fallback_spec.id,
                "value": fallback_spec.clamp(fallback_base * max(0.0, average_multiplier)),
                "weight": float(modified.get(fallback_spec.id, {}).get("weight", 0.70)),
            }
        return list(modified.values())

    def _apply_eye_overlay_output_modifier(
        self,
        parameters: list[dict[str, float | str]],
        event: Mapping[str, Any],
    ) -> list[dict[str, float | str]] | None:
        if not self._blink_timing.enabled and not self._wink_timing.enabled:
            return parameters
        if str(event.get("purpose") or "").strip().lower() == "reset":
            return parameters
        return self._apply_eye_overlay(parameters, event)

    def _build_transition_clip_frames(
        self,
        *,
        start_targets: Mapping[str, float],
        end_targets: Mapping[str, float],
        purpose: str,
        frame_count: int,
        interval_ms: int,
    ) -> list[dict[str, Any]]:
        clip_frames: list[dict[str, Any]] = []
        frame_total = max(2, int(frame_count))
        last_frame_index = frame_total - 1
        keys = set(start_targets) | set(end_targets)
        for frame_index in range(frame_total):
            offset_ms = frame_index * max(20, int(interval_ms))
            progress = frame_index / max(1, last_frame_index)
            eased_progress = 0.5 - (0.5 * math.cos(math.pi * progress))
            frame_targets = {
                key: float(start_targets.get(key, 0.0))
                + ((float(end_targets.get(key, start_targets.get(key, 0.0))) - float(start_targets.get(key, 0.0))) * eased_progress)
                for key in keys
            }
            parameters = self._resolve_parameters(frame_targets, remember_eye_base=False)
            if not parameters:
                continue
            clip_frames.append(
                {
                    "offset_ms": offset_ms,
                    "parameters": parameters,
                    "purpose": purpose,
                }
            )
        if clip_frames:
            self._remember_eye_open_base(list(clip_frames[0].get("parameters") or []))
        return clip_frames

    def _build_expressive_clip(
        self,
        snapshot: EmbodiedStateSnapshot,
        *,
        current_targets: Mapping[str, float],
        now: float,
    ) -> EmbodiedMotionClip | None:
        start_targets = self._anchor_targets(current_targets=current_targets)
        end_targets = self._build_expressive_targets(snapshot, base_targets=current_targets, now=now)
        frames = self._build_transition_clip_frames(
            start_targets=start_targets,
            end_targets=end_targets,
            purpose="expressive",
            frame_count=_EXPRESSIVE_CLIP_FRAME_COUNT,
            interval_ms=_CLIP_FRAME_INTERVAL_MS,
        )
        if not frames:
            return None
        return EmbodiedMotionClip(
            purpose="expressive",
            frames=frames,
            end_targets=end_targets,
            duration_sec=self._clip_duration_seconds(frames),
        )

    def _build_speech_clip(
        self,
        audio_timeline: Mapping[str, Any],
        *,
        snapshot: EmbodiedStateSnapshot,
        base_targets: Mapping[str, float],
        emotion_gain: float,
        timeline_id: str,
    ) -> EmbodiedMotionClip | None:
        del timeline_id
        samples = self._extract_speech_amplitude_samples(audio_timeline)
        if not samples:
            return None
        smoothed_envelope = 0.0
        previous_targets = self._anchor_targets(current_targets=base_targets)
        if not previous_targets:
            previous_targets = dict(base_targets)
        clip_frames: list[dict[str, Any]] = []
        end_targets = dict(previous_targets)
        anchor_parameters = self._resolve_speech_parameters(previous_targets)
        if anchor_parameters:
            clip_frames.append(
                {
                    "offset_ms": 0,
                    "parameters": anchor_parameters,
                    "purpose": "speech",
                }
            )
        for offset_ms, amplitude in samples:
            if clip_frames and int(offset_ms) <= 0:
                continue
            previous_envelope = smoothed_envelope
            smoothed_envelope = max(float(amplitude), smoothed_envelope * 0.72)
            desired_targets = self._build_speech_targets(
                snapshot,
                base_targets=base_targets,
                offset_ms=offset_ms,
                amplitude=smoothed_envelope,
                amplitude_velocity=smoothed_envelope - previous_envelope,
                emotion_gain=emotion_gain,
            )
            if previous_targets:
                settle_alpha = min(
                    0.56,
                    max(
                        0.20,
                        0.22
                        + (0.16 * smoothed_envelope)
                        + (0.06 * max(0.0, smoothed_envelope - previous_envelope)),
                    ),
                )
                keys = set(previous_targets) | set(desired_targets)
                frame_targets = {
                    key: float(previous_targets.get(key, desired_targets.get(key, 0.0)))
                    + (
                        (
                            float(desired_targets.get(key, previous_targets.get(key, 0.0)))
                            - float(previous_targets.get(key, desired_targets.get(key, 0.0)))
                        )
                        * settle_alpha
                    )
                    for key in keys
                }
            else:
                frame_targets = desired_targets
            frame_targets = self._filter_speech_motion_targets(
                previous_targets=previous_targets,
                current_targets=frame_targets,
                snapshot=snapshot,
                emotion_gain=emotion_gain,
            )
            frame_targets = self._limit_speech_frame_step(
                previous_targets=previous_targets,
                current_targets=frame_targets,
                snapshot=snapshot,
                emotion_gain=emotion_gain,
            )
            parameters = self._resolve_speech_parameters(frame_targets)
            if not parameters:
                continue
            clip_frames.append(
                {
                    "offset_ms": max(0, int(offset_ms)),
                    "parameters": parameters,
                    "purpose": "speech",
                }
            )
            previous_targets = dict(frame_targets)
            end_targets = dict(frame_targets)
        if not clip_frames:
            return None
        return EmbodiedMotionClip(
            purpose="speech",
            frames=clip_frames,
            end_targets=end_targets,
            duration_sec=self._clip_duration_seconds(clip_frames),
        )

    def _build_idle_clip(
        self,
        snapshot: EmbodiedStateSnapshot,
        *,
        current_targets: Mapping[str, float],
        now: float,
    ) -> EmbodiedMotionClip | None:
        start_targets = self._anchor_targets(current_targets=current_targets)
        frames, end_targets = self._build_idle_clip_frames(
            snapshot,
            start_targets=start_targets,
            base_targets=current_targets,
        )
        if not frames:
            return None
        return EmbodiedMotionClip(
            purpose="idle",
            frames=frames,
            end_targets=end_targets,
            duration_sec=self._clip_duration_seconds(frames),
        )

    def _build_expressive_targets(
        self,
        snapshot: EmbodiedStateSnapshot,
        *,
        base_targets: Mapping[str, float],
        now: float,
    ) -> dict[str, float]:
        targets = dict(base_targets or self._build_semantic_targets(snapshot, now=now))
        intensity = _clamp_unit(
            (0.30 * snapshot.energy)
            + (0.24 * snapshot.arousal)
            + (0.18 * snapshot.attention)
            + (0.12 * snapshot.confidence)
            + (0.16 * abs(snapshot.valence))
        )
        emphasis = 0.34 + (0.70 * intensity)
        targets["head.yaw"] = _clamp_signed((targets.get("head.yaw", 0.0) * (1.46 + emphasis)) + (0.26 * snapshot.social_approach))
        targets["head.pitch"] = _clamp_signed(
            targets.get("head.pitch", 0.0)
            + (0.36 * snapshot.confidence)
            + (0.26 * snapshot.energy)
            - (0.10 * snapshot.cognitive_load)
        )
        targets["head.roll"] = _clamp_signed(
            targets.get("head.roll", 0.0)
            + (0.42 * snapshot.valence)
            + (0.22 * snapshot.social_approach)
        )
        targets["body.yaw"] = _clamp_signed(
            (targets.get("body.yaw", 0.0) * (1.54 + (0.60 * intensity))) + (0.26 * snapshot.gaze_x)
        )
        targets["body.pitch"] = _clamp_signed(
            targets.get("body.pitch", 0.0)
            + (0.50 * snapshot.social_approach)
            + (0.34 * snapshot.energy)
            - (0.14 * snapshot.cognitive_load)
        )
        targets["body.roll"] = _clamp_signed(
            targets.get("body.roll", 0.0)
            + (0.38 * snapshot.valence)
            + (0.20 * snapshot.social_approach)
        )
        targets["body.shoulder"] = _clamp_signed(
            targets.get("body.shoulder", 0.0)
            + (0.44 * snapshot.energy)
            + (0.30 * snapshot.social_approach)
        )
        targets["eye.gaze.x"] = _clamp_signed(targets.get("eye.gaze.x", 0.0) + (0.05 * snapshot.social_approach))
        targets["eye.gaze.y"] = _clamp_signed(targets.get("eye.gaze.y", 0.0) - (0.04 * snapshot.cognitive_load))
        return targets

    def _build_idle_clip_frames(
        self,
        snapshot: EmbodiedStateSnapshot,
        *,
        start_targets: Mapping[str, float],
        base_targets: Mapping[str, float],
    ) -> tuple[list[dict[str, Any]], dict[str, float]]:
        frame_total = max(2, _IDLE_CLIP_FRAME_COUNT)
        last_frame_index = frame_total - 1
        frame_step_sec = max(0.02, _CLIP_FRAME_INTERVAL_MS / 1000.0)
        motion_start = self._idle_motion_cursor
        previous_targets = dict(start_targets) if start_targets else self._build_idle_targets(
            snapshot,
            base_targets=base_targets,
            motion_t=motion_start,
        )
        end_targets = dict(previous_targets)
        clip_frames: list[dict[str, Any]] = []
        for frame_index in range(frame_total):
            offset_ms = frame_index * max(20, int(_CLIP_FRAME_INTERVAL_MS))
            if frame_index == 0 and start_targets:
                frame_targets = dict(previous_targets)
            else:
                motion_t = motion_start + (frame_index * frame_step_sec)
                desired_targets = self._build_idle_targets(
                    snapshot,
                    base_targets=base_targets,
                    motion_t=motion_t,
                )
                if not previous_targets:
                    frame_targets = dict(desired_targets)
                else:
                    progress = frame_index / max(1, last_frame_index)
                    settle_alpha = 0.28 + (0.18 * progress)
                    keys = set(previous_targets) | set(desired_targets)
                    frame_targets = {
                        key: float(previous_targets.get(key, desired_targets.get(key, 0.0)))
                        + (
                            (float(desired_targets.get(key, previous_targets.get(key, 0.0)))
                            - float(previous_targets.get(key, desired_targets.get(key, 0.0))))
                            * settle_alpha
                        )
                        for key in keys
                    }
                    frame_targets = self._limit_idle_frame_step(
                        previous_targets=previous_targets,
                        current_targets=frame_targets,
                    )
            parameters = self._resolve_parameters(frame_targets, remember_eye_base=False)
            if parameters:
                clip_frames.append(
                    {
                        "offset_ms": offset_ms,
                        "parameters": parameters,
                        "purpose": "idle",
                    }
                )
            previous_targets = dict(frame_targets)
            end_targets = dict(frame_targets)
        if clip_frames:
            self._remember_eye_open_base(list(clip_frames[0].get("parameters") or []))
        self._idle_motion_cursor = motion_start + (last_frame_index * frame_step_sec)
        return clip_frames, end_targets

    def _build_idle_targets(
        self,
        snapshot: EmbodiedStateSnapshot,
        *,
        base_targets: Mapping[str, float],
        motion_t: float,
    ) -> dict[str, float]:
        targets = dict(base_targets or self._build_semantic_targets(snapshot, now=time.monotonic()))
        state_scale = {
            "running": 0.62,
            "wait": 0.50,
            "stop": 0.34,
        }.get(str(snapshot.agent_state or "").strip().lower(), 0.42)
        engagement = _clamp_unit((0.40 * snapshot.attention) + (0.34 * snapshot.energy) + (0.18 * snapshot.confidence))
        sway_scale = state_scale * (0.95 + (1.05 * engagement))
        amplitude_mod = 0.92 + (0.34 * math.sin((math.tau * 0.035 * motion_t) + self._idle_phase_offsets["amplitude"]))
        yaw_wave = (
            math.sin((math.tau * 0.11 * motion_t) + self._idle_phase_offsets["yaw_primary"])
            + (0.34 * math.sin((math.tau * 0.047 * motion_t) + self._idle_phase_offsets["yaw_secondary"]))
        )
        pitch_wave = (
            math.sin((math.tau * 0.087 * motion_t) + self._idle_phase_offsets["pitch_primary"])
            + (0.24 * math.sin((math.tau * 0.036 * motion_t) + self._idle_phase_offsets["pitch_secondary"]))
        )
        roll_wave = (
            math.sin((math.tau * 0.094 * motion_t) + self._idle_phase_offsets["roll_primary"])
            + (0.28 * math.sin((math.tau * 0.041 * motion_t) + self._idle_phase_offsets["roll_secondary"]))
        )
        shoulder_wave = math.sin((math.tau * 0.17 * motion_t) + self._idle_phase_offsets["shoulder"])
        breath_wave = math.sin((math.tau * 0.24 * motion_t) + self._idle_phase_offsets["breath"])
        drift = yaw_wave * amplitude_mod
        pitch_drift = pitch_wave * amplitude_mod
        roll_drift = roll_wave * amplitude_mod
        idle_follow_scale = state_scale * (0.18 + (0.20 * engagement))
        yaw_follow = self._idle_random_follow_value(
            "yaw_follow",
            motion_t,
            interval_sec=4.8,
            amplitude=idle_follow_scale,
        )
        pitch_follow = self._idle_random_follow_value(
            "pitch_follow",
            motion_t,
            interval_sec=5.2,
            amplitude=idle_follow_scale * 0.80,
        )
        roll_follow = self._idle_random_follow_value(
            "roll_follow",
            motion_t,
            interval_sec=5.6,
            amplitude=idle_follow_scale * 0.62,
        )
        shoulder_follow = self._idle_random_follow_value(
            "shoulder_follow",
            motion_t,
            interval_sec=4.4,
            amplitude=idle_follow_scale * 0.54,
        )

        targets["head.yaw"] = _clamp_signed(
            (targets.get("head.yaw", 0.0) * 0.68)
            + (drift * sway_scale * 1.28)
            + (yaw_follow * 0.72)
        )
        targets["head.pitch"] = _clamp_signed(
            (targets.get("head.pitch", 0.0) * 0.58)
            + (pitch_drift * sway_scale * 0.74)
            - (0.05 * snapshot.cognitive_load)
            + (pitch_follow * 0.68)
        )
        targets["head.roll"] = _clamp_signed(
            (targets.get("head.roll", 0.0) * 0.56)
            + (roll_drift * sway_scale * 0.50)
            + (roll_follow * 0.56)
        )
        targets["body.yaw"] = _clamp_signed(
            (targets.get("body.yaw", 0.0) * 0.72)
            + (targets["head.yaw"] * 0.38)
            + (drift * sway_scale * 0.24)
            + (yaw_follow * 0.24)
        )
        targets["body.pitch"] = _clamp_signed(
            (targets.get("body.pitch", 0.0) * 0.72)
            + (pitch_drift * sway_scale * 0.38)
            + (0.08 * snapshot.social_approach)
            - (0.04 * snapshot.cognitive_load)
            + (pitch_follow * 0.28)
        )
        targets["body.roll"] = _clamp_signed(
            (targets.get("body.roll", 0.0) * 0.66)
            + (roll_drift * sway_scale * 0.28)
            + (roll_follow * 0.26)
        )
        targets["body.shoulder"] = _clamp_signed(
            (targets.get("body.shoulder", 0.0) * 0.74)
            + (shoulder_wave * sway_scale * 0.44)
            + (0.10 * breath_wave)
            + (shoulder_follow * 0.36)
        )
        targets["eye.gaze.x"] = _clamp_signed((targets["head.yaw"] * 0.58) + (drift * sway_scale * 0.24))
        targets["eye.gaze.y"] = _clamp_signed((targets["head.pitch"] * 0.46) + (pitch_drift * sway_scale * 0.16))
        targets["breath"] = _clamp_unit(0.14 + (0.14 * breath_wave) + (0.22 * snapshot.energy))
        return targets

    def _idle_random_follow_value(
        self,
        axis: str,
        motion_t: float,
        *,
        interval_sec: float,
        amplitude: float,
    ) -> float:
        bucket_span = max(1.0, float(interval_sec))
        follow_amplitude = max(0.0, float(amplitude))
        if follow_amplitude <= 0.0:
            return 0.0
        phase = max(0.0, float(motion_t)) / bucket_span
        bucket = math.floor(phase)
        progress = phase - bucket
        eased_progress = progress * progress * (3.0 - (2.0 * progress))
        axis_offset = self._idle_follow_offsets.get(axis, 0.0)

        def _bucket_value(index: int) -> float:
            harmonic = math.sin(((index + 1.0) * 1.61803398875 * 1.91) + axis_offset)
            harmonic += 0.56 * math.sin(((index + 1.0) * 0.713) + (axis_offset * 1.93))
            return _clamp_signed(harmonic / 1.56)

        start = _bucket_value(bucket)
        end = _bucket_value(bucket + 1)
        return _clamp_signed(start + ((end - start) * eased_progress)) * follow_amplitude

    def _build_speech_targets(
        self,
        snapshot: EmbodiedStateSnapshot,
        *,
        base_targets: Mapping[str, float],
        offset_ms: int,
        amplitude: float,
        amplitude_velocity: float,
        emotion_gain: float,
    ) -> dict[str, float]:
        targets = dict(base_targets)
        envelope = _clamp_unit(amplitude)
        emotion_scale = 1.0 + (0.72 * min(_MAX_REPLY_EMOTION_GAIN, max(0.0, float(emotion_gain))))
        liveliness = _clamp_unit(
            (0.36 * snapshot.energy)
            + (0.28 * snapshot.arousal)
            + (0.20 * snapshot.attention)
            + (0.16 * snapshot.confidence)
        )
        motion_gain = envelope * (0.68 + (0.42 * liveliness)) * emotion_scale
        vertical_gain = motion_gain * 0.62
        lateral_gain = motion_gain * 0.66
        roll_gain = motion_gain * 0.48
        shoulder_gain = motion_gain * 0.56
        speech_t = max(0.0, float(offset_ms) / 1000.0)
        cadence_vertical = math.sin((math.tau * 3.4 * speech_t) + 0.35)
        cadence_lateral = math.sin((math.tau * 2.15 * speech_t) + 1.15)
        cadence_roll = math.sin((math.tau * 2.75 * speech_t) + 2.05)
        impulse = _clamp_signed(float(amplitude_velocity) * 1.55)
        vertical_wave = (0.82 * cadence_vertical) + (0.18 * impulse)
        lateral_wave = (0.84 * cadence_lateral) + (0.16 * impulse)
        roll_wave = (0.86 * cadence_roll) + (0.14 * cadence_lateral)

        base_head_yaw = float(targets.get("head.yaw", 0.0))
        base_body_yaw = float(targets.get("body.yaw", 0.0))
        base_head_pitch = float(targets.get("head.pitch", 0.0))
        base_body_pitch = float(targets.get("body.pitch", 0.0))
        base_head_roll = float(targets.get("head.roll", 0.0))
        base_shoulder = float(targets.get("body.shoulder", 0.0))

        yaw_direction = _signed_direction(base_body_yaw, fallback=snapshot.gaze_x or snapshot.social_approach or 1.0)
        pitch_direction = _signed_direction(base_body_pitch, fallback=snapshot.social_approach or snapshot.confidence or 1.0)
        roll_direction = _signed_direction(base_head_roll, fallback=snapshot.valence or snapshot.social_approach or 1.0)
        shoulder_direction = _signed_direction(base_shoulder, fallback=snapshot.energy or snapshot.social_approach or 1.0)

        targets["head.yaw"] = _clamp_signed(
            (base_head_yaw * 0.54)
            + (yaw_direction * 0.08)
            + (lateral_gain * 0.84 * lateral_wave)
        )
        targets["body.yaw"] = _clamp_signed(
            (base_body_yaw * 0.46)
            + (yaw_direction * 0.10)
            + (lateral_gain * 0.96 * lateral_wave)
        )
        targets["head.pitch"] = _clamp_signed(
            (base_head_pitch * 0.42)
            + (pitch_direction * 0.06)
            + (vertical_gain * 0.88 * vertical_wave)
        )
        targets["body.pitch"] = _clamp_signed(
            (base_body_pitch * 0.52)
            + (pitch_direction * 0.10)
            + (vertical_gain * 1.02 * vertical_wave)
        )
        targets["head.roll"] = _clamp_signed(
            (base_head_roll * 0.48)
            + (roll_direction * 0.06)
            + (roll_gain * 0.82 * roll_wave)
        )
        targets["body.shoulder"] = _clamp_signed(
            (base_shoulder * 0.40)
            + (shoulder_direction * 0.08)
            + (shoulder_gain * 0.96 * vertical_wave)
        )
        return targets

    def _extract_speech_amplitude_samples(self, audio_timeline: Mapping[str, Any]) -> list[tuple[int, float]]:
        raw_amplitudes = audio_timeline.get("amplitudes")
        if not isinstance(raw_amplitudes, list):
            return []
        sparse_samples: list[tuple[int, float]] = []
        for entry in raw_amplitudes:
            if not isinstance(entry, Mapping):
                continue
            try:
                offset_ms = max(0, int(entry.get("offset_ms") or 0))
                value = _clamp_unit(float(entry.get("value") or 0.0))
            except (TypeError, ValueError):
                continue
            sparse_samples.append((offset_ms, value))
        sparse_samples.sort(key=lambda item: item[0])
        if not sparse_samples:
            return []
        if len(sparse_samples) == 1:
            return list(sparse_samples)
        terminal_offset_ms = max(
            int(audio_timeline.get("audio_duration_ms") or 0),
            sparse_samples[-1][0],
        )
        interval_ms = max(8, int(_SPEECH_RESAMPLE_INTERVAL_MS))
        resampled: list[tuple[int, float]] = []
        sample_index = 0
        for offset_ms in range(0, terminal_offset_ms + 1, interval_ms):
            while sample_index + 1 < len(sparse_samples) and sparse_samples[sample_index + 1][0] < offset_ms:
                sample_index += 1
            lower_offset, lower_value = sparse_samples[sample_index]
            if sample_index + 1 < len(sparse_samples):
                upper_offset, upper_value = sparse_samples[sample_index + 1]
            else:
                upper_offset, upper_value = lower_offset, lower_value
            if upper_offset <= lower_offset:
                interpolated = lower_value
            else:
                progress = (offset_ms - lower_offset) / max(1, upper_offset - lower_offset)
                progress = min(1.0, max(0.0, progress))
                interpolated = lower_value + ((upper_value - lower_value) * progress)
            resampled.append((offset_ms, _clamp_unit(interpolated)))
        if resampled[-1][0] != terminal_offset_ms:
            resampled.append((terminal_offset_ms, _clamp_unit(sparse_samples[-1][1])))
        return resampled

    @staticmethod
    def _clip_duration_seconds(frames: list[Mapping[str, Any]]) -> float:
        if not frames:
            return 0.0
        last_offset_ms = 0
        for frame in frames:
            try:
                last_offset_ms = max(last_offset_ms, int(frame.get("offset_ms") or 0))
            except (AttributeError, TypeError, ValueError):
                continue
        return max(0.0, float(last_offset_ms) / 1000.0)

    def _apply_mouse_follow_output_modifier(
        self,
        parameters: list[dict[str, float | str]],
        event: Mapping[str, Any],
    ) -> list[dict[str, float | str]]:
        if not self._active or not self.mouse_follow_enabled:
            return [dict(parameter) for parameter in parameters]
        purpose = str(event.get("purpose") or "").strip().lower()
        if purpose in {"lipsync", "reset", "neutral"}:
            return [dict(parameter) for parameter in parameters]
        now = time.monotonic()
        strength = self._mouse_follow_strength(now)
        if strength <= 0.0:
            return [dict(parameter) for parameter in parameters]
        smoothed_x, smoothed_y = self._resolved_mouse_follow_axes(now=now)
        offsets = {
            "eye.gaze.x": smoothed_x * self._mouse_follow_eye_gain * strength,
            "eye.gaze.y": smoothed_y * self._mouse_follow_eye_gain * strength,
            "head.yaw": smoothed_x * self._mouse_follow_head_gain * strength,
            "head.pitch": smoothed_y * self._mouse_follow_head_gain * strength,
            "body.yaw": smoothed_x * self._mouse_follow_body_gain * strength,
            "body.pitch": smoothed_y * self._mouse_follow_body_gain * strength,
        }
        resolved: list[dict[str, float | str]] = []
        for parameter in parameters:
            updated = dict(parameter)
            parameter_id = str(updated.get("id") or "").strip()
            spec = self.controller.profile.parameters.get(parameter_id)
            if spec is None:
                resolved.append(updated)
                continue
            semantic_offset = float(offsets.get(spec.role, 0.0))
            if abs(semantic_offset) <= 1e-6:
                resolved.append(updated)
                continue
            try:
                current_value = float(updated.get("value"))
            except (TypeError, ValueError):
                current_value = spec.default
            updated["value"] = spec.clamp(current_value + self._semantic_offset_to_raw_delta(spec, semantic_offset))
            resolved.append(updated)
        return resolved

    @staticmethod
    def _semantic_offset_to_raw_delta(parameter: Any, semantic_offset: float) -> float:
        value = _clamp_signed(semantic_offset)
        if value < 0.0:
            return value * (parameter.default - parameter.minimum) * parameter.safe_amplitude
        return value * (parameter.maximum - parameter.default) * parameter.safe_amplitude

    def _resolved_mouse_follow_axes(self, *, now: float) -> tuple[float, float]:
        self._update_mouse_follow_smoothing(now=now)
        return self._mouse_follow_smoothed_x, self._mouse_follow_smoothed_y

    def _update_mouse_follow_smoothing(self, *, now: float) -> None:
        target_x = _clamp_signed(self._latest_mouse_snapshot.x_norm if self.mouse_follow_enabled else 0.0)
        target_y = _clamp_signed(self._latest_mouse_snapshot.y_norm if self.mouse_follow_enabled else 0.0)
        if self._mouse_follow_smoothing_sec <= 0.0:
            self._mouse_follow_smoothed_x = target_x
            self._mouse_follow_smoothed_y = target_y
            self._mouse_follow_last_update_monotonic = now
            return
        if self._mouse_follow_last_update_monotonic <= 0.0:
            dt = min(self._mouse_follow_smoothing_sec, _MOUSE_FOLLOW_SMOOTHING_FALLBACK_STEP_SECONDS)
        else:
            dt = max(0.0, now - self._mouse_follow_last_update_monotonic)
        self._mouse_follow_last_update_monotonic = now
        if dt <= 0.0:
            return
        alpha = min(1.0, 1.0 - math.exp(-dt / self._mouse_follow_smoothing_sec))
        self._mouse_follow_smoothed_x += (target_x - self._mouse_follow_smoothed_x) * alpha
        self._mouse_follow_smoothed_y += (target_y - self._mouse_follow_smoothed_y) * alpha

    def _reset_mouse_follow_smoothing(self) -> None:
        self._mouse_follow_smoothed_x = 0.0
        self._mouse_follow_smoothed_y = 0.0
        self._mouse_follow_last_update_monotonic = 0.0

    def _mouse_follow_strength(self, now: float) -> float:
        if not self.mouse_follow_enabled:
            return 0.0
        if now <= self._mouse_follow_lease_until:
            return 1.0
        if self._mouse_follow_cooldown_sec <= 0.0:
            return 0.0
        cooldown_elapsed = now - self._mouse_follow_lease_until
        if cooldown_elapsed <= 0.0:
            return 1.0
        if cooldown_elapsed >= self._mouse_follow_cooldown_sec:
            return 0.0
        return max(0.0, 1.0 - (cooldown_elapsed / self._mouse_follow_cooldown_sec))

    def _anchor_targets(self, *, current_targets: Mapping[str, float] | None = None) -> dict[str, float]:
        if self._playback_anchor_targets:
            return dict(self._playback_anchor_targets)
        if current_targets:
            return dict(current_targets)
        if self._latest_targets:
            return dict(self._latest_targets)
        return {}

    def _speech_transition_active(self, now: float) -> bool:
        return self.controller.is_speaking or now < self._speech_transition_hold_until

    def _speech_motion_intensity(
        self,
        *,
        snapshot: EmbodiedStateSnapshot,
        emotion_gain: float,
    ) -> float:
        return _clamp_unit(
            (0.28 * abs(snapshot.valence))
            + (0.30 * snapshot.arousal)
            + (0.18 * snapshot.energy)
            + (0.12 * snapshot.confidence)
            + (0.12 * min(_MAX_REPLY_EMOTION_GAIN, max(0.0, float(emotion_gain))) / _MAX_REPLY_EMOTION_GAIN)
        )

    def _filter_speech_motion_targets(
        self,
        *,
        previous_targets: Mapping[str, float],
        current_targets: Mapping[str, float],
        snapshot: EmbodiedStateSnapshot,
        emotion_gain: float,
    ) -> dict[str, float]:
        if not previous_targets:
            return dict(current_targets)
        emotion_intensity = self._speech_motion_intensity(
            snapshot=snapshot,
            emotion_gain=emotion_gain,
        )
        filtered = dict(current_targets)
        frame_step_sec = max(0.008, _SPEECH_RESAMPLE_INTERVAL_MS / 1000.0)
        filter_time_constants = {
            "head.yaw": (0.16, 0.10),
            "head.pitch": (0.18, 0.12),
            "head.roll": (0.17, 0.11),
            "body.yaw": (0.22, 0.14),
            "body.pitch": (0.25, 0.16),
            "body.shoulder": (0.24, 0.15),
        }
        for key, (calm_time_constant, intense_time_constant) in filter_time_constants.items():
            previous_value = float(previous_targets.get(key, current_targets.get(key, 0.0)))
            current_value = float(current_targets.get(key, previous_value))
            time_constant = calm_time_constant + ((intense_time_constant - calm_time_constant) * emotion_intensity)
            moving_toward_center = abs(current_value) < abs(previous_value) or ((previous_value * current_value) < 0.0)
            if moving_toward_center:
                time_constant *= 0.18
            if time_constant <= 0.0:
                filtered[key] = current_value
                continue
            alpha = min(1.0, 1.0 - math.exp(-frame_step_sec / time_constant))
            filtered[key] = previous_value + ((current_value - previous_value) * alpha)
        return filtered

    def _limit_speech_frame_step(
        self,
        *,
        previous_targets: Mapping[str, float],
        current_targets: Mapping[str, float],
        snapshot: EmbodiedStateSnapshot,
        emotion_gain: float,
    ) -> dict[str, float]:
        if not previous_targets:
            return dict(current_targets)
        emotion_intensity = self._speech_motion_intensity(
            snapshot=snapshot,
            emotion_gain=emotion_gain,
        )
        limited = dict(current_targets)
        max_step_ranges = {
            "head.yaw": (0.11, 0.18),
            "head.pitch": (0.08, 0.14),
            "head.roll": (0.09, 0.16),
            "body.yaw": (0.11, 0.18),
            "body.pitch": (0.08, 0.14),
            "body.shoulder": (0.07, 0.14),
        }
        for key, (base_step, peak_step) in max_step_ranges.items():
            max_step = base_step + ((peak_step - base_step) * emotion_intensity)
            previous_value = float(previous_targets.get(key, current_targets.get(key, 0.0)))
            current_value = float(current_targets.get(key, previous_value))
            delta = current_value - previous_value
            if delta > max_step:
                limited[key] = previous_value + max_step
            elif delta < -max_step:
                limited[key] = previous_value - max_step
        return limited

    def _limit_idle_frame_step(
        self,
        *,
        previous_targets: Mapping[str, float],
        current_targets: Mapping[str, float],
    ) -> dict[str, float]:
        limited = dict(current_targets)
        max_step_ranges = {
            "head.yaw": 0.075,
            "head.pitch": 0.075,
            "head.roll": 0.060,
            "body.yaw": 0.045,
            "body.pitch": 0.045,
            "body.roll": 0.035,
            "body.shoulder": 0.030,
            "eye.gaze.x": 0.050,
            "eye.gaze.y": 0.050,
            "breath": 0.020,
        }
        for key, max_step in max_step_ranges.items():
            current_value = float(current_targets.get(key, previous_targets.get(key, 0.0)))
            previous_value = float(previous_targets.get(key, current_value))
            delta = current_value - previous_value
            if delta > max_step:
                limited[key] = previous_value + max_step
            elif delta < -max_step:
                limited[key] = previous_value - max_step
        return limited

    def _enqueue_motion_clip(self, clip: EmbodiedMotionClip) -> None:
        if clip.purpose == "speech":
            queued_speech = [queued for queued in self._motion_queue if queued.purpose == "speech"]
            self._motion_queue.clear()
            self._motion_queue.extend(queued_speech)
            self._motion_queue.append(clip)
            return
        queued_speech = [queued for queued in self._motion_queue if queued.purpose == "speech"]
        if queued_speech:
            self._motion_queue.clear()
            self._motion_queue.extend(queued_speech)
            self._motion_queue.append(clip)
            return
        self._motion_queue.clear()
        self._motion_queue.append(clip)

    async def _motion_scheduler_loop(self) -> None:
        while self._active:
            try:
                await asyncio.sleep(_SCHEDULER_TICK_SECONDS)
                clip: EmbodiedMotionClip | None = None
                snapshot: EmbodiedStateSnapshot | None = None
                now = time.monotonic()
                async with self._dispatch_lock:
                    now = time.monotonic()
                    if now < self._clip_busy_until:
                        continue
                    if self._motion_queue:
                        if self._speech_transition_active(now) and self._motion_queue[0].purpose != "speech":
                            continue
                        clip = self._motion_queue.popleft()
                    elif self._speech_transition_active(now):
                        continue
                    elif self._received_snapshot and self._latest_snapshot is not None:
                        snapshot = self._latest_snapshot
                        clip = self._build_idle_clip(
                            snapshot,
                            current_targets=self._latest_targets,
                            now=now,
                        )
                    if clip is None:
                        continue
                    result = await self._dispatch_clip(clip, now=now)
                if not bool(result.get("success")):
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_warning("Live2D embodied scheduler failed: %s", exc)

    async def _dispatch_clip(self, clip: EmbodiedMotionClip, *, now: float) -> dict[str, Any]:
        result = await self.controller.send_timeline_clip(
            clip.frames,
            easing="easeInOut",
            blend="replace",
            priority=4,
            purpose=clip.purpose,
        )
        if not bool(result.get("success")):
            self._consecutive_errors += 1
            if self._consecutive_errors >= 3:
                return await self._handle_failure("write_error_threshold")
            return {
                "success": False,
                "reason": "write_error",
                "error": result.get("error") or "",
            }
        self._consecutive_errors = 0
        self._clip_busy_until = max(self._clip_busy_until, now + clip.duration_sec)
        self._playback_anchor_targets = dict(clip.end_targets)
        return result

    def _build_default_parameter(self, role: str, *, weight: float) -> dict[str, float | str] | None:
        parameter = self.controller.profile.find_by_role(role)
        if parameter is None:
            return None
        return {
            "id": parameter.id,
            "value": parameter.clamp(parameter.default),
            "weight": min(1.0, max(0.0, float(weight))),
        }

    async def _watchdog_loop(self) -> None:
        while self._active:
            await asyncio.sleep(0.2)
            if not self._received_snapshot:
                continue
            reference = self._last_snapshot_monotonic or self._started_monotonic
            if reference <= 0:
                continue
            if (time.monotonic() - reference) > self.stale_after_sec:
                if not self._stale_warning_emitted:
                    self._stale_warning_emitted = True
                    self._log_warning(
                        "Live2D embodied snapshot stream is quiet; keeping embodied idle control active"
                    )

    async def _handle_failure(self, reason: str) -> dict[str, Any]:
        if not self._active:
            return {"success": False, "reason": reason}
        self._active = False
        self._log_warning("Live2D embodied driver disabling itself: %s", reason)
        await self.handle_reset()
        if self.fallback_to_legacy:
            await self.controller.set_embodied_mode(False)
        await _maybe_await(self.on_disable(reason) if self.on_disable is not None else None)
        return {"success": False, "reason": reason}

    def _log_info(self, message: str, *args: Any) -> None:
        if self.logger is not None and hasattr(self.logger, "info"):
            formatted = message % args if args else message
            self.logger.info(formatted)

    def _log_warning(self, message: str, *args: Any) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            formatted = message % args if args else message
            self.logger.warning(formatted)


class EmbodiedStateSubscriber:
    """Subscribe to core avatar_state snapshots over WebSocket."""

    def __init__(
        self,
        *,
        source_url: str,
        source_token: str,
        source_session_id: str,
        on_snapshot: Callable[[EmbodiedStateSnapshot], Awaitable[dict[str, Any]]],
        on_reset: Callable[[], Awaitable[dict[str, Any]]],
        on_connected: SubscriberConnectedCallback | None = None,
        on_disconnected: SubscriberDisconnectedCallback | None = None,
        logger: Any = None,
    ) -> None:
        self.source_url = str(source_url or "").strip()
        self.source_token = str(source_token or "").strip()
        self.source_session_id = str(source_session_id or "").strip()
        self.on_snapshot = on_snapshot
        self.on_reset = on_reset
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.logger = logger
        self._session: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._ws: Any = None
        self._event_log_count = 0
        self._last_event_log_monotonic = 0.0
        self._connected = False

    async def start(self) -> None:
        if not AIOHTTP_AVAILABLE:
            self._log_warning("aiohttp is unavailable; embodied avatar_state subscriber disabled")
            return
        self._stop_requested = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run_loop(),
                name="live2d_adaptive.embodied_subscriber",
            )

    async def stop(self) -> None:
        self._stop_requested = True
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        task = self._task
        self._task = None
        current_task = asyncio.current_task()
        if task is not None:
            task.cancel()
            if task is not current_task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        session = self._session
        self._session = None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    async def _run_loop(self) -> None:
        while not self._stop_requested:
            disconnect_reason = "connection_closed"
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                disconnect_reason = str(exc) or type(exc).__name__
                self._log_warning(f"avatar_state subscriber loop failed: {exc}")
            finally:
                ws = self._ws
                self._ws = None
                if ws is not None:
                    with contextlib.suppress(Exception):
                        await ws.close()
                await self._handle_disconnect(disconnect_reason)
            if not self._stop_requested:
                await asyncio.sleep(1.0)

    async def _connect_once(self) -> None:
        if not self.source_url:
            self._log_warning("avatar_state subscriber source_url is empty")
            return
        if self._session is None:
            timeout = ClientTimeout(total=None, connect=10.0)
            self._session = ClientSession(timeout=timeout)
        self._log_info(
            "Connecting embodied avatar_state subscriber: url=%s session=%s token=%s",
            self.source_url,
            self.source_session_id or "<all>",
            _mask_secret(self.source_token),
        )
        self._ws = await self._session.ws_connect(_append_query_token(self.source_url, self.source_token))
        await self._ws.send_str(
            json.dumps(
                {
                    "op": "subscribe",
                    "id": uuid4().hex,
                    "domain": AVATAR_STATE_DOMAIN,
                    "topic": AVATAR_STATE_TOPIC,
                },
                ensure_ascii=False,
            )
        )
        async for message in self._ws:
            if WSMsgType is not None and message.type != WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            if not isinstance(payload, Mapping):
                continue
            operation = str(payload.get("op") or "")
            if operation == "response":
                if not bool(payload.get("ok")):
                    error = payload.get("error")
                    raise RuntimeError(f"avatar_state subscribe failed: {error or payload}")
                self._log_info("Embodied avatar_state subscription acknowledged")
                await self._handle_connected()
                continue
            if operation != "event":
                continue
            if str(payload.get("domain") or "") != AVATAR_STATE_DOMAIN:
                continue
            if str(payload.get("topic") or "") != AVATAR_STATE_TOPIC:
                continue
            event_name = str(payload.get("event") or "")
            data = payload.get("data")
            if not isinstance(data, Mapping):
                continue
            if self.source_session_id and str(data.get("session_id") or "").strip() != self.source_session_id:
                continue
            if event_name == "snapshot":
                self._log_event(data, event_name)
                await self.on_snapshot(EmbodiedStateSnapshot.from_payload(data))
            elif event_name == "reset":
                self._log_info("Embodied avatar_state reset received")
                await self.on_reset()

    async def _handle_connected(self) -> None:
        if self._connected:
            return
        self._connected = True
        await _maybe_await(self.on_connected() if self.on_connected is not None else None)

    async def _handle_disconnect(self, reason: str) -> None:
        if not self._connected:
            return
        self._connected = False
        await _maybe_await(self.on_disconnected(reason) if self.on_disconnected is not None else None)

    def _log_warning(self, message: str) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(message)

    def _log_info(self, message: str, *args: Any) -> None:
        if self.logger is not None and hasattr(self.logger, "info"):
            formatted = message % args if args else message
            self.logger.info(formatted)

    def _log_event(self, data: Mapping[str, Any], event_name: str) -> None:
        now = time.monotonic()
        self._event_log_count += 1
        if self._event_log_count <= 3 or now - self._last_event_log_monotonic >= 2.0:
            self._log_info(
                "Embodied avatar_state %s seq=%s state=%s session=%s",
                event_name,
                data.get("seq"),
                data.get("agent_state"),
                data.get("session_id"),
            )
            self._last_event_log_monotonic = now


class EmbodiedLive2DRuntime:
    """Manage embodied subscription lifecycle around a controller."""

    def __init__(
        self,
        *,
        controller: Live2DController,
        source_url: str,
        source_token: str,
        source_session_id: str,
        fallback_to_legacy: bool,
        mouse_follow_enabled: bool = False,
        mouse_follow_poll_interval_ms: int = 12,
        mouse_follow_smoothing_ms: int = 45,
        mouse_follow_return_after_sec: float = 1.2,
        mouse_follow_cooldown_sec: float = 0.45,
        mouse_follow_eye_gain: float = 0.55,
        mouse_follow_head_gain: float = 0.28,
        mouse_follow_body_gain: float = 0.12,
        blink_enabled: bool = False,
        blink_interval_min_sec: float = 2.8,
        blink_interval_max_sec: float = 5.5,
        blink_double_blink_chance: float = 0.03,
        blink_close_ms: int = 60,
        blink_hold_ms: int = 28,
        blink_open_ms: int = 110,
        blink_double_blink_gap_ms: int = 140,
        wink_enabled: bool = False,
        wink_close_ms: int = 65,
        wink_hold_ms: int = 90,
        wink_open_ms: int = 110,
        wink_request_cooldown_sec: float = 1.8,
        wink_non_target_eye_drop: float = 0.08,
        logger: Any = None,
    ) -> None:
        self.controller = controller
        self.fallback_to_legacy = bool(fallback_to_legacy)
        self.disabled_reason = ""
        self._subscriber_connected = False
        self._mouse_follow_task: asyncio.Task[None] | None = None
        self._mouse_follow_sync_task: asyncio.Task[None] | None = None
        self._speech_envelope_tasks: set[asyncio.Task[None]] = set()
        self._mouse_follow_sync_queue: deque[bool] = deque()
        self._mouse_follow_sync_interval_sec = max(0.001, float(mouse_follow_poll_interval_ms) / 1000.0)
        self.driver = EmbodiedParamDriver(
            controller=controller,
            logger=logger,
            fallback_to_legacy=fallback_to_legacy,
            on_disable=self._handle_disable,
            mouse_follow_enabled=mouse_follow_enabled,
            mouse_follow_smoothing_ms=mouse_follow_smoothing_ms,
            mouse_follow_return_after_sec=mouse_follow_return_after_sec,
            mouse_follow_cooldown_sec=mouse_follow_cooldown_sec,
            mouse_follow_eye_gain=mouse_follow_eye_gain,
            mouse_follow_head_gain=mouse_follow_head_gain,
            mouse_follow_body_gain=mouse_follow_body_gain,
            blink_enabled=blink_enabled,
            blink_interval_min_sec=blink_interval_min_sec,
            blink_interval_max_sec=blink_interval_max_sec,
            blink_double_blink_chance=blink_double_blink_chance,
            blink_close_ms=blink_close_ms,
            blink_hold_ms=blink_hold_ms,
            blink_open_ms=blink_open_ms,
            blink_double_blink_gap_ms=blink_double_blink_gap_ms,
            wink_enabled=wink_enabled,
            wink_close_ms=wink_close_ms,
            wink_hold_ms=wink_hold_ms,
            wink_open_ms=wink_open_ms,
            wink_request_cooldown_sec=wink_request_cooldown_sec,
            wink_non_target_eye_drop=wink_non_target_eye_drop,
        )
        self.mouse_follow_runtime: GlobalMouseFollowRuntime = GlobalMouseFollowRuntime(
            poll_interval_ms=mouse_follow_poll_interval_ms,
        )
        self.subscriber = EmbodiedStateSubscriber(
            source_url=source_url,
            source_token=source_token,
            source_session_id=source_session_id,
            on_snapshot=self._handle_snapshot,
            on_reset=self.driver.handle_reset,
            on_connected=self._handle_subscriber_connected,
            on_disconnected=self._handle_subscriber_disconnected,
            logger=logger,
        )
        self._mouse_follow_runtime_running = False
        self._mouse_follow_runtime_started_once = False
        self._mouse_follow_sync_closed = False
        self._mouse_follow_sync_lock = asyncio.Lock()
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False

    async def start(self) -> None:
        self._runtime_loop = asyncio.get_running_loop()
        self._mouse_follow_sync_closed = False
        self._stopping = False
        self.controller.set_speech_envelope_sink(self._handle_speech_envelope)
        await self.controller.set_embodied_mode(False)
        await self.driver.start()
        if self.driver.mouse_follow_enabled:
            await self.mouse_follow_runtime.start()
            self._mouse_follow_runtime_running = True
            self._mouse_follow_runtime_started_once = True
            self._start_mouse_follow_task()
        await self.subscriber.start()

    async def stop(self) -> None:
        self._stopping = True
        self._mouse_follow_sync_closed = True
        self._subscriber_connected = False
        self.controller.set_speech_envelope_sink(None)
        await self.subscriber.stop()
        await self._cancel_speech_envelope_tasks()
        await self._stop_mouse_follow_task()
        async with self._mouse_follow_sync_lock:
            self._mouse_follow_sync_queue.clear()
            if self._mouse_follow_runtime_running:
                await self.mouse_follow_runtime.stop()
                self._mouse_follow_runtime_running = False
            elif not self._mouse_follow_runtime_started_once:
                await self.mouse_follow_runtime.stop()
            self._mouse_follow_runtime_started_once = False
        await self.driver.stop()
        self._runtime_loop = None
        await self.controller.set_embodied_mode(False)

    def set_mouse_follow_enabled(self, enabled: bool) -> None:
        if self._mouse_follow_sync_closed:
            self.driver.set_mouse_follow_enabled(enabled)
            return
        self.driver.set_mouse_follow_enabled(enabled)
        self._mouse_follow_sync_queue.append(bool(enabled))
        self._schedule_mouse_follow_sync()

    def debug_status(self) -> dict[str, Any]:
        latest_snapshot = self.driver.debug_latest_snapshot()
        return {
            "subscriber_connected": self._subscriber_connected,
            "disabled_reason": self.disabled_reason,
            "mouse_follow_enabled": self.driver.mouse_follow_enabled,
            "source_session_id": self.subscriber.source_session_id,
            "source_url": self.subscriber.source_url,
            "has_snapshot": latest_snapshot is not None,
            "latest_session_id": latest_snapshot.session_id if latest_snapshot is not None else "",
            "latest_targets": self.driver.debug_latest_targets(),
        }

    async def debug_reset_pose(self) -> dict[str, Any]:
        return await self.driver.handle_reset()

    async def debug_apply_emotion(self, *, emotion_intent: str, emotion_gain: float = 1.0) -> dict[str, Any]:
        self.driver.update_mouse_follow_snapshot(self.mouse_follow_runtime.latest_snapshot())
        return await self.driver.debug_apply_emotion_preview(
            emotion_intent=emotion_intent,
            emotion_gain=emotion_gain,
        )

    async def request_wink(self, *, side: str = "") -> bool:
        return self.driver.request_wink(side=side, now=time.monotonic())

    async def request_special_move(
        self,
        *,
        action: str,
        move: str,
        duration_sec: float,
    ) -> bool:
        now = time.monotonic()
        accepted = self.driver.request_special_move(
            action=action,
            move=move,
            duration_sec=duration_sec,
            now=now,
        )
        if not accepted:
            return False
        result = await self.driver.dispatch_special_move(now=now)
        return bool(result.get("success"))

    async def _handle_snapshot(self, snapshot: EmbodiedStateSnapshot) -> dict[str, Any]:
        self.driver.update_mouse_follow_snapshot(self.mouse_follow_runtime.latest_snapshot())
        return await self.driver.handle_snapshot(snapshot)

    async def _handle_speech_envelope(
        self,
        *,
        text: str,
        audio_timeline: Mapping[str, Any],
        emotion_gain: float,
        timeline_id: str,
        emotion_intent: str = "",
    ) -> None:
        if self._stopping or not self._subscriber_connected:
            return
        self.driver.update_mouse_follow_snapshot(self.mouse_follow_runtime.latest_snapshot())
        task = asyncio.create_task(
            self.driver.handle_speech_envelope(
                audio_timeline,
                text=text,
                emotion_intent=emotion_intent,
                emotion_gain=emotion_gain,
                timeline_id=timeline_id,
            ),
            name="live2d_adaptive.embodied_speech_motion",
        )
        self._speech_envelope_tasks.add(task)
        task.add_done_callback(self._speech_envelope_tasks.discard)
        await task

    async def _cancel_speech_envelope_tasks(self) -> None:
        tasks = list(self._speech_envelope_tasks)
        self._speech_envelope_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _schedule_mouse_follow_sync(self) -> None:
        if self._mouse_follow_sync_closed:
            return
        loop = self._runtime_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._ensure_mouse_follow_sync_task)

    def _ensure_mouse_follow_sync_task(self) -> None:
        if self._mouse_follow_sync_closed:
            return
        if self._mouse_follow_sync_task is not None and not self._mouse_follow_sync_task.done():
            return
        self._mouse_follow_sync_task = asyncio.create_task(
            self._mouse_follow_sync_loop(),
            name="live2d_adaptive.embodied_mouse_follow_sync",
        )

    def _start_mouse_follow_task(self) -> None:
        if self._mouse_follow_task is not None and not self._mouse_follow_task.done():
            return
        self._mouse_follow_task = asyncio.create_task(
            self._mouse_follow_loop(),
            name="live2d_adaptive.embodied_mouse_follow",
        )

    async def _stop_mouse_follow_task(self) -> None:
        task = self._mouse_follow_task
        self._mouse_follow_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _mouse_follow_loop(self) -> None:
        while True:
            self.driver.update_mouse_follow_snapshot(self.mouse_follow_runtime.latest_snapshot())
            await asyncio.sleep(self._mouse_follow_sync_interval_sec)

    async def _mouse_follow_sync_loop(self) -> None:
        try:
            while True:
                try:
                    desired_enabled = self._mouse_follow_sync_queue.popleft()
                except IndexError:
                    return
                async with self._mouse_follow_sync_lock:
                    if self._mouse_follow_sync_closed:
                        self._mouse_follow_sync_queue.clear()
                        return
                    if desired_enabled:
                        if not self._mouse_follow_runtime_running:
                            await self.mouse_follow_runtime.start()
                            self._mouse_follow_runtime_running = True
                            self._mouse_follow_runtime_started_once = True
                        if self._mouse_follow_task is None or self._mouse_follow_task.done():
                            self._start_mouse_follow_task()
                        continue
                    if self._mouse_follow_task is not None:
                        await self._stop_mouse_follow_task()
                    if self._mouse_follow_runtime_running:
                        await self.mouse_follow_runtime.stop()
                        self._mouse_follow_runtime_running = False
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_warning("Live2D embodied mouse follow sync failed: %s", exc)
        finally:
            if self._mouse_follow_sync_task is asyncio.current_task():
                self._mouse_follow_sync_task = None

    async def _handle_subscriber_connected(self) -> None:
        self._subscriber_connected = True
        self.driver.update_mouse_follow_snapshot(self.mouse_follow_runtime.latest_snapshot())
        await self.controller.set_embodied_mode(True)

    async def _handle_subscriber_disconnected(self, _reason: str) -> None:
        if not self._subscriber_connected:
            return
        self._subscriber_connected = False

    async def _handle_disable(self, reason: str) -> None:
        self.disabled_reason = reason
        self._subscriber_connected = False
        await self.subscriber.stop()
        if self.fallback_to_legacy:
            await self.controller.set_embodied_mode(False)


async def _maybe_await(result: Awaitable[None] | None) -> None:
    if result is None:
        return
    await result


def _build_debug_snapshot() -> EmbodiedStateSnapshot:
    return EmbodiedStateSnapshot(
        session_id="live2d-debug",
        ts=0.0,
        seq=0,
        agent_state="wait",
        valence=0.08,
        arousal=0.08,
        attention=0.45,
        cognitive_load=0.12,
        confidence=0.55,
        social_approach=0.18,
        energy=0.35,
        gaze_x=0.0,
        gaze_y=0.0,
    )


def _append_query_token(url: str, token: str) -> str:
    if not token:
        return url
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["token"] = token
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _clamp_signed(value: float) -> float:
    return min(1.0, max(-1.0, float(value)))


def _clamp_unit(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _smoothstep_unit(value: float) -> float:
    clamped = _clamp_unit(value)
    return clamped * clamped * (3.0 - (2.0 * clamped))


def _signed_direction(value: float, *, fallback: float) -> float:
    if value > 0.0:
        return 1.0
    if value < 0.0:
        return -1.0
    return 1.0 if fallback >= 0.0 else -1.0


def _mask_secret(secret: str) -> str:
    value = str(secret or "").strip()
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"

"""Synchronized text/audio speech timeline generation for Live2D."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence
from uuid import uuid4

from ..constants import LIVE2D_BRIDGE_SOURCE
from .semantic_mapper import Live2DSemanticMapper

try:
    from pypinyin import Style, lazy_pinyin

    PYPINYIN_AVAILABLE = True
except ImportError:  # pragma: no cover - optional accuracy dependency
    Style = None  # type: ignore[assignment]
    lazy_pinyin = None  # type: ignore[assignment]
    PYPINYIN_AVAILABLE = False


@dataclass(frozen=True)
class MouthCue:
    """A rough viseme cue inferred from text."""

    text: str
    mouth_open: float
    mouth_form: float
    weight: float = 1.0
    pause_ms: int = 0
    silence: bool = False


@dataclass(frozen=True)
class TimelineEvent:
    """A single bridge event in a speech timeline."""

    type: str
    timeline_id: str
    offset_ms: int
    payload: dict[str, Any]

    def to_bridge_payload(self) -> dict[str, Any]:
        """Convert the event to a JSON bridge payload."""

        return {
            "type": self.type,
            "timeline_id": self.timeline_id,
            "offset_ms": self.offset_ms,
            "source": LIVE2D_BRIDGE_SOURCE,
            **self.payload,
        }


@dataclass(frozen=True)
class SpeechTimeline:
    """A complete synchronized Live2D speech timeline."""

    timeline_id: str
    text: str
    estimated_duration_ms: int
    release_ms: int
    events: list[TimelineEvent]


DEFAULT_MOUTH_VOWEL_SHAPES: dict[str, tuple[float, float]] = {
    "a": (1.0, 1.0),
    "e": (0.6, 0.6),
    "i": (0.2, 0.5),
    "o": (1.0, 0.0),
    "u": (0.3, 0.2),
}


class SpeechTimelineBuilder:
    """Build Live2D events synchronized with text or future TTS metadata."""

    def __init__(
        self,
        mapper: Live2DSemanticMapper,
        *,
        chars_per_second: float = 7.5,
        prepare_ms: int = 180,
        release_ms: int = 600,
        mouth_update_interval_ms: int = 80,
        mouth_closed_value: float = 0.0,
        mouth_open_threshold: float = 0.08,
        mouth_open_gamma: float = 1.45,
        mouth_open_gain: float = 1.15,
        mouth_open_max: float = 0.88,
        mouth_sync_mode: str = "hybrid",
        mouth_amplitude_mix: float = 0.65,
        mouth_viseme_lead_ms: int = 40,
        mouth_open_smoothing: float = 0.55,
        mouth_open_attack_smoothing: float | None = None,
        mouth_open_release_smoothing: float | None = None,
        mouth_open_min_delta: float = 0.04,
        mouth_form_smoothing: float = 0.40,
        mouth_form_min_delta: float = 0.03,
        mouth_keyframe_transition_ms: int = 100,
        mouth_vowel_shapes: Mapping[str, Any] | None = None,
        speech_sway_enabled: bool = True,
        speech_sway_intensity: float = 0.45,
        speech_sway_update_interval_ms: int = 160,
    ) -> None:
        self.mapper = mapper
        self.chars_per_second = max(1.0, float(chars_per_second))
        self.prepare_ms = max(1, int(prepare_ms))
        self.release_ms = max(1, int(release_ms))
        self.mouth_update_interval_ms = max(20, int(mouth_update_interval_ms))
        self.mouth_closed_value = min(1.0, max(-1.0, float(mouth_closed_value)))
        self.mouth_open_threshold = min(0.95, max(0.0, float(mouth_open_threshold)))
        self.mouth_open_gamma = min(4.0, max(0.2, float(mouth_open_gamma)))
        self.mouth_open_gain = min(3.0, max(0.1, float(mouth_open_gain)))
        self.mouth_open_max = min(1.0, max(0.0, float(mouth_open_max)))
        self.mouth_sync_mode = _normalize_mouth_sync_mode(mouth_sync_mode)
        self.mouth_amplitude_mix = min(1.0, max(0.0, float(mouth_amplitude_mix)))
        self.mouth_viseme_lead_ms = max(0, int(mouth_viseme_lead_ms))
        self.mouth_open_smoothing = min(0.95, max(0.0, float(mouth_open_smoothing)))
        self.mouth_open_attack_smoothing = _normalize_smoothing(
            mouth_open_attack_smoothing,
            min(0.35, self.mouth_open_smoothing * 0.35),
        )
        self.mouth_open_release_smoothing = _normalize_smoothing(
            mouth_open_release_smoothing,
            self.mouth_open_smoothing,
        )
        self.mouth_open_min_delta = min(0.5, max(0.0, float(mouth_open_min_delta)))
        self.mouth_form_smoothing = min(0.95, max(0.0, float(mouth_form_smoothing)))
        self.mouth_form_min_delta = min(2.0, max(0.0, float(mouth_form_min_delta)))
        self.mouth_keyframe_transition_ms = max(0, int(mouth_keyframe_transition_ms))
        self.mouth_vowel_shapes = _normalize_vowel_shapes(mouth_vowel_shapes)
        self.speech_sway_enabled = bool(speech_sway_enabled)
        self.speech_sway_intensity = min(1.0, max(0.0, float(speech_sway_intensity)))
        self.speech_sway_update_interval_ms = max(80, int(speech_sway_update_interval_ms))

    def build_reply_timeline(
        self,
        text: str,
        *,
        timeline_id: str | None = None,
        audio_timeline: Mapping[str, Any] | None = None,
        motion_intensity: float | None = None,
        prepare_ms_override: int | None = None,
    ) -> SpeechTimeline:
        """Build prepare/start/frame/end events for a reply."""

        normalized_text = str(text or "")
        timeline_id = timeline_id or uuid4().hex
        prepare_ms = self.prepare_ms if prepare_ms_override is None else max(0, int(prepare_ms_override))
        duration_ms = self._estimate_duration_ms(normalized_text, audio_timeline=audio_timeline)
        segments = split_text_segments(normalized_text)
        audio_ref = str(audio_timeline.get("audio_ref") or "").strip() if isinstance(audio_timeline, Mapping) else ""
        provider = str(audio_timeline.get("provider") or "").strip() if isinstance(audio_timeline, Mapping) else ""
        events = [
            TimelineEvent(
                type="bot_reply.prepare",
                timeline_id=timeline_id,
                offset_ms=0,
                payload={
                    "text": normalized_text,
                    "segments": segments,
                    "estimated_duration_ms": duration_ms,
                    "prepare_ms": prepare_ms,
                    **({"audio_ref": audio_ref} if audio_ref else {}),
                    **({"provider": provider} if provider else {}),
                },
            ),
            TimelineEvent(
                type="bot_reply.start",
                timeline_id=timeline_id,
                offset_ms=prepare_ms,
                payload={
                    "text": normalized_text,
                    "estimated_duration_ms": duration_ms,
                    **({"audio_ref": audio_ref} if audio_ref else {}),
                    **({"provider": provider} if provider else {}),
                },
            ),
        ]
        events.extend(
            self._with_prepare_offset(
                self._build_speech_sway_frames(timeline_id, duration_ms, motion_intensity),
                prepare_ms,
            )
        )
        events.append(
            TimelineEvent(
                type="bot_reply.end",
                timeline_id=timeline_id,
                offset_ms=prepare_ms + duration_ms,
                payload={"release_ms": self.release_ms},
            )
        )
        reset_prefixes = ["head.", "body.", "eye.", "face."]
        reset_payloads = self.mapper.reset_payloads(reset_prefixes)
        if reset_payloads:
            events.append(
                TimelineEvent(
                    type="live2d.parameters",
                    timeline_id=timeline_id,
                    offset_ms=prepare_ms + duration_ms + self.release_ms,
                    payload={
                        "parameters": reset_payloads,
                        "duration_ms": self.release_ms,
                        "easing": "easeOutQuad",
                        "blend": "replace",
                        "priority": 1,
                    },
                )
            )
        return SpeechTimeline(
            timeline_id=timeline_id,
            text=normalized_text,
            estimated_duration_ms=duration_ms,
            release_ms=self.release_ms,
            events=events,
        )

    def _with_prepare_offset(self, events: list[TimelineEvent], prepare_ms: int) -> list[TimelineEvent]:
        delta_ms = self.prepare_ms - max(0, int(prepare_ms))
        if delta_ms == 0:
            return events
        return [replace(event, offset_ms=max(0, event.offset_ms - delta_ms)) for event in events]

    def _estimate_duration_ms(self, text: str, *, audio_timeline: Mapping[str, Any] | None = None) -> int:
        if audio_timeline:
            duration = _optional_int(audio_timeline.get("audio_duration_ms"))
            if duration and duration > 0:
                return duration
        text_duration = int(max(500.0, len(text.strip()) / self.chars_per_second * 1000.0))
        punctuation_pause = sum(_punctuation_pause(char) for char in text)
        return text_duration + punctuation_pause

    def _build_mouth_frames(
        self,
        timeline_id: str,
        text: str,
        duration_ms: int,
        audio_timeline: Mapping[str, Any] | None,
    ) -> list[TimelineEvent]:
        if self.mouth_sync_mode == "vts_native":
            return []
        amplitudes = audio_timeline.get("amplitudes") if audio_timeline else None
        visemes = audio_timeline.get("visemes") if audio_timeline else None
        if self.mouth_sync_mode == "amplitude" and isinstance(amplitudes, list):
            return self._build_amplitude_frames(timeline_id, amplitudes)
        if self.mouth_sync_mode == "hybrid" and isinstance(visemes, list):
            return self._build_hybrid_viseme_amplitude_frames(
                timeline_id,
                visemes,
                amplitudes if isinstance(amplitudes, list) else [],
            )
        if self.mouth_sync_mode == "viseme" and isinstance(visemes, list):
            return self._build_viseme_frames(timeline_id, visemes)
        if self.mouth_sync_mode == "hybrid" and text.strip():
            return self._build_inferred_viseme_frames(
                timeline_id,
                text,
                duration_ms,
                amplitudes if isinstance(amplitudes, list) else [],
            )
        if self.mouth_sync_mode == "viseme" and text.strip():
            return self._build_inferred_viseme_frames(timeline_id, text, duration_ms, [])
        if isinstance(amplitudes, list):
            return self._build_amplitude_frames(timeline_id, amplitudes)
        return self._build_text_mouth_frames(timeline_id, text, duration_ms)

    def _build_text_mouth_frames(self, timeline_id: str, text: str, duration_ms: int) -> list[TimelineEvent]:
        frames: list[TimelineEvent] = []
        if not text.strip():
            return frames
        pause_marks = set(",，、.。;；:：")
        strong_marks = set("!?？！")
        frame_count = max(1, duration_ms // self.mouth_update_interval_ms)
        previous_open = self.mouth_closed_value
        previous_form = 0.0
        for frame_index in range(frame_count + 1):
            offset = self.prepare_ms + frame_index * self.mouth_update_interval_ms
            progress = min(1.0, frame_index / max(1, frame_count))
            text_index = min(len(text) - 1, int(progress * max(0, len(text) - 1)))
            char = text[text_index]
            open_value = _text_mouth_open_value(
                char,
                frame_index=frame_index,
                pause_marks=pause_marks,
                strong_marks=strong_marks,
                closed_value=self.mouth_closed_value,
                open_max=self.mouth_open_max,
            )
            open_value = self._stabilize_mouth_open(open_value, previous_open)
            previous_open = open_value
            form_value = 0.25 if char in strong_marks else 0.05
            form_value = self._stabilize_mouth_form(form_value, previous_form)
            previous_form = form_value
            open_payload = self.mapper.profile.resolve("mouth.open", open_value, weight=1.0)
            form_payload = self.mapper.profile.resolve("mouth.form", form_value, weight=0.75)
            parameters = [payload for payload in (open_payload, form_payload) if payload]
            if not parameters:
                continue
            frames.append(
                TimelineEvent(
                    type="live2d.timeline.frame",
                    timeline_id=timeline_id,
                    offset_ms=offset,
                    payload={"parameters": parameters},
                )
            )
        close_parameters = [
            payload
            for payload in (
                self.mapper.profile.resolve("mouth.open", self.mouth_closed_value, weight=1.0),
                self.mapper.profile.resolve("mouth.form", 0.0, weight=0.8),
            )
            if payload
        ]
        if close_parameters:
            frames.append(
                TimelineEvent(
                    type="live2d.timeline.frame",
                    timeline_id=timeline_id,
                    offset_ms=self.prepare_ms + duration_ms,
                    payload={"parameters": close_parameters},
                )
            )
        return frames

    def _build_inferred_viseme_frames(
        self,
        timeline_id: str,
        text: str,
        duration_ms: int,
        amplitudes: list[Any],
    ) -> list[TimelineEvent]:
        cues = self._infer_mouth_cues(text)
        if not cues:
            return self._build_amplitude_frames(timeline_id, amplitudes) if amplitudes else []
        spans = _allocate_cue_spans(cues, duration_ms)
        frames: list[TimelineEvent] = []
        frame_count = max(1, duration_ms // self.mouth_update_interval_ms)
        previous_open = self.mouth_closed_value
        previous_form = 0.0
        for frame_index in range(frame_count + 1):
            relative_offset = min(duration_ms, frame_index * self.mouth_update_interval_ms)
            look_offset = min(duration_ms, relative_offset + self.mouth_viseme_lead_ms)
            cue = _smoothed_cue_for_frame(
                spans,
                look_offset,
                min(duration_ms, look_offset + self.mouth_update_interval_ms),
                self.mouth_keyframe_transition_ms,
            )
            amplitude = _amplitude_at_offset(amplitudes, relative_offset)
            mouth_open = self._shape_hybrid_mouth_open(cue, amplitude if amplitudes else None)
            mouth_open = self._stabilize_mouth_open(mouth_open, previous_open)
            previous_open = mouth_open
            mouth_form = self._stabilize_mouth_form(cue.mouth_form, previous_form)
            previous_form = mouth_form
            parameters = [
                payload
                for payload in (
                    self.mapper.profile.resolve("mouth.open", mouth_open, weight=1.0),
                    self.mapper.profile.resolve("mouth.form", mouth_form, weight=0.82),
                )
                if payload
            ]
            if not parameters:
                continue
            frames.append(
                TimelineEvent(
                    type="live2d.timeline.frame",
                    timeline_id=timeline_id,
                    offset_ms=self.prepare_ms + relative_offset,
                    payload={"parameters": parameters},
                )
            )
        close_parameters = [
            payload
            for payload in (
                self.mapper.profile.resolve("mouth.open", self.mouth_closed_value, weight=1.0),
                self.mapper.profile.resolve("mouth.form", 0.0, weight=0.85),
            )
            if payload
        ]
        if close_parameters:
            frames.append(
                TimelineEvent(
                    type="live2d.timeline.frame",
                    timeline_id=timeline_id,
                    offset_ms=self.prepare_ms + duration_ms,
                    payload={"parameters": close_parameters},
                )
            )
        return frames

    def _build_speech_sway_frames(
        self,
        timeline_id: str,
        duration_ms: int,
        motion_intensity: float | None,
    ) -> list[TimelineEvent]:
        frames: list[TimelineEvent] = []
        if not self.speech_sway_enabled:
            return frames
        intensity = self.speech_sway_intensity if motion_intensity is None else float(motion_intensity)
        intensity = min(1.0, max(0.0, intensity))
        frame_count = max(1, duration_ms // self.speech_sway_update_interval_ms)
        for frame_index in range(frame_count + 1):
            offset = self.prepare_ms + frame_index * self.speech_sway_update_interval_ms
            progress = frame_index / max(1, frame_count)
            phase = progress * 6.283185307179586 * max(1.0, duration_ms / 1300.0)
            parameters = self.mapper.build_sway(phase, intensity=intensity, speaking=True)
            if not parameters:
                continue
            frames.append(
                TimelineEvent(
                    type="live2d.timeline.frame",
                    timeline_id=timeline_id,
                    offset_ms=offset,
                    payload={"parameters": parameters},
                )
            )
        return frames

    def _build_viseme_frames(self, timeline_id: str, visemes: list[Any]) -> list[TimelineEvent]:
        frames: list[TimelineEvent] = []
        previous_open = self.mouth_closed_value
        previous_form = 0.0
        for raw_viseme in visemes:
            if not isinstance(raw_viseme, Mapping):
                continue
            offset = max(0, int(raw_viseme.get("offset_ms") or 0))
            mouth_open = _optional_float(raw_viseme.get("mouth_open"), 0.0)
            mouth_form = _optional_float(raw_viseme.get("mouth_form"), 0.0)
            if bool(raw_viseme.get("silence", False)):
                mouth_open = self.mouth_closed_value
            mouth_open = self._shape_mouth_open(mouth_open)
            mouth_open = self._stabilize_mouth_open(mouth_open, previous_open)
            previous_open = mouth_open
            mouth_form = self._stabilize_mouth_form(mouth_form, previous_form)
            previous_form = mouth_form
            open_payload = self.mapper.profile.resolve("mouth.open", mouth_open, weight=1.0)
            form_payload = self.mapper.profile.resolve("mouth.form", mouth_form, weight=0.8)
            parameters = [payload for payload in (open_payload, form_payload) if payload]
            if parameters:
                frames.append(
                    TimelineEvent(
                        type="live2d.timeline.frame",
                        timeline_id=timeline_id,
                        offset_ms=self.prepare_ms + offset,
                        payload={"parameters": parameters},
                    )
                )
        return frames

    def _build_hybrid_viseme_amplitude_frames(
        self,
        timeline_id: str,
        visemes: list[Any],
        amplitudes: list[Any],
    ) -> list[TimelineEvent]:
        frames: list[TimelineEvent] = []
        previous_open = self.mouth_closed_value
        previous_form = 0.0
        for raw_viseme in visemes:
            if not isinstance(raw_viseme, Mapping):
                continue
            offset = max(0, int(raw_viseme.get("offset_ms") or 0))
            cue = MouthCue(
                text="",
                mouth_open=_optional_float(raw_viseme.get("mouth_open"), 0.0),
                mouth_form=_optional_float(raw_viseme.get("mouth_form"), 0.0),
                silence=bool(raw_viseme.get("silence", False)),
            )
            amplitude = _amplitude_at_offset(amplitudes, offset) if amplitudes else None
            mouth_open = self._shape_hybrid_mouth_open(cue, amplitude)
            mouth_open = self._stabilize_mouth_open(mouth_open, previous_open)
            previous_open = mouth_open
            mouth_form = self._stabilize_mouth_form(cue.mouth_form, previous_form)
            previous_form = mouth_form
            parameters = [
                payload
                for payload in (
                    self.mapper.profile.resolve("mouth.open", mouth_open, weight=1.0),
                    self.mapper.profile.resolve("mouth.form", mouth_form, weight=0.82),
                )
                if payload
            ]
            if parameters:
                frames.append(
                    TimelineEvent(
                        type="live2d.timeline.frame",
                        timeline_id=timeline_id,
                        offset_ms=self.prepare_ms + offset,
                        payload={"parameters": parameters},
                    )
                )
        return frames

    def _build_amplitude_frames(self, timeline_id: str, amplitudes: list[Any]) -> list[TimelineEvent]:
        frames: list[TimelineEvent] = []
        previous_open = self.mouth_closed_value
        for raw_amplitude in amplitudes:
            if not isinstance(raw_amplitude, Mapping):
                continue
            offset = max(0, int(raw_amplitude.get("offset_ms") or 0))
            value = _optional_float(raw_amplitude.get("value"), 0.0)
            mouth_open = self._shape_mouth_open(value)
            mouth_open = self._stabilize_mouth_open(mouth_open, previous_open)
            previous_open = mouth_open
            payload = self.mapper.profile.resolve("mouth.open", mouth_open, weight=1.0)
            if payload:
                frames.append(
                    TimelineEvent(
                        type="live2d.timeline.frame",
                        timeline_id=timeline_id,
                        offset_ms=self.prepare_ms + offset,
                        payload={"parameters": [payload]},
                    )
                )
        return frames

    def _shape_mouth_open(self, raw_value: float) -> float:
        """Apply a noise gate and response curve to speech mouth openness."""

        value = min(1.0, max(0.0, float(raw_value)))
        if value <= self.mouth_open_threshold:
            return self.mouth_closed_value
        normalized = (value - self.mouth_open_threshold) / max(0.001, 1.0 - self.mouth_open_threshold)
        curved = (normalized**self.mouth_open_gamma) * self.mouth_open_gain
        if curved <= 0.001:
            return self.mouth_closed_value
        return min(self.mouth_open_max, max(self.mouth_closed_value, curved))

    def _shape_hybrid_mouth_open(self, cue: MouthCue, amplitude: float | None) -> float:
        if cue.silence:
            return self.mouth_closed_value
        cue_open = min(self.mouth_open_max, max(self.mouth_closed_value, cue.mouth_open))
        if amplitude is None:
            return cue_open
        shaped_amplitude = self._shape_mouth_open(amplitude)
        if shaped_amplitude <= self.mouth_closed_value and amplitude <= self.mouth_open_threshold:
            return self.mouth_closed_value
        mixed = cue_open * (1.0 - self.mouth_amplitude_mix) + shaped_amplitude * self.mouth_amplitude_mix
        return min(self.mouth_open_max, max(self.mouth_closed_value, mixed))

    def _stabilize_mouth_open(self, target_value: float, previous_value: float) -> float:
        if target_value <= self.mouth_closed_value and previous_value <= self.mouth_open_min_delta:
            return self.mouth_closed_value
        if abs(target_value - previous_value) < self.mouth_open_min_delta:
            return previous_value
        smoothing = (
            self.mouth_open_attack_smoothing
            if target_value > previous_value
            else self.mouth_open_release_smoothing
        )
        smoothed = previous_value * smoothing + target_value * (1.0 - smoothing)
        if smoothed <= self.mouth_open_min_delta and target_value <= self.mouth_open_threshold:
            return self.mouth_closed_value
        return min(self.mouth_open_max, max(self.mouth_closed_value, smoothed))

    def _stabilize_mouth_form(self, target_value: float, previous_value: float) -> float:
        clamped_target = min(1.0, max(-2.0, float(target_value)))
        delta = abs(clamped_target - previous_value)
        if delta < self.mouth_form_min_delta:
            return previous_value
        if abs(clamped_target) <= self.mouth_form_min_delta and abs(previous_value) <= self.mouth_form_min_delta:
            return 0.0
        adaptive_scale = max(0.10, 1.0 - min(1.0, delta / 0.8))
        smoothing = self.mouth_form_smoothing * adaptive_scale
        smoothed = previous_value * smoothing + clamped_target * (1.0 - smoothing)
        if abs(smoothed) <= self.mouth_form_min_delta and abs(clamped_target) <= self.mouth_form_min_delta:
            return 0.0
        return min(1.0, max(-2.0, smoothed))

    def _infer_mouth_cues(self, text: str) -> list[MouthCue]:
        return _infer_mouth_cues(text, self.mouth_vowel_shapes)


CueSpan = tuple[int, int, MouthCue]


def build_text_viseme_timeline(
    text: str,
    duration_ms: int,
    *,
    frame_interval_ms: int = 80,
    mouth_vowel_shapes: Mapping[str, Any] | None = None,
    mouth_keyframe_transition_ms: int = 100,
    mouth_viseme_lead_ms: int = 0,
) -> list[dict[str, int | float | bool]]:
    """Build explicit viseme keyframes from text for a known audio duration."""

    normalized_duration_ms = max(0, int(duration_ms))
    if normalized_duration_ms <= 0 or not str(text or "").strip():
        return []
    interval_ms = max(20, int(frame_interval_ms))
    transition_ms = max(0, int(mouth_keyframe_transition_ms))
    lead_ms = max(0, int(mouth_viseme_lead_ms))
    cues = _infer_mouth_cues(str(text or ""), _normalize_vowel_shapes(mouth_vowel_shapes))
    if not cues:
        return []
    spans = _allocate_cue_spans(cues, normalized_duration_ms)
    visemes: list[dict[str, int | float | bool]] = []
    for span_start_ms, span_end_ms, _cue in spans:
        offset_ms = min(normalized_duration_ms, max(0, span_start_ms))
        end_ms = min(normalized_duration_ms, max(offset_ms + 1, span_end_ms))
        look_start_ms = min(normalized_duration_ms, offset_ms + lead_ms)
        look_end_ms = min(normalized_duration_ms, end_ms + lead_ms)
        cue = _smoothed_cue_for_frame(spans, look_start_ms, look_end_ms, transition_ms)
        mouth_open = 0.0 if cue.silence else min(1.0, max(0.0, float(cue.mouth_open)))
        mouth_form = min(1.0, max(-2.0, float(cue.mouth_form)))
        if visemes and _same_viseme(visemes[-1], mouth_open, mouth_form, bool(cue.silence)):
            visemes[-1]["duration_ms"] = max(1, end_ms - int(visemes[-1]["offset_ms"]))
            continue
        visemes.append(
            {
                "offset_ms": offset_ms,
                "duration_ms": max(1, end_ms - offset_ms),
                "mouth_open": mouth_open,
                "mouth_form": mouth_form,
                "silence": bool(cue.silence),
            }
        )
    if visemes and float(visemes[-1]["mouth_open"]) > 0.0 and int(visemes[-1]["offset_ms"]) < normalized_duration_ms:
        visemes.append(
            {
                "offset_ms": normalized_duration_ms,
                "duration_ms": max(1, interval_ms),
                "mouth_open": 0.0,
                "mouth_form": 0.0,
                "silence": True,
            }
        )
    return visemes


def _same_viseme(
    raw_viseme: Mapping[str, Any],
    mouth_open: float,
    mouth_form: float,
    silence: bool,
) -> bool:
    return (
        bool(raw_viseme.get("silence", False)) == silence
        and abs(_optional_float(raw_viseme.get("mouth_open"), 0.0) - mouth_open) < 0.001
        and abs(_optional_float(raw_viseme.get("mouth_form"), 0.0) - mouth_form) < 0.001
    )


def _text_mouth_open_value(
    char: str,
    *,
    frame_index: int,
    pause_marks: set[str],
    strong_marks: set[str],
    closed_value: float,
    open_max: float,
) -> float:
    if char.isspace():
        return closed_value
    phase = frame_index % 4
    if char in pause_marks:
        return closed_value if phase in {1, 3} else min(open_max, 0.08)
    if char in strong_marks:
        return min(open_max, 0.76) if phase in {0, 2} else min(open_max, 0.12)
    return min(open_max, 0.58) if phase in {0, 2} else closed_value


def _infer_mouth_cues(text: str, vowel_shapes: Mapping[str, tuple[float, float]]) -> list[MouthCue]:
    cues: list[MouthCue] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            cues.append(MouthCue(char, 0.0, 0.0, weight=0.3, pause_ms=80, silence=True))
            index += 1
            continue
        pause = _punctuation_pause(char)
        if pause:
            cues.append(MouthCue(char, 0.0, 0.0, weight=0.2, pause_ms=pause, silence=True))
            index += 1
            continue
        if _is_ascii_letter(char):
            start = index
            while index < len(text) and _is_ascii_letter(text[index]):
                index += 1
            cues.extend(_english_word_to_cues(text[start:index], vowel_shapes))
            continue
        cues.extend(_char_to_cues(char, vowel_shapes))
        index += 1
    return cues


def _char_to_cues(char: str, vowel_shapes: Mapping[str, tuple[float, float]]) -> list[MouthCue]:
    pinyin = _pinyin_for_char(char)
    if not pinyin:
        return [MouthCue(char, 0.5, 0.0, weight=1.0)]
    return _syllable_to_cues(pinyin, char, vowel_shapes)


def _english_word_to_cues(word: str, vowel_shapes: Mapping[str, tuple[float, float]]) -> list[MouthCue]:
    cues: list[MouthCue] = []
    index = 0
    lowered = word.lower()
    while index < len(lowered):
        letter = lowered[index]
        if letter in "aeiou":
            start = index
            while index < len(lowered) and lowered[index] in "aeiouy":
                index += 1
            vowel = lowered[start:index]
            open_value, form_value = _vowel_to_shape(vowel, vowel_shapes)
            cues.append(MouthCue(word[start:index], open_value, form_value, weight=max(0.6, len(vowel) * 0.7)))
            continue
        if letter in "bmp":
            cues.append(MouthCue(letter, 0.0, 0.0, weight=0.25, silence=True))
        elif letter in "fvw":
            cues.append(MouthCue(letter, 0.12, 0.45, weight=0.35))
        else:
            cues.append(MouthCue(letter, 0.14, -0.1, weight=0.35))
        index += 1
    return cues or [MouthCue(word, 0.45, 0.0, weight=1.0)]


def _syllable_to_cues(
    pinyin: str,
    source_text: str,
    vowel_shapes: Mapping[str, tuple[float, float]],
) -> list[MouthCue]:
    normalized = pinyin.lower().replace("\u00fc", "v")
    initial = _pinyin_initial(normalized)
    final = normalized[len(initial) :] or normalized
    open_value, form_value = _vowel_to_shape(final, vowel_shapes)
    cues: list[MouthCue] = []
    if initial in {"b", "p", "m"}:
        cues.append(MouthCue(initial, 0.0, 0.0, weight=0.22, silence=True))
    elif initial in {"f", "w"}:
        cues.append(MouthCue(initial, 0.14, 0.48, weight=0.25))
    elif initial:
        cues.append(MouthCue(initial, 0.12, -0.08, weight=0.20))
    cues.append(MouthCue(source_text, open_value, form_value, weight=max(0.75, len(final) * 0.45)))
    return cues


def _pinyin_for_char(char: str) -> str:
    if not _is_cjk(char) or not PYPINYIN_AVAILABLE or lazy_pinyin is None or Style is None:
        return ""
    result = lazy_pinyin(char, style=Style.NORMAL, errors="ignore")
    return str(result[0] if result else "").strip().lower()


def _pinyin_initial(pinyin: str) -> str:
    for initial in (
        "zh",
        "ch",
        "sh",
        "b",
        "p",
        "m",
        "f",
        "d",
        "t",
        "n",
        "l",
        "g",
        "k",
        "h",
        "j",
        "q",
        "x",
        "r",
        "z",
        "c",
        "s",
        "y",
        "w",
    ):
        if pinyin.startswith(initial):
            return initial
    return ""


def _vowel_to_shape(vowel: str, vowel_shapes: Mapping[str, tuple[float, float]]) -> tuple[float, float]:
    normalized = vowel.lower().replace("\u00fc", "v")
    if not normalized:
        return 0.38, 0.0
    vowel_key = _vowel_key_for(normalized)
    if vowel_key:
        return vowel_shapes[vowel_key]
    return 0.40, 0.0


def _vowel_key_for(normalized_vowel: str) -> str:
    if "a" in normalized_vowel:
        return "a"
    if "o" in normalized_vowel:
        return "o"
    if "u" in normalized_vowel or "v" in normalized_vowel:
        return "u"
    if "i" in normalized_vowel or "y" in normalized_vowel:
        return "i"
    if "e" in normalized_vowel:
        return "e"
    return ""


def _normalize_vowel_shapes(raw_shapes: Mapping[str, Any] | None) -> dict[str, tuple[float, float]]:
    shapes = dict(DEFAULT_MOUTH_VOWEL_SHAPES)
    for key, raw_value in dict(raw_shapes or {}).items():
        normalized_key = _vowel_key_for(str(key).strip().lower()) or str(key).strip().lower()
        if normalized_key not in shapes:
            continue
        parsed = _coerce_vowel_shape(raw_value)
        if parsed is not None:
            shapes[normalized_key] = parsed
    return shapes


def _coerce_vowel_shape(raw_value: Any) -> tuple[float, float] | None:
    if isinstance(raw_value, Mapping):
        open_value = _optional_float(raw_value.get("open"), DEFAULT_MOUTH_VOWEL_SHAPES["a"][0])
        form_value = _optional_float(raw_value.get("form"), 0.0)
        return min(1.0, max(0.0, open_value)), min(1.0, max(-2.0, form_value))
    if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)) and len(raw_value) >= 2:
        open_value = _optional_float(raw_value[0], DEFAULT_MOUTH_VOWEL_SHAPES["a"][0])
        form_value = _optional_float(raw_value[1], 0.0)
        return min(1.0, max(0.0, open_value)), min(1.0, max(-2.0, form_value))
    return None


def _allocate_cue_spans(cues: list[MouthCue], duration_ms: int) -> list[CueSpan]:
    if duration_ms <= 0:
        return []
    raw_pause_ms = sum(max(0, cue.pause_ms) for cue in cues if cue.silence)
    fixed_pause_ms = min(int(duration_ms * 0.45), raw_pause_ms)
    pause_scale = fixed_pause_ms / max(1, raw_pause_ms)
    weighted_cues = [cue for cue in cues if not cue.silence or cue.pause_ms <= 0]
    total_weight = sum(max(0.1, cue.weight) for cue in weighted_cues) or 1.0
    remaining_ms = max(1, duration_ms - fixed_pause_ms)
    spans: list[CueSpan] = []
    offset = 0
    for cue in cues:
        if cue.silence and cue.pause_ms > 0:
            cue_duration = max(35, min(int(cue.pause_ms * pause_scale), duration_ms - offset))
        else:
            cue_duration = max(35, int(remaining_ms * max(0.1, cue.weight) / total_weight))
        end = min(duration_ms, offset + cue_duration)
        spans.append((offset, end, cue))
        offset = end
        if offset >= duration_ms:
            break
    if not spans:
        spans.append((0, duration_ms, MouthCue("", 0.0, 0.0, silence=True)))
    elif spans[-1][1] < duration_ms:
        start, _, cue = spans[-1]
        spans[-1] = (start, duration_ms, cue)
    return spans


def _cue_at_offset(spans: list[CueSpan], offset_ms: int) -> MouthCue:
    for start, end, cue in spans:
        if start <= offset_ms < end:
            return cue
    return spans[-1][2] if spans else MouthCue("", 0.0, 0.0, silence=True)


def _cue_for_frame(spans: list[CueSpan], start_ms: int, end_ms: int) -> MouthCue:
    if not spans:
        return MouthCue("", 0.0, 0.0, silence=True)
    if end_ms <= start_ms:
        return _cue_at_offset(spans, start_ms)
    best_score = -1.0
    best_cue = _cue_at_offset(spans, start_ms)
    for start, end, cue in spans:
        overlap = max(0, min(end, end_ms) - max(start, start_ms))
        if overlap <= 0:
            continue
        score = overlap * (max(0.1, cue.weight) + cue.mouth_open * 0.8)
        if cue.silence:
            score *= 0.45
        if score > best_score:
            best_score = score
            best_cue = cue
    return best_cue


def _smoothed_cue_for_frame(
    spans: list[CueSpan],
    start_ms: int,
    end_ms: int,
    transition_ms: int,
) -> MouthCue:
    base_cue = _cue_for_frame(spans, start_ms, end_ms)
    if not spans or transition_ms <= 0:
        return base_cue
    center_ms = (start_ms + end_ms) // 2
    span_index = _cue_span_index_at_offset(spans, center_ms)
    if span_index < 0:
        return base_cue
    span_start, span_end, current_cue = spans[span_index]
    span_duration = max(1, span_end - span_start)
    blend_window = max(1, min(transition_ms, max(1, span_duration // 2)))
    result = current_cue
    if span_index > 0 and center_ms < span_start + blend_window:
        previous_cue = spans[span_index - 1][2]
        if not previous_cue.silence and not current_cue.silence:
            factor = _smoothstep((center_ms - span_start) / blend_window)
            result = _blend_cues(previous_cue, result, factor)
    if span_index + 1 < len(spans) and center_ms > span_end - blend_window:
        next_cue = spans[span_index + 1][2]
        if not next_cue.silence and not current_cue.silence:
            factor = _smoothstep((center_ms - (span_end - blend_window)) / blend_window)
            result = _blend_cues(result, next_cue, factor)
    return result


def _cue_span_index_at_offset(spans: list[CueSpan], offset_ms: int) -> int:
    for index, (start, end, _) in enumerate(spans):
        if start <= offset_ms < end:
            return index
    return len(spans) - 1 if spans else -1


def _blend_cues(left: MouthCue, right: MouthCue, factor: float) -> MouthCue:
    mix = min(1.0, max(0.0, float(factor)))
    inverse = 1.0 - mix
    return MouthCue(
        text=right.text if mix >= 0.5 else left.text,
        mouth_open=left.mouth_open * inverse + right.mouth_open * mix,
        mouth_form=left.mouth_form * inverse + right.mouth_form * mix,
        weight=left.weight * inverse + right.weight * mix,
        pause_ms=int(left.pause_ms * inverse + right.pause_ms * mix),
        silence=left.silence and right.silence,
    )


def _smoothstep(value: float) -> float:
    clamped = min(1.0, max(0.0, float(value)))
    return clamped * clamped * (3.0 - 2.0 * clamped)


def _amplitude_at_offset(amplitudes: list[Any], offset_ms: int) -> float:
    best_offset = -1
    best_value = 0.0
    for raw_amplitude in amplitudes:
        if not isinstance(raw_amplitude, Mapping):
            continue
        current_offset = max(0, int(raw_amplitude.get("offset_ms") or 0))
        if current_offset <= offset_ms and current_offset >= best_offset:
            best_offset = current_offset
            best_value = _optional_float(raw_amplitude.get("value"), 0.0)
    return best_value


def _normalize_mouth_sync_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower().replace("-", "_")
    if normalized in {"vts_native", "vts", "native", "native_vts", "none", "off", "disabled"}:
        return "vts_native"
    if normalized in {"hybrid", "hybrid_viseme", "viseme_hybrid"}:
        return "hybrid"
    if normalized in {"viseme", "text_viseme", "text"}:
        return "viseme"
    if normalized in {"amplitude", "audio", "rms"}:
        return "amplitude"
    return "vts_native"


def _normalize_smoothing(value: float | None, default: float) -> float:
    if value is None:
        parsed = default
    else:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
    return min(0.95, max(0.0, parsed))


def _is_ascii_letter(char: str) -> bool:
    return ("a" <= char <= "z") or ("A" <= char <= "Z")


def _is_cjk(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def split_text_segments(text: str) -> list[str]:
    """Split a reply into simple speech segments."""

    segments: list[str] = []
    current: list[str] = []
    for char in text:
        current.append(char)
        if char in "。！？!?":
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
    tail = "".join(current).strip()
    if tail:
        segments.append(tail)
    return segments or ([text.strip()] if text.strip() else [])


def _punctuation_pause(char: str) -> int:
    if char in ",，":
        return 120
    if char in ".。!！?？;；":
        return 250
    return 0


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

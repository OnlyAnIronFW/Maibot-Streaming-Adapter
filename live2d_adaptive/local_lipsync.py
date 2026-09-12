"""Plugin-local lip sync clip generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .profile import ParameterProfile

try:
    from pypinyin import Style, lazy_pinyin

    PYPINYIN_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    Style = None  # type: ignore[assignment]
    lazy_pinyin = None  # type: ignore[assignment]
    PYPINYIN_AVAILABLE = False


@dataclass(frozen=True)
class LipCue:
    label: str
    mouth_open: float
    mouth_form: float
    weight: float
    pause_ms: int = 0
    silence: bool = False


CueSpan = tuple[int, int, LipCue]

DEFAULT_VOWEL_SHAPES: dict[str, tuple[float, float]] = {
    "a": (1.0, 1.0),
    "e": (0.62, 0.55),
    "i": (0.24, 0.52),
    "o": (0.92, 0.04),
    "u": (0.36, 0.18),
}


class PluginLocalLipSyncEngine:
    """Generate bridge-side mouth clips from TTS audio and lightweight phonetic inference."""

    def __init__(
        self,
        profile: ParameterProfile,
        *,
        update_interval_ms: int = 50,
        mouth_closed_value: float = 0.0,
        mouth_closed_form_value: float | None = None,
        mouth_open_threshold: float = 0.035,
        mouth_open_gamma: float = 0.75,
        mouth_open_gain: float = 2.15,
        mouth_open_max: float = 1.0,
        mouth_amplitude_mix: float = 0.35,
        mouth_viseme_lead_ms: int = 0,
        mouth_open_attack_smoothing: float = 0.32,
        mouth_open_release_smoothing: float = 0.72,
        mouth_open_min_delta: float = 0.03,
        mouth_form_smoothing: float = 0.45,
        mouth_form_min_delta: float = 0.04,
        mouth_keyframe_transition_ms: int = 110,
    ) -> None:
        self.profile = profile
        self.update_interval_ms = max(20, int(update_interval_ms))
        self.mouth_tail_settle_delay_ms = max(self.update_interval_ms, 60)
        self.mouth_closed_value = min(1.0, max(-1.0, float(mouth_closed_value)))
        self.mouth_closed_form_value = _resolve_closed_mouth_form_value(profile, mouth_closed_form_value)
        self.mouth_open_threshold = min(0.95, max(0.0, float(mouth_open_threshold)))
        self.mouth_open_gamma = min(4.0, max(0.2, float(mouth_open_gamma)))
        self.mouth_open_gain = min(3.0, max(0.1, float(mouth_open_gain)))
        self.mouth_open_max = min(1.0, max(0.0, float(mouth_open_max)))
        self.mouth_amplitude_mix = min(1.0, max(0.0, float(mouth_amplitude_mix)))
        self.mouth_viseme_lead_ms = max(0, int(mouth_viseme_lead_ms))
        self.mouth_open_attack_smoothing = min(0.95, max(0.0, float(mouth_open_attack_smoothing)))
        self.mouth_open_release_smoothing = min(0.95, max(0.0, float(mouth_open_release_smoothing)))
        self.mouth_open_min_delta = min(0.5, max(0.0, float(mouth_open_min_delta)))
        self.mouth_form_smoothing = min(0.95, max(0.0, float(mouth_form_smoothing)))
        self.mouth_form_min_delta = min(2.0, max(0.0, float(mouth_form_min_delta)))
        self.mouth_keyframe_transition_ms = max(0, int(mouth_keyframe_transition_ms))

    def build_timeline_frames(
        self,
        *,
        timeline_id: str,
        text: str,
        duration_ms: int,
        audio_timeline: Mapping[str, Any] | None,
        prepare_ms: int = 0,
    ) -> list[dict[str, Any]]:
        normalized_duration_ms = max(0, int(duration_ms))
        if normalized_duration_ms <= 0:
            return []
        normalized_text = str(text or "").strip()
        amplitudes = _normalized_amplitudes(audio_timeline)
        cues = _build_cues(normalized_text)
        cue_spans = _allocate_cue_spans(cues, normalized_duration_ms) if cues else []
        frame_offsets = _frame_offsets(normalized_duration_ms, self.update_interval_ms)

        frames: list[dict[str, Any]] = []
        previous_open = self.mouth_closed_value
        previous_form = self.mouth_closed_form_value
        for offset_ms in frame_offsets:
            cue_offset_ms = min(normalized_duration_ms, offset_ms + self.mouth_viseme_lead_ms)
            cue = _cue_for_offset(cue_spans, cue_offset_ms, self.mouth_keyframe_transition_ms)
            amplitude = _amplitude_at_offset(amplitudes, offset_ms)
            target_open = self._resolve_open(cue, amplitude)
            target_form = self._resolve_form(cue, amplitude)
            mouth_open = self._stabilize_open(target_open, previous_open)
            mouth_form = self._stabilize_form(target_form, previous_form)
            previous_open = mouth_open
            previous_form = mouth_form
            parameters = self._resolve_mouth_parameters(mouth_open, mouth_form)
            if not parameters:
                continue
            frames.append(
                {
                    "type": "live2d.timeline.frame",
                    "timeline_id": timeline_id,
                    "offset_ms": max(0, int(prepare_ms)) + offset_ms,
                    "parameters": parameters,
                    "purpose": "lipsync",
                }
            )

        close_parameters = self._resolve_mouth_parameters(self.mouth_closed_value, previous_form)
        if close_parameters:
            frames.append(
                {
                    "type": "live2d.timeline.frame",
                    "timeline_id": timeline_id,
                    "offset_ms": max(0, int(prepare_ms)) + normalized_duration_ms,
                    "parameters": close_parameters,
                    "purpose": "lipsync",
                }
            )
        if abs(previous_form - self.mouth_closed_form_value) > self.mouth_form_min_delta:
            settle_parameters = self._resolve_mouth_parameters(self.mouth_closed_value, self.mouth_closed_form_value)
            if settle_parameters:
                frames.append(
                    {
                        "type": "live2d.timeline.frame",
                        "timeline_id": timeline_id,
                        "offset_ms": max(0, int(prepare_ms)) + normalized_duration_ms + self.mouth_tail_settle_delay_ms,
                        "parameters": settle_parameters,
                        "purpose": "lipsync",
                    }
                )
        return frames

    def build_idle_parameters(self) -> list[dict[str, Any]]:
        """Return the idle closed-mouth baseline used outside active speech."""

        return self._resolve_mouth_parameters(
            self.mouth_closed_value,
            self.mouth_closed_form_value,
        )

    def _resolve_open(self, cue: LipCue | None, amplitude: float | None) -> float:
        cue_open = self.mouth_closed_value if cue is None or cue.silence else cue.mouth_open
        if amplitude is None:
            return cue_open
        shaped_amplitude = self._shape_amplitude(amplitude)
        mixed = cue_open * (1.0 - self.mouth_amplitude_mix) + shaped_amplitude * self.mouth_amplitude_mix
        if cue is not None and cue.silence and shaped_amplitude <= self.mouth_open_min_delta:
            return self.mouth_closed_value
        return min(self.mouth_open_max, max(self.mouth_closed_value, mixed))

    def _resolve_form(self, cue: LipCue | None, amplitude: float | None) -> float:
        if cue is None or cue.silence:
            return self.mouth_closed_form_value
        base_form = min(1.0, max(-2.0, float(cue.mouth_form)))
        if amplitude is None:
            return base_form
        shaped_amplitude = self._shape_amplitude(amplitude)
        emphasis = 0.55 + shaped_amplitude * 0.75
        return min(1.0, max(-2.0, base_form * emphasis))

    def _shape_amplitude(self, raw_value: float) -> float:
        value = min(1.0, max(0.0, float(raw_value)))
        if value <= self.mouth_open_threshold:
            return self.mouth_closed_value
        normalized = (value - self.mouth_open_threshold) / max(0.001, 1.0 - self.mouth_open_threshold)
        curved = (normalized**self.mouth_open_gamma) * self.mouth_open_gain
        if curved <= 0.001:
            return self.mouth_closed_value
        return min(self.mouth_open_max, max(self.mouth_closed_value, curved))

    def _stabilize_open(self, target_value: float, previous_value: float) -> float:
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

    def _stabilize_form(self, target_value: float, previous_value: float) -> float:
        clamped_target = min(1.0, max(-2.0, float(target_value)))
        closed_form = self.mouth_closed_form_value
        if abs(clamped_target - previous_value) < self.mouth_form_min_delta:
            return previous_value
        if abs(clamped_target - closed_form) <= self.mouth_form_min_delta and abs(previous_value - closed_form) <= self.mouth_form_min_delta:
            return closed_form
        smoothing = self.mouth_form_smoothing
        smoothed = previous_value * smoothing + clamped_target * (1.0 - smoothing)
        if abs(smoothed - closed_form) <= self.mouth_form_min_delta and abs(clamped_target - closed_form) <= self.mouth_form_min_delta:
            return closed_form
        return min(1.0, max(-2.0, smoothed))

    def _resolve_mouth_parameters(self, mouth_open: float, mouth_form: float) -> list[dict[str, Any]]:
        parameters = [
            payload
            for payload in (
                self.profile.resolve("mouth.open", mouth_open, weight=1.0),
                self.profile.resolve("mouth.form", mouth_form, weight=0.88),
            )
            if payload
        ]
        return parameters


def _normalized_amplitudes(audio_timeline: Mapping[str, Any] | None) -> list[dict[str, float]]:
    if not isinstance(audio_timeline, Mapping):
        return []
    raw_amplitudes = audio_timeline.get("amplitudes")
    if not isinstance(raw_amplitudes, list):
        return []
    amplitudes: list[dict[str, float]] = []
    for raw_amplitude in raw_amplitudes:
        if not isinstance(raw_amplitude, Mapping):
            continue
        amplitudes.append(
            {
                "offset_ms": float(max(0, int(raw_amplitude.get("offset_ms") or 0))),
                "value": float(min(1.0, max(0.0, float(raw_amplitude.get("value") or 0.0)))),
            }
        )
    return amplitudes


def _resolve_closed_mouth_form_value(profile: ParameterProfile, explicit_value: float | None) -> float:
    if explicit_value is not None:
        return min(1.0, max(-2.0, float(explicit_value)))
    mouth_form = profile.find_by_role("mouth.form")
    baseline = 0.0 if mouth_form is None else float(mouth_form.default)
    identity = f"{profile.model_id} {profile.model_name}".strip().lower()
    if "hiyori" in identity:
        return min(1.0, max(-2.0, max(0.18, baseline)))
    return min(1.0, max(-2.0, baseline))


def _frame_offsets(duration_ms: int, interval_ms: int) -> list[int]:
    offsets = list(range(0, max(1, duration_ms), max(20, int(interval_ms))))
    if not offsets:
        offsets = [0]
    if offsets[-1] != max(0, int(duration_ms)):
        offsets.append(max(0, int(duration_ms)))
    return offsets


def _amplitude_at_offset(amplitudes: list[dict[str, float]], offset_ms: int) -> float | None:
    if not amplitudes:
        return None
    matched_value = amplitudes[0]["value"]
    for item in amplitudes:
        current_offset = int(item.get("offset_ms") or 0)
        if current_offset > offset_ms:
            break
        matched_value = float(item.get("value") or 0.0)
    return matched_value


def _build_cues(text: str) -> list[LipCue]:
    cues: list[LipCue] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            cues.append(LipCue(char, 0.0, 0.0, weight=0.25, pause_ms=70, silence=True))
            index += 1
            continue
        punctuation_pause = _punctuation_pause(char)
        if punctuation_pause > 0:
            cues.append(LipCue(char, 0.0, 0.0, weight=0.2, pause_ms=punctuation_pause, silence=True))
            index += 1
            continue
        if _is_ascii_letter(char):
            start = index
            while index < len(text) and _is_ascii_letter(text[index]):
                index += 1
            cues.extend(_english_cues(text[start:index]))
            continue
        cues.append(_char_to_cue(char))
        index += 1
    return cues


def _char_to_cue(char: str) -> LipCue:
    syllable = _syllable_for_char(char)
    open_value, form_value = _shape_for_syllable(syllable or char)
    return LipCue(char, open_value, form_value, weight=max(0.5, len(syllable or char) * 0.5))


def _english_cues(word: str) -> list[LipCue]:
    cues: list[LipCue] = []
    lowered = word.lower()
    index = 0
    while index < len(lowered):
        current = lowered[index]
        if current in "aeiouy":
            start = index
            while index < len(lowered) and lowered[index] in "aeiouy":
                index += 1
            chunk = lowered[start:index]
            open_value, form_value = _shape_for_syllable(chunk)
            cues.append(LipCue(chunk, open_value, form_value, weight=max(0.55, len(chunk) * 0.45)))
            continue
        if current in "bmp":
            cues.append(LipCue(current, 0.0, 0.0, weight=0.2, silence=True))
        elif current in "fvw":
            cues.append(LipCue(current, 0.18, 0.42, weight=0.25))
        else:
            cues.append(LipCue(current, 0.14, -0.08, weight=0.25))
        index += 1
    return cues or [LipCue(word, 0.4, 0.0, weight=1.0)]


def _shape_for_syllable(syllable: str) -> tuple[float, float]:
    normalized = str(syllable or "").strip().lower().replace("\u00fc", "v")
    if not normalized:
        return 0.38, 0.0
    vowel_key = _vowel_key(normalized)
    if vowel_key:
        return DEFAULT_VOWEL_SHAPES[vowel_key]
    if normalized[0] in {"b", "p", "m"}:
        return 0.0, 0.0
    if normalized[0] in {"f", "v", "w"}:
        return 0.18, 0.4
    return 0.26, -0.08


def _vowel_key(value: str) -> str:
    if "a" in value:
        return "a"
    if "o" in value:
        return "o"
    if "u" in value or "v" in value:
        return "u"
    if "i" in value or "y" in value:
        return "i"
    if "e" in value:
        return "e"
    return ""


def _syllable_for_char(char: str) -> str:
    if not _is_cjk(char) or not PYPINYIN_AVAILABLE or lazy_pinyin is None or Style is None:
        return ""
    result = lazy_pinyin(char, style=Style.NORMAL, errors="ignore")
    return str(result[0] if result else "").strip().lower()


def _allocate_cue_spans(cues: list[LipCue], duration_ms: int) -> list[CueSpan]:
    if duration_ms <= 0 or not cues:
        return []
    raw_pause_ms = sum(max(0, cue.pause_ms) for cue in cues if cue.silence)
    fixed_pause_ms = min(int(duration_ms * 0.4), raw_pause_ms)
    pause_scale = fixed_pause_ms / max(1, raw_pause_ms) if raw_pause_ms > 0 else 0.0
    weighted_cues = [cue for cue in cues if not cue.silence or cue.pause_ms <= 0]
    total_weight = sum(max(0.1, cue.weight) for cue in weighted_cues) or 1.0
    remaining_ms = max(1, duration_ms - fixed_pause_ms)
    spans: list[CueSpan] = []
    offset_ms = 0
    for cue in cues:
        if cue.silence and cue.pause_ms > 0:
            span_ms = max(25, min(int(cue.pause_ms * pause_scale), duration_ms - offset_ms))
        else:
            span_ms = max(30, int(remaining_ms * max(0.1, cue.weight) / total_weight))
        end_ms = min(duration_ms, offset_ms + span_ms)
        spans.append((offset_ms, end_ms, cue))
        offset_ms = end_ms
        if offset_ms >= duration_ms:
            break
    if spans and spans[-1][1] < duration_ms:
        start_ms, _, cue = spans[-1]
        spans[-1] = (start_ms, duration_ms, cue)
    return spans


def _cue_for_offset(spans: list[CueSpan], offset_ms: int, transition_ms: int) -> LipCue | None:
    if not spans:
        return None
    for index, (start_ms, end_ms, cue) in enumerate(spans):
        if start_ms <= offset_ms < end_ms:
            if transition_ms <= 0:
                return cue
            blend_window = max(1, min(transition_ms, max(1, (end_ms - start_ms) // 2)))
            current = cue
            if index > 0 and offset_ms < start_ms + blend_window:
                previous = spans[index - 1][2]
                factor = _smoothstep((offset_ms - start_ms) / blend_window)
                current = _blend_cues(previous, current, factor)
            if index + 1 < len(spans) and offset_ms > end_ms - blend_window:
                following = spans[index + 1][2]
                factor = _smoothstep((offset_ms - (end_ms - blend_window)) / blend_window)
                current = _blend_cues(current, following, factor)
            return current
    return spans[-1][2]


def _blend_cues(left: LipCue, right: LipCue, factor: float) -> LipCue:
    mix = min(1.0, max(0.0, float(factor)))
    inverse = 1.0 - mix
    return LipCue(
        label=right.label if mix >= 0.5 else left.label,
        mouth_open=left.mouth_open * inverse + right.mouth_open * mix,
        mouth_form=left.mouth_form * inverse + right.mouth_form * mix,
        weight=left.weight * inverse + right.weight * mix,
        pause_ms=int(left.pause_ms * inverse + right.pause_ms * mix),
        silence=left.silence and right.silence,
    )


def _smoothstep(value: float) -> float:
    clamped = min(1.0, max(0.0, float(value)))
    return clamped * clamped * (3.0 - 2.0 * clamped)


def _punctuation_pause(char: str) -> int:
    if char in ",，":
        return 100
    if char in ".。!！?？;；":
        return 220
    return 0


def _is_ascii_letter(char: str) -> bool:
    return ("a" <= char <= "z") or ("A" <= char <= "Z")


def _is_cjk(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"

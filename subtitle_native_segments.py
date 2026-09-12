"""Helpers for safe-copy native subtitle segment rendering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class RuntimeSubtitleSegment:
    """Normalized runtime segment payload for native subtitle rendering."""

    index: int
    chinese_text: str
    english_text: str
    duration_ms: int


def normalize_runtime_segment(payload: Mapping[str, Any]) -> RuntimeSubtitleSegment:
    """Normalize old and new payload shapes into one runtime segment model."""

    chinese_text = str(
        payload.get("subtitle_text") or payload.get("chinese_text") or payload.get("text") or ""
    ).strip()
    english_text = str(
        payload.get("speech_text") or payload.get("english_text") or payload.get("original_text") or ""
    ).strip()
    return RuntimeSubtitleSegment(
        index=int(payload.get("index") or 0),
        chinese_text=chinese_text,
        english_text=english_text,
        duration_ms=max(120, int(payload.get("duration_ms") or 600)),
    )


def reveal_bilingual_text(english_text: str, chinese_text: str, *, progress: float) -> tuple[str, str]:
    """Return the visible English and Chinese substrings for one shared reveal progress."""

    clamped_progress = min(1.0, max(0.0, float(progress)))
    english_chars = max(0, min(len(english_text), int(len(english_text) * clamped_progress)))
    chinese_chars = max(0, min(len(chinese_text), int(len(chinese_text) * clamped_progress)))
    return english_text[:english_chars], chinese_text[:chinese_chars]


def compute_track_overflow_px(*, track_height: int, viewport_height: int) -> int:
    """Return the positive overflow between the track height and the viewport height."""

    return max(0, int(track_height) - int(viewport_height))

"""Persistent live-topic state for idle-topic continuation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

import json
import time


class LiveTopicStatus(StrEnum):
    """The current live-topic continuation status."""

    ACTIVE = "active"
    TAILING = "tailing"
    EMPTY = "empty"


@dataclass(frozen=True)
class TopicExpansionResult:
    """Structured expansion result returned by the auxiliary topic LLM."""

    current_topic: str
    related_topic: str
    expansion_angle: str
    handoff_prompt: str
    why_related: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_topic": self.current_topic,
            "related_topic": self.related_topic,
            "expansion_angle": self.expansion_angle,
            "handoff_prompt": self.handoff_prompt,
            "why_related": self.why_related,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "TopicExpansionResult | None":
        if not isinstance(payload, Mapping):
            return None
        related_topic = _normalize_text(payload.get("related_topic"))
        handoff_prompt = _normalize_text(payload.get("handoff_prompt"))
        if not related_topic or not handoff_prompt:
            return None
        return cls(
            current_topic=_normalize_text(payload.get("current_topic")),
            related_topic=related_topic,
            expansion_angle=_normalize_text(payload.get("expansion_angle")),
            handoff_prompt=handoff_prompt,
            why_related=_normalize_text(payload.get("why_related")),
        )


@dataclass(frozen=True)
class LiveTopicSnapshot:
    """A restorable summary of the most recent live-topic thread."""

    room_id: str
    live_chat_id: str
    current_topic: str
    previous_topic: str
    recent_viewer_messages: list[str]
    recent_bot_outputs: list[str]
    last_expansion: TopicExpansionResult | None
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "room_id": self.room_id,
            "live_chat_id": self.live_chat_id,
            "current_topic": self.current_topic,
            "previous_topic": self.previous_topic,
            "recent_viewer_messages": list(self.recent_viewer_messages),
            "recent_bot_outputs": list(self.recent_bot_outputs),
            "last_expansion": self.last_expansion.to_dict() if self.last_expansion is not None else None,
            "updated_at": float(self.updated_at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "LiveTopicSnapshot | None":
        if not isinstance(payload, Mapping):
            return None
        room_id = _normalize_text(payload.get("room_id"))
        live_chat_id = _normalize_text(payload.get("live_chat_id"))
        if not room_id or not live_chat_id:
            return None
        try:
            updated_at = float(payload.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            updated_at = 0.0
        return cls(
            room_id=room_id,
            live_chat_id=live_chat_id,
            current_topic=_normalize_text(payload.get("current_topic")),
            previous_topic=_normalize_text(payload.get("previous_topic")),
            recent_viewer_messages=_normalize_text_list(payload.get("recent_viewer_messages")),
            recent_bot_outputs=_normalize_text_list(payload.get("recent_bot_outputs")),
            last_expansion=TopicExpansionResult.from_dict(payload.get("last_expansion")),
            updated_at=updated_at or time.time(),
        )


class LiveTopicStateStore:
    """Persist and restore a live-topic snapshot between same-room restarts."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def save(self, snapshot: LiveTopicSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self, *, room_id: str, live_chat_id: str, max_age_sec: float) -> LiveTopicSnapshot | None:
        if not self.path.exists():
            return None
        try:
            raw_payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None
        snapshot = LiveTopicSnapshot.from_dict(raw_payload)
        if snapshot is None:
            return None
        if snapshot.room_id != _normalize_text(room_id):
            return None
        if snapshot.live_chat_id != _normalize_text(live_chat_id):
            return None
        if max(0.0, time.time() - snapshot.updated_at) > max(0.0, float(max_age_sec)):
            return None
        return snapshot


def classify_live_topic_status(
    *,
    snapshot: LiveTopicSnapshot | None,
    recent_timeline: list[dict[str, Any]],
    recent_viewer_messages: list[str],
    recent_bot_outputs: list[str],
) -> LiveTopicStatus:
    """Heuristically classify whether the previous topic is active, tailing, or empty."""

    current_topic = ""
    if snapshot is not None:
        current_topic = _normalize_text(snapshot.current_topic or snapshot.previous_topic)
    recent_texts = [_normalize_text(record.get("text")) for record in recent_timeline if isinstance(record, Mapping)]
    recent_texts = [item for item in recent_texts if item]
    viewer_messages = [item for item in _normalize_text_list(recent_viewer_messages) if item]
    bot_outputs = [item for item in _normalize_text_list(recent_bot_outputs) if item]
    if not current_topic and not bot_outputs and not recent_texts:
        return LiveTopicStatus.EMPTY

    active_score = 0
    tailing_score = 0
    for text in viewer_messages[-3:]:
        active_score += _count_contains(text, _ACTIVE_VIEWER_MARKERS)
        tailing_score += _count_contains(text, _TAILING_VIEWER_MARKERS)
    for text in bot_outputs[-2:]:
        active_score += _count_contains(text, _ACTIVE_BOT_MARKERS)
        tailing_score += _count_contains(text, _TAILING_BOT_MARKERS)
    if viewer_messages and any("?" in text or "？" in text for text in viewer_messages[-2:]):
        active_score += 2
    if not current_topic and active_score <= 0 and tailing_score <= 0:
        return LiveTopicStatus.EMPTY
    if active_score > tailing_score:
        return LiveTopicStatus.ACTIVE
    return LiveTopicStatus.TAILING


_ACTIVE_VIEWER_MARKERS = (
    "?",
    "？",
    "吗",
    "呢",
    "怎么",
    "为什么",
    "还有",
    "然后",
    "继续",
    "说说",
)
_TAILING_VIEWER_MARKERS = (
    "确实",
    "哈哈",
    "是的",
    "对对对",
    "懂了",
    "原来如此",
    "可以",
)
_ACTIVE_BOT_MARKERS = (
    "?",
    "？",
    "你们也可以",
    "你觉得",
    "有没有",
    "要不要",
    "说说",
)
_TAILING_BOT_MARKERS = (
    "差不多",
    "就这些",
    "先这样",
    "回头再",
    "总之",
    "先收一下",
    "好了",
)


def _count_contains(text: str, markers: tuple[str, ...]) -> int:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return 0
    return sum(1 for marker in markers if marker and marker.lower() in lowered)


def _normalize_text(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def _normalize_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        text = _normalize_text(item)
        if text:
            normalized.append(text)
    return normalized

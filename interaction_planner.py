"""Live danmaku selection before injecting messages into MaiBot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import json
import time

from .config import InteractionConfig

_PAID_EVENT_TYPES = {"super_chat", "gift", "guard"}


@dataclass(frozen=True)
class PlannerSelection:
    """A selected live event and its reason."""

    event: dict[str, Any]
    reason: str
    score: float


class _MessageCapabilityProtocol(Protocol):
    async def count_new(self, chat_id: str, since: str) -> Any:
        ...


class LiveInteractionPlanner:
    """Select high-value live events for MaiBot interaction."""

    def __init__(
        self,
        config: InteractionConfig,
        llm: Any = None,
        logger: Any = None,
        *,
        message_capability: _MessageCapabilityProtocol | None = None,
        chat_id: str = "",
    ) -> None:
        self.config = config
        self.llm = llm
        self.logger = logger
        self.message_capability = message_capability
        self.chat_id = str(chat_id or "").strip()
        self._last_inject_at = 0.0
        self._recent_injections: list[float] = []
        self._pending_count_anchor_at = time.time()

    async def select(
        self,
        events: list[Mapping[str, Any]],
        *,
        ai_speaking: bool = False,
    ) -> list[PlannerSelection]:
        """Select events from a live window."""

        if not events:
            return []
        if not self.config.enabled:
            return [self._passthrough_selection(dict(event), ai_speaking=ai_speaking) for event in events]
        now = time.time()
        if await self._should_route_all_events(now):
            selected = [self._force_all_selection(dict(event), ai_speaking=ai_speaking) for event in events]
            selected = _sort_by_live_reply_priority(selected)
            if selected:
                self._record_injections(now, len(selected))
                self._log_info(
                    "Live interaction planner routed all buffered events: "
                    f"pending_count<={self.config.route_all_when_pending_leq} selected={len(selected)}"
                )
            return selected
        if not self._can_inject(now) and not _has_high_priority(events):
            return []
        scored = [self._score_event(dict(event), ai_speaking=ai_speaking) for event in events]
        scored = [item for item in scored if item.score > 0]
        if not scored:
            return []
        selected = await self._try_llm_select(scored) if self.config.llm_enabled else []
        if selected:
            selected = _include_paid_events(scored, selected)
        if not selected:
            selected = _sort_scored_for_selection(scored)[: self.config.max_selected_per_window]
        else:
            selected = _sort_by_live_reply_priority(selected)
        selected = selected[: self.config.max_selected_per_window]
        if selected:
            self._record_injections(now, len(selected))
        return selected

    async def should_flush_immediately(self, events: list[Mapping[str, Any]]) -> bool:
        """Return whether the current buffered events should bypass the selection window."""

        if not events:
            return False
        if not self.config.enabled:
            return True
        if _has_high_priority(events):
            return True
        return await self._should_route_all_events(time.time())

    async def get_pending_message_count(self) -> int | None:
        """Return the number of newer pending MaiBot messages visible to the planner."""

        if self.message_capability is None or not self.chat_id:
            return None
        try:
            pending_count = await self.message_capability.count_new(
                chat_id=self.chat_id,
                since=str(self._pending_count_anchor_at),
            )
        except Exception as exc:
            self._log_warning(f"Live interaction planner pending-count query failed: {exc}")
            return None
        try:
            normalized_count = int(pending_count)
        except (TypeError, ValueError):
            return None
        return max(0, normalized_count)

    def record_external_injection(self, *, when: float | None = None, count: int = 1) -> None:
        """Advance the pending-count anchor for non-planner injections such as idle-topic prompts."""

        normalized_count = max(1, int(count))
        timestamp = time.time() if when is None else float(when)
        self._record_injections(timestamp, normalized_count)

    def advance_pending_anchor(self, *, when: float | None = None) -> None:
        """Move the pending-count anchor forward without consuming injection budget."""

        timestamp = time.time() if when is None else float(when)
        self._pending_count_anchor_at = max(self._pending_count_anchor_at, timestamp)

    async def _should_route_all_events(self, now: float) -> bool:
        del now
        if self.config.route_all_when_pending_leq <= 0:
            return False
        normalized_count = await self.get_pending_message_count()
        if normalized_count is None:
            return False
        if normalized_count <= self.config.route_all_when_pending_leq:
            return True
        return False

    def _force_all_selection(self, event: dict[str, Any], *, ai_speaking: bool) -> PlannerSelection:
        scored = self._score_event(event, ai_speaking=ai_speaking)
        reason_parts = [part for part in str(scored.reason or "").split(",") if part]
        if "small_queue_all" not in reason_parts:
            reason_parts.append("small_queue_all")
        return PlannerSelection(
            event=scored.event,
            reason=",".join(reason_parts) or "small_queue_all",
            score=max(scored.score, 1.0),
        )

    def _passthrough_selection(self, event: dict[str, Any], *, ai_speaking: bool) -> PlannerSelection:
        scored = self._score_event(event, ai_speaking=ai_speaking)
        reason_parts = [part for part in str(scored.reason or "").split(",") if part]
        if "interaction_disabled_passthrough" not in reason_parts:
            reason_parts.append("interaction_disabled_passthrough")
        return PlannerSelection(
            event=scored.event,
            reason=",".join(reason_parts) or "interaction_disabled_passthrough",
            score=max(scored.score, 1.0),
        )

    def _score_event(self, event: dict[str, Any], *, ai_speaking: bool) -> PlannerSelection:
        text = str(event.get("text") or event.get("summary") or "").strip()
        event_type = str(event.get("type") or "").strip()
        lowered_text = text.lower()
        score = 0.0
        reasons: list[str] = []
        if text:
            score += 0.35
            reasons.append("baseline")
        if event_type in _PAID_EVENT_TYPES:
            score += 6.0
            reasons.append(event_type)
        if any(name.lower() in lowered_text for name in self.config.bot_names):
            score += 4.0
            reasons.append("bot_name")
        if any(keyword.lower() in lowered_text for keyword in self.config.keywords):
            score += 2.5
            reasons.append("keyword")
        if any(mark in text for mark in "?!？！吗呢怎么为什么"):
            score += 1.8
            reasons.append("question")
        if any(mark in text for mark in "哈哈草笑哭绝绷乐"):
            score += 1.3
            reasons.append("funny")
        if len(text) >= 8:
            score += 0.7
        if ai_speaking and event_type not in _PAID_EVENT_TYPES:
            score *= self.config.speaking_slowdown_factor
            reasons.append("ai_speaking_slowdown")
        return PlannerSelection(event=event, reason=",".join(reasons) or "score", score=score)

    async def _try_llm_select(self, scored: list[PlannerSelection]) -> list[PlannerSelection]:
        if self.llm is None or len(scored) < 4:
            return []
        candidates = sorted(scored, key=lambda item: item.score, reverse=True)[: self.config.max_batch_size]
        prompt = self._build_llm_prompt(candidates)
        try:
            response = await self.llm.generate(prompt=prompt, temperature=0.1, max_tokens=256)
        except Exception as exc:
            self._log_warning(f"Live interaction planner LLM failed: {exc}")
            return []
        raw_text = _extract_llm_text(response)
        try:
            payload = json.loads(raw_text)
        except Exception:
            return []
        selected_ids = []
        if isinstance(payload, Mapping) and isinstance(payload.get("selected"), list):
            for item in payload["selected"]:
                if isinstance(item, Mapping):
                    selected_ids.append(str(item.get("event_id") or "").strip())
                else:
                    selected_ids.append(str(item).strip())
        if not selected_ids:
            return []
        by_id = {str(item.event.get("event_id") or ""): item for item in candidates}
        return [by_id[event_id] for event_id in selected_ids if event_id in by_id]

    def _build_llm_prompt(self, candidates: list[PlannerSelection]) -> str:
        lines = [
            "Pick 0-2 Bilibili live chat messages that are most worth responding to on stream.",
            "Prefer direct questions, funny remarks, super chats, gifts, or messages with strong show value.",
            'Return strict JSON only: {"selected":[{"event_id":"...","reason":"..."}]}',
            "Candidates:",
        ]
        for item in candidates:
            lines.append(
                f"- event_id={item.event.get('event_id')} user={item.event.get('username')} "
                f"type={item.event.get('type')} score={item.score:.2f} text={item.event.get('text')}"
            )
        return "\n".join(lines)

    def _can_inject(self, now: float) -> bool:
        if now - self._last_inject_at < self.config.min_inject_interval_sec:
            return False
        one_minute_ago = now - 60.0
        self._recent_injections = [stamp for stamp in self._recent_injections if stamp >= one_minute_ago]
        return len(self._recent_injections) < self.config.max_injections_per_minute

    def _record_injections(self, now: float, count: int) -> None:
        self._last_inject_at = now
        self._pending_count_anchor_at = now
        self._recent_injections.extend([now] * count)

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)

    def _log_info(self, message: str) -> None:
        if self.logger is not None:
            self.logger.info(message)


def _has_high_priority(events: list[Mapping[str, Any]]) -> bool:
    return any(str(event.get("type") or "") in _PAID_EVENT_TYPES for event in events)


def _include_paid_events(
    scored: list[PlannerSelection],
    selected: list[PlannerSelection],
) -> list[PlannerSelection]:
    selected_ids = {str(item.event.get("event_id") or "") for item in selected}
    paid = [
        item
        for item in scored
        if _live_reply_priority_rank(item.event) < 2 and str(item.event.get("event_id") or "") not in selected_ids
    ]
    return paid + selected


def _sort_scored_for_selection(items: list[PlannerSelection]) -> list[PlannerSelection]:
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: (_live_reply_priority_rank(pair[1].event), -pair[1].score, pair[0]))
    return [item for _, item in indexed]


def _sort_by_live_reply_priority(items: list[PlannerSelection]) -> list[PlannerSelection]:
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: (_live_reply_priority_rank(pair[1].event), pair[0]))
    return [item for _, item in indexed]


def _live_reply_priority_rank(event: Mapping[str, Any]) -> int:
    event_type = str(event.get("type") or "").strip()
    if event_type == "super_chat":
        return 0
    if event_type in {"gift", "guard"}:
        return 1
    return 2


def _extract_llm_text(response: Any) -> str:
    if isinstance(response, Mapping):
        return str(response.get("response") or response.get("text") or "")
    return str(response or "")

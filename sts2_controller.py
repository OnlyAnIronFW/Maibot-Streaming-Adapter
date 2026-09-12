"""STS2 gameplay controller for the Bilibili live adapter."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Protocol

import asyncio
import contextlib
import random
import time
from uuid import uuid4

from .config import LiveAdapterSettings
from .constants import GATEWAY_NAME
from .message_codec import build_sts2_message_dict, sanitize_model_reserved_tokens


class _GatewayProtocol(Protocol):
    async def route_message(
        self,
        gateway_name: str,
        message: dict[str, Any],
        *,
        route_metadata: dict[str, Any] | None = None,
        external_message_id: str = "",
        dedupe_key: str = "",
    ) -> bool:
        ...


class _Sts2MCPProtocol(Protocol):
    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...

    async def health_check(self) -> dict[str, Any]:
        ...

    async def get_game_state(self) -> dict[str, Any]:
        ...

    async def get_available_actions(self) -> list[dict[str, Any]]:
        ...

    async def wait_until_actionable(self, *, timeout_seconds: float) -> dict[str, Any]:
        ...

    async def act(
        self,
        *,
        action: str,
        card_index: int | None = None,
        target_index: int | None = None,
        option_index: int | None = None,
    ) -> dict[str, Any]:
        ...


class _DecisionClientProtocol(Protocol):
    async def decide(
        self,
        *,
        state: Mapping[str, Any],
        available_actions: list[dict[str, Any]],
        history: list[dict[str, Any]],
    ) -> "STS2Decision":
        ...


class _RuntimeStateProtocol(Protocol):
    @property
    def is_ready(self) -> bool:
        ...

    async def wait_until_ready(self) -> None:
        ...

    def mark_not_ready_locally(self) -> None:
        ...


_OPTION_INDEX_ACTIONS = frozenset(
    {
        "buy_card",
        "buy_potion",
        "buy_relic",
        "choose_event_option",
        "choose_map_node",
        "choose_rest_option",
        "choose_reward_card",
        "choose_timeline_epoch",
        "choose_treasure_relic",
        "claim_reward",
        "discard_potion",
        "select_character",
        "select_deck_card",
        "use_potion",
    }
)

_NO_INDEX_ACTIONS = frozenset(
    {
        "abandon_run",
        "close_main_menu_submenu",
        "close_shop_inventory",
        "collect_rewards_and_proceed",
        "confirm_modal",
        "confirm_selection",
        "confirm_timeline_overlay",
        "continue_run",
        "decrease_ascension",
        "dismiss_modal",
        "embark",
        "end_turn",
        "increase_ascension",
        "open_character_select",
        "open_chest",
        "open_shop_inventory",
        "open_timeline",
        "proceed",
        "remove_card_at_shop",
        "return_to_main_menu",
        "skip_reward_cards",
        "unready",
    }
)


@dataclass(frozen=True)
class STS2Decision:
    """One STS2 action selected by the decision LLM."""

    decision_id: str
    action: str
    reason: str
    narration: str
    card_index: int | None = None
    target_index: int | None = None
    option_index: int | None = None
    expected_result: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def action_kwargs(self) -> dict[str, Any]:
        action = str(self.action or "").strip().lower()
        kwargs: dict[str, Any] = {"action": action}
        if action == "play_card":
            card_index = self.card_index if self.card_index is not None else self.option_index
            if card_index is not None:
                kwargs["card_index"] = int(card_index)
        elif action in _OPTION_INDEX_ACTIONS:
            option_index = self.option_index if self.option_index is not None else self.card_index
            if option_index is not None:
                kwargs["option_index"] = int(option_index)
        elif action not in _NO_INDEX_ACTIONS:
            if self.card_index is not None:
                kwargs["card_index"] = int(self.card_index)
            if self.option_index is not None:
                kwargs["option_index"] = int(self.option_index)
        if self.target_index is not None:
            kwargs["target_index"] = int(self.target_index)
        return kwargs


@dataclass
class _PendingTreasureChestSequence:
    decision_id: str
    entry_decision: STS2Decision
    entry_state: dict[str, Any]
    entry_actions: list[dict[str, Any]]
    open_result: dict[str, Any] | None = None
    reward_decision: STS2Decision | None = None
    reward_plan_task: asyncio.Task[STS2Decision | None] | None = None
    reward_result: dict[str, Any] | None = None
    exit_decision: STS2Decision | None = None


@dataclass
class _CombatTurnActionRecord:
    decision_id: str
    action: dict[str, Any]
    reason: str
    expected_result: str


class STS2Controller:
    """Owns the STS2 run loop and the speech-start action gate."""

    def __init__(
        self,
        *,
        gateway: _GatewayProtocol,
        settings: LiveAdapterSettings,
        mcp_client: _Sts2MCPProtocol,
        decision_client: _DecisionClientProtocol | None,
        runtime_state: _RuntimeStateProtocol | None = None,
        logger: Any = None,
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.mcp_client = mcp_client
        self.decision_client = decision_client
        self._runtime_state = runtime_state
        self.logger = logger
        self._active = False
        self._run_task: asyncio.Task[None] | None = None
        self._pending_decision: STS2Decision | None = None
        self._pending_future: asyncio.Future[dict[str, Any]] | None = None
        self._pending_executing = False
        self._pending_treasure_chest_sequence: _PendingTreasureChestSequence | None = None
        self._history: list[dict[str, Any]] = []
        self._live_context: list[dict[str, Any]] = []
        self._pending_live_replies: list[dict[str, Any]] = []
        self._combat_turn_actions: list[_CombatTurnActionRecord] = []
        self._last_selected_character_choice_key: str | None = None

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def has_pending_decision(self) -> bool:
        return self._pending_decision is not None

    @property
    def pending_decision_id(self) -> str:
        decision = self._pending_decision
        return decision.decision_id if decision is not None else ""

    async def start_from_command(self, event: dict[str, Any]) -> bool:
        self._log_info(f"STS2 start command received: user_id={event.get('user_id')} text={event.get('text')!r}")
        if not self.settings.sts2.enabled:
            await self._route_text("STS2 功能未启用，已忽略启动命令。", event_type="sts2_status")
            return False
        if self._active:
            await self._route_text("STS2 已经在运行中，我会继续当前这局。", event_type="sts2_status")
            return True
        self._active = True
        self._history.clear()
        self._live_context.clear()
        self._pending_live_replies.clear()
        self._clear_combat_turn_actions()
        await self._route_text(
            "收到管理员指令，准备开始游玩杀戮尖塔2。先确认游戏和 MCP 服务状态。",
            event_type="sts2_status",
            payload={"command_event": dict(event)},
        )
        self._run_task = asyncio.create_task(self._run_loop(), name="bilibili_live.sts2_controller")
        return True

    async def stop_from_command(self, event: dict[str, Any]) -> bool:
        self._log_info(f"STS2 stop command received: user_id={event.get('user_id')} text={event.get('text')!r}")
        del event
        await self.stop(reason="管理员停止了 STS2 游玩。")
        return True

    async def status_from_command(self, event: dict[str, Any]) -> bool:
        del event
        status = "运行中" if self._active else "未运行"
        pending = "，有一个动作正在等待语音同步" if self.has_pending_decision else ""
        await self._route_text(f"STS2 当前状态：{status}{pending}。", event_type="sts2_status")
        return True

    async def stop(self, *, reason: str = "") -> None:
        self._log_info(f"Stopping STS2 controller: reason={reason!r}")
        was_active = self._active
        self._active = False
        task = self._run_task
        self._run_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._clear_pending()
        self._clear_combat_turn_actions()
        with contextlib.suppress(Exception):
            await self.mcp_client.stop()
        if was_active and reason:
            await self._route_text(reason, event_type="sts2_status")

    def queue_pending_decision(
        self,
        decision: STS2Decision,
        *,
        state: Mapping[str, Any] | None = None,
        available_actions: list[dict[str, Any]] | None = None,
    ) -> asyncio.Future[dict[str, Any]]:
        self._log_info(
            "Queued STS2 decision waiting for commentary audio: "
            f"decision_id={decision.decision_id} action={decision.action_kwargs()}"
        )
        self._pending_decision = decision
        self._pending_executing = False
        self._pending_future = asyncio.get_running_loop().create_future()
        if self._should_use_treasure_chest_sequence(decision):
            self._pending_treasure_chest_sequence = _PendingTreasureChestSequence(
                decision_id=decision.decision_id,
                entry_decision=decision,
                entry_state=dict(state or {}),
                entry_actions=[dict(item) for item in (available_actions or []) if isinstance(item, Mapping)],
            )
        else:
            self._pending_treasure_chest_sequence = None
        return self._pending_future

    def _should_execute_on_audio_complete(self, decision: STS2Decision | None) -> bool:
        if decision is None:
            return False
        return str(decision.action or "").strip().lower() == "end_turn"

    def _should_use_treasure_chest_sequence(self, decision: STS2Decision | None) -> bool:
        if decision is None:
            return False
        return str(decision.action or "").strip().lower() == "open_chest"

    def build_audio_start_callback(self) -> Callable[[], None] | None:
        decision = self._pending_decision
        if decision is None or self._should_execute_on_audio_complete(decision):
            return None
        loop = asyncio.get_running_loop()
        fired = False

        def on_audio_start() -> None:
            nonlocal fired
            if fired:
                return
            fired = True
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self.notify_commentary_audio_started(decision.decision_id))
            )

        return on_audio_start

    def build_audio_complete_callback(self) -> Callable[[], None] | None:
        decision = self._pending_decision
        if (
            decision is None
            or not (
                self._should_execute_on_audio_complete(decision)
                or self._pending_treasure_chest_sequence is not None
            )
        ):
            return None
        loop = asyncio.get_running_loop()
        fired = False

        def on_audio_complete() -> None:
            nonlocal fired
            if fired:
                return
            fired = True
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self.notify_commentary_audio_completed(decision.decision_id))
            )

        return on_audio_complete

    def build_segment_audio_start_callback(self) -> Callable[[int, int], None] | None:
        sequence = self._pending_treasure_chest_sequence
        if sequence is None:
            return None
        loop = asyncio.get_running_loop()
        fired = False

        def on_segment_audio_start(segment_index: int, segment_count: int) -> None:
            nonlocal fired
            midpoint_segment_index = _treasure_chest_midpoint_segment_index(segment_count)
            if fired or int(segment_index) != midpoint_segment_index:
                return
            fired = True
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self.notify_commentary_segment_started(
                        sequence.decision_id,
                        segment_index=segment_index,
                        segment_count=segment_count,
                    )
                )
            )

        return on_segment_audio_start

    def _is_combat_state(self, state: Mapping[str, Any] | None) -> bool:
        if not isinstance(state, Mapping):
            return False
        if bool(state.get("in_combat")):
            return True
        return str(state.get("screen") or "").strip().upper() == "COMBAT"

    def _should_execute_immediately_in_combat_turn(
        self,
        decision: STS2Decision,
        *,
        state: Mapping[str, Any],
    ) -> bool:
        if not self._is_combat_state(state):
            return False
        action = str(decision.action or "").strip().lower()
        if not action or action == "end_turn":
            return False
        if self._should_use_treasure_chest_sequence(decision):
            return False
        return True

    def _should_route_combat_turn_summary(
        self,
        decision: STS2Decision,
        *,
        state: Mapping[str, Any],
    ) -> bool:
        return (
            self._is_combat_state(state)
            and str(decision.action or "").strip().lower() == "end_turn"
            and bool(self._combat_turn_actions)
        )

    def _should_execute_before_commentary(
        self,
        decision: STS2Decision,
        *,
        state: Mapping[str, Any],
    ) -> bool:
        action = str(decision.action or "").strip().lower()
        if not action:
            return False
        if self._should_execute_immediately_in_combat_turn(decision, state=state):
            return False
        if self._should_use_treasure_chest_sequence(decision):
            return False
        return action != "end_turn"

    def _resolve_execution_decision(
        self,
        decision: STS2Decision,
        available_actions: list[dict[str, Any]],
    ) -> STS2Decision:
        action_name = str(decision.action or "").strip().lower()
        matching_actions = [dict(item) for item in available_actions if _action_name(item) == action_name]
        if not matching_actions:
            return decision
        if action_name == "select_character":
            return self._resolve_select_character_decision(decision, matching_actions)
        return _merge_decision_with_available_actions(decision, matching_actions)

    def _resolve_select_character_decision(
        self,
        decision: STS2Decision,
        available_actions: list[dict[str, Any]],
    ) -> STS2Decision:
        candidates: list[tuple[dict[str, Any], int, str]] = []
        for action in available_actions:
            option_index = _optional_int(action.get("option_index"))
            if option_index is None:
                continue
            candidates.append((dict(action), option_index, _character_choice_key(action)))
        if not candidates:
            return _merge_decision_with_available_actions(decision, available_actions)

        candidate_pool = list(candidates)
        if self._last_selected_character_choice_key and len(candidate_pool) > 1:
            filtered = [
                candidate
                for candidate in candidate_pool
                if candidate[2] != self._last_selected_character_choice_key
            ]
            if filtered:
                candidate_pool = filtered
        chosen_action, chosen_option_index, chosen_choice_key = random.choice(candidate_pool)
        resolved = replace(
            decision,
            option_index=chosen_option_index,
            card_index=None,
            target_index=_optional_int(chosen_action.get("target_index")),
            raw={
                **dict(chosen_action),
                **dict(decision.raw),
                "_resolved_action": dict(chosen_action),
                "_resolved_character_choice_key": chosen_choice_key,
            },
        )
        self._log_info(
            "STS2 character selection randomized: "
            f"decision_id={decision.decision_id} option_index={chosen_option_index} "
            f"choice_key={chosen_choice_key!r} previous_choice={self._last_selected_character_choice_key!r}"
        )
        return resolved

    async def _maybe_wait_for_combat_action_delay(self) -> None:
        min_delay = max(0.0, float(self.settings.sts2.narration.combat_action_delay_min_sec))
        max_delay = max(0.0, float(self.settings.sts2.narration.combat_action_delay_max_sec))
        delay_seconds = random.uniform(min(min_delay, max_delay), max(min_delay, max_delay))
        if delay_seconds <= 0.0:
            return
        await asyncio.sleep(delay_seconds)

    async def _execute_immediate_combat_turn_action(self, decision: STS2Decision) -> dict[str, Any]:
        await self._maybe_wait_for_combat_action_delay()
        result = await self._execute_action(decision)
        self._record_history(decision=decision, result=result)
        self._remember_successful_action(decision, result)
        if bool(result.get("ok", True)) and not result.get("error"):
            self._combat_turn_actions.append(
                _CombatTurnActionRecord(
                    decision_id=decision.decision_id,
                    action=decision.action_kwargs(),
                    reason=decision.reason,
                    expected_result=decision.expected_result,
                )
            )
            max_items = max(1, int(self.settings.sts2.narration.max_recent_steps))
            self._combat_turn_actions = self._combat_turn_actions[-max_items:]
        return result

    def _clear_combat_turn_actions(self) -> None:
        self._combat_turn_actions = []

    def _remember_successful_action(self, decision: STS2Decision, result: Mapping[str, Any]) -> None:
        if not bool(result.get("ok", True)) or result.get("error"):
            return
        if str(decision.action or "").strip().lower() != "select_character":
            return
        choice_key = _resolved_character_choice_key(decision)
        if choice_key:
            self._last_selected_character_choice_key = choice_key

    def _post_action_commentary_state(
        self,
        state: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        state_after = result.get("state_after")
        if isinstance(state_after, Mapping):
            return self._state_with_live_context(dict(state_after))
        return self._state_with_live_context(state)

    @staticmethod
    def _post_action_available_actions(
        result: Mapping[str, Any],
        fallback_actions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        actions_after = result.get("available_actions_after")
        if not isinstance(actions_after, list):
            return [dict(item) for item in fallback_actions if isinstance(item, Mapping)]
        return [dict(item) for item in actions_after if isinstance(item, Mapping)]

    def record_live_event_context(self, event: dict[str, Any]) -> None:
        context_item = _build_live_context_item(event)
        if not context_item:
            return
        self._live_context.append(context_item)
        self._pending_live_replies.append(context_item)
        max_items = max(4, int(self.settings.sts2.narration.max_recent_steps) * 2)
        self._live_context = self._live_context[-max_items:]

    async def notify_commentary_audio_started(self, decision_id: str) -> dict[str, Any]:
        return await self._notify_commentary_audio_triggered(decision_id, trigger="started")

    async def notify_commentary_audio_completed(self, decision_id: str) -> dict[str, Any]:
        return await self._notify_commentary_audio_triggered(decision_id, trigger="completed")

    async def notify_commentary_segment_started(
        self,
        decision_id: str,
        *,
        segment_index: int,
        segment_count: int,
    ) -> dict[str, Any]:
        decision = self._pending_decision
        future = self._pending_future
        sequence = self._pending_treasure_chest_sequence
        if decision is None or future is None or sequence is None:
            return {"ok": False, "error": "no_pending_decision"}
        if decision.decision_id != str(decision_id or "").strip():
            return {"ok": False, "error": "decision_id_mismatch"}
        if int(segment_index) != _treasure_chest_midpoint_segment_index(segment_count):
            return {"ok": False, "error": "not_midpoint_segment"}
        if sequence.reward_result is not None:
            return sequence.reward_result
        if self._pending_executing:
            return {"ok": False, "error": "action_in_progress"}
        self._pending_executing = True
        try:
            reward_decision = await self._resolve_treasure_chest_reward_decision(sequence)
            if reward_decision is None:
                payload = {"ok": False, "error": "reward_decision_unavailable"}
                if not future.done():
                    future.set_result(payload)
                self._clear_pending()
                return payload
            sequence.reward_decision = reward_decision
            self._log_info(
                "Treasure chest midpoint reached; taking chest reward: "
                f"decision_id={decision.decision_id} action={reward_decision.action_kwargs()}"
            )
            result = await self._execute_action(reward_decision)
            sequence.reward_result = {**result, "sequence_phase": "choose_treasure_relic"}
            sequence.exit_decision = self._build_treasure_chest_exit_decision(sequence.reward_result)
            return sequence.reward_result
        except Exception as exc:
            payload = {"ok": False, "error": str(exc)}
            if not future.done():
                future.set_result(payload)
            self._clear_pending()
            return payload
        finally:
            if self._pending_treasure_chest_sequence is not None:
                self._pending_executing = False

    async def _notify_commentary_audio_triggered(
        self,
        decision_id: str,
        *,
        trigger: str,
    ) -> dict[str, Any]:
        decision = self._pending_decision
        future = self._pending_future
        if decision is None or future is None:
            return {"ok": False, "error": "no_pending_decision"}
        if decision.decision_id != str(decision_id or "").strip():
            return {"ok": False, "error": "decision_id_mismatch"}
        if self._pending_treasure_chest_sequence is not None:
            return await self._notify_treasure_chest_audio_triggered(
                decision=decision,
                future=future,
                trigger=trigger,
            )
        expects_audio_complete = self._should_execute_on_audio_complete(decision)
        if trigger == "started" and expects_audio_complete:
            return {"ok": False, "error": "audio_completion_required"}
        if trigger == "completed" and not expects_audio_complete:
            return {"ok": False, "error": "audio_start_required"}
        if self._pending_executing:
            return await future
        self._pending_executing = True
        try:
            self._log_info(
                f"Commentary audio {trigger}; executing STS2 action: "
                f"decision_id={decision.decision_id} action={decision.action_kwargs()}"
            )
            result = await self.mcp_client.act(**decision.action_kwargs())
            with contextlib.suppress(Exception):
                result = {**result, "state_after": await self.mcp_client.get_game_state()}
            if not future.done():
                future.set_result(result)
            self._record_history(decision=decision, result=result)
            self._remember_successful_action(decision, result)
            if expects_audio_complete and bool(result.get("ok", True)) and not result.get("error"):
                self._clear_combat_turn_actions()
            self._log_info(f"STS2 action executed: decision_id={decision.decision_id} result={result}")
            return result
        except Exception as exc:
            payload = {"ok": False, "error": str(exc), "action": decision.action_kwargs()}
            self._log_exception(
                f"STS2 action failed after commentary audio {trigger}: "
                f"decision_id={decision.decision_id} action={decision.action_kwargs()}"
            )
            if not future.done():
                future.set_result(payload)
            return payload
        finally:
            self._clear_pending()

    async def _notify_treasure_chest_audio_triggered(
        self,
        *,
        decision: STS2Decision,
        future: asyncio.Future[dict[str, Any]],
        trigger: str,
    ) -> dict[str, Any]:
        sequence = self._pending_treasure_chest_sequence
        if sequence is None:
            return {"ok": False, "error": "no_pending_decision"}
        if trigger == "started":
            if sequence.open_result is not None:
                return sequence.open_result
            if self._pending_executing:
                return {"ok": False, "error": "action_in_progress"}
            self._pending_executing = True
            try:
                self._log_info(
                    "Treasure chest commentary started; opening chest: "
                    f"decision_id={decision.decision_id} action={decision.action_kwargs()}"
                )
                result = await self._execute_action(sequence.entry_decision)
                sequence.open_result = {**result, "sequence_phase": "open_chest"}
                sequence.reward_plan_task = asyncio.create_task(
                    self._plan_treasure_chest_reward_decision(sequence),
                    name=f"sts2_treasure_chest_reward_{decision.decision_id}",
                )
                return sequence.open_result
            except Exception as exc:
                payload = {"ok": False, "error": str(exc), "action": decision.action_kwargs()}
                if not future.done():
                    future.set_result(payload)
                self._clear_pending()
                return payload
            finally:
                if self._pending_treasure_chest_sequence is not None:
                    self._pending_executing = False

        if sequence.reward_result is None:
            reward_result = await self.notify_commentary_segment_started(
                decision.decision_id,
                segment_index=1,
                segment_count=1,
            )
            if not reward_result.get("ok"):
                reward_decision = await self._resolve_treasure_chest_reward_decision(sequence)
                if reward_decision is not None:
                    sequence.reward_decision = reward_decision
                    self._pending_executing = True
                    try:
                        reward_exec_result = await self._execute_action(reward_decision)
                        sequence.reward_result = {
                            **reward_exec_result,
                            "sequence_phase": "choose_treasure_relic",
                        }
                        sequence.exit_decision = self._build_treasure_chest_exit_decision(sequence.reward_result)
                    finally:
                        if self._pending_treasure_chest_sequence is not None:
                            self._pending_executing = False
        exit_decision = sequence.exit_decision or self._build_treasure_chest_exit_decision(
            sequence.reward_result or sequence.open_result or {}
        )
        if exit_decision is None:
            payload = {"ok": False, "error": "exit_action_unavailable"}
            if not future.done():
                future.set_result(payload)
            self._clear_pending()
            return payload
        self._pending_executing = True
        try:
            self._log_info(
                "Treasure chest commentary completed; exiting room: "
                f"decision_id={decision.decision_id} action={exit_decision.action_kwargs()}"
            )
            final_result = await self._execute_action(exit_decision)
            aggregate_result = self._build_treasure_chest_sequence_result(
                sequence,
                final_result={**final_result, "sequence_phase": "exit_room"},
            )
            if not future.done():
                future.set_result(aggregate_result)
            self._record_history(decision=decision, result=aggregate_result)
            return aggregate_result
        except Exception as exc:
            payload = {"ok": False, "error": str(exc), "action": exit_decision.action_kwargs()}
            if not future.done():
                future.set_result(payload)
            return payload
        finally:
            self._clear_pending()

    async def _run_loop(self) -> None:
        try:
            self._log_info("STS2 controller loop starting")
            await self.mcp_client.start()
            health = await self.mcp_client.health_check()
            self._log_info(f"STS2 MCP health check result: {health}")
            await self._route_text(
                "STS2 MCP 已连接，开始读取当前局面并准备行动。",
                event_type="sts2_status",
                payload={"health": health},
            )
            while self._active:
                actionable = await self.mcp_client.wait_until_actionable(
                    timeout_seconds=self.settings.sts2.mcp.wait_actionable_timeout_sec
                )
                state = self._extract_state(actionable) or await self.mcp_client.get_game_state()
                available_actions = self._extract_actions(actionable) or await self.mcp_client.get_available_actions()
                self._log_info(
                    "STS2 actionable state loaded: "
                    f"state_keys={list(state)[:20]} available_actions={len(available_actions)}"
                )
                if not available_actions:
                    await asyncio.sleep(1.0)
                    continue
                if self.decision_client is None:
                    await self._route_text("STS2 决策模型未初始化，游玩已暂停。", event_type="sts2_error")
                    self._active = False
                    break
                decision_state = self._state_with_live_context(state)
                pending_live_reply_keys = _live_reply_keys(decision_state.get("pending_live_replies", []))
                decision = await self.decision_client.decide(
                    state=decision_state,
                    available_actions=available_actions,
                    history=list(self._history),
                )
                decision = self._resolve_execution_decision(decision, available_actions)
                self._log_info(
                    "STS2 decision received: "
                    f"decision_id={decision.decision_id} action={decision.action_kwargs()} reason={decision.reason!r}"
                )
                if not self._is_available_action(decision.action, available_actions):
                    await self._route_text(
                        f"STS2 决策动作 {decision.action!r} 不在当前可用动作中，已跳过并重新读取状态。",
                        event_type="sts2_error",
                        payload={"decision": decision.raw, "available_actions": available_actions},
                    )
                    continue
                if self._should_execute_immediately_in_combat_turn(decision, state=decision_state):
                    result = await self._execute_immediate_combat_turn_action(decision)
                    if not self._is_combat_state(result.get("state_after")):
                        self._clear_combat_turn_actions()
                    await self._route_result(decision, result)
                    continue
                if self._should_execute_before_commentary(decision, state=decision_state):
                    result = await self._execute_action(decision)
                    self._record_history(decision=decision, result=result)
                    self._remember_successful_action(decision, result)
                    if bool(result.get("ok", True)) and not result.get("error"):
                        commentary_state = self._post_action_commentary_state(decision_state, result)
                        await self._route_decision(
                            decision,
                            state=commentary_state,
                            available_actions=self._post_action_available_actions(result, available_actions),
                        )
                        self._remove_pending_live_replies(
                            _live_reply_keys(commentary_state.get("pending_live_replies", []))
                        )
                    await self._route_result(decision, result)
                    continue
                self._remove_pending_live_replies(pending_live_reply_keys)
                pending = self.queue_pending_decision(
                    decision,
                    state=decision_state,
                    available_actions=available_actions,
                )
                await self._route_decision(decision, state=decision_state, available_actions=available_actions)
                result = await self._wait_for_pending_result(pending, decision)
                await self._route_result(decision, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._active = False
            self._log_exception(f"STS2 controller stopped after error: {exc}")
            await self._route_text(f"STS2 游玩遇到错误，已暂停：{exc}", event_type="sts2_error")
        finally:
            self._log_info("STS2 controller loop stopped")
            self._active = False
            self._clear_pending()
            with contextlib.suppress(Exception):
                await self.mcp_client.stop()

    async def _wait_for_pending_result(
        self,
        pending: asyncio.Future[dict[str, Any]],
        decision: STS2Decision,
    ) -> dict[str, Any]:
        if not self.settings.sts2.narration.action_on_audio_start:
            return await self.notify_commentary_audio_started(decision.decision_id)
        return await pending

    async def _route_decision(
        self,
        decision: STS2Decision,
        *,
        state: Mapping[str, Any],
        available_actions: list[dict[str, Any]],
    ) -> None:
        action_text = _format_action(decision.action_kwargs())
        pending_live_replies = _coerce_live_reply_list(state.get("pending_live_replies"))
        pending_live_reply_text = _format_pending_live_replies(pending_live_replies)
        if self._should_route_combat_turn_summary(decision, state=state):
            payload = {
                "decision": decision.action_kwargs(),
                "state": dict(state),
                "pending_live_replies": pending_live_replies,
                "combat_turn_actions": _combat_turn_action_payloads(self._combat_turn_actions),
                "execution_plan": {
                    "type": "combat_turn_summary",
                    "execute_pending_action_on": "audio_complete",
                },
            }
            text = (
                "[Slay the Spire 2] Speak as first-person livestream commentary. "
                "Summarize the whole combat turn in one flowing reply while also responding to the pending live messages.\n"
                "{combat_turn_summary}\n"
                "These combat actions have already been executed this turn:\n"
                f"{_format_combat_turn_actions(self._combat_turn_actions)}\n"
                "The prepared step below has not executed yet and will happen right after this spoken summary finishes:\n"
                f"{action_text}\n"
                "Do not mention external models, plugins, tools, JSON, or that you were given a list.\n"
                "Treat the earlier actions as already happened, and treat the final end-turn step as the immediate follow-up after the summary.\n"
                "{pending_live_replies}\n"
                f"{pending_live_reply_text}\n"
                "Prioritize super chats, memberships, and gifts before ordinary danmaku."
            )
            while True:
                await self._wait_for_gateway_ready()
                try:
                    await self._route_text(
                        text,
                        event_type="sts2_decision",
                        decision_id=decision.decision_id,
                        payload=payload,
                    )
                    return
                except Exception as exc:
                    if not _is_gateway_not_ready_error(exc):
                        raise
                    if self._runtime_state is None:
                        raise
                    self._mark_gateway_not_ready_locally()
                    self._log_warning(
                        "STS2 decision routing is waiting for the live gateway to recover: "
                        f"decision_id={decision.decision_id} action={decision.action_kwargs()} error={exc}"
                    )
        if self._pending_treasure_chest_sequence is not None:
            payload = {
                "decision": decision.action_kwargs(),
                "state": dict(state),
                "pending_live_replies": pending_live_replies,
                "execution_plan": {"type": "treasure_chest", "min_segments": 3},
            }
            text = (
                "銆愭潃鎴皷濉?銆戣鐢ㄧ涓€浜虹О鍋氫竴娈靛畬鏁寸殑瀹濈鎴跨洿鎾В璇达紝鎶婂紑绠便€佹嬁璧版渶浼樺疂鐗┿€佺寮€鎴块棿璁叉垚涓€娈佃繛缁殑璇濄€俓n"
                "娉ㄦ剰锛氫綘寮€鍙ｆ椂浼氬悓姝ュ紑绠憋紱瑙ｈ杩囧崐鏃朵細鍚屾鎷垮彇瀹濈鐗╁搧锛涙暣娈佃В璇寸粨鏉熸椂浼氬悓姝ョ寮€鎴块棿銆俓n"
                "涓嶈鎻愬埌澶栭儴妯″瀷銆佹彃浠舵垨 JSON 瀛楁锛屼篃涓嶈璇翠綘姝ｅ噯澶囪鍋氫粈涔堛€俓n"
                "璇疯嚜鐒跺湴鎶婃父鎴忓垽鏂拰瀵瑰脊骞曠殑鍥炲簲铻嶅悎鍦ㄤ竴璧枫€俓n"
                "{鐩存挱闂村緟鍥炲寮瑰箷鍒楄〃}\n"
                f"{pending_live_reply_text}\n"
                "浼樺厛鍥炲簲 SC銆佽埌闀垮拰绀肩墿锛涙櫘閫氬脊骞曟嫨瑕佸洖搴斻€?"
            )
            while True:
                await self._wait_for_gateway_ready()
                try:
                    await self._route_text(
                        text,
                        event_type="sts2_decision",
                        decision_id=decision.decision_id,
                        payload=payload,
                    )
                    return
                except Exception as exc:
                    if not _is_gateway_not_ready_error(exc):
                        raise
                    if self._runtime_state is None:
                        raise
                    self._mark_gateway_not_ready_locally()
                    self._log_warning(
                        "STS2 decision routing is waiting for the live gateway to recover: "
                        f"decision_id={decision.decision_id} action={decision.action_kwargs()} error={exc}"
                    )
        text = (
            "【杀戮尖塔2】请用第一人称自然直播解说，把游戏决策和弹幕回应融合成一段，"
            "不要提到外部模型、插件或 JSON 字段。\n"
            "{sts2决策}\n"
            "注意：这一步决策在你开口解说时已经同步执行，请直接按执行后的视角解说。\n"
            f"已执行的操作：{action_text}。\n"
            "不要说“我准备”“我打算”“我接下来要”，而要像动作刚刚已经做完那样继续解说。\n"
            "请根据当前游戏状态和刚刚已经执行的动作，自然组织解说与回复。\n"
            "{在做出决策的同时直播间待回复的弹幕列表}\n"
            f"{pending_live_reply_text}\n"
            "优先回应 SC、舰长和礼物；普通弹幕择要回应。"
        )
        while True:
            await self._wait_for_gateway_ready()
            try:
                await self._route_text(
                    text,
                    event_type="sts2_decision",
                    decision_id=decision.decision_id,
                    payload={
                        "decision": decision.action_kwargs(),
                        "state": dict(state),
                        "pending_live_replies": pending_live_replies,
                    },
                )
                return
            except Exception as exc:
                if not _is_gateway_not_ready_error(exc):
                    raise
                if self._runtime_state is None:
                    raise
                self._mark_gateway_not_ready_locally()
                self._log_warning(
                    "STS2 decision routing is waiting for the live gateway to recover: "
                    f"decision_id={decision.decision_id} action={decision.action_kwargs()} error={exc}"
                )

    async def _route_result(self, decision: STS2Decision, result: Mapping[str, Any]) -> None:
        ok = bool(result.get("ok", True)) and not result.get("error")
        if ok:
            return
        action_text = _format_action(decision.action_kwargs())
        text = f"【杀戮尖塔2】刚才的操作 {action_text} 没有成功：{result.get('error', '未知错误')}。请说明并重新判断。"
        try:
            await self._route_text(
                text,
                event_type="sts2_result",
                payload={"decision_id": decision.decision_id, "result": dict(result)},
            )
        except Exception as exc:
            if _is_gateway_not_ready_error(exc):
                self._log_warning(
                    "Suppressed STS2 result routing because the live gateway is not ready: "
                    f"decision_id={decision.decision_id} action={decision.action_kwargs()} error={exc}"
                )
                return
            raise

    async def _execute_action(self, decision: STS2Decision) -> dict[str, Any]:
        result = await self.mcp_client.act(**decision.action_kwargs())
        with contextlib.suppress(Exception):
            result = {**result, "state_after": await self.mcp_client.get_game_state()}
        with contextlib.suppress(Exception):
            result = {**result, "available_actions_after": await self.mcp_client.get_available_actions()}
        return result

    async def _resolve_treasure_chest_reward_decision(
        self,
        sequence: _PendingTreasureChestSequence,
    ) -> STS2Decision | None:
        if sequence.reward_decision is not None:
            return sequence.reward_decision
        task = sequence.reward_plan_task
        if task is not None and task.done():
            with contextlib.suppress(Exception):
                sequence.reward_decision = task.result()
                return sequence.reward_decision
        if task is None:
            task = asyncio.create_task(
                self._plan_treasure_chest_reward_decision(sequence),
                name=f"sts2_treasure_chest_reward_plan_{sequence.decision_id}",
            )
            sequence.reward_plan_task = task
        try:
            sequence.reward_decision = await task
        except Exception as exc:
            self._log_warning(f"Failed to plan treasure chest reward decision: {exc}")
            sequence.reward_decision = None
        return sequence.reward_decision

    async def _plan_treasure_chest_reward_decision(
        self,
        sequence: _PendingTreasureChestSequence,
    ) -> STS2Decision | None:
        state_after = sequence.open_result.get("state_after") if sequence.open_result is not None else None
        available_actions = sequence.open_result.get("available_actions_after") if sequence.open_result is not None else None
        normalized_state = dict(state_after) if isinstance(state_after, Mapping) else {}
        normalized_actions = [dict(item) for item in available_actions] if isinstance(available_actions, list) else []
        if self.decision_client is not None and normalized_actions:
            decision = await self.decision_client.decide(
                state=self._state_with_live_context(normalized_state),
                available_actions=normalized_actions,
                history=list(self._history),
            )
            if self._is_available_action(decision.action, normalized_actions):
                return decision
        return _build_fallback_treasure_chest_reward_decision(
            normalized_actions,
            decision_id=f"{sequence.decision_id}-reward",
        )

    def _build_treasure_chest_exit_decision(
        self,
        result: Mapping[str, Any],
    ) -> STS2Decision | None:
        available_actions = result.get("available_actions_after")
        normalized_actions = [dict(item) for item in available_actions] if isinstance(available_actions, list) else []
        for action_name in ("collect_rewards_and_proceed", "proceed", "claim_reward", "confirm_modal"):
            decision = _build_decision_from_available_actions(
                normalized_actions,
                action_name=action_name,
                decision_id=f"{self.pending_decision_id}-exit",
                reason="Leave the treasure room after taking the reward.",
            )
            if decision is not None:
                return decision
        return None

    def _build_treasure_chest_sequence_result(
        self,
        sequence: _PendingTreasureChestSequence,
        *,
        final_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        for step_decision, step_result in (
            (sequence.entry_decision, sequence.open_result),
            (sequence.reward_decision, sequence.reward_result),
            (sequence.exit_decision, final_result),
        ):
            if step_decision is None or step_result is None:
                continue
            steps.append({"action": step_decision.action_kwargs(), "result": dict(step_result)})
        return {
            "ok": all(bool(step["result"].get("ok", True)) and not step["result"].get("error") for step in steps),
            "sequence_type": "treasure_chest",
            "steps": steps,
            "state_after": dict(final_result.get("state_after") or {}),
            "available_actions_after": list(final_result.get("available_actions_after") or []),
        }

    async def _route_text(
        self,
        text: str,
        *,
        event_type: str,
        decision_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> bool:
        message = build_sts2_message_dict(
            self.settings,
            text=text,
            event_type=event_type,
            decision_id=decision_id,
            payload=payload or {},
        )
        message_id = str(message.get("message_id") or f"sts2-{uuid4().hex}")
        route_metadata = {
            "source": "sts2",
            "room_id": self.settings.bilibili.room_id,
            "selection_reason": "sts2_priority",
            "selection_score": 9999.0,
            "sts2_priority": True,
        }
        return await self.gateway.route_message(
            GATEWAY_NAME,
            message,
            route_metadata=route_metadata,
            external_message_id=message_id,
            dedupe_key=message_id,
        )

    def _record_history(self, *, decision: STS2Decision, result: Mapping[str, Any]) -> None:
        self._history.append(
            {
                "at": time.time(),
                "decision": decision.raw or decision.action_kwargs(),
                "reason": decision.reason,
                "result": dict(result),
            }
        )
        max_items = max(1, int(self.settings.sts2.narration.max_recent_steps))
        self._history = self._history[-max_items:]

    def _state_with_live_context(self, state: Mapping[str, Any]) -> dict[str, Any]:
        enriched = dict(state)
        if self._live_context:
            enriched["live_chat_context"] = [dict(item) for item in self._live_context]
        if self._pending_live_replies:
            enriched["pending_live_replies"] = _prioritize_live_replies(self._pending_live_replies)
        return enriched

    def _remove_pending_live_replies(self, handled_keys: set[str]) -> None:
        if not handled_keys:
            return
        self._pending_live_replies = [
            item for item in self._pending_live_replies if _live_reply_key(item) not in handled_keys
        ]

    def _clear_pending(self) -> None:
        sequence = self._pending_treasure_chest_sequence
        if sequence is not None and sequence.reward_plan_task is not None and not sequence.reward_plan_task.done():
            sequence.reward_plan_task.cancel()
        self._pending_decision = None
        self._pending_future = None
        self._pending_executing = False
        self._pending_treasure_chest_sequence = None

    async def _wait_for_gateway_ready(self) -> None:
        runtime_state = self._runtime_state
        if runtime_state is None or runtime_state.is_ready:
            return
        self._log_info("STS2 decision routing is waiting for the live gateway to become ready.")
        await runtime_state.wait_until_ready()

    def _mark_gateway_not_ready_locally(self) -> None:
        runtime_state = self._runtime_state
        if runtime_state is None:
            return
        with contextlib.suppress(Exception):
            runtime_state.mark_not_ready_locally()

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)

    def _log_info(self, message: str) -> None:
        if self.logger is not None:
            try:
                self.logger.info(message)
            except AttributeError:
                pass

    def _log_exception(self, message: str) -> None:
        if self.logger is not None:
            try:
                self.logger.exception(message)
                return
            except AttributeError:
                pass
            self._log_warning(message)

    @staticmethod
    def _extract_state(actionable: Mapping[str, Any]) -> dict[str, Any]:
        state = actionable.get("state")
        return dict(state) if isinstance(state, Mapping) else {}

    @staticmethod
    def _extract_actions(actionable: Mapping[str, Any]) -> list[dict[str, Any]]:
        actions = actionable.get("actions")
        if not isinstance(actions, list):
            return []
        return [dict(item) for item in actions if isinstance(item, Mapping)]

    @staticmethod
    def _is_available_action(action: str, available_actions: list[dict[str, Any]]) -> bool:
        normalized = str(action or "").strip().lower()
        if not normalized:
            return False
        return any(_action_name(item) == normalized for item in available_actions)


def _action_name(action: Mapping[str, Any]) -> str:
    for key in ("action", "name", "id"):
        value = str(action.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def _is_gateway_not_ready_error(exc: Exception) -> bool:
    text = str(exc)
    return (
        "E_METHOD_NOT_ALLOWED" in text
        and "尚未就绪" in text
        and "不能注入外部消息" in text
    )


def _is_gateway_not_ready_error(exc: Exception) -> bool:
    text = str(exc)
    return (
        "E_METHOD_NOT_ALLOWED" in text
        and "bilibili_live_gateway" in text
        and ("尚未就绪" in text or "灏氭湭灏辩华" in text)
    )


def _format_action(action_kwargs: Mapping[str, Any]) -> str:
    parts = [str(action_kwargs.get("action") or "").strip()]
    for key in ("card_index", "target_index", "option_index"):
        if key in action_kwargs:
            parts.append(f"{key}={action_kwargs[key]}")
    return ", ".join(part for part in parts if part)


def _combat_turn_action_payloads(actions: list[_CombatTurnActionRecord]) -> list[dict[str, Any]]:
    return [
        {
            "decision_id": action.decision_id,
            "action": dict(action.action),
            "reason": action.reason,
            "expected_result": action.expected_result,
        }
        for action in actions
    ]


def _format_combat_turn_actions(actions: list[_CombatTurnActionRecord]) -> str:
    if not actions:
        return "1. No earlier in-turn actions were executed before ending the turn."
    lines: list[str] = []
    for index, action in enumerate(actions, start=1):
        line = f"{index}. executed {_format_action(action.action)}"
        if action.reason:
            line = f"{line} | reason: {action.reason}"
        if action.expected_result:
            line = f"{line} | expected: {action.expected_result}"
        lines.append(line)
    return "\n".join(lines)


def _treasure_chest_midpoint_segment_index(segment_count: int) -> int:
    normalized_count = max(1, int(segment_count))
    return (normalized_count // 2) + 1


def _build_fallback_treasure_chest_reward_decision(
    available_actions: list[dict[str, Any]],
    *,
    decision_id: str,
) -> STS2Decision | None:
    return _build_decision_from_available_actions(
        available_actions,
        action_name="choose_treasure_relic",
        decision_id=decision_id,
        reason="Fallback to the first available treasure chest reward.",
    )


def _build_decision_from_available_actions(
    available_actions: list[dict[str, Any]],
    *,
    action_name: str,
    decision_id: str,
    reason: str,
) -> STS2Decision | None:
    normalized_action_name = str(action_name or "").strip().lower()
    if not normalized_action_name:
        return None
    for action in available_actions:
        if _action_name(action) != normalized_action_name:
            continue
        return STS2Decision(
            decision_id=decision_id,
            action=normalized_action_name,
            reason=reason,
            narration="",
            card_index=_optional_int(action.get("card_index")),
            target_index=_optional_int(action.get("target_index")),
            option_index=_optional_int(action.get("option_index")),
            expected_result="",
            raw=dict(action),
        )
    return None


def _merge_decision_with_available_actions(
    decision: STS2Decision,
    available_actions: list[dict[str, Any]],
) -> STS2Decision:
    if not available_actions:
        return decision
    matching_actions = [
        dict(action)
        for action in available_actions
        if _candidate_matches_decision(action, decision)
    ]
    if len(matching_actions) == 1:
        return _merge_decision_with_action(decision, matching_actions[0])
    if len(available_actions) == 1:
        return _merge_decision_with_action(decision, available_actions[0])
    return decision


def _merge_decision_with_action(
    decision: STS2Decision,
    action: Mapping[str, Any],
) -> STS2Decision:
    merged_raw = {**dict(action), **dict(decision.raw)}
    merged_raw["_resolved_action"] = dict(action)
    return replace(
        decision,
        card_index=decision.card_index if decision.card_index is not None else _optional_int(action.get("card_index")),
        option_index=(
            decision.option_index if decision.option_index is not None else _optional_int(action.get("option_index"))
        ),
        target_index=(
            decision.target_index if decision.target_index is not None else _optional_int(action.get("target_index"))
        ),
        raw=merged_raw,
    )


def _candidate_matches_decision(
    action: Mapping[str, Any],
    decision: STS2Decision,
) -> bool:
    if _action_name(action) != str(decision.action or "").strip().lower():
        return False
    action_name = str(decision.action or "").strip().lower()
    if action_name == "play_card":
        if decision.card_index is not None and _optional_int(action.get("card_index")) != int(decision.card_index):
            return False
    elif action_name in _OPTION_INDEX_ACTIONS:
        if decision.option_index is not None and _optional_int(action.get("option_index")) != int(decision.option_index):
            return False
    else:
        if decision.card_index is not None and _optional_int(action.get("card_index")) != int(decision.card_index):
            return False
        if decision.option_index is not None and _optional_int(action.get("option_index")) != int(decision.option_index):
            return False
    if decision.target_index is not None and _optional_int(action.get("target_index")) != int(decision.target_index):
        return False
    return True


def _character_choice_key(action: Mapping[str, Any]) -> str:
    for key in ("character_id", "character_name", "name", "label"):
        value = str(action.get(key) or "").strip()
        if value:
            return value
    option_index = _optional_int(action.get("option_index"))
    if option_index is not None:
        return f"option:{option_index}"
    return str(action)


def _resolved_character_choice_key(decision: STS2Decision) -> str:
    raw = decision.raw if isinstance(decision.raw, Mapping) else {}
    value = str(raw.get("_resolved_character_choice_key") or "").strip()
    if value:
        return value
    if decision.option_index is not None:
        return f"option:{decision.option_index}"
    return ""


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_live_reply_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _prioritize_live_replies(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: (_live_reply_priority_rank(pair[1]), pair[0]))
    return [dict(item) for _, item in indexed]


def _live_reply_priority_rank(item: Mapping[str, Any]) -> int:
    priority = str(item.get("priority") or "").strip()
    event_type = str(item.get("type") or "").strip()
    if priority == "super_chat" or event_type == "super_chat":
        return 0
    if priority in {"guard", "gift"} or event_type in {"guard", "gift"}:
        return 1
    return 2


def _format_pending_live_replies(items: list[dict[str, Any]]) -> str:
    if not items:
        return "（暂无待回复弹幕）"
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        event_type = str(item.get("type") or "danmaku").strip() or "danmaku"
        username = str(item.get("username") or item.get("user_id") or "anonymous").strip() or "anonymous"
        text = str(item.get("text") or item.get("summary") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if summary and summary != text:
            text = f"{text}（{summary}）"
        if not text:
            continue
        lines.append(f"{index}. [{event_type}] {username}: {text}")
    return "\n".join(lines) if lines else "（暂无待回复弹幕）"


def _live_reply_keys(value: Any) -> set[str]:
    return {_live_reply_key(item) for item in _coerce_live_reply_list(value)}


def _live_reply_key(item: Mapping[str, Any]) -> str:
    event_id = str(item.get("event_id") or "").strip()
    if event_id:
        return f"id:{event_id}"
    user_id = str(item.get("user_id") or "").strip()
    text = str(item.get("text") or item.get("summary") or "").strip()
    return f"fallback:{user_id}:{text}"


def _build_live_context_item(event: Mapping[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "").strip()
    text = sanitize_model_reserved_tokens(str(event.get("text") or ""))
    summary = sanitize_model_reserved_tokens(str(event.get("summary") or text))
    if not text and not summary:
        return {}
    priority = "super_chat" if event_type == "super_chat" else "normal"
    if event_type in {"gift", "guard"}:
        priority = event_type
    return {
        "event_id": str(event.get("event_id") or "").strip(),
        "type": event_type,
        "text": text or summary,
        "summary": summary,
        "username": str(event.get("username") or "").strip(),
        "user_id": str(event.get("user_id") or "").strip(),
        "priority": priority,
    }

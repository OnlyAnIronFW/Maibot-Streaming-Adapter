"""Separate LLM client for STS2 decisions."""

from __future__ import annotations

from typing import Any, Mapping

import json
import re
from uuid import uuid4

from openai import AsyncOpenAI

from src.config.config import config_manager
from src.config.model_configs import APIProvider, ModelInfo
from src.llm_models.openai_compat import build_openai_compatible_client_config, split_openai_request_overrides

from .config import STS2LLMConfig
from .sts2_controller import STS2Decision


class STS2DecisionClient:
    """Call a configured OpenAI-compatible model for STS2 action decisions."""

    def __init__(self, config: STS2LLMConfig, *, logger: Any = None) -> None:
        self.config = config
        self.logger = logger

    async def decide(
        self,
        *,
        state: Mapping[str, Any],
        available_actions: list[dict[str, Any]],
        history: list[dict[str, Any]],
    ) -> STS2Decision:
        provider, model_identifier, model_extra_params = self._resolve_provider_and_model()
        self._log_info(
            "STS2 decision LLM resolved: "
            f"provider={getattr(provider, 'name', self.config.api_provider)!r} model={model_identifier!r} "
            f"enable_thinking={bool(self.config.enable_thinking)}"
        )
        client_config = build_openai_compatible_client_config(provider)
        request_overrides = split_openai_request_overrides(
            {
                **model_extra_params,
                "enable_thinking": bool(self.config.enable_thinking),
            }
        )
        client = AsyncOpenAI(
            api_key=client_config.api_key,
            base_url=client_config.base_url,
            timeout=self.config.timeout_sec,
            max_retries=provider.max_retry,
            default_headers=client_config.default_headers or None,
            default_query=client_config.default_query or None,
        )
        prompt = build_sts2_decision_prompt(
            state=state,
            available_actions=available_actions,
            history=history,
        )
        try:
            self._log_info(
                "Requesting STS2 decision: "
                f"state_keys={list(state)[:20]} available_actions={len(available_actions)} history={len(history)}"
            )
            response = await client.chat.completions.create(
                model=model_identifier,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are controlling Slay the Spire 2 through a constrained MCP tool. "
                            "Return only JSON. Do not include hidden reasoning or chain-of-thought."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                response_format={"type": "json_object"},
                extra_headers=request_overrides.extra_headers or None,
                extra_query=request_overrides.extra_query or None,
                extra_body=request_overrides.extra_body or None,
            )
            content = response.choices[0].message.content if response.choices else ""
            decision = parse_sts2_decision_response(str(content or ""))
            self._log_info(f"STS2 decision parsed: decision_id={decision.decision_id} action={decision.action_kwargs()}")
            return decision
        except Exception:
            self._log_exception("STS2 decision LLM request failed")
            raise

    def _resolve_provider_and_model(self) -> tuple[APIProvider, str, dict[str, Any]]:
        model_config = config_manager.get_model_config()
        models_by_name = {model.name: model for model in model_config.models}
        providers_by_name = {provider.name: provider for provider in model_config.api_providers}

        model_info: ModelInfo | None = None
        if self.config.model_name:
            model_info = models_by_name.get(self.config.model_name)
            if model_info is None:
                raise RuntimeError(f"STS2 LLM model_name not found in model_config: {self.config.model_name}")

        if model_info is not None:
            provider = providers_by_name.get(model_info.api_provider)
            if provider is None:
                raise RuntimeError(f"STS2 LLM provider not found in model_config: {model_info.api_provider}")
            return provider, model_info.model_identifier, dict(model_info.extra_params or {})

        provider = providers_by_name.get(self.config.api_provider)
        if provider is None:
            raise RuntimeError(f"STS2 LLM api_provider not found in model_config: {self.config.api_provider}")
        model_identifier = self.config.model_identifier.strip()
        if not model_identifier:
            raise RuntimeError("STS2 LLM model_identifier is empty.")
        return provider, model_identifier, {}

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
            self._log_info(message)


def build_sts2_decision_prompt(
    *,
    state: Mapping[str, Any],
    available_actions: list[dict[str, Any]],
    history: list[dict[str, Any]],
) -> str:
    """Build the strict JSON decision prompt."""

    pending_live_replies = _coerce_live_reply_list(state.get("pending_live_replies"))
    payload = {
        "sts2_decision": {
            "game_state": state,
            "available_actions": available_actions,
            "recent_history": history,
        },
        "pending_live_replies": pending_live_replies,
    }
    instructions = (
        "Choose exactly one legal Slay the Spire 2 action from available_actions.\n"
        "Rules:\n"
        "- action must exactly match one available_actions item.\n"
        "- Do not invent unavailable actions, ids, or indexes.\n"
        "- Use card_index only for play_card and only from combat.hand[].index.\n"
        "- Use option_index for every other indexed action, including select_deck_card from selection.cards[].index.\n"
        "- Use target_index only when the selected available action needs a target.\n"
        "- Return strict JSON with exactly these keys: action, card_index, target_index, option_index, reason, narration, expected_result.\n"
        "- Keep reason short and concrete. Do not reveal chain-of-thought.\n"
        "- narration should sound like first-person live commentary for MaiBot.\n"
        "- pending_live_replies contains messages that still need a response.\n"
        "- reply to the pending live messages naturally inside narration while explaining the action.\n"
        "- Super chat suggestions are high priority. Consider them when tactically valid, but never choose an unavailable or bad action just because chat suggested it.\n"
        "- live_chat_context may provide extra viewer context.\n"
        "{sts2_decision}\n"
        "See sts2_decision below.\n"
        "{pending_live_replies}\n"
        "See pending_live_replies below.\n"
    )
    return f"{instructions}{json.dumps(payload, ensure_ascii=False, default=str)}"


def parse_sts2_decision_response(response_text: str) -> STS2Decision:
    """Parse a strict JSON decision response into a normalized decision."""

    payload = _extract_json_object(response_text)
    action = str(payload.get("action") or payload.get("name") or "").strip().lower()
    if not action:
        raise ValueError("STS2 decision response is missing action.")
    reason = str(payload.get("reason") or "").strip()
    narration = str(payload.get("narration") or payload.get("commentary") or reason).strip()
    return STS2Decision(
        decision_id=str(payload.get("decision_id") or f"sts2-decision-{uuid4().hex}"),
        action=action,
        card_index=_optional_int(payload.get("card_index")),
        target_index=_optional_int(payload.get("target_index")),
        option_index=_optional_int(payload.get("option_index")),
        reason=reason or action,
        narration=narration or reason or action,
        expected_result=str(payload.get("expected_result") or "").strip(),
        raw=dict(payload),
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    normalized = str(text or "").strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?", "", normalized.strip(), flags=re.IGNORECASE).strip()
        normalized = re.sub(r"```$", "", normalized).strip()
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError:
        start = normalized.find("{")
        end = normalized.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(normalized[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("STS2 decision response must be a JSON object.")
    return payload


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_live_reply_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]

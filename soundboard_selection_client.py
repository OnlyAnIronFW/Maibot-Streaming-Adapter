"""Optional LLM selector for soundboard auto-reaction cues."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import json
import re

from openai import AsyncOpenAI

from src.config.config import config_manager
from src.config.model_configs import APIProvider, ModelInfo
from src.llm_models.openai_compat import build_openai_compatible_client_config, split_openai_request_overrides

from .config import SoundboardAutoSelectLLMConfig


@dataclass(frozen=True)
class SoundboardAutoSelectLLMResult:
    cue_id: str
    reason: str
    no_match: bool = False


class SoundboardAutoSelectClient:
    """Call a configured OpenAI-compatible model to choose one cue id."""

    def __init__(self, config: SoundboardAutoSelectLLMConfig, *, logger: Any = None) -> None:
        self.config = config
        self.logger = logger

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    async def select_cue(
        self,
        *,
        cue_summaries: list[dict[str, Any]],
        intent: str,
        reason: str,
        text: str,
    ) -> SoundboardAutoSelectLLMResult | None:
        if not self.enabled or not cue_summaries:
            return None
        provider, model_identifier, model_extra_params = self._resolve_provider_and_model()
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
        response = await client.chat.completions.create(
            model=model_identifier,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You choose one existing soundboard cue for a livestream reaction tool. "
                        "Return strict JSON only. Never invent a cue id. "
                        "If nothing fits, return cue_id as an empty string and no_match as true."
                    ),
                },
                {
                    "role": "user",
                    "content": build_soundboard_auto_select_prompt(
                        cue_summaries=cue_summaries,
                        intent=intent,
                        reason=reason,
                        text=text,
                    ),
                },
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            response_format={"type": "json_object"},
            extra_headers=request_overrides.extra_headers or None,
            extra_query=request_overrides.extra_query or None,
            extra_body=request_overrides.extra_body or None,
        )
        content = str(response.choices[0].message.content or "") if response.choices else ""
        return parse_soundboard_auto_select_response(content)

    def _resolve_provider_and_model(self) -> tuple[APIProvider, str, dict[str, Any]]:
        model_config = config_manager.get_model_config()
        models_by_name = {model.name: model for model in model_config.models}
        providers_by_name = {provider.name: provider for provider in model_config.api_providers}

        model_info: ModelInfo | None = None
        if self.config.model_name:
            model_info = models_by_name.get(self.config.model_name)
            if model_info is None:
                raise RuntimeError(
                    f"Soundboard auto-select model_name not found in model_config: {self.config.model_name}"
                )

        if model_info is not None:
            provider = providers_by_name.get(model_info.api_provider)
            if provider is None:
                raise RuntimeError(
                    f"Soundboard auto-select provider not found in model_config: {model_info.api_provider}"
                )
            return provider, model_info.model_identifier, dict(model_info.extra_params or {})

        provider = providers_by_name.get(self.config.api_provider)
        if provider is None:
            raise RuntimeError(
                f"Soundboard auto-select api_provider not found in model_config: {self.config.api_provider}"
            )
        model_identifier = str(self.config.model_identifier or "").strip()
        if not model_identifier:
            raise RuntimeError("Soundboard auto-select model_identifier is empty.")
        return provider, model_identifier, {}


def build_soundboard_auto_select_prompt(
    *,
    cue_summaries: list[dict[str, Any]],
    intent: str,
    reason: str,
    text: str,
) -> str:
    payload = {
        "intent": str(intent or "").strip(),
        "reason": str(reason or "").strip(),
        "text": str(text or "").strip(),
        "available_cues": [
            {
                "id": str(item.get("id") or "").strip(),
                "label": str(item.get("label") or "").strip(),
                "keywords": list(item.get("keywords") or []),
                "usage_hint": str(item.get("usage_hint") or "").strip(),
            }
            for item in cue_summaries
            if str(item.get("id") or "").strip()
        ],
    }
    return (
        "Pick the best matching cue for this reaction moment.\n"
        "Return strict JSON with exactly these keys: cue_id, reason, no_match.\n"
        "Rules:\n"
        "- cue_id must be one of available_cues[].id exactly, or an empty string when nothing fits.\n"
        "- no_match must be true only when no available cue is a good fit.\n"
        "- Prefer semantic fit from usage_hint, then keywords, then label.\n"
        "- Keep reason short.\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def parse_soundboard_auto_select_response(response_text: str) -> SoundboardAutoSelectLLMResult:
    payload = _extract_json_object(response_text)
    cue_id = str(payload.get("cue_id") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    no_match = bool(payload.get("no_match")) if cue_id else bool(payload.get("no_match", True))
    return SoundboardAutoSelectLLMResult(cue_id=cue_id, reason=reason, no_match=no_match)


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
        raise ValueError("Soundboard auto-select response must be a JSON object.")
    return payload

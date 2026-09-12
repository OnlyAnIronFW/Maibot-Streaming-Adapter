"""Auxiliary LLM client for live-topic expansion and seed extraction."""

from __future__ import annotations

from typing import Any

import json
import re

from openai import AsyncOpenAI

from src.config.config import config_manager
from src.config.model_configs import APIProvider, ModelInfo
from src.llm_models.openai_compat import build_openai_compatible_client_config, split_openai_request_overrides

from .config import TopicExtensionConfig
from .topic_state import TopicExpansionResult


class TopicExtensionClient:
    """Call a dedicated OpenAI-compatible model for live-topic continuation support."""

    def __init__(self, config: TopicExtensionConfig, *, logger: Any = None) -> None:
        self.config = config
        self.logger = logger

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    async def expand_topic(
        self,
        *,
        current_topic: str,
        recent_timeline: list[dict[str, Any]],
        recent_viewer_messages: list[str],
        recent_bot_outputs: list[str],
    ) -> TopicExpansionResult | None:
        if not self.enabled or not str(current_topic or "").strip():
            return None
        prompt = build_topic_expansion_prompt(
            current_topic=current_topic,
            recent_timeline=recent_timeline,
            recent_viewer_messages=recent_viewer_messages,
            recent_bot_outputs=recent_bot_outputs,
        )
        raw_text = await self._request_json(prompt)
        return parse_topic_expansion_result(raw_text)

    async def extract_seed_topic(
        self,
        *,
        recent_timeline: list[dict[str, Any]],
        recent_viewer_messages: list[str],
        recent_bot_outputs: list[str],
    ) -> TopicExpansionResult | None:
        if not self.enabled or not recent_viewer_messages:
            return None
        prompt = build_seed_topic_prompt(
            recent_timeline=recent_timeline,
            recent_viewer_messages=recent_viewer_messages,
            recent_bot_outputs=recent_bot_outputs,
        )
        raw_text = await self._request_json(prompt)
        return parse_topic_expansion_result(raw_text)

    async def _request_json(self, prompt: str) -> str:
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
                        "You help a livestream host continue conversations naturally. "
                        "Return strict JSON only. Do not reveal hidden reasoning. "
                        "Prefer playful livestream banter, meme hooks, surreal pivots, fake-malfunction bits, "
                        "and chatty audience interaction over formal analysis or article-style framing."
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
        return str(response.choices[0].message.content or "") if response.choices else ""

    def _resolve_provider_and_model(self) -> tuple[APIProvider, str, dict[str, Any]]:
        model_config = config_manager.get_model_config()
        models_by_name = {model.name: model for model in model_config.models}
        providers_by_name = {provider.name: provider for provider in model_config.api_providers}

        model_info: ModelInfo | None = None
        if self.config.model_name:
            model_info = models_by_name.get(self.config.model_name)
            if model_info is None:
                raise RuntimeError(f"Topic extension model_name not found in model_config: {self.config.model_name}")

        if model_info is not None:
            provider = providers_by_name.get(model_info.api_provider)
            if provider is None:
                raise RuntimeError(f"Topic extension provider not found in model_config: {model_info.api_provider}")
            return provider, model_info.model_identifier, dict(model_info.extra_params or {})

        provider = providers_by_name.get(self.config.api_provider)
        if provider is None:
            raise RuntimeError(f"Topic extension api_provider not found in model_config: {self.config.api_provider}")
        model_identifier = str(self.config.model_identifier or "").strip()
        if not model_identifier:
            raise RuntimeError("Topic extension model_identifier is empty.")
        return provider, model_identifier, {}


def build_topic_expansion_prompt(
    *,
    current_topic: str,
    recent_timeline: list[dict[str, Any]],
    recent_viewer_messages: list[str],
    recent_bot_outputs: list[str],
) -> str:
    payload = {
        "current_topic": str(current_topic or "").strip(),
        "recent_timeline": recent_timeline,
        "recent_viewer_messages": list(recent_viewer_messages),
        "recent_bot_outputs": list(recent_bot_outputs),
    }
    return (
        "The current livestream topic is close to wrapping up. "
        "Expand it into a naturally related next topic instead of ending the chat.\n"
        "Return strict JSON with keys: current_topic, related_topic, expansion_angle, handoff_prompt, why_related.\n"
        "Requirements:\n"
        "- related_topic must be clearly and strongly connected to current_topic.\n"
        "- related_topic must be a short, chatty hook instead of a formal title, lecture topic, article heading, or podcast episode name.\n"
        "- Prefer meme riffs, surreal turns, fake malfunction bits, playful teasing, weird hypotheticals, viewer call-outs, or awkward chaotic pivots that still match the topic.\n"
        "- Avoid academic, psychology-class, self-help, therapy, article, podcast, panel-discussion, or essay wording unless the chat itself already forced that style; even then, twist it into a more jokeable livestream angle.\n"
        "- If the current topic is serious, convert it into a more grounded, more playful, more streamer-style hook before expanding it.\n"
        "- handoff_prompt must tell the host how to bridge naturally, keep the conversation open, and sound like live banter rather than an outline.\n"
        "- Use the dominant language and vibe already present in current_topic and recent messages.\n"
        "- Do not dead-end the chat.\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def build_seed_topic_prompt(
    *,
    recent_timeline: list[dict[str, Any]],
    recent_viewer_messages: list[str],
    recent_bot_outputs: list[str],
) -> str:
    payload = {
        "recent_timeline": recent_timeline,
        "recent_viewer_messages": list(recent_viewer_messages),
        "recent_bot_outputs": list(recent_bot_outputs),
    }
    return (
        "No previous stable topic is available. "
        "Extract one concrete seed topic directly from the recent livestream chat.\n"
        "Return strict JSON with keys: current_topic, related_topic, expansion_angle, handoff_prompt, why_related.\n"
        "Requirements:\n"
        "- current_topic should be an empty string.\n"
        "- related_topic is the extracted seed topic.\n"
        "- related_topic must be a short, concrete, streamer-style chat hook instead of a formal title.\n"
        "- Prefer hooks that are easy to joke about, riff on, exaggerate, fake-break, or turn into a weird what-if.\n"
        "- Avoid abstract, academic, self-help, therapy, article, or podcast wording.\n"
        "- handoff_prompt must keep the chat open, easy for viewers to continue, and sound like a live streamer setting up a bit.\n"
        "- Use the dominant language and vibe already present in recent messages.\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def parse_topic_expansion_result(response_text: str) -> TopicExpansionResult | None:
    try:
        payload = _extract_json_object(response_text)
    except Exception:
        return None
    return TopicExpansionResult.from_dict(payload)


def _extract_json_object(text: str) -> dict[str, Any]:
    normalized = str(text or "").strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?", "", normalized, flags=re.IGNORECASE).strip()
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
        raise ValueError("Topic expansion response must be a JSON object.")
    return payload

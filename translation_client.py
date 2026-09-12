"""Subtitle translation client for bilingual Bilibili live replies."""

from __future__ import annotations

import inspect
import re
from typing import Any

from openai import AsyncOpenAI

from src.config.config import config_manager
from src.config.model_configs import APIProvider, ModelInfo
from src.llm_models.openai_compat import build_openai_compatible_client_config, split_openai_request_overrides

from .config import SubtitleTranslationConfig


class SubtitleTranslationClient:
    """Translate spoken replies into Simplified Chinese subtitles."""

    def __init__(self, config: SubtitleTranslationConfig, *, logger: Any = None) -> None:
        self.config = config
        self.logger = logger
        self._resolved_provider: APIProvider | None = None
        self._resolved_model_identifier: str = ""
        self._resolved_model_extra_params: dict[str, Any] | None = None
        self._request_overrides: Any = None
        self._client: AsyncOpenAI | None = None

    async def start(self) -> None:
        self._ensure_client()

    async def stop(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        close_method = getattr(client, "close", None)
        if close_method is None:
            return
        result = close_method()
        if inspect.isawaitable(result):
            await result

    async def translate_to_chinese(self, text: str, *, source_language: str = "English") -> str:
        source_text = str(text or "").strip()
        if not source_text:
            return ""
        normalized_source_language = _normalize_source_language(source_language)
        provider, model_identifier, model_extra_params = self._resolve_provider_and_model()
        self._log_info(
            "Subtitle translation LLM resolved: "
            f"provider={getattr(provider, 'name', self.config.api_provider)!r} model={model_identifier!r} "
            f"enable_thinking={bool(self.config.enable_thinking)} source_language={normalized_source_language!r}"
        )
        client = self._ensure_client()
        request_overrides = self._request_overrides
        assert request_overrides is not None
        try:
            if _uses_qwen_mt_native_translation(model_identifier):
                translation_options = dict(request_overrides.extra_body.get("translation_options") or {})
                translation_options.setdefault("source_lang", normalized_source_language)
                translation_options.setdefault("target_lang", "Chinese")
                extra_body = dict(request_overrides.extra_body)
                extra_body.pop("enable_thinking", None)
                extra_body["translation_options"] = translation_options
                response = await client.chat.completions.create(
                    model=model_identifier,
                    messages=[{"role": "user", "content": source_text}],
                    extra_headers=request_overrides.extra_headers or None,
                    extra_query=request_overrides.extra_query or None,
                    extra_body=extra_body or None,
                )
                content = response.choices[0].message.content if response.choices else ""
                translated = _clean_translation_response(str(content or ""))
                if _should_retry_cantonese_translation(
                    source_text,
                    translated,
                    source_language=normalized_source_language,
                ):
                    self._log_info(
                        "Subtitle translation looked untranslated in Cantonese mode; retrying with prompt fallback."
                    )
                    translated = await _request_prompt_translation(
                        client,
                        model_identifier=model_identifier,
                        source_text=source_text,
                        source_language=normalized_source_language,
                        temperature=self.config.temperature,
                        max_tokens=self.config.max_tokens,
                        request_overrides=request_overrides,
                    )
            else:
                translated = await _request_prompt_translation(
                    client,
                    model_identifier=model_identifier,
                    source_text=source_text,
                    source_language=normalized_source_language,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    request_overrides=request_overrides,
                )
            if translated:
                self._log_info("Subtitle translation completed.")
            return translated
        except Exception:
            self._log_exception("Subtitle translation LLM request failed")
            raise

    def _resolve_provider_and_model(self) -> tuple[APIProvider, str, dict[str, Any]]:
        if (
            self._resolved_provider is not None
            and self._resolved_model_identifier
            and self._resolved_model_extra_params is not None
        ):
            return (
                self._resolved_provider,
                self._resolved_model_identifier,
                dict(self._resolved_model_extra_params),
            )
        model_config = config_manager.get_model_config()
        models_by_name = {model.name: model for model in model_config.models}
        providers_by_name = {provider.name: provider for provider in model_config.api_providers}

        model_info: ModelInfo | None = None
        if self.config.model_name:
            model_info = models_by_name.get(self.config.model_name)
            if model_info is None:
                raise RuntimeError(f"Subtitle translation model_name not found in model_config: {self.config.model_name}")

        if model_info is not None:
            provider = providers_by_name.get(model_info.api_provider)
            if provider is None:
                raise RuntimeError(f"Subtitle translation provider not found in model_config: {model_info.api_provider}")
            model_extra_params = dict(model_info.extra_params or {})
            self._resolved_provider = provider
            self._resolved_model_identifier = model_info.model_identifier
            self._resolved_model_extra_params = dict(model_extra_params)
            return provider, model_info.model_identifier, model_extra_params

        provider = providers_by_name.get(self.config.api_provider)
        if provider is None:
            raise RuntimeError(f"Subtitle translation api_provider not found in model_config: {self.config.api_provider}")
        model_identifier = self.config.model_identifier.strip()
        if not model_identifier:
            raise RuntimeError("Subtitle translation model_identifier is empty.")
        self._resolved_provider = provider
        self._resolved_model_identifier = model_identifier
        self._resolved_model_extra_params = {}
        return provider, model_identifier, {}

    def _ensure_client(self) -> AsyncOpenAI:
        if self._client is not None and self._request_overrides is not None:
            return self._client
        provider, _, model_extra_params = self._resolve_provider_and_model()
        client_config = build_openai_compatible_client_config(provider)
        self._request_overrides = split_openai_request_overrides(
            {
                **model_extra_params,
                "enable_thinking": bool(self.config.enable_thinking),
            }
        )
        self._client = AsyncOpenAI(
            api_key=client_config.api_key,
            base_url=client_config.base_url,
            timeout=self.config.timeout_sec,
            max_retries=provider.max_retry,
            default_headers=client_config.default_headers or None,
            default_query=client_config.default_query or None,
        )
        return self._client

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


def _clean_translation_response(text: str) -> str:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:text|markdown|md)?", "", cleaned.strip(), flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


async def _request_prompt_translation(
    client: AsyncOpenAI,
    *,
    model_identifier: str,
    source_text: str,
    source_language: str,
    temperature: float,
    max_tokens: int,
    request_overrides: Any,
) -> str:
    extra_body = dict(request_overrides.extra_body or {})
    extra_body.pop("enable_thinking", None)
    extra_body.pop("translation_options", None)
    if _uses_qwen_mt_native_translation(model_identifier):
        messages = [
            {
                "role": "user",
                "content": _build_translation_user_message(
                    source_text=source_text,
                    source_language=source_language,
                ),
            }
        ]
    else:
        messages = [
            {
                "role": "user",
                "content": _build_translation_user_message(
                    source_text=source_text,
                    source_language=source_language,
                ),
            }
        ]
    response = await client.chat.completions.create(
        model=model_identifier,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_headers=request_overrides.extra_headers or None,
        extra_query=request_overrides.extra_query or None,
        extra_body=extra_body or None,
    )
    content = response.choices[0].message.content if response.choices else ""
    translated = _clean_translation_response(str(content or ""))
    if not _contains_chinese(translated):
        return ""  # fallback to original text
    return translated


def _build_translation_system_prompt(source_language: str) -> str:
    return (
        f"Translate {source_language} live-stream captions into natural Simplified Chinese subtitles. "
        "Keep names, memes, and technical terms concise. Return only the Chinese subtitle text; "
        "do not include quotes, notes, markdown, or explanations."
    )


def _build_translation_user_message(*, source_text: str, source_language: str) -> str:
    return (
        f"{_build_translation_system_prompt(source_language)}\n\n"
        f"Source text:\n{source_text}"
    )


def _normalize_source_language(source_language: str) -> str:
    normalized = str(source_language or "").strip().lower()
    if normalized in {"cantonese", "yue", "\u7ca4\u8bed", "\u5ee3\u6771\u8a71", "\u5e7f\u4e1c\u8bdd"}:
        return "Cantonese"
    return "English"


def _uses_qwen_mt_native_translation(model_identifier: str) -> bool:
    return str(model_identifier or "").strip().lower().startswith("qwen-mt-")


def _contains_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))


def _should_retry_cantonese_translation(source_text: str, translated_text: str, *, source_language: str) -> bool:
    if source_language != "Cantonese":
        return False
    normalized_source = _normalize_translation_compare_text(source_text)
    normalized_translated = _normalize_translation_compare_text(translated_text)
    if not normalized_source or not normalized_translated:
        return False
    if normalized_source == normalized_translated:
        return True
    return any(marker in translated_text for marker in _CANTONESE_UNTRANSLATED_MARKERS)


def _normalize_translation_compare_text(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text or "")).lower()


_CANTONESE_UNTRANSLATED_MARKERS = (
    "哋",
    "咗",
    "喺",
    "冇",
    "啲",
    "咩",
    "嗰",
    "而家",
    "唔",
    "嚟",
    "嘅",
    "咁",
    "係咪",
    "喎",
)

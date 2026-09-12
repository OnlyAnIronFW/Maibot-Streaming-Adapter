"""Controller for local microphone speech input and its native control window."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncio
import contextlib
import time
import tomllib

import tomlkit

from .config import LiveAdapterSettings, LocalVoiceInputConfig
from .local_voice_input import LocalVoiceInputService
from .local_voice_state import (
    LocalVoiceModelOption,
    LocalVoiceSettingsView,
    LocalVoiceSnapshot,
    LocalVoiceTranscriptEntry,
)


_SENTENCE_END_PUNCTUATION = frozenset({".", "!", "?", "。", "！", "？"})
_SENTENCE_CLOSING_TAILS = frozenset({'"', "'", "”", "’", ")", "]", "}", "》", "」", "』"})
_QUESTION_ENDINGS = ("吗", "么", "嘛", "呢", "?", "？")
_QUESTION_HINTS = (
    "什么",
    "怎么",
    "为何",
    "为什么",
    "谁",
    "哪",
    "几",
    "how",
    "what",
    "why",
    "when",
    "where",
    "who",
    "which",
)
_BOT_ECHO_GUARD_WINDOW_SEC = 20.0
_BOT_ECHO_GUARD_DEDUPE_SEC = 1.0
_BOT_ECHO_GUARD_MIN_CHARS = 5
_BOT_ECHO_GUARD_SUBSTRING_MIN_CHARS = 6
_BOT_ECHO_GUARD_LONGEST_MATCH_RATIO = 0.72
_BOT_ECHO_GUARD_SEQUENCE_RATIO = 0.8


class LocalVoiceController:
    """Owns local voice runtime state, persistence, and UI-facing updates."""

    def __init__(
        self,
        *,
        settings: LiveAdapterSettings,
        on_transcript_route: Callable[[str, Mapping[str, Any] | None], Awaitable[bool]],
        on_state_change: Callable[[LocalVoiceSnapshot], None] | None = None,
        on_settings_changed: Callable[[LiveAdapterSettings], None] | None = None,
        config_path: Path | None = None,
        model_root: Path | None = None,
        service_factory: Callable[..., LocalVoiceInputService] | None = None,
        runtime_factory: Callable[..., Any] | None = None,
        device_query: Callable[[], Sequence[Any]] | None = None,
        logger: Any = None,
    ) -> None:
        self._settings = settings.model_copy(deep=True)
        self._route_callback = on_transcript_route
        self._state_callback = on_state_change
        self._settings_changed_callback = on_settings_changed
        self._config_path = Path(config_path) if config_path is not None else _plugin_dir() / "config.toml"
        self._model_root = Path(model_root) if model_root is not None else _plugin_dir() / "data" / "sherpa_models"
        self._service_factory = service_factory or LocalVoiceInputService
        self._runtime_factory = runtime_factory
        self._device_query = device_query
        self._logger = logger
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runtime: Any = None
        self._service: LocalVoiceInputService | None = None
        self._devices: list[str] = []
        self._models: list[LocalVoiceModelOption] = []
        self._current_display_text = ""
        self._transcript_log: list[LocalVoiceTranscriptEntry] = []
        self._last_error = ""
        self._selected_model_label = ""
        self._is_listening = False
        self._sentence_flush_task: asyncio.Task[None] | None = None
        self._pending_sentence_text = ""
        self._pending_sentence_chunk_count = 0
        self._pending_sentence_metadata: dict[str, Any] = {}
        self._sentence_route_sequence = 0
        self._routed_context_text = ""
        self._recent_bot_outputs: list[tuple[str, str, float]] = []

    async def start(self) -> None:
        """Start the control runtime and local voice listener when enabled."""

        self._loop = asyncio.get_running_loop()
        self.refresh_devices()
        self._models = []
        self._selected_model_label = ""
        runtime_factory = self._runtime_factory or _default_runtime_factory
        if runtime_factory is not None:
            try:
                self._runtime = runtime_factory(
                    on_refresh_devices=self._schedule_refresh_devices,
                    on_refresh_models=self._schedule_refresh_models,
                    on_toggle_listening=self._schedule_toggle_listening,
                    on_apply_settings=self._schedule_apply_settings,
                    on_clear_log=self.clear_transcript_log,
                    logger=self._logger,
                )
                await asyncio.to_thread(self._runtime.start)
                self._runtime.update_snapshot(self.snapshot())
            except Exception as exc:
                self._runtime = None
                self._last_error = f"Local voice control window failed to start: {exc}"
        if self._settings.local_voice.enabled:
            await self.start_listening()
        else:
            self._publish_state()

    async def stop(self) -> None:
        """Stop listening and close the control runtime."""

        await self.stop_listening()
        self._cancel_sentence_flush_task()
        runtime = self._runtime
        self._runtime = None
        if runtime is not None:
            await asyncio.to_thread(runtime.stop)

    def snapshot(self) -> LocalVoiceSnapshot:
        """Return the current UI snapshot."""

        return LocalVoiceSnapshot(
            is_listening=self._is_listening,
            current_display_text=self._current_display_text,
            last_error=self._last_error,
            selected_model_label=self._selected_model_label,
            available_devices=tuple(self._devices),
            available_models=tuple(self._models),
            transcript_log=tuple(self._transcript_log),
            settings=_settings_view_from_config(self._settings.local_voice),
        )

    def refresh_devices(self) -> list[str]:
        """Refresh and return available microphone device names."""

        self._devices = list_input_devices(query_devices=self._device_query)
        self._publish_state()
        return list(self._devices)

    def refresh_models(self) -> list[LocalVoiceModelOption]:
        """Refresh and return discovered sherpa model presets."""

        self._models = discover_sherpa_models(self._model_root)
        self._selected_model_label = _match_model_label(self._models, self._settings.local_voice)
        self._publish_state()
        return list(self._models)

    async def handle_transcript(self, text: str, metadata: Mapping[str, Any] | None = None) -> bool:
        """Record transcript text for the UI and route it into MaiBot."""

        normalized = str(text or "").strip()
        if not normalized and bool((metadata or {}).get("speech_boundary", False)):
            if self._settings.local_voice.sentence_postprocess_enabled:
                accepted = await self._flush_pending_sentence(flush_reason="speech_boundary")
                self._publish_state()
                return bool(accepted)
            return True
        if not normalized:
            return False
        if self._looks_like_recent_bot_echo(normalized):
            self._publish_state()
            return True
        if self._loop is None:
            with contextlib.suppress(RuntimeError):
                self._loop = asyncio.get_running_loop()
        entry = LocalVoiceTranscriptEntry(
            timestamp_text=time.strftime("%H:%M:%S"),
            text=normalized,
            partial=bool((metadata or {}).get("partial", False)),
        )
        self._transcript_log.append(entry)
        self._transcript_log = self._transcript_log[-200:]
        if not bool((metadata or {}).get("route_to_maibot", True)):
            self._current_display_text = normalized
            self._publish_state()
            return True
        if self._settings.local_voice.sentence_postprocess_enabled:
            accepted = await self._handle_postprocessed_transcript(normalized, dict(metadata or {}))
            self._current_display_text = self._pending_sentence_text
        else:
            self._current_display_text = _join_stream_fragments(self._current_display_text, normalized)
            accepted = bool(await self._route_callback(normalized, dict(metadata or {})))
        self._publish_state()
        return bool(accepted)

    def clear_transcript_log(self) -> None:
        """Clear current display text and transcript history."""

        self._cancel_sentence_flush_task()
        self._current_display_text = ""
        self._transcript_log = []
        self._pending_sentence_text = ""
        self._pending_sentence_chunk_count = 0
        self._pending_sentence_metadata = {}
        self._routed_context_text = ""
        self._publish_state()

    def record_recent_bot_output(self, text: str) -> None:
        """Remember bot speech text so matching self-echo transcripts can be ignored."""

        normalized = str(text or "").strip()
        normalized_key = _normalize_echo_guard_text(normalized)
        if _semantic_length(normalized_key) < _BOT_ECHO_GUARD_MIN_CHARS:
            return
        now = time.time()
        self._prune_recent_bot_outputs(now)
        if self._recent_bot_outputs:
            _last_text, last_key, last_stamp = self._recent_bot_outputs[-1]
            if last_key == normalized_key and now - last_stamp <= _BOT_ECHO_GUARD_DEDUPE_SEC:
                return
        self._recent_bot_outputs.append((normalized, normalized_key, now))
        self._recent_bot_outputs = self._recent_bot_outputs[-8:]

    def apply_model_selection(self, label: str) -> None:
        """Apply one discovered model preset into the active config and persist it."""

        normalized = str(label or "").strip()
        model = next((item for item in self._models if item.label == normalized), None)
        if model is None:
            raise ValueError(f"unknown local voice model label: {normalized}")
        patch = {
            "sherpa_model_type": model.model_type,
            "sherpa_encoder": str(model.encoder_path),
            "sherpa_decoder": str(model.decoder_path),
            "sherpa_joiner": str(model.joiner_path) if model.joiner_path is not None else "",
            "sherpa_tokens": str(model.tokens_path),
        }
        self._apply_patch(patch)
        self._selected_model_label = model.label
        self._persist_settings()
        self._publish_state()

    def apply_settings_patch(self, patch: Mapping[str, Any]) -> LiveAdapterSettings:
        """Apply a UI/config patch and persist it synchronously."""

        self._refresh_settings_from_disk()
        normalized_patch = dict(patch)
        if not str(normalized_patch.get("rasr_api_key", "") or "").strip():
            normalized_patch.pop("rasr_api_key", None)
        if not str(normalized_patch.get("rasr_api_key_env", "") or "").strip():
            normalized_patch.pop("rasr_api_key_env", None)
        selected_model_label = str(normalized_patch.pop("selected_model_label", "") or "").strip()
        if selected_model_label:
            if not self._models:
                self.refresh_models()
            self.apply_model_selection(selected_model_label)
        if normalized_patch:
            self._apply_patch(normalized_patch)
            self._persist_settings()
            self._publish_state()
        return self._settings.model_copy(deep=True)

    async def start_listening(self) -> None:
        """Start the microphone listener if it is not already running."""

        if self._is_listening:
            return
        await self.stop_listening()
        self._refresh_settings_from_disk()
        try:
            service = self._service_factory(
                self._settings.local_voice,
                on_transcript=self.handle_transcript,
                logger=self._logger,
            )
            await service.start()
        except Exception as exc:
            self._service = None
            self._is_listening = False
            self._last_error = str(exc)
            self._publish_state()
            return
        self._service = service
        self._is_listening = True
        self._last_error = ""
        self._settings = self._settings.model_copy(
            update={"local_voice": self._settings.local_voice.model_copy(update={"enabled": True})},
            deep=True,
        )
        self._persist_settings()
        self._publish_state()

    async def stop_listening(self) -> None:
        """Stop the microphone listener if it is currently running."""

        service = self._service
        self._service = None
        if service is not None:
            with contextlib.suppress(Exception):
                await service.stop()
        await self._flush_pending_sentence(flush_reason="stop")
        self._refresh_settings_from_disk()
        if self._is_listening or self._settings.local_voice.enabled:
            self._settings = self._settings.model_copy(
                update={"local_voice": self._settings.local_voice.model_copy(update={"enabled": False})},
                deep=True,
            )
            self._persist_settings()
        self._is_listening = False
        self._routed_context_text = ""
        self._publish_state()

    async def toggle_listening(self) -> None:
        """Toggle the live microphone listener."""

        if self._is_listening:
            await self.stop_listening()
        else:
            await self.start_listening()

    async def apply_settings_patch_async(self, patch: Mapping[str, Any]) -> None:
        """Apply a patch, persist it, and restart the listener when needed."""

        was_listening = self._is_listening
        self.apply_settings_patch(patch)
        if was_listening:
            await self.stop_listening()
            await self.start_listening()

    async def _handle_postprocessed_transcript(self, text: str, metadata: Mapping[str, Any]) -> bool:
        self._pending_sentence_text = _join_stream_fragments(self._pending_sentence_text, text)
        self._pending_sentence_chunk_count += 1
        self._pending_sentence_metadata = dict(metadata)

        completed_sentences, remainder = _split_completed_sentences(self._pending_sentence_text)
        accepted = True
        if completed_sentences:
            routed = await self._route_completed_sentences(
                completed_sentences,
                metadata=self._pending_sentence_metadata,
                chunk_count=max(1, self._pending_sentence_chunk_count),
                flush_reason="punctuation",
            )
            accepted = accepted and routed
            self._pending_sentence_text = remainder
            self._pending_sentence_chunk_count = 1 if remainder else 0
            self._pending_sentence_metadata = dict(metadata) if remainder else {}

        if not self._pending_sentence_text:
            self._cancel_sentence_flush_task()
            return accepted

        if not bool(metadata.get("partial", False)):
            return accepted and await self._flush_pending_sentence(flush_reason="final")

        if _semantic_length(self._pending_sentence_text) >= max(1, int(self._settings.local_voice.sentence_force_emit_chars)):
            return accepted and await self._flush_pending_sentence(flush_reason="max_chars")

        self._schedule_sentence_flush()
        return accepted

    async def _route_completed_sentences(
        self,
        sentences: Sequence[str],
        *,
        metadata: Mapping[str, Any],
        chunk_count: int,
        flush_reason: str,
    ) -> bool:
        routed = True
        for sentence in sentences:
            routed = (
                await self._route_sentence(
                    sentence,
                    metadata,
                    auto_punctuated=False,
                    chunk_count=chunk_count,
                    flush_reason=flush_reason,
                )
                and routed
            )
        return routed

    async def _route_sentence(
        self,
        text: str,
        metadata: Mapping[str, Any] | None,
        *,
        auto_punctuated: bool,
        chunk_count: int,
        flush_reason: str,
    ) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return False
        self._sentence_route_sequence += 1
        prepared_text, next_context_text = _strip_already_routed_context(text, self._routed_context_text)
        normalized = str(prepared_text or "").strip()
        if not normalized:
            return False
        route_metadata = dict(metadata or {})
        route_metadata.update(
            {
                "phrase_id": f"local-voice-sentence-{self._sentence_route_sequence}-{uuid4().hex}",
                "partial": False,
                "postprocess_sentence": True,
                "postprocess_auto_punctuation": auto_punctuated,
                "postprocess_chunk_count": max(1, int(chunk_count)),
                "postprocess_flush_reason": str(flush_reason or "unknown"),
            }
        )
        accepted = bool(await self._route_callback(normalized, route_metadata))
        if accepted:
            self._routed_context_text = next_context_text
        return accepted

    def _schedule_sentence_flush(self) -> None:
        delay_ms = max(1, int(self._settings.local_voice.sentence_flush_inactivity_ms))
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        self._cancel_sentence_flush_task()
        self._sentence_flush_task = loop.create_task(
            self._flush_pending_sentence_after_delay(delay_ms),
            name="maibot_local_voice.sentence_flush",
        )

    def _cancel_sentence_flush_task(self) -> None:
        task = self._sentence_flush_task
        self._sentence_flush_task = None
        if task is not None:
            task.cancel()

    async def _flush_pending_sentence_after_delay(self, delay_ms: int) -> None:
        try:
            await asyncio.sleep(max(0.0, float(delay_ms) / 1000.0))
            await self._flush_pending_sentence(flush_reason="inactivity")
        except asyncio.CancelledError:
            raise
        finally:
            current_task = asyncio.current_task()
            if current_task is self._sentence_flush_task:
                self._sentence_flush_task = None

    async def _flush_pending_sentence(self, *, flush_reason: str) -> bool:
        text = str(self._pending_sentence_text or "").strip()
        if not text:
            self._pending_sentence_text = ""
            self._pending_sentence_chunk_count = 0
            self._pending_sentence_metadata = {}
            return True
        self._cancel_sentence_flush_task()
        auto_punctuated = False
        if self._settings.local_voice.sentence_auto_punctuation:
            text, auto_punctuated = _append_terminal_punctuation(text)
        routed = await self._route_sentence(
            text,
            self._pending_sentence_metadata,
            auto_punctuated=auto_punctuated,
            chunk_count=max(1, self._pending_sentence_chunk_count),
            flush_reason=flush_reason,
        )
        self._pending_sentence_text = ""
        self._pending_sentence_chunk_count = 0
        self._pending_sentence_metadata = {}
        self._current_display_text = ""
        return routed

    def _apply_patch(self, patch: Mapping[str, Any]) -> None:
        self._cancel_sentence_flush_task()
        local_voice_data = self._settings.local_voice.model_dump(mode="python")
        local_voice_data.update(dict(patch))
        new_local_voice = LocalVoiceInputConfig.model_validate(local_voice_data)
        self._settings = self._settings.model_copy(update={"local_voice": new_local_voice}, deep=True)
        self._selected_model_label = _match_model_label(self._models, new_local_voice)

    def _persist_settings(self) -> None:
        document = tomlkit.document()
        if self._config_path.exists():
            try:
                document = tomlkit.parse(self._config_path.read_text(encoding="utf-8"))
            except Exception:
                document = tomlkit.document()
        document["local_voice"] = self._settings.local_voice.model_dump(mode="python")
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(tomlkit.dumps(document), encoding="utf-8")
        if self._settings_changed_callback is not None:
            self._settings_changed_callback(self._settings.model_copy(deep=True))

    def _publish_state(self) -> None:
        snapshot = self.snapshot()
        if self._state_callback is not None:
            self._state_callback(snapshot)
        runtime = self._runtime
        if runtime is not None:
            runtime.update_snapshot(snapshot)

    def _looks_like_recent_bot_echo(self, text: str) -> bool:
        normalized_key = _normalize_echo_guard_text(text)
        if _semantic_length(normalized_key) < _BOT_ECHO_GUARD_MIN_CHARS:
            return False
        self._prune_recent_bot_outputs()
        for _original_text, candidate_key, _stamp in reversed(self._recent_bot_outputs):
            if _looks_like_echo_guard_match(normalized_key, candidate_key):
                return True
        return False

    def _prune_recent_bot_outputs(self, now: float | None = None) -> None:
        cutoff = float(now if now is not None else time.time()) - _BOT_ECHO_GUARD_WINDOW_SEC
        self._recent_bot_outputs = [entry for entry in self._recent_bot_outputs if entry[2] >= cutoff]

    def _schedule_refresh_devices(self) -> None:
        self.refresh_devices()

    def _schedule_refresh_models(self) -> None:
        self.refresh_models()

    def _schedule_toggle_listening(self) -> None:
        self._submit_async(self.toggle_listening())

    def _schedule_apply_settings(self, patch: Mapping[str, Any]) -> None:
        self._submit_async(self.apply_settings_patch_async(patch))

    def _submit_async(self, coroutine: Awaitable[Any]) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(asyncio.create_task, coroutine)

    def _refresh_settings_from_disk(self) -> None:
        settings = self._load_settings_from_disk()
        if settings is None:
            return
        self._settings = settings.model_copy(deep=True)
        self._selected_model_label = _match_model_label(self._models, self._settings.local_voice)

    def _load_settings_from_disk(self) -> LiveAdapterSettings | None:
        if not self._config_path.exists():
            return None
        try:
            data = tomllib.loads(self._config_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        try:
            return LiveAdapterSettings.model_validate(data)
        except Exception:
            return None


def discover_sherpa_models(root: Path | str) -> list[LocalVoiceModelOption]:
    """Discover usable sherpa-onnx models from the plugin-local model directory."""

    model_root = Path(root)
    if not model_root.exists():
        return []
    discovered: list[LocalVoiceModelOption] = []
    for directory in sorted((item for item in model_root.iterdir() if item.is_dir()), key=lambda item: item.name.lower()):
        encoder_path = _pick_file(directory, "encoder.int8.onnx", "encoder.onnx")
        decoder_path = _pick_file(directory, "decoder.int8.onnx", "decoder.onnx")
        tokens_path = _pick_file(directory, "tokens.txt")
        if encoder_path is None or decoder_path is None or tokens_path is None:
            continue
        joiner_path = _pick_file(directory, "joiner.int8.onnx", "joiner.onnx")
        model_type = "transducer" if joiner_path is not None else "paraformer"
        discovered.append(
            LocalVoiceModelOption(
                label=directory.name,
                model_type=model_type,
                directory=directory,
                encoder_path=encoder_path,
                decoder_path=decoder_path,
                tokens_path=tokens_path,
                joiner_path=joiner_path,
            )
        )
    return discovered


def list_input_devices(*, query_devices: Callable[[], Sequence[Any]] | None = None) -> list[str]:
    """Return input-capable device names for the local voice control window."""

    try:
        devices = list((query_devices or _query_sound_devices)())
    except Exception:
        return []
    device_names: list[str] = []
    for device in devices:
        if _max_input_channels(device) <= 0:
            continue
        name = _device_name(device)
        if name:
            device_names.append(name)
    return device_names


def _query_sound_devices() -> Sequence[Any]:
    import sounddevice as sd

    return list(sd.query_devices())


def _device_name(device: Any) -> str:
    if isinstance(device, Mapping):
        return str(device.get("name") or "").strip()
    try:
        return str(device["name"] or "").strip()
    except Exception:
        return ""


def _max_input_channels(device: Any) -> int:
    try:
        if isinstance(device, Mapping):
            return int(device.get("max_input_channels") or 0)
        return int(device["max_input_channels"] or 0)
    except Exception:
        return 0


def _pick_file(directory: Path, *names: str) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _match_model_label(models: Sequence[LocalVoiceModelOption], config: LocalVoiceInputConfig) -> str:
    encoder = _normalize_path_key(config.sherpa_encoder)
    if not encoder:
        return ""
    for model in models:
        if _normalize_path_key(model.encoder_path) == encoder:
            return model.label
    return ""


def _settings_view_from_config(config: LocalVoiceInputConfig) -> LocalVoiceSettingsView:
    return LocalVoiceSettingsView(
        enabled=bool(config.enabled),
        speaker_user_id=str(config.speaker_user_id or "local-mic"),
        speaker_username=str(config.speaker_username or "Local Mic"),
        input_device=str(config.input_device or ""),
        engine=str(config.engine or "aliyun_rasr"),
        rasr_model=str(config.rasr_model or "fun-asr-realtime"),
        rasr_ws_url=str(config.rasr_ws_url or "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"),
        rasr_api_key_env=str(config.rasr_api_key_env or "DASHSCOPE_API_KEY"),
        rasr_api_key=str(config.rasr_api_key or ""),
        rasr_audio_format=str(config.rasr_audio_format or "pcm"),
        rasr_language_hint=str(config.rasr_language_hint or ""),
        rasr_enable_intermediate_result=bool(config.rasr_enable_intermediate_result),
        rasr_enable_punctuation_prediction=bool(config.rasr_enable_punctuation_prediction),
        rasr_enable_inverse_text_normalization=bool(config.rasr_enable_inverse_text_normalization),
        rasr_max_sentence_silence_ms=max(1, int(config.rasr_max_sentence_silence_ms)),
        rasr_heartbeat=bool(config.rasr_heartbeat),
        rasr_route_partials_to_maibot=bool(config.rasr_route_partials_to_maibot),
        rasr_speech_noise_threshold=min(1.0, max(-1.0, float(config.rasr_speech_noise_threshold))),
        rasr_disfluency_removal_enabled=bool(config.rasr_disfluency_removal_enabled),
        sample_rate_hz=max(1, int(config.sample_rate_hz)),
        channels=max(1, int(config.channels)),
        block_duration_ms=max(1, int(config.block_duration_ms)),
        sentence_postprocess_enabled=bool(config.sentence_postprocess_enabled),
        sentence_flush_inactivity_ms=max(1, int(config.sentence_flush_inactivity_ms)),
        sentence_force_emit_chars=max(1, int(config.sentence_force_emit_chars)),
        sentence_auto_punctuation=bool(config.sentence_auto_punctuation),
        speech_vad_enabled=bool(config.speech_vad_enabled),
        speech_noise_reduction_enabled=bool(config.speech_noise_reduction_enabled),
        speech_vad_start_threshold=min(1.0, max(0.0, float(config.speech_vad_start_threshold))),
        speech_vad_noise_ratio=max(1.0, float(config.speech_vad_noise_ratio)),
        speech_vad_hold_ms=max(0, int(config.speech_vad_hold_ms)),
        pre_speech_padding_ms=max(0, int(config.pre_speech_padding_ms)),
        speech_reset_on_silence=bool(config.speech_reset_on_silence),
        speech_noise_floor_adaptation=min(1.0, max(0.0, float(config.speech_noise_floor_adaptation))),
        speech_noise_suppression_strength=min(1.0, max(0.0, float(config.speech_noise_suppression_strength))),
        sherpa_model_type=str(config.sherpa_model_type or "transducer"),
        sherpa_provider=str(config.sherpa_provider or "cpu"),
        sherpa_num_threads=max(1, int(config.sherpa_num_threads)),
        sherpa_model_sample_rate_hz=max(1, int(config.sherpa_model_sample_rate_hz)),
        sherpa_feature_dim=max(1, int(config.sherpa_feature_dim)),
        sherpa_decoding_method=str(config.sherpa_decoding_method or "greedy_search"),
        sherpa_max_active_paths=max(1, int(config.sherpa_max_active_paths)),
        sherpa_hotwords_file=str(config.sherpa_hotwords_file or ""),
        sherpa_hotwords_score=float(config.sherpa_hotwords_score),
        sherpa_blank_penalty=max(0.0, float(config.sherpa_blank_penalty)),
        sherpa_enable_endpoint=bool(config.sherpa_enable_endpoint),
        sherpa_encoder=str(config.sherpa_encoder or ""),
        sherpa_decoder=str(config.sherpa_decoder or ""),
        sherpa_joiner=str(config.sherpa_joiner or ""),
        sherpa_tokens=str(config.sherpa_tokens or ""),
        min_transcript_length=max(1, int(config.min_transcript_length)),
        stable_emit_min_chars=max(1, int(config.stable_emit_min_chars)),
    )


def _join_stream_fragments(left: str, right: str) -> str:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text:
        return right_text
    if not right_text:
        return left_text
    if _needs_space_between(left_text, right_text):
        return f"{left_text} {right_text}"
    return f"{left_text}{right_text}"


def _needs_space_between(left: str, right: str) -> bool:
    left_tail = _ascii_word_suffix(left)
    right_head = _ascii_word_prefix(right)
    if not left_tail or not right_head:
        return False
    return len(left_tail) >= 4 and len(right_head) >= 4


def _ascii_word_suffix(text: str) -> str:
    collected: list[str] = []
    for char in reversed(str(text or "")):
        if not _is_ascii_word_char(char):
            break
        collected.append(char)
    return "".join(reversed(collected))


def _ascii_word_prefix(text: str) -> str:
    collected: list[str] = []
    for char in str(text or ""):
        if not _is_ascii_word_char(char):
            break
        collected.append(char)
    return "".join(collected)


def _is_ascii_word_char(char: str) -> bool:
    return bool(char) and char.isascii() and (char.isalnum() or char in {"_", "'"})


def _split_completed_sentences(text: str) -> tuple[list[str], str]:
    normalized = str(text or "").strip()
    if not normalized:
        return [], ""
    completed: list[str] = []
    sentence_start = 0
    for index, char in enumerate(normalized):
        if char not in _SENTENCE_END_PUNCTUATION:
            continue
        sentence = normalized[sentence_start : index + 1].strip()
        if sentence:
            completed.append(sentence)
        sentence_start = index + 1
    remainder = normalized[sentence_start:].strip()
    return completed, remainder


def _append_terminal_punctuation(text: str) -> tuple[str, bool]:
    normalized = str(text or "").strip()
    if not normalized:
        return "", False
    if _has_terminal_sentence_punctuation(normalized):
        return normalized, False
    punctuation = _suggest_terminal_punctuation(normalized)
    return f"{normalized}{punctuation}", True


def _strip_already_routed_context(text: str, routed_context_text: str) -> tuple[str, str]:
    normalized = str(text or "").strip()
    if not normalized:
        return "", ""
    candidate_core, candidate_suffix = _split_terminal_suffix(normalized)
    routed_core, _routed_suffix = _split_terminal_suffix(str(routed_context_text or "").strip())
    if routed_core and candidate_core.startswith(routed_core) and len(candidate_core) > len(routed_core):
        remainder_core = candidate_core[len(routed_core) :].strip()
        if remainder_core:
            return f"{remainder_core}{candidate_suffix}", candidate_core
    return normalized, candidate_core


def _has_terminal_sentence_punctuation(text: str) -> bool:
    for char in reversed(str(text or "").rstrip()):
        if char in _SENTENCE_END_PUNCTUATION:
            return True
        if char in _SENTENCE_CLOSING_TAILS:
            continue
        return False
    return False


def _suggest_terminal_punctuation(text: str) -> str:
    if _looks_like_question(text):
        return "？" if _contains_cjk(text) else "?"
    return "。" if _contains_cjk(text) else "."


def _looks_like_question(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if normalized.endswith(_QUESTION_ENDINGS):
        return True
    lowered = normalized.lower()
    return any(token in lowered for token in _QUESTION_HINTS)


def _contains_cjk(text: str) -> bool:
    return any(_is_cjk_char(char) for char in str(text or ""))


def _is_cjk_char(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _semantic_length(text: str) -> int:
    return sum(1 for char in str(text or "") if char.isalnum() or _is_cjk_char(char))


def _normalize_echo_guard_text(text: str) -> str:
    return "".join(
        char
        for char in str(text or "").lower()
        if char.isalnum() or _is_cjk_char(char)
    )


def _looks_like_echo_guard_match(candidate: str, reference: str) -> bool:
    left = str(candidate or "").strip()
    right = str(reference or "").strip()
    if not left or not right:
        return False
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) >= _BOT_ECHO_GUARD_SUBSTRING_MIN_CHARS and shorter in longer:
        if len(shorter) / max(1, len(longer)) >= 0.58:
            return True
    matcher = SequenceMatcher(a=left, b=right, autojunk=False)
    longest_match = matcher.find_longest_match(0, len(left), 0, len(right)).size
    if longest_match >= _BOT_ECHO_GUARD_SUBSTRING_MIN_CHARS:
        min_length = max(1, min(len(left), len(right)))
        if longest_match / min_length >= _BOT_ECHO_GUARD_LONGEST_MATCH_RATIO:
            return True
    return matcher.ratio() >= _BOT_ECHO_GUARD_SEQUENCE_RATIO


def _split_terminal_suffix(text: str) -> tuple[str, str]:
    normalized = str(text or "").rstrip()
    if not normalized:
        return "", ""
    suffix_chars: list[str] = []
    index = len(normalized) - 1
    while index >= 0:
        char = normalized[index]
        if char in _SENTENCE_END_PUNCTUATION or char in _SENTENCE_CLOSING_TAILS:
            suffix_chars.append(char)
            index -= 1
            continue
        break
    core = normalized[: index + 1].rstrip()
    suffix = "".join(reversed(suffix_chars))
    return core, suffix


def _plugin_dir() -> Path:
    return Path(__file__).resolve().parent


def _normalize_path_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return Path(text).as_posix().lower()


def _default_runtime_factory(**kwargs: Any) -> Any:
    from .local_voice_native_runtime import LocalVoiceNativeRuntime

    return LocalVoiceNativeRuntime(**kwargs)

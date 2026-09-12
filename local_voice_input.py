"""Continuous local microphone capture with realtime speech recognition backends."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import os
import sys
import time

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import uuid4

import numpy as np

from .config import LocalVoiceInputConfig


_PLUGIN_VENDOR_PATH = Path(__file__).resolve().parent / "vendor"


@dataclass(frozen=True)
class LocalVoiceTranscriptSegment:
    """One ASR text segment with enough timing intent for sentence postprocessing."""

    text: str
    partial: bool
    boundary: bool = False
    begin_time_ms: int | None = None
    end_time_ms: int | None = None
    task_id: str = ""


def ensure_plugin_vendor_path() -> Path | None:
    """Add this plugin's vendored dependency directory to import search path."""

    if not _PLUGIN_VENDOR_PATH.exists():
        return None
    vendor_path = str(_PLUGIN_VENDOR_PATH)
    if vendor_path not in sys.path:
        sys.path.insert(0, vendor_path)
    return _PLUGIN_VENDOR_PATH


def resolve_aliyun_rasr_api_key(config: LocalVoiceInputConfig) -> str:
    """Resolve the DashScope API key from config first, then the configured environment variable."""

    configured_key = str(getattr(config, "rasr_api_key", "") or "").strip()
    if configured_key:
        return configured_key
    env_name = str(getattr(config, "rasr_api_key_env", "") or "DASHSCOPE_API_KEY").strip() or "DASHSCOPE_API_KEY"
    return str(os.environ.get(env_name, "") or "").strip()


def build_aliyun_rasr_run_task_message(config: LocalVoiceInputConfig, *, task_id: str) -> dict[str, Any]:
    """Build the DashScope run-task command for hosted Fun-ASR realtime recognition."""

    language_hints = _resolve_rasr_language_hints(getattr(config, "rasr_language_hint", ""))
    parameters: dict[str, Any] = {
        "format": str(getattr(config, "rasr_audio_format", "") or "pcm").strip().lower() or "pcm",
        "sample_rate": max(1, int(getattr(config, "sample_rate_hz", 16000))),
        "disfluency_removal_enabled": bool(getattr(config, "rasr_disfluency_removal_enabled", False)),
        "enable_intermediate_result": bool(getattr(config, "rasr_enable_intermediate_result", True)),
        "enable_punctuation_prediction": bool(getattr(config, "rasr_enable_punctuation_prediction", True)),
        "enable_inverse_text_normalization": bool(getattr(config, "rasr_enable_inverse_text_normalization", True)),
        "max_sentence_silence": max(1, int(getattr(config, "rasr_max_sentence_silence_ms", 800))),
        "heartbeat": bool(getattr(config, "rasr_heartbeat", True)),
        "speech_noise_threshold": min(1.0, max(-1.0, float(getattr(config, "rasr_speech_noise_threshold", 0.3)))),
    }
    if language_hints:
        parameters["language_hints"] = language_hints
    return {
        "header": {
            "action": "run-task",
            "task_id": str(task_id),
            "streaming": "duplex",
        },
        "payload": {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": str(getattr(config, "rasr_model", "") or "fun-asr-realtime").strip() or "fun-asr-realtime",
            "parameters": parameters,
            "input": {},
        },
    }


def build_aliyun_rasr_finish_task_message(task_id: str) -> dict[str, Any]:
    """Build the DashScope finish-task command for a running realtime ASR task."""

    return {
        "header": {
            "action": "finish-task",
            "task_id": str(task_id),
            "streaming": "duplex",
        },
        "payload": {"input": {}},
    }


def parse_aliyun_rasr_event(message: Mapping[str, Any]) -> LocalVoiceTranscriptSegment | None:
    """Parse a DashScope RASR server event into a local transcript segment."""

    header = message.get("header", {})
    if not isinstance(header, Mapping) or header.get("event") != "result-generated":
        return None
    payload = message.get("payload", {})
    if not isinstance(payload, Mapping):
        return None
    output = payload.get("output", {})
    if not isinstance(output, Mapping):
        return None
    sentence = output.get("sentence", {})
    if not isinstance(sentence, Mapping):
        return None
    if bool(sentence.get("heartbeat", False)):
        return None
    text = _normalize_transcript_text(str(sentence.get("text") or ""))
    if not text:
        return None
    sentence_end = bool(sentence.get("sentence_end", False))
    return LocalVoiceTranscriptSegment(
        text=text,
        partial=not sentence_end,
        boundary=sentence_end,
        begin_time_ms=_optional_int(sentence.get("begin_time")),
        end_time_ms=_optional_int(sentence.get("end_time")),
        task_id=str(header.get("task_id") or ""),
    )


def convert_float32_to_pcm16(samples: np.ndarray | Sequence[float]) -> bytes:
    """Convert sounddevice float32 samples to little-endian signed 16-bit PCM."""

    mono = _normalize_mono_float32(samples)
    if mono.size == 0:
        return b""
    clipped = np.clip(mono, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2", copy=False).tobytes()


class AliyunRasrStreamingTranscriber:
    """Streaming client for Aliyun Model Studio hosted Fun-ASR realtime recognition."""

    def __init__(self, config: LocalVoiceInputConfig, *, logger: Any = None) -> None:
        self.config = config
        self.logger = logger
        self.sample_rate_hz = max(1, int(config.sample_rate_hz))
        self.task_id = uuid4().hex
        self.api_key = resolve_aliyun_rasr_api_key(config)
        if not self.api_key:
            env_name = str(getattr(config, "rasr_api_key_env", "") or "DASHSCOPE_API_KEY").strip() or "DASHSCOPE_API_KEY"
            raise RuntimeError(f"Aliyun RASR requires API key in local_voice.rasr_api_key or {env_name}")
        self._session: Any = None
        self._ws: Any = None
        self._receive_task: asyncio.Task[None] | None = None
        self._segments: asyncio.Queue[LocalVoiceTranscriptSegment] = asyncio.Queue()
        self._started = asyncio.Event()
        self._finished = asyncio.Event()
        self._startup_error = ""
        self._audio_preprocessor = LocalVoiceAudioPreprocessor(config)
        self._speech_context_active = False

    async def start(self) -> None:
        """Open the WebSocket, send run-task, and wait until the server accepts audio."""

        try:
            import aiohttp
        except ImportError as exc:  # pragma: no cover - dependency is declared by the app
            raise RuntimeError("aiohttp is required for Aliyun RASR realtime speech recognition") from exc

        self._session = aiohttp.ClientSession()
        ws_url = str(getattr(self.config, "rasr_ws_url", "") or "").strip()
        if not ws_url:
            ws_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"
        self._ws = await self._session.ws_connect(
            ws_url,
            headers={"Authorization": f"bearer {self.api_key}"},
            heartbeat=30.0 if bool(getattr(self.config, "rasr_heartbeat", True)) else None,
        )
        await self._send_json(build_aliyun_rasr_run_task_message(self.config, task_id=self.task_id))
        self._receive_task = asyncio.create_task(self._receive_loop(), name="maibot_local_voice.aliyun_rasr.receive")
        try:
            await asyncio.wait_for(self._started.wait(), timeout=10.0)
        except Exception:
            await self.close()
            raise
        if self._startup_error:
            await self.close()
            raise RuntimeError(self._startup_error)

    async def accept_audio_events(self, samples: np.ndarray | Sequence[float]) -> list[LocalVoiceTranscriptSegment]:
        """Send one microphone chunk and return any RASR results currently available."""

        processed = self._audio_preprocessor.process(samples)
        pcm = b""
        if processed.size != 0:
            pcm = convert_float32_to_pcm16(processed)
            if pcm:
                self._speech_context_active = True
        elif self._speech_context_active:
            # Keep a zeroed silence frame flowing after speech so server-side VAD can still close the sentence.
            pcm = convert_float32_to_pcm16(np.zeros_like(_normalize_mono_float32(samples), dtype=np.float32))
        ws = self._ws
        if ws is not None and pcm:
            await ws.send_bytes(pcm)
        segments = self._drain_segments()
        if any(bool(segment.boundary) for segment in segments):
            self._speech_context_active = False
        return segments

    async def flush_events(self) -> list[LocalVoiceTranscriptSegment]:
        """Tell RASR the current task is finished and return queued final results."""

        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._send_json(build_aliyun_rasr_finish_task_message(self.task_id))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._finished.wait(), timeout=2.0)
        return self._drain_segments()

    async def flush(self) -> list[str]:
        """Compatibility wrapper used by older service tests."""

        return [segment.text for segment in await self.flush_events() if segment.text]

    async def close(self) -> None:
        """Close the WebSocket session and receiver task."""

        task = self._receive_task
        self._receive_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        session = self._session
        self._session = None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    async def _receive_loop(self) -> None:
        try:
            async for message in self._ws:
                message_type = getattr(getattr(message, "type", None), "name", "")
                if message_type == "TEXT":
                    await self._handle_text_message(str(message.data or ""))
                elif message_type in {"CLOSED", "ERROR", "CLOSE"}:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_warning(f"Aliyun RASR receive loop failed: {exc}")
        finally:
            self._finished.set()

    async def _handle_text_message(self, data: str) -> None:
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            self._log_warning(f"Aliyun RASR returned non-JSON event: {data[:120]!r}")
            return
        header = message.get("header", {}) if isinstance(message, Mapping) else {}
        event = str(header.get("event") or "") if isinstance(header, Mapping) else ""
        if event == "task-started":
            self._started.set()
            return
        if event == "task-finished":
            self._finished.set()
            return
        if event == "task-failed":
            error_message = str(header.get("error_message") or header.get("error_code") or "unknown error")
            self._startup_error = error_message
            self._finished.set()
            self._started.set()
            self._log_warning(f"Aliyun RASR task failed: {error_message}")
            return
        segment = parse_aliyun_rasr_event(message)
        if segment is not None:
            await self._segments.put(segment)

    async def _send_json(self, payload: Mapping[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("Aliyun RASR WebSocket is not connected")
        await ws.send_str(json.dumps(payload, ensure_ascii=False))

    def _drain_segments(self) -> list[LocalVoiceTranscriptSegment]:
        segments: list[LocalVoiceTranscriptSegment] = []
        while True:
            try:
                segments.append(self._segments.get_nowait())
            except asyncio.QueueEmpty:
                return segments

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)


class StableTranscriptEmitter:
    """Emit only the stable prefix that appears in consecutive partial results."""

    def __init__(self, *, min_chars: int = 1) -> None:
        self.min_chars = max(1, int(min_chars))
        self._previous_partial = ""
        self._emitted_text = ""

    def update(self, partial_text: str) -> list[str]:
        normalized = _normalize_transcript_text(partial_text)
        if not normalized:
            self._previous_partial = ""
            return []
        stable_prefix = _longest_common_prefix(self._previous_partial, normalized)
        self._previous_partial = normalized
        return self._emit_prefix(stable_prefix, force=False)

    def flush(self, final_text: str | None = None) -> list[str]:
        normalized = _normalize_transcript_text(self._previous_partial if final_text is None else final_text)
        self._previous_partial = normalized
        return self._emit_prefix(normalized, force=True)

    def reset(self) -> None:
        self._previous_partial = ""
        self._emitted_text = ""

    def _emit_prefix(self, stable_prefix: str, *, force: bool) -> list[str]:
        if not stable_prefix:
            return []
        if self._emitted_text and not stable_prefix.startswith(self._emitted_text):
            return []
        delta = stable_prefix[len(self._emitted_text) :]
        if not delta:
            return []
        if not force and len(delta) < self.min_chars:
            return []
        self._emitted_text = stable_prefix
        return [delta]


class LocalVoiceAudioPreprocessor:
    """Low-cost microphone cleanup before audio reaches the streaming recognizer."""

    _EPSILON = 1e-7

    def __init__(self, config: LocalVoiceInputConfig) -> None:
        self.config = config
        self.sample_rate_hz = max(1, int(config.sample_rate_hz))
        start_threshold = self._start_threshold
        self._noise_floor = max(self._EPSILON, start_threshold / self._noise_ratio * 0.5)
        self._hold_samples_remaining = 0
        self._was_passing_audio = False
        self._silence_transition_pending = False
        self._last_output_was_silence_hold = False
        self._pre_speech_buffer: deque[np.ndarray] = deque()
        self._pre_speech_samples = 0

    def process(self, samples: np.ndarray | Sequence[float]) -> np.ndarray:
        self._silence_transition_pending = False
        self._last_output_was_silence_hold = False
        normalized = _normalize_mono_float32(samples)
        if normalized.size == 0:
            return normalized

        cleaned = _remove_dc_offset(normalized) if self._noise_reduction_enabled else normalized
        if not self._vad_enabled:
            return cleaned

        rms = _rms(cleaned)
        peak = float(np.max(np.abs(cleaned))) if cleaned.size else 0.0
        speech_threshold = self._speech_threshold()
        is_speech = rms >= speech_threshold or (
            peak >= speech_threshold * 3.0 and rms >= max(self._EPSILON, self._noise_floor * 1.5)
        )

        if is_speech:
            self._hold_samples_remaining = self._hold_sample_count
            self._was_passing_audio = True
            return self._prepend_pre_speech(cleaned)

        self._adapt_noise_floor(rms)
        if self._hold_samples_remaining > 0:
            self._hold_samples_remaining = max(0, self._hold_samples_remaining - int(cleaned.size))
            self._was_passing_audio = True
            self._last_output_was_silence_hold = True
            return self._silence_non_speech(cleaned)

        if self._was_passing_audio:
            self._silence_transition_pending = True
        self._was_passing_audio = False
        self._remember_pre_speech(cleaned)
        return np.zeros(0, dtype=np.float32)

    def consume_silence_transition(self) -> bool:
        transitioned = self._silence_transition_pending
        self._silence_transition_pending = False
        return transitioned

    def consume_silence_hold_output(self) -> bool:
        was_silence_hold = self._last_output_was_silence_hold
        self._last_output_was_silence_hold = False
        return was_silence_hold

    @property
    def _vad_enabled(self) -> bool:
        return bool(getattr(self.config, "speech_vad_enabled", True))

    @property
    def _noise_reduction_enabled(self) -> bool:
        return bool(getattr(self.config, "speech_noise_reduction_enabled", True))

    @property
    def _start_threshold(self) -> float:
        return max(0.0, float(getattr(self.config, "speech_vad_start_threshold", 0.024)))

    @property
    def _noise_ratio(self) -> float:
        return max(1.0, float(getattr(self.config, "speech_vad_noise_ratio", 4.0)))

    @property
    def _hold_sample_count(self) -> int:
        hold_ms = max(0, int(getattr(self.config, "speech_vad_hold_ms", 250)))
        return int(round(self.sample_rate_hz * hold_ms / 1000.0))

    @property
    def _pre_speech_sample_limit(self) -> int:
        padding_ms = max(0, int(getattr(self.config, "pre_speech_padding_ms", 160)))
        return int(round(self.sample_rate_hz * padding_ms / 1000.0))

    @property
    def _noise_adaptation(self) -> float:
        return min(1.0, max(0.0, float(getattr(self.config, "speech_noise_floor_adaptation", 0.05))))

    @property
    def _suppression_strength(self) -> float:
        return min(1.0, max(0.0, float(getattr(self.config, "speech_noise_suppression_strength", 0.8))))

    def _speech_threshold(self) -> float:
        return max(self._start_threshold, self._noise_floor * self._noise_ratio)

    def _adapt_noise_floor(self, rms: float) -> None:
        if rms <= self._EPSILON:
            return
        adaptation = self._noise_adaptation
        self._noise_floor = (self._noise_floor * (1.0 - adaptation)) + (float(rms) * adaptation)

    def _silence_non_speech(self, samples: np.ndarray) -> np.ndarray:
        if not self._noise_reduction_enabled:
            return samples
        strength = self._suppression_strength
        if strength >= 0.8:
            return np.zeros_like(samples, dtype=np.float32)
        return (samples * (1.0 - strength)).astype(np.float32, copy=False)

    def _remember_pre_speech(self, samples: np.ndarray) -> None:
        limit = self._pre_speech_sample_limit
        if limit <= 0 or samples.size == 0:
            self._pre_speech_buffer.clear()
            self._pre_speech_samples = 0
            return
        self._pre_speech_buffer.append(np.array(samples, dtype=np.float32, copy=True))
        self._pre_speech_samples += int(samples.size)
        while self._pre_speech_samples > limit and self._pre_speech_buffer:
            removed = self._pre_speech_buffer.popleft()
            self._pre_speech_samples -= int(removed.size)

    def _prepend_pre_speech(self, speech: np.ndarray) -> np.ndarray:
        if not self._pre_speech_buffer:
            return speech
        chunks = [*self._pre_speech_buffer, speech]
        self._pre_speech_buffer.clear()
        self._pre_speech_samples = 0
        return np.concatenate(chunks).astype(np.float32, copy=False)


class SherpaOnnxStreamingTranscriber:
    """Thin wrapper around sherpa-onnx OnlineRecognizer for continuous chunks."""

    def __init__(self, config: LocalVoiceInputConfig, *, logger: Any = None) -> None:
        self.config = config
        self.logger = logger
        self.sample_rate_hz = max(1, int(config.sample_rate_hz))
        self._recognizer = self._create_recognizer()
        self._stream = self._recognizer.create_stream()
        self._emitter = StableTranscriptEmitter(min_chars=config.stable_emit_min_chars)
        self._audio_preprocessor = LocalVoiceAudioPreprocessor(config)
        self._last_result_text = ""

    def accept_audio(self, samples: np.ndarray | Sequence[float]) -> list[str]:
        return [segment.text for segment in self.accept_audio_events(samples) if segment.text]

    def accept_audio_events(self, samples: np.ndarray | Sequence[float]) -> list[LocalVoiceTranscriptSegment]:
        processed = self._audio_preprocessor.process(samples)
        if processed.size == 0:
            if self._audio_preprocessor.consume_silence_transition():
                return self._finalize_current_result(self._last_result_text or None)
            return []
        self._stream.accept_waveform(self.sample_rate_hz, processed)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        result_text = self._get_result_text()
        if self._audio_preprocessor.consume_silence_hold_output() or self._should_reset_on_endpoint():
            return self._finalize_current_result(result_text)
        return self._make_segments(self._emitter.update(result_text), partial=True)

    def flush(self) -> list[str]:
        return self._filter_transcripts(self._emitter.flush(self._get_result_text()))

    def _finalize_current_result(self, result_text: str | None) -> list[LocalVoiceTranscriptSegment]:
        segments = self._make_segments(self._emitter.flush(result_text), partial=False)
        if bool(getattr(self.config, "speech_reset_on_silence", True)):
            self._reset_streaming_context()
        if not segments:
            return [LocalVoiceTranscriptSegment(text="", partial=False, boundary=True)]
        return segments

    def _reset_streaming_context(self) -> None:
        if hasattr(self._recognizer, "reset"):
            self._recognizer.reset(self._stream)
        self._emitter.reset()
        self._last_result_text = ""

    def _create_recognizer(self) -> Any:
        model_type = str(self.config.sherpa_model_type or "transducer").strip().lower()
        if model_type not in {"transducer", "paraformer"}:
            raise RuntimeError(
                "local_voice.sherpa_model_type supports 'transducer' or 'paraformer' for streaming microphone input"
            )
        required_fields = ["sherpa_encoder", "sherpa_decoder", "sherpa_tokens"]
        if model_type == "transducer":
            required_fields.append("sherpa_joiner")
        missing = [
            field_name
            for field_name in required_fields
            if not str(getattr(self.config, field_name, "") or "").strip()
        ]
        if missing:
            missing_fields = ", ".join(f"local_voice.{field_name}" for field_name in missing)
            raise RuntimeError(f"sherpa-onnx local voice input requires {missing_fields}")
        ensure_plugin_vendor_path()
        try:
            sherpa_onnx = importlib.import_module("sherpa_onnx")
        except ImportError as exc:  # pragma: no cover - depends on runtime environment
            raise RuntimeError("sherpa-onnx is required for local voice input; install package 'sherpa-onnx'") from exc
        online_recognizer = getattr(sherpa_onnx, "OnlineRecognizer", None)
        factory_name = "from_transducer" if model_type == "transducer" else "from_paraformer"
        recognizer_factory = getattr(online_recognizer, factory_name, None)
        if online_recognizer is None or recognizer_factory is None:
            raise RuntimeError(f"sherpa_onnx.OnlineRecognizer.{factory_name} is required for local voice input")
        kwargs = {
            "tokens": self.config.sherpa_tokens,
            "encoder": self.config.sherpa_encoder,
            "decoder": self.config.sherpa_decoder,
            "num_threads": max(1, int(self.config.sherpa_num_threads)),
            "sample_rate": max(1, int(self.config.sherpa_model_sample_rate_hz)),
            "feature_dim": max(1, int(self.config.sherpa_feature_dim)),
            "enable_endpoint_detection": bool(self.config.sherpa_enable_endpoint),
            "decoding_method": self.config.sherpa_decoding_method,
            "provider": self.config.sherpa_provider,
        }
        if model_type == "transducer":
            kwargs.update(
                {
                    "joiner": self.config.sherpa_joiner,
                    "max_active_paths": max(1, int(self.config.sherpa_max_active_paths)),
                    "hotwords_score": float(self.config.sherpa_hotwords_score),
                    "blank_penalty": max(0.0, float(self.config.sherpa_blank_penalty)),
                }
            )
            if self.config.sherpa_hotwords_file:
                kwargs["hotwords_file"] = self.config.sherpa_hotwords_file
        return recognizer_factory(**kwargs)

    def _get_result_text(self) -> str:
        result = self._recognizer.get_result(self._stream)
        text = getattr(result, "text", result)
        self._last_result_text = _normalize_transcript_text(str(text or ""))
        return self._last_result_text

    def _should_reset_on_endpoint(self) -> bool:
        if not bool(self.config.sherpa_enable_endpoint):
            return False
        if not hasattr(self._recognizer, "is_endpoint") or not hasattr(self._recognizer, "reset"):
            return False
        return bool(self._recognizer.is_endpoint(self._stream))

    def _filter_transcripts(self, transcripts: Sequence[str]) -> list[str]:
        min_length = max(1, int(self.config.min_transcript_length))
        return [text for text in (_normalize_transcript_text(item) for item in transcripts) if len(text) >= min_length]

    def _make_segments(self, transcripts: Sequence[str], *, partial: bool) -> list[LocalVoiceTranscriptSegment]:
        return [
            LocalVoiceTranscriptSegment(text=text, partial=partial)
            for text in self._filter_transcripts(transcripts)
        ]


class LocalVoiceInputService:
    """Background microphone listener that streams Aliyun RASR transcripts upstream."""

    def __init__(
        self,
        config: LocalVoiceInputConfig,
        *,
        on_transcript: Callable[[str, Mapping[str, Any] | None], Awaitable[bool]],
        logger: Any = None,
    ) -> None:
        self.config = config
        self.on_transcript = on_transcript
        self.logger = logger
        self._audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._capture_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._stream: Any = None
        self._transcriber: Any = None
        self._dropped_audio_chunks = 0
        self._transcript_sequence = 0
        self._startup_event = asyncio.Event()
        self._startup_exception: Exception | None = None

    async def start(self) -> None:
        """Start microphone capture and streaming recognition."""

        if self._capture_task is not None and not self._capture_task.done():
            return
        self._stop_event = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._startup_event = asyncio.Event()
        self._startup_exception = None
        self._capture_task = asyncio.create_task(self._run_streaming_loop(), name="maibot_local_voice.aliyun_rasr")
        await asyncio.wait_for(self._startup_event.wait(), timeout=15.0)
        if self._startup_exception is not None:
            task = self._capture_task
            self._capture_task = None
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            raise self._startup_exception

    async def stop(self) -> None:
        """Stop microphone capture and flush any stable transcript tail."""

        self._stop_event.set()
        await self._close_stream()
        task = self._capture_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._capture_task = None
        self._transcriber = None

    async def _run_streaming_loop(self) -> None:
        try:
            self._transcriber = AliyunRasrStreamingTranscriber(self.config, logger=self.logger)
            await self._transcriber.start()
            await asyncio.to_thread(self._open_stream)
        except Exception as exc:
            self._startup_exception = exc
            self._startup_event.set()
            self._log_warning(f"Local microphone Aliyun RASR input could not start: {exc}")
            return
        self._startup_event.set()
        self._log_info("Local microphone Aliyun RASR input started.")
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = await asyncio.wait_for(self._audio_queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                await self._process_audio_chunk(chunk)
        except asyncio.CancelledError:
            await self._flush_transcriber()
            raise
        finally:
            await self._flush_transcriber()
            await self._close_stream()
            transcriber = self._transcriber
            if transcriber is not None and hasattr(transcriber, "close"):
                with contextlib.suppress(Exception):
                    await transcriber.close()
            self._log_info("Local microphone Aliyun RASR input stopped.")

    async def _process_audio_chunk(self, chunk: np.ndarray) -> None:
        transcriber = self._transcriber
        if transcriber is None:
            return
        try:
            if hasattr(transcriber, "accept_audio_events"):
                result = transcriber.accept_audio_events(chunk)
                segments = await result if inspect.isawaitable(result) else result
            else:
                segments = [
                    LocalVoiceTranscriptSegment(text=text, partial=True)
                    for text in transcriber.accept_audio(chunk)
                ]
        except Exception as exc:
            if _is_rasr_transport_closing_error(exc):
                await self._restart_transcriber_after_transport_close(exc)
                return
            self._log_warning(f"Local microphone Aliyun RASR decoding failed: {exc}")
            return
        for segment in segments:
            await self._route_transcript(
                str(segment.text),
                partial=bool(segment.partial),
                speech_boundary=bool(getattr(segment, "boundary", False)),
                segment=segment,
            )

    async def _restart_transcriber_after_transport_close(self, exc: BaseException) -> None:
        self._log_warning(f"Local microphone Aliyun RASR connection closed; reconnecting: {exc}")
        transcriber = self._transcriber
        self._transcriber = None
        if transcriber is not None and hasattr(transcriber, "close"):
            with contextlib.suppress(Exception):
                result = transcriber.close()
                if inspect.isawaitable(result):
                    await result
        try:
            replacement = AliyunRasrStreamingTranscriber(self.config, logger=self.logger)
            await replacement.start()
        except Exception as restart_exc:
            self._stop_event.set()
            await self._close_stream()
            self._log_warning(
                "Local microphone Aliyun RASR reconnection failed; stopping input: "
                f"{restart_exc}"
            )
            return
        self._transcriber = replacement
        self._log_info("Local microphone Aliyun RASR connection reconnected.")

    async def _flush_transcriber(self) -> None:
        transcriber = self._transcriber
        if transcriber is None:
            return
        try:
            if hasattr(transcriber, "flush_events"):
                result = transcriber.flush_events()
                segments = await result if inspect.isawaitable(result) else result
                for segment in segments:
                    await self._route_transcript(
                        str(segment.text),
                        partial=bool(segment.partial),
                        speech_boundary=bool(getattr(segment, "boundary", False)),
                        segment=segment,
                    )
                return
            result = transcriber.flush()
            transcripts = await result if inspect.isawaitable(result) else result
        except Exception as exc:
            self._log_warning(f"Local microphone Aliyun RASR flush failed: {exc}")
            return
        for text in transcripts:
            await self._route_transcript(text, partial=False)

    async def _route_transcript(
        self,
        text: str,
        *,
        partial: bool,
        speech_boundary: bool = False,
        segment: LocalVoiceTranscriptSegment | None = None,
    ) -> None:
        normalized_text = _normalize_transcript_text(text)
        if len(normalized_text) < max(1, int(self.config.min_transcript_length)) and not speech_boundary:
            return
        self._transcript_sequence += 1
        metadata = {
            "phrase_id": f"local-voice-{self._transcript_sequence}-{uuid4().hex}",
            "sequence": self._transcript_sequence,
            "sample_rate_hz": max(1, int(self.config.sample_rate_hz)),
            "source": "microphone",
            "engine": "aliyun_rasr",
            "model": str(getattr(self.config, "rasr_model", "") or "fun-asr-realtime"),
            "partial": partial,
            "route_to_maibot": bool(getattr(self.config, "rasr_route_partials_to_maibot", False)) if partial else True,
            "captured_at": time.time(),
        }
        if segment is not None:
            begin_time_ms = getattr(segment, "begin_time_ms", None)
            end_time_ms = getattr(segment, "end_time_ms", None)
            task_id = str(getattr(segment, "task_id", "") or "")
            if begin_time_ms is not None:
                metadata["begin_time_ms"] = begin_time_ms
            if end_time_ms is not None:
                metadata["end_time_ms"] = end_time_ms
            if task_id:
                metadata["rasr_task_id"] = task_id
        if speech_boundary:
            metadata["speech_boundary"] = True
        try:
            await self.on_transcript(normalized_text, metadata)
        except Exception as exc:
            self._log_warning(f"Local microphone transcript routing failed: {exc}")

    def _open_stream(self) -> None:
        try:
            import sounddevice as sd
        except ImportError as exc:  # pragma: no cover - depends on runtime environment
            raise RuntimeError("sounddevice is required for local microphone input") from exc
        devices = sd.query_devices()
        device_index = find_input_device_index(devices, self.config.input_device)
        if self.config.input_device and device_index is None:
            available = ", ".join(_device_name(device) for device in devices if _max_input_channels(device) > 0)
            raise RuntimeError(
                f"input device containing {self.config.input_device!r} was not found; available inputs: {available}"
            )
        stream = sd.InputStream(
            samplerate=max(1, int(self.config.sample_rate_hz)),
            channels=max(1, int(self.config.channels)),
            dtype="float32",
            blocksize=max(1, int(round(self.config.sample_rate_hz * self.config.block_duration_ms / 1000.0))),
            device=device_index,
            callback=self._on_audio_callback,
        )
        stream.start()
        self._stream = stream

    async def _close_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        await asyncio.to_thread(_stop_stream_sync, stream)

    def _on_audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: Any,
        status: Any,
    ) -> None:
        del frames, time_info
        if status:
            self._log_warning(f"Local microphone input status: {status}")
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        chunk = np.array(indata, dtype=np.float32, copy=True)
        loop.call_soon_threadsafe(self._enqueue_audio_chunk, chunk)

    def _enqueue_audio_chunk(self, chunk: np.ndarray) -> None:
        try:
            self._audio_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            self._dropped_audio_chunks += 1
            if self._dropped_audio_chunks == 1 or self._dropped_audio_chunks % 20 == 0:
                self._log_warning(
                    f"Local microphone audio queue overflowed; dropped chunks={self._dropped_audio_chunks}"
                )

    def _log_info(self, message: str) -> None:
        info = getattr(self.logger, "info", None)
        if info is not None:
            info(message)

    def _log_warning(self, message: str) -> None:
        warning = getattr(self.logger, "warning", None)
        if warning is not None:
            warning(message)


def find_input_device_index(devices: Sequence[Any], device_name: str) -> int | None:
    """Find the first input-capable device whose name contains the configured text."""

    target = str(device_name or "").strip().lower()
    default_input: int | None = None
    for index, device in enumerate(devices):
        if _max_input_channels(device) <= 0:
            continue
        if default_input is None:
            default_input = index
        if target and target in _device_name(device).lower():
            return index
    return default_input if not target else None


def _normalize_mono_float32(samples: np.ndarray | Sequence[float]) -> np.ndarray:
    array = np.asarray(samples, dtype=np.float32)
    if array.ndim == 0:
        return np.zeros(0, dtype=np.float32)
    if array.ndim == 1:
        return array.astype(np.float32, copy=False)
    if array.shape[1] == 1:
        return array[:, 0].astype(np.float32, copy=False)
    return array.mean(axis=1, dtype=np.float32).astype(np.float32, copy=False)


def _remove_dc_offset(samples: np.ndarray) -> np.ndarray:
    cleaned = samples.astype(np.float32, copy=True)
    if cleaned.size == 0:
        return cleaned
    cleaned -= float(np.mean(cleaned, dtype=np.float32))
    return cleaned


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float32), dtype=np.float32)))


def _normalize_transcript_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _is_rasr_transport_closing_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, BrokenPipeError)):
        return True
    message = f"{exc.__class__.__name__}: {exc}".lower()
    return any(
        marker in message
        for marker in (
            "cannot write to closing transport",
            "closed transport",
            "connection reset",
            "connection aborted",
            "connection lost",
            "websocket is closed",
            "websocket connection is closed",
        )
    )


def _resolve_rasr_language_hints(value: Any) -> list[str]:
    allowed = {"zh", "en"}
    tokens: list[str] = []
    normalized = str(value or "").strip().lower()
    if not normalized:
        return ["zh", "en"]
    for chunk in normalized.replace("，", ",").replace("；", ",").replace("/", ",").replace("|", ",").split(","):
        for token in chunk.split():
            candidate = token.strip().lower()
            if candidate in allowed and candidate not in tokens:
                tokens.append(candidate)
    return tokens or ["zh", "en"]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _longest_common_prefix(left: str, right: str) -> str:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return left[:index]


def _device_name(device: Any) -> str:
    if isinstance(device, dict):
        return str(device.get("name") or "")
    try:
        return str(device["name"] or "")
    except Exception:
        return ""


def _max_input_channels(device: Any) -> int:
    if isinstance(device, dict):
        return _optional_int(device.get("max_input_channels"))
    try:
        return _optional_int(device["max_input_channels"])
    except Exception:
        return 0


def _optional_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _stop_stream_sync(stream: Any) -> None:
    with contextlib.suppress(Exception):
        stream.stop()
    with contextlib.suppress(Exception):
        stream.close()

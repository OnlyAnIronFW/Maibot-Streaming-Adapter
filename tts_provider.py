"""Local TTS providers for the Bilibili live adapter."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

import contextlib
import json
import math
import time
import wave
from uuid import uuid4

try:
    from aiohttp import ClientSession, ClientTimeout

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False


@dataclass(frozen=True)
class SynthesizedSpeech:
    """A synthesized speech artifact with metadata for timeline sync."""

    provider: str
    text: str
    audio_ref: str
    audio_duration_ms: int
    sample_rate: int
    amplitudes: list[dict[str, int | float]]
    amplitude_stats: Mapping[str, Any] | None = None
    content_type: str = "audio/wav"

    def to_audio_timeline(self) -> dict[str, Any]:
        """Convert the synthesized speech metadata to a Live2D audio timeline."""

        return {
            "audio_duration_ms": self.audio_duration_ms,
            "audio_ref": self.audio_ref,
            "amplitudes": list(self.amplitudes),
            "amplitude_stats": dict(self.amplitude_stats or {}),
            "provider": self.provider,
            "content_type": self.content_type,
        }


@runtime_checkable
class TTSProviderProtocol(Protocol):
    """Small async protocol used by the plugin runtime."""

    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...

    async def synthesize(
        self,
        text: str,
        request_overrides: Mapping[str, Any] | None = None,
    ) -> SynthesizedSpeech:
        ...


class GPTSoVITSTTSProvider:
    """Thin HTTP client for a local GPT-SoVITS v2 `/tts` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        connect_timeout_sec: float,
        request_timeout_sec: float = 45.0,
        output_dir: str,
        request_defaults: Mapping[str, Any],
        gpt_weights_path: str = "",
        sovits_weights_path: str = "",
        amplitude_interval_ms: int = 80,
        amplitude_normalization_enabled: bool = True,
        amplitude_noise_floor: float = 0.015,
        amplitude_peak_percentile: float = 0.95,
        amplitude_normalization_gain: float = 1.0,
        logger: Any = None,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.connect_timeout_sec = max(1.0, float(connect_timeout_sec or 30.0))
        self.request_timeout_sec = max(self.connect_timeout_sec, float(request_timeout_sec or 45.0))
        self.output_dir = Path(output_dir).expanduser()
        self.request_defaults = dict(request_defaults)
        self.gpt_weights_path = str(gpt_weights_path or "").strip()
        self.sovits_weights_path = str(sovits_weights_path or "").strip()
        self.amplitude_interval_ms = max(20, int(amplitude_interval_ms))
        self.amplitude_normalization_enabled = bool(amplitude_normalization_enabled)
        self.amplitude_noise_floor = min(1.0, max(0.0, float(amplitude_noise_floor)))
        self.amplitude_peak_percentile = min(1.0, max(0.0, float(amplitude_peak_percentile)))
        self.amplitude_normalization_gain = min(4.0, max(0.0, float(amplitude_normalization_gain)))
        self.logger = logger
        self._session: Any = None

    async def start(self) -> None:
        if not AIOHTTP_AVAILABLE:
            self._log_warning("GPT-SoVITS provider disabled because aiohttp is unavailable")
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self._session is None:
            timeout = ClientTimeout(
                total=self.request_timeout_sec,
                connect=self.connect_timeout_sec,
                sock_read=self.request_timeout_sec,
            )
            self._session = ClientSession(timeout=timeout, headers={"User-Agent": "MaiBot-Bilibili-Live-Adapter/0.1"})
        await self._switch_model_weights()

    async def stop(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    async def _switch_model_weights(self) -> None:
        """Switch GPT and SoVITS model weights via the API if paths are configured."""
        if self._session is None:
            return
        if self.gpt_weights_path:
            self._log_info(f"GPT-SoVITS switching GPT weights: {self.gpt_weights_path}")
            try:
                async with self._session.get(
                    f"{self.base_url}/set_gpt_weights",
                    params={"weights_path": self.gpt_weights_path},
                ) as response:
                    if response.status >= 400:
                        body = await response.read()
                        self._log_warning(f"set_gpt_weights failed ({response.status}): {body.decode('utf-8', errors='ignore')}")
                    else:
                        self._log_info("GPT-SoVITS GPT weights switched successfully")
            except Exception as exc:
                self._log_warning(f"set_gpt_weights error: {exc}")
        if self.sovits_weights_path:
            self._log_info(f"GPT-SoVITS switching SoVITS weights: {self.sovits_weights_path}")
            try:
                async with self._session.get(
                    f"{self.base_url}/set_sovits_weights",
                    params={"weights_path": self.sovits_weights_path},
                ) as response:
                    if response.status >= 400:
                        body = await response.read()
                        self._log_warning(f"set_sovits_weights failed ({response.status}): {body.decode('utf-8', errors='ignore')}")
                    else:
                        self._log_info("GPT-SoVITS SoVITS weights switched successfully")
            except Exception as exc:
                self._log_warning(f"set_sovits_weights error: {exc}")

    async def synthesize(
        self,
        text: str,
        request_overrides: Mapping[str, Any] | None = None,
    ) -> SynthesizedSpeech:
        normalized_text = str(text or "").strip()
        if not normalized_text:
            raise ValueError("text is required for GPT-SoVITS synthesis")
        if not self.base_url:
            raise ValueError("GPT-SoVITS base_url is empty")
        if self._session is None:
            await self.start()
        if self._session is None:
            raise RuntimeError("GPT-SoVITS HTTP session is unavailable")

        payload = build_gpt_sovits_request(
            self.request_defaults,
            normalized_text,
            request_overrides=request_overrides,
        )
        self._log_info(
            "GPT-SoVITS /tts request: "
            f"text_lang={payload.get('text_lang')!r} prompt_lang={payload.get('prompt_lang')!r} "
            f"text_split_method={payload.get('text_split_method')!r} text_len={len(payload.get('text') or '')} "
            f"text_preview={_preview_text(payload.get('text'))!r}"
        )
        async with self._session.post(f"{self.base_url}/tts", json=payload) as response:
            response_bytes = await response.read()
            if response.status >= 400:
                raise RuntimeError(_format_tts_error(response_bytes, response.status))
            content_type = str(response.headers.get("Content-Type") or "audio/wav")

        output_path = self.output_dir / f"{int(time.time() * 1000)}-{uuid4().hex}.wav"
        output_path.write_bytes(response_bytes)
        audio_duration_ms, sample_rate, amplitudes, amplitude_stats = extract_audio_timeline_from_wav_bytes(
            response_bytes,
            amplitude_interval_ms=self.amplitude_interval_ms,
            amplitude_normalization_enabled=self.amplitude_normalization_enabled,
            amplitude_noise_floor=self.amplitude_noise_floor,
            amplitude_peak_percentile=self.amplitude_peak_percentile,
            amplitude_normalization_gain=self.amplitude_normalization_gain,
        )
        return SynthesizedSpeech(
            provider="gpt_sovits_v2",
            text=normalized_text,
            audio_ref=str(output_path),
            audio_duration_ms=audio_duration_ms,
            sample_rate=sample_rate,
            amplitudes=amplitudes,
            amplitude_stats=amplitude_stats,
            content_type=content_type,
        )

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)

    def _log_info(self, message: str) -> None:
        if self.logger is not None:
            try:
                self.logger.info(message)
            except AttributeError:
                pass


def build_gpt_sovits_request(
    defaults: Mapping[str, Any],
    text: str,
    request_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a GPT-SoVITS `/tts` request payload from defaults and text."""

    merged_defaults = dict(defaults)
    if request_overrides:
        merged_defaults.update(dict(request_overrides))
    payload = {
        "text": str(text or "").strip(),
        "text_lang": str(merged_defaults.get("text_lang") or "zh").strip().lower(),
        "media_type": "wav",
        "streaming_mode": False,
    }
    if "ref_audio_path" in merged_defaults:
        payload["ref_audio_path"] = str(merged_defaults.get("ref_audio_path") or "").strip()
    if "aux_ref_audio_paths" in merged_defaults:
        aux_ref_audio_paths = list(merged_defaults.get("aux_ref_audio_paths") or [])
        if aux_ref_audio_paths:
            payload["aux_ref_audio_paths"] = aux_ref_audio_paths
    if "prompt_text" in merged_defaults:
        payload["prompt_text"] = str(merged_defaults.get("prompt_text") or "")
    if "prompt_lang" in merged_defaults:
        payload["prompt_lang"] = str(merged_defaults.get("prompt_lang") or "zh").strip().lower()
    if "top_k" in merged_defaults:
        payload["top_k"] = int(merged_defaults.get("top_k") or 5)
    if "top_p" in merged_defaults:
        payload["top_p"] = float(merged_defaults.get("top_p") or 1.0)
    if "temperature" in merged_defaults:
        payload["temperature"] = float(merged_defaults.get("temperature") or 1.0)
    if "text_split_method" in merged_defaults:
        payload["text_split_method"] = str(merged_defaults.get("text_split_method") or "cut5").strip() or "cut5"
    if "batch_size" in merged_defaults:
        payload["batch_size"] = int(merged_defaults.get("batch_size") or 1)
    if "batch_threshold" in merged_defaults:
        payload["batch_threshold"] = float(merged_defaults.get("batch_threshold") or 0.75)
    if "split_bucket" in merged_defaults:
        payload["split_bucket"] = bool(merged_defaults.get("split_bucket", True))
    if "speed_factor" in merged_defaults:
        payload["speed_factor"] = float(merged_defaults.get("speed_factor") or 1.0)
    if "seed" in merged_defaults:
        payload["seed"] = int(merged_defaults.get("seed") or -1)
    if "parallel_infer" in merged_defaults:
        payload["parallel_infer"] = bool(merged_defaults.get("parallel_infer", True))
    if "repetition_penalty" in merged_defaults:
        payload["repetition_penalty"] = float(merged_defaults.get("repetition_penalty") or 1.35)
    return payload


def _preview_text(text: Any, *, limit: int = 80) -> str:
    normalized = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."


def build_synthesized_speech_from_wav(
    path: str | Path,
    text: str,
    *,
    provider: str = "rvc_song",
    amplitude_interval_ms: int = 80,
    amplitude_normalization_enabled: bool = True,
    amplitude_noise_floor: float = 0.015,
    amplitude_peak_percentile: float = 0.95,
    amplitude_normalization_gain: float = 1.0,
) -> SynthesizedSpeech:
    """Build a SynthesizedSpeech artifact from an existing wav file."""

    resolved_path = Path(path).expanduser().resolve()
    audio_bytes = resolved_path.read_bytes()
    audio_duration_ms, sample_rate, amplitudes, amplitude_stats = extract_audio_timeline_from_wav_bytes(
        audio_bytes,
        amplitude_interval_ms=amplitude_interval_ms,
        amplitude_normalization_enabled=amplitude_normalization_enabled,
        amplitude_noise_floor=amplitude_noise_floor,
        amplitude_peak_percentile=amplitude_peak_percentile,
        amplitude_normalization_gain=amplitude_normalization_gain,
    )
    return SynthesizedSpeech(
        provider=str(provider or "external"),
        text=str(text or ""),
        audio_ref=str(resolved_path),
        audio_duration_ms=audio_duration_ms,
        sample_rate=sample_rate,
        amplitudes=amplitudes,
        amplitude_stats=amplitude_stats,
        content_type="audio/wav",
    )


def extract_audio_timeline_from_wav_bytes(
    audio_bytes: bytes,
    *,
    amplitude_interval_ms: int = 80,
    amplitude_normalization_enabled: bool = True,
    amplitude_noise_floor: float = 0.015,
    amplitude_peak_percentile: float = 0.95,
    amplitude_normalization_gain: float = 1.0,
) -> tuple[int, int, list[dict[str, int | float]], dict[str, bool | float]]:
    """Extract duration and amplitude envelope data from a wav payload."""

    with wave.open(BytesIO(audio_bytes), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        sample_width = wav_file.getsampwidth()
        channels = wav_file.getnchannels()
        total_frames = wav_file.getnframes()
        pcm_bytes = wav_file.readframes(total_frames)

    if sample_rate <= 0 or total_frames <= 0:
        return 0, max(1, sample_rate), [], _amplitude_stats(
            normalization_enabled=amplitude_normalization_enabled,
            noise_floor=amplitude_noise_floor,
            peak_value=0.0,
            raw_max=0.0,
        )

    duration_ms = int(total_frames / sample_rate * 1000.0)
    frames_per_chunk = max(1, int(sample_rate * max(20, amplitude_interval_ms) / 1000.0))
    bytes_per_frame = max(1, sample_width * max(1, channels))
    normalizer = _pcm_normalizer(sample_width)
    raw_amplitudes: list[dict[str, int | float]] = []
    raw_values: list[float] = []
    for start_frame in range(0, total_frames, frames_per_chunk):
        end_frame = min(total_frames, start_frame + frames_per_chunk)
        chunk = pcm_bytes[start_frame * bytes_per_frame : end_frame * bytes_per_frame]
        rms = _pcm_rms(chunk, sample_width)
        raw_value = min(1.0, rms / normalizer) if normalizer > 0 else 0.0
        raw_values.append(raw_value)
        raw_amplitudes.append(
            {
                "offset_ms": int(start_frame / sample_rate * 1000.0),
                "value": raw_value,
            }
        )
    amplitudes, stats = normalize_amplitude_envelope(
        raw_amplitudes,
        normalization_enabled=amplitude_normalization_enabled,
        noise_floor=amplitude_noise_floor,
        peak_percentile=amplitude_peak_percentile,
        normalization_gain=amplitude_normalization_gain,
    )
    stats["raw_max"] = max(raw_values) if raw_values else 0.0
    return duration_ms, sample_rate, amplitudes, stats


def normalize_amplitude_envelope(
    amplitudes: list[dict[str, int | float]],
    *,
    normalization_enabled: bool = True,
    noise_floor: float = 0.015,
    peak_percentile: float = 0.95,
    normalization_gain: float = 1.0,
) -> tuple[list[dict[str, int | float]], dict[str, bool | float]]:
    """Normalize raw RMS amplitudes per utterance for stable mouth-open values."""

    raw_values = [min(1.0, max(0.0, float(item.get("value") or 0.0))) for item in amplitudes]
    raw_max = max(raw_values) if raw_values else 0.0
    normalized_noise_floor = min(1.0, max(0.0, float(noise_floor)))
    normalized_peak_percentile = min(1.0, max(0.0, float(peak_percentile)))
    normalized_gain = min(4.0, max(0.0, float(normalization_gain)))
    peak_value = _percentile(raw_values, normalized_peak_percentile) if raw_values else 0.0
    if not normalization_enabled:
        return [
            {"offset_ms": int(item.get("offset_ms") or 0), "value": raw_value}
            for item, raw_value in zip(amplitudes, raw_values, strict=False)
        ], _amplitude_stats(
            normalization_enabled=False,
            noise_floor=normalized_noise_floor,
            peak_value=peak_value,
            raw_max=raw_max,
        )

    usable_peak = max(peak_value, normalized_noise_floor + 0.001)
    scale = max(0.001, usable_peak - normalized_noise_floor)
    normalized_amplitudes: list[dict[str, int | float]] = []
    for item, raw_value in zip(amplitudes, raw_values, strict=False):
        if raw_value <= normalized_noise_floor:
            value = 0.0
        else:
            value = ((raw_value - normalized_noise_floor) / scale) * normalized_gain
        normalized_amplitudes.append(
            {
                "offset_ms": int(item.get("offset_ms") or 0),
                "value": min(1.0, max(0.0, value)),
            }
        )
    return normalized_amplitudes, _amplitude_stats(
        normalization_enabled=True,
        noise_floor=normalized_noise_floor,
        peak_value=usable_peak,
        raw_max=raw_max,
    )


def _amplitude_stats(
    *,
    normalization_enabled: bool,
    noise_floor: float,
    peak_value: float,
    raw_max: float,
) -> dict[str, bool | float]:
    return {
        "normalization_enabled": bool(normalization_enabled),
        "noise_floor": min(1.0, max(0.0, float(noise_floor))),
        "peak_value": min(1.0, max(0.0, float(peak_value))),
        "raw_max": min(1.0, max(0.0, float(raw_max))),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    clamped = min(1.0, max(0.0, float(percentile)))
    position = clamped * (len(sorted_values) - 1)
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return sorted_values[lower_index]
    fraction = position - lower_index
    return sorted_values[lower_index] * (1.0 - fraction) + sorted_values[upper_index] * fraction


def _format_tts_error(response_bytes: bytes, status_code: int) -> str:
    try:
        payload = json.loads(response_bytes.decode("utf-8"))
    except Exception:
        payload = None
    if isinstance(payload, Mapping):
        message = str(payload.get("message") or payload.get("error") or "").strip()
        details = str(payload.get("Exception") or "").strip()
        if message and details:
            return f"GPT-SoVITS returned {status_code}: {message} ({details})"
        if message:
            return f"GPT-SoVITS returned {status_code}: {message}"
    preview = response_bytes.decode("utf-8", errors="ignore").strip()
    return f"GPT-SoVITS returned {status_code}: {preview or 'unknown error'}"


def _pcm_normalizer(sample_width: int) -> float:
    if sample_width == 1:
        return 127.0
    if sample_width == 2:
        return 32767.0
    if sample_width == 4:
        return 2147483647.0
    raise ValueError(f"unsupported PCM sample width: {sample_width}")


def _pcm_rms(pcm_bytes: bytes, sample_width: int) -> float:
    if not pcm_bytes:
        return 0.0
    if sample_width == 1:
        total = 0.0
        count = 0
        for sample in pcm_bytes:
            centered = float(sample) - 128.0
            total += centered * centered
            count += 1
        return math.sqrt(total / max(1, count))
    if sample_width == 2:
        total = 0.0
        count = 0
        for sample in memoryview(pcm_bytes).cast("h"):
            value = float(sample)
            total += value * value
            count += 1
        return math.sqrt(total / max(1, count))
    if sample_width == 4:
        total = 0.0
        count = 0
        for sample in memoryview(pcm_bytes).cast("i"):
            value = float(sample)
            total += value * value
            count += 1
        return math.sqrt(total / max(1, count))
    raise ValueError(f"unsupported PCM sample width: {sample_width}")

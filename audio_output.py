"""Local wav playback for routing synthesized speech to a user-selected output device."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Sequence

import asyncio
import wave

import numpy as np


class LocalAudioOutputPlayer:
    """Play synthesized wav files to a selected Windows output device."""

    def __init__(
        self,
        *,
        output_device: str = "CABLE Input",
        volume: float = 1.0,
        logger: Any = None,
    ) -> None:
        self.output_device = str(output_device or "").strip()
        self.volume = min(2.0, max(0.0, float(volume)))
        self.logger = logger
        self._lock = asyncio.Lock()

    async def play(
        self,
        audio_ref: str,
        *,
        duration_ms: int = 0,
        volume: float | None = None,
        on_audio_start: Callable[[], None] | None = None,
    ) -> bool:
        """Play one wav file and wait until playback finishes."""

        del duration_ms
        audio_path = Path(audio_ref).expanduser().resolve()
        if not audio_path.exists():
            self._log_warning(f"Local playback audio file is missing: {audio_path}")
            return False
        async with self._lock:
            try:
                await asyncio.to_thread(self._play_sync, audio_path, volume=volume, on_audio_start=on_audio_start)
            except Exception as exc:
                self._log_warning(f"Local audio playback failed: {exc}")
                return False
        return True

    async def stop(self) -> None:
        """Best-effort stop for any in-flight local playback."""

        try:
            await asyncio.to_thread(self._stop_sync)
        except Exception as exc:
            self._log_warning(f"Local audio playback stop failed: {exc}")

    def _play_sync(
        self,
        audio_path: Path,
        *,
        volume: float | None = None,
        on_audio_start: Callable[[], None] | None = None,
    ) -> None:
        try:
            import sounddevice as sd
        except ImportError as exc:  # pragma: no cover - depends on runtime environment
            raise RuntimeError("sounddevice is required for VB-Cable audio playback") from exc

        effective_volume = self.volume if volume is None else min(2.0, max(0.0, float(volume)))
        audio_data, sample_rate = load_wav_float32(audio_path, volume=effective_volume)
        devices = sd.query_devices()
        device_index = find_output_device_index(devices, self.output_device)
        if self.output_device and device_index is None:
            available = ", ".join(_device_name(device) for device in devices if _max_output_channels(device) > 0)
            raise RuntimeError(
                f"output device containing {self.output_device!r} was not found; available outputs: {available}"
            )
        if on_audio_start is not None:
            try:
                on_audio_start()
            except Exception as exc:
                self._log_warning(f"Local audio playback start callback failed: {exc}")
        sd.play(audio_data, samplerate=sample_rate, device=device_index, blocking=True)

    def _stop_sync(self) -> None:
        try:
            import sounddevice as sd
        except ImportError:
            return
        sd.stop()

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)


def find_output_device_index(devices: Sequence[Any], device_name: str) -> int | None:
    """Find the first output-capable device whose name contains the configured text."""

    target = str(device_name or "").strip().lower()
    default_output: int | None = None
    for index, device in enumerate(devices):
        if _max_output_channels(device) <= 0:
            continue
        if default_output is None:
            default_output = index
        if target and target in _device_name(device).lower():
            return index
    return default_output if not target else None


def load_wav_float32(path: Path, *, volume: float = 1.0) -> tuple[np.ndarray, int]:
    """Load PCM wav audio as float32 samples for sounddevice."""

    with wave.open(str(path), "rb") as wav_file:
        channels = max(1, int(wav_file.getnchannels()))
        sample_rate = int(wav_file.getframerate())
        sample_width = int(wav_file.getsampwidth())
        raw_frames = wav_file.readframes(wav_file.getnframes())
    if not raw_frames:
        return np.zeros((0, channels), dtype=np.float32), sample_rate
    samples = _decode_pcm(raw_frames, sample_width)
    if channels > 1:
        samples = samples.reshape(-1, channels)
    return (samples * min(2.0, max(0.0, float(volume)))).astype(np.float32, copy=False), sample_rate


def _decode_pcm(raw_frames: bytes, sample_width: int) -> np.ndarray:
    if sample_width == 1:
        return (np.frombuffer(raw_frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if sample_width == 2:
        return np.frombuffer(raw_frames, dtype="<i2").astype(np.float32) / 32768.0
    if sample_width == 3:
        raw = np.frombuffer(raw_frames, dtype=np.uint8).reshape(-1, 3)
        values = raw[:, 0].astype(np.int32) | (raw[:, 1].astype(np.int32) << 8) | (raw[:, 2].astype(np.int32) << 16)
        values = values - ((values & 0x800000) << 1)
        return values.astype(np.float32) / 8388608.0
    if sample_width == 4:
        return np.frombuffer(raw_frames, dtype="<i4").astype(np.float32) / 2147483648.0
    raise ValueError(f"unsupported wav sample width: {sample_width}")


def _device_name(device: Any) -> str:
    if isinstance(device, dict):
        return str(device.get("name") or "")
    try:
        return str(device["name"] or "")
    except Exception:
        return ""


def _max_output_channels(device: Any) -> int:
    if isinstance(device, dict):
        return _optional_int(device.get("max_output_channels"))
    try:
        return _optional_int(device["max_output_channels"])
    except Exception:
        return 0


def _optional_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

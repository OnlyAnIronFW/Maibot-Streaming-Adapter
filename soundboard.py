"""Sound effect box runtime and green-screen WebUI service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import asyncio
import contextlib
import json
import mimetypes
import re
import shutil
import subprocess
import time
import tomllib
import wave

try:  # pragma: no cover - availability depends on the host runtime.
    from aiohttp import WSMsgType, web
except Exception:  # pragma: no cover
    WSMsgType = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]

from .audio_output import LocalAudioOutputPlayer
from .config import SoundboardConfig, SoundboardCueConfig


@dataclass(frozen=True)
class SoundboardResolvedCue:
    """A cue plus its stable runtime id and resolved audio path."""

    cue: SoundboardCueConfig
    cue_id: str
    audio_path: Path | None
    media_path: Path | None


@dataclass(frozen=True)
class QueuedSoundboardTrigger:
    resolved: SoundboardResolvedCue
    reason: str
    source_text: str
    triggered_by: str
    repeat_count: int


_AUDIO_SUFFIXES = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac"}
_MEDIA_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v", ".gif", ".png", ".jpg", ".jpeg", ".webp", ".avif"}
_CUE_METADATA_FILENAME = "cue.toml"


def normalize_soundboard_cue_id(value: Any, *, fallback: str = "") -> str:
    """Normalize a cue id for tool names and local WebUI routes."""

    raw = str(value or "").strip()
    if not raw:
        raw = str(fallback or "").strip()
    normalized = re.sub(r"\s+", "_", raw.lower())
    normalized = re.sub(r"[^a-z0-9_.-]+", "", normalized)
    return normalized.strip("._-")


def list_soundboard_cues(
    settings: SoundboardConfig,
    *,
    plugin_dir: Path | None = None,
    available_only: bool = False,
) -> list[dict[str, Any]]:
    """Return public cue metadata for tools and the WebUI handshake."""

    settings = load_soundboard_cues_from_files(settings, plugin_dir=plugin_dir)
    result: list[dict[str, Any]] = []
    for index, cue in enumerate(settings.cues):
        if not cue.enabled:
            continue
        if available_only and not soundboard_cue_has_available_assets(settings, cue, plugin_dir=plugin_dir):
            continue
        cue_id = normalize_soundboard_cue_id(cue.id, fallback=cue.label or f"cue_{index + 1}")
        if not cue_id:
            cue_id = f"cue_{index + 1}"
        result.append(
            {
                "id": cue_id,
                "label": cue.label or cue_id,
                "keywords": list(cue.keywords),
                "usage_hint": cue.usage_hint,
                "duration_ms": max(120, int(cue.duration_ms)),
                "has_media": bool(cue.media_path),
                "media_kind": detect_soundboard_media_kind(cue.media_path),
                "play_audio": bool(cue.play_audio),
                "show_effect": bool(cue.show_effect),
            }
        )
    return result


def resolve_soundboard_cue(
    settings: SoundboardConfig,
    cue_name: str,
    *,
    plugin_dir: Path | None = None,
    available_only: bool = False,
) -> SoundboardResolvedCue | None:
    """Resolve a cue by id or label."""

    settings = load_soundboard_cues_from_files(settings, plugin_dir=plugin_dir)
    wanted = str(cue_name or "").strip()
    wanted_id = normalize_soundboard_cue_id(wanted)
    wanted_folded = wanted.casefold()
    if not wanted_id and not wanted_folded:
        return None
    for index, cue in enumerate(settings.cues):
        if not cue.enabled:
            continue
        cue_id = normalize_soundboard_cue_id(cue.id, fallback=cue.label or f"cue_{index + 1}")
        if not cue_id:
            cue_id = f"cue_{index + 1}"
        if available_only and not soundboard_cue_has_available_assets(settings, cue, plugin_dir=plugin_dir):
            continue
        label = str(cue.label or "").strip()
        if wanted_id and wanted_id == cue_id:
            return SoundboardResolvedCue(
                cue=cue,
                cue_id=cue_id,
                audio_path=resolve_soundboard_audio_path(settings, cue, plugin_dir=plugin_dir),
                media_path=resolve_soundboard_media_path(settings, cue, plugin_dir=plugin_dir),
            )
        if label and wanted_folded and wanted_folded == label.casefold():
            return SoundboardResolvedCue(
                cue=cue,
                cue_id=cue_id,
                audio_path=resolve_soundboard_audio_path(settings, cue, plugin_dir=plugin_dir),
                media_path=resolve_soundboard_media_path(settings, cue, plugin_dir=plugin_dir),
            )
    return None


def match_soundboard_keyword(
    settings: SoundboardConfig,
    text: str,
    *,
    plugin_dir: Path | None = None,
    available_only: bool = False,
) -> SoundboardResolvedCue | None:
    """Find the highest-priority enabled cue matching text."""

    settings = load_soundboard_cues_from_files(settings, plugin_dir=plugin_dir)
    normalized_text = str(text or "").strip()
    if not normalized_text:
        return None
    matches: list[tuple[int, int, SoundboardResolvedCue]] = []
    for index, cue in enumerate(settings.cues):
        if not cue.enabled or not cue.keywords:
            continue
        if available_only and not soundboard_cue_has_available_assets(settings, cue, plugin_dir=plugin_dir):
            continue
        if any(_keyword_matches(normalized_text, keyword, match_mode=cue.match_mode) for keyword in cue.keywords):
            cue_id = normalize_soundboard_cue_id(cue.id, fallback=cue.label or f"cue_{index + 1}")
            if not cue_id:
                cue_id = f"cue_{index + 1}"
            resolved = SoundboardResolvedCue(
                cue=cue,
                cue_id=cue_id,
                audio_path=resolve_soundboard_audio_path(settings, cue, plugin_dir=plugin_dir),
                media_path=resolve_soundboard_media_path(settings, cue, plugin_dir=plugin_dir),
            )
            matches.append((int(cue.priority), -index, resolved))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return matches[0][2]


def soundboard_text_matches_cue(text: str, cue: SoundboardCueConfig) -> bool:
    """Return whether the provided text matches any configured keyword for one cue."""

    normalized_text = str(text or "").strip()
    if not normalized_text or not cue.enabled or not cue.keywords:
        return False
    return any(_keyword_matches(normalized_text, keyword, match_mode=cue.match_mode) for keyword in cue.keywords)


def resolve_soundboard_audio_path(
    settings: SoundboardConfig,
    cue: SoundboardCueConfig,
    *,
    plugin_dir: Path | None = None,
) -> Path | None:
    """Resolve a cue audio path against the configured base directory."""

    raw_path = str(cue.audio_path or "").strip()
    if not raw_path:
        return None
    audio_path = Path(raw_path).expanduser()
    if audio_path.is_absolute():
        return audio_path.resolve()
    base_dir = resolve_soundboard_base_dir(settings, plugin_dir=plugin_dir)
    return (base_dir / audio_path).resolve()


def resolve_soundboard_cached_audio_path(
    settings: SoundboardConfig,
    cue: SoundboardCueConfig,
    *,
    plugin_dir: Path | None = None,
) -> Path | None:
    """Resolve an existing derived audio path for a video cue without requiring config changes."""

    explicit_audio_path = resolve_soundboard_audio_path(settings, cue, plugin_dir=plugin_dir)
    if explicit_audio_path is not None and explicit_audio_path.exists():
        return explicit_audio_path
    media_path = resolve_soundboard_media_path(settings, cue, plugin_dir=plugin_dir)
    if media_path is None or detect_soundboard_media_kind(media_path) != "video":
        return explicit_audio_path
    cue_dir = media_path.parent
    preferred_audio_path = cue_dir / "audio.wav"
    if preferred_audio_path.exists():
        return preferred_audio_path.resolve()
    for candidate in cue_dir.glob("audio.*"):
        if candidate.is_file() and candidate.suffix.lower() in _AUDIO_SUFFIXES:
            return candidate.resolve()
    return explicit_audio_path


def soundboard_cue_has_available_assets(
    settings: SoundboardConfig,
    cue: SoundboardCueConfig,
    *,
    plugin_dir: Path | None = None,
) -> bool:
    """Return whether a cue can currently produce playable output."""

    audio_path = resolve_soundboard_cached_audio_path(settings, cue, plugin_dir=plugin_dir)
    if audio_path is not None and audio_path.exists() and audio_path.is_file():
        return True
    media_path = resolve_soundboard_media_path(settings, cue, plugin_dir=plugin_dir)
    if media_path is not None and media_path.exists() and media_path.is_file():
        return True
    return False


def resolve_soundboard_media_path(
    settings: SoundboardConfig,
    cue: SoundboardCueConfig,
    *,
    plugin_dir: Path | None = None,
) -> Path | None:
    """Resolve an optional cue media asset against the configured base directory."""

    raw_path = str(cue.media_path or "").strip()
    if not raw_path:
        return None
    media_path = Path(raw_path).expanduser()
    if media_path.is_absolute():
        return media_path.resolve()
    base_dir = resolve_soundboard_base_dir(settings, plugin_dir=plugin_dir)
    return (base_dir / media_path).resolve()


def resolve_soundboard_base_dir(
    settings: SoundboardConfig,
    *,
    plugin_dir: Path | None = None,
) -> Path:
    """Resolve the soundboard asset base directory."""

    base_dir = Path(settings.audio_base_dir or "data/soundboard").expanduser()
    if not base_dir.is_absolute():
        base_dir = (plugin_dir or Path(__file__).resolve().parent) / base_dir
    return base_dir.resolve()


def load_soundboard_cues_from_files(
    settings: SoundboardConfig,
    *,
    plugin_dir: Path | None = None,
) -> SoundboardConfig:
    """Load per-cue metadata files under data/soundboard/cues and merge them into runtime settings."""

    base_dir = resolve_soundboard_base_dir(settings, plugin_dir=plugin_dir)
    cues_dir = base_dir / "cues"
    discovered_cues: list[SoundboardCueConfig] = []
    seen_ids: set[str] = set()
    if cues_dir.exists():
        for cue_dir in sorted((entry for entry in cues_dir.iterdir() if entry.is_dir()), key=lambda entry: entry.name.lower()):
            discovered = _load_soundboard_cue_from_directory(cue_dir, settings=settings, base_dir=base_dir)
            if discovered is None:
                continue
            cue_id = normalize_soundboard_cue_id(discovered.id, fallback=discovered.label or cue_dir.name)
            if cue_id and cue_id not in seen_ids:
                seen_ids.add(cue_id)
                discovered_cues.append(discovered)
    for cue in settings.cues:
        cue_id = normalize_soundboard_cue_id(cue.id, fallback=cue.label)
        if cue_id and cue_id in seen_ids:
            continue
        discovered_cues.append(cue)
        if cue_id:
            seen_ids.add(cue_id)
    return settings.model_copy(update={"cues": discovered_cues})


def _load_soundboard_cue_from_directory(
    cue_dir: Path,
    *,
    settings: SoundboardConfig,
    base_dir: Path,
) -> SoundboardCueConfig | None:
    cue_file = cue_dir / _CUE_METADATA_FILENAME
    if not cue_file.exists() or not cue_file.is_file():
        return None
    try:
        with cue_file.open("rb") as file_handle:
            raw_config = tomllib.load(file_handle)
    except Exception:
        return None
    if not isinstance(raw_config, Mapping):
        return None
    raw_data = dict(raw_config)
    raw_data["id"] = str(raw_data.get("id") or cue_dir.name).strip() or cue_dir.name
    raw_data["label"] = str(raw_data.get("label") or raw_data["id"]).strip() or raw_data["id"]
    raw_data["audio_path"] = _normalize_cue_asset_reference(
        raw_data.get("audio_path"),
        cue_dir=cue_dir,
        base_dir=base_dir,
        default_prefix="audio",
        allowed_suffixes=_AUDIO_SUFFIXES,
    )
    raw_data["media_path"] = _normalize_cue_asset_reference(
        raw_data.get("media_path"),
        cue_dir=cue_dir,
        base_dir=base_dir,
        default_prefix="media",
        allowed_suffixes=_MEDIA_SUFFIXES,
    )
    try:
        return SoundboardCueConfig.model_validate(raw_data)
    except Exception:
        return None


def _normalize_cue_asset_reference(
    raw_value: Any,
    *,
    cue_dir: Path,
    base_dir: Path,
    default_prefix: str,
    allowed_suffixes: set[str],
) -> str:
    raw_path = str(raw_value or "").strip()
    resolved_path: Path | None = None
    if raw_path:
        candidate = Path(raw_path).expanduser()
        if candidate.is_absolute():
            resolved_path = candidate.resolve()
        else:
            local_candidate = (cue_dir / candidate).resolve()
            if local_candidate.exists():
                resolved_path = local_candidate
            else:
                base_candidate = (base_dir / candidate).resolve()
                if base_candidate.exists():
                    resolved_path = base_candidate
    if resolved_path is None:
        resolved_path = _discover_cue_asset_path(cue_dir, prefix=default_prefix, allowed_suffixes=allowed_suffixes)
    if resolved_path is None:
        return ""
    try:
        return resolved_path.relative_to(base_dir).as_posix()
    except ValueError:
        return str(resolved_path)


def _discover_cue_asset_path(cue_dir: Path, *, prefix: str, allowed_suffixes: set[str]) -> Path | None:
    exact_match = cue_dir / f"{prefix}.wav"
    if prefix == "media":
        exact_match = cue_dir / "media.mp4"
    if exact_match.exists() and exact_match.is_file():
        return exact_match.resolve()
    for candidate in sorted(cue_dir.iterdir(), key=lambda entry: entry.name.lower()):
        if not candidate.is_file() or candidate.suffix.lower() not in allowed_suffixes:
            continue
        if candidate.stem.lower().startswith(prefix):
            return candidate.resolve()
    return None


def detect_soundboard_media_kind(path_value: Any) -> str:
    """Infer whether a cue asset should render as an image or a video."""

    suffix = Path(str(path_value or "")).suffix.lower()
    if suffix in {".mp4", ".webm", ".mov", ".m4v"}:
        return "video"
    if suffix in {".gif", ".png", ".jpg", ".jpeg", ".webp", ".avif"}:
        return "image"
    return ""


def cue_uses_media_audio(
    cue: SoundboardCueConfig,
    *,
    media_path: Path | None,
    browser_audio_enabled: bool,
) -> bool:
    """Return whether a video asset should provide audio in the browser source."""

    if not browser_audio_enabled or not cue.play_audio:
        return False
    if media_path is None or not media_path.exists():
        return False
    return detect_soundboard_media_kind(media_path) == "video"


class SoundboardService:
    """Play configured cues and broadcast green-screen media overlays."""

    def __init__(
        self,
        settings: SoundboardConfig,
        *,
        plugin_dir: Path | None = None,
        logger: Any = None,
    ) -> None:
        self.settings = settings
        self.plugin_dir = plugin_dir or Path(__file__).resolve().parent
        self.logger = logger
        self._runner: Any = None
        self._site: Any = None
        self._websockets: set[Any] = set()
        self._trigger_queue: asyncio.Queue[QueuedSoundboardTrigger] = asyncio.Queue()
        self._queue_worker_task: asyncio.Task[None] | None = None
        self._audio_tasks: set[asyncio.Task[bool]] = set()
        self._audio_extract_locks: dict[str, asyncio.Lock] = {}
        self._last_triggered_at: dict[str, float] = {}
        self._last_global_triggered_at = 0.0
        self._player: LocalAudioOutputPlayer | None = None

    @property
    def url(self) -> str:
        return f"http://{self.settings.webui_host}:{self.settings.webui_port}/"

    async def start(self) -> None:
        if self.settings.audio_playback_enabled:
            self._player = LocalAudioOutputPlayer(
                output_device=self.settings.audio_output_device,
                volume=self.settings.audio_output_volume,
                logger=self.logger,
            )
        self._ensure_queue_worker()
        if self.settings.webui_enabled:
            try:
                await self._start_webui()
            except Exception as exc:
                self._log_warning(f"Soundboard WebUI failed to start; audio runtime will stay available: {exc}")

    async def stop(self) -> None:
        queue_worker = self._queue_worker_task
        self._queue_worker_task = None
        if queue_worker is not None:
            queue_worker.cancel()
            await asyncio.gather(queue_worker, return_exceptions=True)
        self._trigger_queue = asyncio.Queue()
        for task in tuple(self._audio_tasks):
            task.cancel()
        if self._audio_tasks:
            await asyncio.gather(*self._audio_tasks, return_exceptions=True)
        self._audio_tasks.clear()
        if self._player is not None:
            await self._player.stop()
        self._player = None
        for socket in tuple(self._websockets):
            with contextlib.suppress(Exception):
                await socket.close()
        self._websockets.clear()
        runner = self._runner
        self._runner = None
        self._site = None
        if runner is not None:
            await runner.cleanup()

    async def trigger(
        self,
        cue_name: str,
        *,
        reason: str = "",
        source_text: str = "",
        triggered_by: str = "tool",
        repeat_count: int = 1,
    ) -> dict[str, Any]:
        resolved = resolve_soundboard_cue(
            self.settings,
            cue_name,
            plugin_dir=self.plugin_dir,
            available_only=True,
        )
        if resolved is None:
            return {
                "success": False,
                "error": f"unknown sound cue: {cue_name}",
                "available_cues": list_soundboard_cues(
                    self.settings,
                    plugin_dir=self.plugin_dir,
                    available_only=True,
                ),
            }
        resolved = await self._ensure_resolved_audio_path(resolved)
        return await self._trigger_resolved(
            resolved,
            reason=reason,
            source_text=source_text,
            triggered_by=triggered_by,
            repeat_count=repeat_count,
            respect_cooldown=False,
        )

    async def trigger_for_text(
        self,
        text: str,
        *,
        reason: str = "keyword",
        triggered_by: str = "keyword",
    ) -> dict[str, Any] | None:
        if not self.settings.keyword_triggers_enabled:
            return None
        resolved = match_soundboard_keyword(
            self.settings,
            text,
            plugin_dir=self.plugin_dir,
            available_only=True,
        )
        if resolved is None:
            return None
        resolved = await self._ensure_resolved_audio_path(resolved)
        return await self._trigger_resolved(
            resolved,
            reason=reason,
            source_text=text,
            triggered_by=triggered_by,
            respect_cooldown=True,
        )

    async def _start_webui(self) -> None:
        if web is None:
            self._log_warning("Soundboard WebUI disabled because aiohttp is unavailable")
            return
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/soundboard.css", self._handle_static)
        app.router.add_get("/soundboard.js", self._handle_static)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/audio/{cue_id}", self._handle_audio)
        app.router.add_get("/media/{cue_id}", self._handle_media)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.settings.webui_host, self.settings.webui_port)
        await site.start()
        self._runner = runner
        self._site = site
        self._log_info(f"Soundboard green-screen WebUI started at {self.url}")

    async def _trigger_resolved(
        self,
        resolved: SoundboardResolvedCue,
        *,
        reason: str,
        source_text: str,
        triggered_by: str,
        repeat_count: int = 1,
        respect_cooldown: bool,
    ) -> dict[str, Any]:
        cue = resolved.cue
        normalized_repeat_count = _normalize_repeat_count(repeat_count)
        if respect_cooldown:
            cooldown_result = self._check_and_mark_cooldown(resolved)
            if cooldown_result is not None:
                return cooldown_result
        queue_position = self._enqueue_trigger(
            QueuedSoundboardTrigger(
                resolved=resolved,
                reason=reason,
                source_text=source_text,
                triggered_by=triggered_by,
                repeat_count=normalized_repeat_count,
            )
        )
        return {
            "success": True,
            "queued": True,
            "queue_position": queue_position,
            "cue_id": resolved.cue_id,
            "label": cue.label or resolved.cue_id,
            "repeat_count": normalized_repeat_count,
            "webui_url": self.url if self.settings.webui_enabled else "",
        }

    def _check_and_mark_cooldown(self, resolved: SoundboardResolvedCue) -> dict[str, Any] | None:
        cue = resolved.cue
        now = time.monotonic()
        global_remaining = self.settings.global_cooldown_sec - (now - self._last_global_triggered_at)
        cue_remaining = cue.cooldown_sec - (now - self._last_triggered_at.get(resolved.cue_id, 0.0))
        if global_remaining > 0 or cue_remaining > 0:
            return {
                "success": False,
                "skipped": True,
                "reason": "cooldown",
                "cue_id": resolved.cue_id,
                "cooldown_remaining_sec": round(max(global_remaining, cue_remaining), 3),
            }
        self._last_global_triggered_at = now
        self._last_triggered_at[resolved.cue_id] = now
        return None

    def _enqueue_trigger(self, queued_trigger: QueuedSoundboardTrigger) -> int:
        self._ensure_queue_worker()
        self._trigger_queue.put_nowait(queued_trigger)
        return self._trigger_queue.qsize()

    async def wait_until_idle(self, *, timeout_sec: float | None = None) -> bool:
        waiter = self._trigger_queue.join()
        if timeout_sec is None or timeout_sec <= 0:
            await waiter
            return True
        try:
            await asyncio.wait_for(waiter, timeout=timeout_sec)
        except asyncio.TimeoutError:
            return False
        return True

    def _ensure_queue_worker(self) -> None:
        existing_task = self._queue_worker_task
        if existing_task is not None and not existing_task.done():
            return
        task = asyncio.create_task(
            self._run_trigger_queue(),
            name="maibot_bilibili_live_adapter.soundboard_queue",
        )
        self._queue_worker_task = task

        def _on_done(done_task: asyncio.Task[None]) -> None:
            if self._queue_worker_task is done_task:
                self._queue_worker_task = None
            with contextlib.suppress(asyncio.CancelledError):
                exc = done_task.exception()
                if exc is not None:
                    self._log_warning(f"Soundboard queue worker failed: {exc}")

        task.add_done_callback(_on_done)

    async def _run_trigger_queue(self) -> None:
        while True:
            queued_trigger = await self._trigger_queue.get()
            try:
                await self._play_queued_trigger(queued_trigger)
            finally:
                self._trigger_queue.task_done()

    async def _play_queued_trigger(self, queued_trigger: QueuedSoundboardTrigger) -> None:
        resolved = queued_trigger.resolved
        self._mark_trigger_started(resolved.cue_id)
        payload = self._build_trigger_payload(
            resolved,
            reason=queued_trigger.reason,
            source_text=queued_trigger.source_text,
            triggered_by=queued_trigger.triggered_by,
            repeat_count=queued_trigger.repeat_count,
        )
        audio_task = None
        if not _soundboard_payload_prefers_browser_audio(self.settings, resolved.cue, payload):
            audio_task = self._start_audio_playback_task(resolved, repeat_count=queued_trigger.repeat_count)
        if not self._payload_has_playback_output(payload) and audio_task is None:
            self._log_warning(f"Soundboard skipped cue without playable assets: cue={resolved.cue_id}")
            return
        if resolved.cue.show_effect and self.settings.webui_enabled:
            await self._broadcast(payload)
        await self._wait_for_trigger_completion(
            resolved,
            payload=payload,
            audio_task=audio_task,
            repeat_count=queued_trigger.repeat_count,
        )

    def _mark_trigger_started(self, cue_id: str) -> None:
        now = time.monotonic()
        self._last_global_triggered_at = now
        self._last_triggered_at[cue_id] = now

    async def _wait_for_trigger_completion(
        self,
        resolved: SoundboardResolvedCue,
        *,
        payload: Mapping[str, Any],
        audio_task: asyncio.Task[bool] | None,
        repeat_count: int,
    ) -> None:
        wait_seconds = self._estimate_trigger_playback_seconds(resolved, repeat_count=repeat_count)
        if audio_task is not None:
            audio_started = False
            try:
                audio_started = bool(await audio_task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_warning(f"Soundboard queued audio playback failed: {exc}")
            if not audio_started and self._should_wait_for_browser_playback(payload) and wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            return
        if self._should_wait_for_browser_playback(payload) and wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

    def _estimate_trigger_playback_seconds(self, resolved: SoundboardResolvedCue, *, repeat_count: int) -> float:
        per_play_duration_ms = _read_wav_duration_ms(resolved.audio_path)
        if per_play_duration_ms <= 0:
            per_play_duration_ms = max(120, int(resolved.cue.duration_ms))
        total_duration_ms = max(120, per_play_duration_ms * _normalize_repeat_count(repeat_count))
        return total_duration_ms / 1000.0

    def _should_wait_for_browser_playback(self, payload: Mapping[str, Any]) -> bool:
        return bool(payload.get("media_url") or payload.get("audio_url"))

    def _payload_has_playback_output(self, payload: Mapping[str, Any]) -> bool:
        return bool(payload.get("media_url") or payload.get("audio_url"))

    async def _ensure_resolved_audio_path(self, resolved: SoundboardResolvedCue) -> SoundboardResolvedCue:
        audio_path = resolve_soundboard_cached_audio_path(self.settings, resolved.cue, plugin_dir=self.plugin_dir)
        if audio_path is not None and audio_path.exists():
            return SoundboardResolvedCue(
                cue=resolved.cue,
                cue_id=resolved.cue_id,
                audio_path=audio_path,
                media_path=resolved.media_path,
            )
        media_path = resolved.media_path
        if (
            not resolved.cue.play_audio
            or media_path is None
            or not media_path.exists()
            or detect_soundboard_media_kind(media_path) != "video"
        ):
            return resolved
        cached_audio_path = media_path.parent / "audio.wav"
        lock = self._audio_extract_locks.setdefault(resolved.cue_id, asyncio.Lock())
        async with lock:
            if cached_audio_path.exists():
                return SoundboardResolvedCue(
                    cue=resolved.cue,
                    cue_id=resolved.cue_id,
                    audio_path=cached_audio_path.resolve(),
                    media_path=media_path,
                )
            extracted_audio_path = await asyncio.to_thread(
                _extract_soundboard_video_audio,
                media_path,
                cached_audio_path,
            )
        if extracted_audio_path is None:
            self._log_warning(
                f"Soundboard video audio extraction failed for cue={resolved.cue_id} media={media_path}"
            )
            return resolved
        self._log_info(f"Soundboard extracted cue audio: cue={resolved.cue_id} audio={extracted_audio_path}")
        return SoundboardResolvedCue(
            cue=resolved.cue,
            cue_id=resolved.cue_id,
            audio_path=extracted_audio_path,
            media_path=media_path,
        )

    def _build_trigger_payload(
        self,
        resolved: SoundboardResolvedCue,
        *,
        reason: str,
        source_text: str,
        triggered_by: str,
        repeat_count: int = 1,
    ) -> dict[str, Any]:
        cue = resolved.cue
        normalized_repeat_count = _normalize_repeat_count(repeat_count)
        audio_url = ""
        media_url = ""
        media_kind = ""
        media_audio_enabled = False
        if (
            self.settings.browser_audio_enabled
            and cue.play_audio
            and resolved.audio_path is not None
            and resolved.audio_path.exists()
        ):
            audio_url = f"/audio/{resolved.cue_id}"
        if resolved.media_path is not None and resolved.media_path.exists():
            media_url = f"/media/{resolved.cue_id}"
            media_kind = detect_soundboard_media_kind(resolved.media_path)
            media_audio_enabled = not audio_url and cue_uses_media_audio(
                cue,
                media_path=resolved.media_path,
                browser_audio_enabled=self.settings.browser_audio_enabled,
            )
        return {
            "type": "soundboard.trigger",
            "cue_id": resolved.cue_id,
            "label": cue.label or resolved.cue_id,
            "usage_hint": cue.usage_hint,
            "duration_ms": max(120, int(cue.duration_ms)),
            "repeat_count": normalized_repeat_count,
            "volume": min(1.0, max(0.0, self.settings.browser_audio_volume * cue.volume)),
            "audio_url": audio_url,
            "media_url": media_url,
            "media_kind": media_kind,
            "media_audio_enabled": media_audio_enabled,
            "reason": str(reason or ""),
            "source_text": str(source_text or "")[:240],
            "triggered_by": str(triggered_by or ""),
            "created_at_ms": int(time.time() * 1000),
        }

    def _start_audio_playback(self, resolved: SoundboardResolvedCue, *, repeat_count: int = 1) -> bool:
        return self._start_audio_playback_task(resolved, repeat_count=repeat_count) is not None

    def _start_audio_playback_task(
        self,
        resolved: SoundboardResolvedCue,
        *,
        repeat_count: int = 1,
    ) -> asyncio.Task[bool] | None:
        cue = resolved.cue
        player = self._player
        if (
            player is None
            or not self.settings.audio_playback_enabled
            or not cue.play_audio
            or resolved.audio_path is None
            or not resolved.audio_path.exists()
        ):
            return None
        normalized_repeat_count = _normalize_repeat_count(repeat_count)
        task = asyncio.create_task(
            self._play_audio_repeated(player, resolved, repeat_count=normalized_repeat_count),
            name=f"maibot_bilibili_live_adapter.soundboard_audio.{resolved.cue_id}",
        )
        self._audio_tasks.add(task)

        def _on_done(done_task: asyncio.Task[bool]) -> None:
            self._audio_tasks.discard(done_task)
            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                self._log_warning(f"Soundboard audio playback failed: {exc}")

        task.add_done_callback(_on_done)
        return task

    async def _play_audio_repeated(
        self,
        player: LocalAudioOutputPlayer,
        resolved: SoundboardResolvedCue,
        *,
        repeat_count: int,
    ) -> bool:
        audio_path = resolved.audio_path
        if audio_path is None:
            return False
        cue = resolved.cue
        overall_success = False
        for _ in range(_normalize_repeat_count(repeat_count)):
            played = await player.play(
                str(audio_path),
                duration_ms=max(120, int(cue.duration_ms)),
                volume=min(2.0, max(0.0, self.settings.audio_output_volume * cue.volume)),
            )
            overall_success = played or overall_success
            if not played:
                break
        return overall_success

    async def _broadcast(self, payload: Mapping[str, Any]) -> None:
        if not self._websockets:
            return
        data = json.dumps(dict(payload), ensure_ascii=False)
        stale: list[Any] = []
        for socket in tuple(self._websockets):
            try:
                await socket.send_str(data)
            except Exception:
                stale.append(socket)
        for socket in stale:
            self._websockets.discard(socket)

    async def _handle_index(self, request: Any) -> Any:
        del request
        return await self._file_response(_webui_dir() / "index.html", content_type="text/html")

    async def _handle_static(self, request: Any) -> Any:
        filename = str(request.match_info.get("filename") or request.path.lstrip("/"))
        if filename not in {"soundboard.css", "soundboard.js"}:
            raise web.HTTPNotFound()
        return await self._file_response(_webui_dir() / filename)

    async def _handle_audio(self, request: Any) -> Any:
        cue_id = normalize_soundboard_cue_id(request.match_info.get("cue_id"))
        if not cue_id:
            raise web.HTTPNotFound()
        path = self._resolve_asset_path(cue_id, asset_kind="audio")
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def _handle_media(self, request: Any) -> Any:
        cue_id = normalize_soundboard_cue_id(request.match_info.get("cue_id"))
        if not cue_id:
            raise web.HTTPNotFound()
        path = self._resolve_asset_path(cue_id, asset_kind="media")
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def _handle_ws(self, request: Any) -> Any:
        socket = web.WebSocketResponse(heartbeat=20)
        await socket.prepare(request)
        self._websockets.add(socket)
        await socket.send_str(
            json.dumps(
                {
                    "type": "soundboard.hello",
                    "cues": list_soundboard_cues(
                        self.settings,
                        plugin_dir=self.plugin_dir,
                        available_only=True,
                    ),
                    "browser_audio_enabled": bool(self.settings.browser_audio_enabled),
                },
                ensure_ascii=False,
            )
        )
        try:
            async for message in socket:
                if WSMsgType is not None and message.type == WSMsgType.ERROR:
                    break
        finally:
            self._websockets.discard(socket)
        return socket

    async def _file_response(self, path: Path, *, content_type: str | None = None) -> Any:
        if not path.exists():
            raise web.HTTPNotFound()
        guessed_type = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return web.FileResponse(path, headers={"Content-Type": guessed_type, "Cache-Control": "no-store"})

    def _resolve_asset_path(self, cue_id: str, *, asset_kind: str) -> Path | None:
        for index, cue in enumerate(self.settings.cues):
            if not cue.enabled:
                continue
            if not soundboard_cue_has_available_assets(self.settings, cue, plugin_dir=self.plugin_dir):
                continue
            resolved_id = normalize_soundboard_cue_id(cue.id, fallback=cue.label or f"cue_{index + 1}")
            if not resolved_id:
                resolved_id = f"cue_{index + 1}"
            if resolved_id != cue_id:
                continue
            if asset_kind == "media":
                path = resolve_soundboard_media_path(self.settings, cue, plugin_dir=self.plugin_dir)
            else:
                path = resolve_soundboard_cached_audio_path(self.settings, cue, plugin_dir=self.plugin_dir)
            if path is None or not path.exists() or not path.is_file():
                return None
            return path
        return None

    def _log_info(self, message: str) -> None:
        if self.logger is not None:
            self.logger.info(message)

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)


def _keyword_matches(text: str, keyword: str, *, match_mode: str) -> bool:
    normalized_keyword = str(keyword or "").strip()
    if not normalized_keyword:
        return False
    normalized_mode = str(match_mode or "contains").strip().lower()
    if normalized_mode == "exact":
        return text.casefold() == normalized_keyword.casefold()
    if normalized_mode == "regex":
        try:
            return re.search(normalized_keyword, text, flags=re.IGNORECASE) is not None
        except re.error:
            return False
    if _looks_like_ascii_wordish_keyword(normalized_keyword):
        return _ascii_keyword_matches_with_boundaries(text, normalized_keyword)
    return _cjk_keyword_matches(text, normalized_keyword)


_CJK_RANGES: list[tuple[int, int]] = [
    (0x3400, 0x4DBF),   # CJK Extension A
    (0x4E00, 0x9FFF),   # CJK Unified
    (0xF900, 0xFAFF),   # CJK Compatibility
    (0x20000, 0x2A6DF), # CJK Extension B
    (0x2A700, 0x2B73F), # CJK Extension C
    (0x2F800, 0x2FA1F), # CJK Compatibility Supplement
]


def _is_cjk(char: str) -> bool:
    cp = ord(char)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _cjk_keyword_matches(text: str, keyword: str) -> bool:
    """Match keyword against text with CJK word-boundary awareness.

    A keyword matches only when it appears as a contiguous substring
    that starts and ends at CJK/ASCII/non-word boundaries — i.e. the
    character immediately before the match (if any) and immediately
    after the match (if any) must not be of the same character class
    as the adjacent keyword character.

    This prevents "ah" from matching inside "BahaMen" and "笑" from
    matching isolated inside "可笑吗" where the surrounding characters
    are also CJK (allowed — contiguous CJK substring is fine).
    """
    if not keyword or not text:
        return False
    keyword_lower = keyword.casefold()
    text_lower = text.casefold()
    start = 0
    while True:
        pos = text_lower.find(keyword_lower, start)
        if pos == -1:
            return False
        start = pos + 1
        if pos > 0:
            prev_char = text[pos - 1]
            kw_first = keyword[0]
            if _is_cjk(prev_char) and _is_cjk(kw_first):
                pass
            elif prev_char.isalnum() and kw_first.isalnum():
                pass
            elif not prev_char.isalnum() and not kw_first.isalnum():
                pass
            else:
                continue
        if pos + len(keyword) < len(text):
            next_char = text[pos + len(keyword)]
            kw_last = keyword[-1]
            if _is_cjk(next_char) and _is_cjk(kw_last):
                pass
            elif next_char.isalnum() and kw_last.isalnum():
                pass
            elif not next_char.isalnum() and not kw_last.isalnum():
                pass
            else:
                continue
        return True


def _soundboard_overlap_score(
    candidate_text: str,
    reference_text: str,
) -> float:
    """Compute term-overlap score between two text snippets.

    Splits both texts into normalized CJK bigrams and ASCII word
    tokens, then returns the Jaccard-like ratio of shared tokens.
    """
    candidate_tokens = _soundboard_text_tokens(candidate_text)
    reference_tokens = _soundboard_text_tokens(reference_text)
    if not candidate_tokens or not reference_tokens:
        return 0.0
    intersection = candidate_tokens & reference_tokens
    return len(intersection) / max(1, min(len(candidate_tokens), len(reference_tokens)))


def _soundboard_text_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    if not text:
        return tokens
    normalized = str(text).strip().casefold()
    for match in re.finditer(r"[A-Za-z0-9]+", normalized):
        word = match.group()
        if len(word) >= 2:
            tokens.add(word)
    cjk_run: list[str] = []
    for ch in normalized:
        if _is_cjk(ch):
            cjk_run.append(ch)
        else:
            if cjk_run:
                for i in range(len(cjk_run) - 1):
                    tokens.add(cjk_run[i] + cjk_run[i + 1])
                cjk_run.clear()
    if cjk_run:
        for i in range(len(cjk_run) - 1):
            tokens.add(cjk_run[i] + cjk_run[i + 1])
    return tokens


def _find_explicit_cue_mention(text: str, *, cue_id: str, cue_label: str) -> bool:
    """Check whether text explicitly mentions a cue by its id or label."""
    normalized = str(text or "").strip().casefold()
    if not normalized:
        return False
    cue_id_lower = str(cue_id or "").strip().casefold()
    cue_label_lower = str(cue_label or "").strip().casefold()
    if cue_id_lower and cue_id_lower in normalized:
        return True
    if cue_label_lower and cue_label_lower in normalized:
        return True
    return False


def _looks_like_ascii_wordish_keyword(keyword: str) -> bool:
    normalized_keyword = str(keyword or "").strip()
    if not normalized_keyword:
        return False
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]*", normalized_keyword) is not None


def _ascii_keyword_matches_with_boundaries(text: str, keyword: str) -> bool:
    parts = re.findall(r"[A-Za-z0-9]+", str(keyword or "").strip())
    if not parts:
        return False
    pattern = r"(?<![A-Za-z0-9])" + r"[\s_-]+".join(re.escape(part) for part in parts) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _soundboard_payload_prefers_browser_audio(
    settings: SoundboardConfig,
    cue: SoundboardCueConfig,
    payload: Mapping[str, Any],
) -> bool:
    if not settings.webui_enabled or not cue.show_effect:
        return False
    return bool(payload.get("audio_url") or payload.get("media_audio_enabled"))


def _normalize_repeat_count(value: Any) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _read_wav_duration_ms(audio_path: Path | None) -> int:
    if audio_path is None or not audio_path.exists() or audio_path.suffix.lower() != ".wav":
        return 0
    try:
        with wave.open(str(audio_path), "rb") as wav_file:
            frame_count = int(wav_file.getnframes())
            sample_rate = int(wav_file.getframerate())
    except Exception:
        return 0
    if frame_count <= 0 or sample_rate <= 0:
        return 0
    return max(120, int((frame_count / sample_rate) * 1000))


def _extract_soundboard_video_audio(media_path: Path, audio_path: Path) -> Path | None:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return None
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_path,
        "-y",
        "-i",
        str(media_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(audio_path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not audio_path.exists():
        with contextlib.suppress(FileNotFoundError):
            audio_path.unlink()
        return None
    return audio_path.resolve()


def _webui_dir() -> Path:
    return Path(__file__).resolve().parent / "webui" / "soundboard"

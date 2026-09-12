"""Background queue for RVC song requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncio
import contextlib
import shutil
from uuid import uuid4

from .config import SongRequestConfig
from .music_source_provider import LegacyNeteaseMusicSourceProvider, MusicSourceProvider, SongCandidate
from .rvc_song_pipeline import RvcSongPipeline, RvcSongPipelineResult
from .song_request_console import SongRequestConsoleSession
from .tts_provider import SynthesizedSpeech

BuildSpeechCallback = Callable[[Path, str], SynthesizedSpeech]
ReadyRenderCallback = Callable[..., Any | Awaitable[Any]]
SongRenderCallback = Callable[..., Any | Awaitable[Any]]


@dataclass(frozen=True)
class QueuedSongRequest:
    """A song request accepted from MaiBot."""

    request_id: str
    song_keyword: str
    stream_id: str = ""
    requester: str = ""
    artist: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class RvcSongRequestService:
    """Queue and process RVC song requests without blocking MaiBot tool calls."""

    def __init__(
        self,
        *,
        settings: SongRequestConfig,
        pipeline: RvcSongPipeline | Any,
        music_source_provider: MusicSourceProvider | Any | None = None,
        netease_client: Any | None = None,
        build_speech: BuildSpeechCallback,
        render_ready_reply: ReadyRenderCallback,
        render_song_reply: SongRenderCallback,
        logger: Any = None,
        console_session: SongRequestConsoleSession | None = None,
    ) -> None:
        self.settings = settings
        if music_source_provider is None:
            if netease_client is None:
                raise ValueError("music_source_provider is required")
            music_source_provider = LegacyNeteaseMusicSourceProvider(netease_client)
        self.music_source_provider = music_source_provider
        self.netease_client = netease_client or getattr(music_source_provider, "client", None)
        self.pipeline = pipeline
        self.build_speech = build_speech
        self.render_ready_reply = render_ready_reply
        self.render_song_reply = render_song_reply
        self.logger = logger
        self.console_session = console_session
        self._queue: asyncio.Queue[QueuedSongRequest] = asyncio.Queue(maxsize=max(1, settings.max_queue_size))
        self._worker_task: asyncio.Task[None] | None = None
        self._command_task: asyncio.Task[None] | None = None
        self._current_request: QueuedSongRequest | None = None
        self._current_song_title = ""
        self._last_error = ""
        self._playback_active = False
        self._stopping = False

    @property
    def is_playback_active(self) -> bool:
        return self._playback_active

    @property
    def pending_count(self) -> int:
        return self._queue.qsize() + (1 if self._current_request is not None else 0)

    async def start(self) -> None:
        if not self.settings.is_available():
            return
        self._stopping = False
        self._last_error = ""
        self._current_song_title = ""
        self._update_console_state()
        if hasattr(self.music_source_provider, "start"):
            await self.music_source_provider.start()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker_loop(),
                name="maibot_bilibili_live_adapter.rvc_song_request",
            )
        if self.console_session is not None and (self._command_task is None or self._command_task.done()):
            self._command_task = asyncio.create_task(
                self._command_loop(),
                name="maibot_bilibili_live_adapter.rvc_song_request_console",
            )
        self._log_info("RVC song request service started")

    async def stop(self) -> None:
        self._stopping = True
        command_task = self._command_task
        self._command_task = None
        if command_task is not None:
            command_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await command_task
        worker_task = self._worker_task
        self._worker_task = None
        if worker_task is not None:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        if hasattr(self.music_source_provider, "stop"):
            with contextlib.suppress(Exception):
                await self.music_source_provider.stop()
        self._current_request = None
        self._current_song_title = ""
        self._last_error = ""
        self._playback_active = False
        self._update_console_state()
        self._log_info("RVC song request service stopped")

    async def submit(
        self,
        *,
        song_keyword: str,
        stream_id: str = "",
        requester: str = "",
        artist: str = "",
    ) -> dict[str, Any]:
        normalized_keyword = str(song_keyword or "").strip()
        if self.settings.hard_disable:
            return _tool_failure("\u70b9\u6b4c\u548cRVC\u529f\u80fd\u5f53\u524d\u88ab\u603b\u5f00\u5173\u7981\u7528\u4e86\u3002")
        if not self.settings.enabled:
            return _tool_failure("\u70b9\u6b4c\u529f\u80fd\u8fd8\u6ca1\u6709\u5f00\u542f\u3002")
        if not normalized_keyword:
            return _tool_failure("\u8bf7\u5148\u544a\u8bc9\u6211\u8981\u70b9\u54ea\u9996\u6b4c\u3002")
        if self._queue.full():
            return _tool_failure("\u70b9\u6b4c\u961f\u5217\u5df2\u7ecf\u6ee1\u4e86\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\u3002")

        request = QueuedSongRequest(
            request_id=uuid4().hex,
            song_keyword=normalized_keyword,
            stream_id=str(stream_id or "").strip(),
            requester=str(requester or "").strip(),
            artist=str(artist or "").strip(),
            metadata={
                "stream_id": str(stream_id or "").strip(),
                "requester": str(requester or "").strip(),
                "song_keyword": normalized_keyword,
            },
        )
        self._queue.put_nowait(request)
        self._update_console_state()
        prompt = _format_prompt(
            self.settings.wait_prompt_template,
            song_title=normalized_keyword,
            artist=request.artist,
            requester=request.requester,
        )
        return {
            "success": True,
            "queued": True,
            "request_id": request.request_id,
            "prompt": prompt,
            "message": prompt,
            "pending_count": self.pending_count,
        }

    async def wait_until_idle(self, *, timeout_sec: float = 30.0) -> None:
        deadline = asyncio.get_running_loop().time() + max(0.05, float(timeout_sec))
        while self.pending_count > 0:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("RVC song request service did not become idle")
            await asyncio.sleep(0.02)

    async def _worker_loop(self) -> None:
        while True:
            request = await self._queue.get()
            self._current_request = request
            self._current_song_title = ""
            self._last_error = ""
            self._update_console_state()
            try:
                await self._process_request(request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_warning(f"RVC song request failed unexpectedly: {exc}")
                self._last_error = str(exc)
                self._update_console_state()
                await self._render_failure(request, request.song_keyword)
            finally:
                self._playback_active = False
                self._current_request = None
                self._current_song_title = ""
                self._queue.task_done()
                self._update_console_state()

    async def _process_request(self, request: QueuedSongRequest) -> None:
        failure_title = request.song_keyword
        try:
            songs = await self.music_source_provider.search(request.song_keyword, artist_hint=request.artist)
            if not songs:
                await self._render_failure(request, failure_title)
                return
            song = songs[0]
            failure_title = song.title
            self._current_song_title = song.title
            self._last_error = ""
            self._update_console_state()
            if song.duration_ms > int(self.settings.max_song_duration_sec) * 1000:
                self._last_error = f"song too long: {song.title}"
                self._update_console_state()
                await self._render_failure(request, failure_title)
                return
            resolved_audio = await self.music_source_provider.resolve_audio(song)
            song_url = resolved_audio.audio_url
            if not song_url:
                self._last_error = f"song url unavailable: {song.title}"
                self._update_console_state()
                await self._render_failure(request, failure_title)
                return
        except Exception as exc:
            self._log_warning(f"Song source lookup failed: {exc}")
            self._last_error = str(exc)
            self._update_console_state()
            await self._render_failure(request, failure_title)
            return
        try:
            result = await self.pipeline.process(song=song, song_url=song_url, request_id=request.request_id)
        except Exception as exc:
            self._log_warning(f"RVC song pipeline failed: {exc}")
            self._last_error = str(exc)
            self._update_console_state()
            await self._render_failure(request, failure_title)
            return

        caption = _format_prompt(
            self.settings.subtitle_template,
            song_title=song.title,
            artist=song.artist_text,
            requester=request.requester,
        )
        speech = self.build_speech(Path(result.final_wav_path), caption)
        ready_text = _format_prompt(
            self.settings.ready_prompt_template,
            song_title=song.title,
            artist=song.artist_text,
            requester=request.requester,
        )
        metadata = _metadata_for(request, song=song, result=result)
        await _maybe_await(self.render_ready_reply(ready_text, metadata=metadata))
        self._playback_active = True
        self._last_error = ""
        self._update_console_state()
        self._log_info(f"RVC song playback started: {song.title}")
        try:
            await _maybe_await(self.render_song_reply(caption, speech, metadata=metadata))
        finally:
            self._playback_active = False
            self._update_console_state()
            self._log_info(f"RVC song playback finished: {song.title}")
            if self.settings.cleanup_successful_tasks:
                with contextlib.suppress(Exception):
                    shutil.rmtree(result.work_dir)

    async def _render_failure(self, request: QueuedSongRequest, song_title: str) -> None:
        text = _format_prompt(
            self.settings.failure_prompt_template,
            song_title=song_title,
            artist=request.artist,
            requester=request.requester,
        )
        self._current_song_title = str(song_title or "").strip()
        self._last_error = text
        self._update_console_state()
        await _maybe_await(
            self.render_ready_reply(
                text,
                metadata={
                    "stream_id": request.stream_id,
                    "requester": request.requester,
                    "song_keyword": request.song_keyword,
                    "rvc_song_failed": True,
                },
            )
        )

    async def _command_loop(self) -> None:
        assert self.console_session is not None
        while True:
            commands = self.console_session.consume_commands()
            for command in commands:
                await self._handle_console_command(command)
            await asyncio.sleep(0.05)

    async def _handle_console_command(self, payload: dict[str, Any]) -> None:
        command_name = str(payload.get("command") or "").strip().lower()
        source = str(payload.get("source") or "").strip()
        if not command_name:
            return
        if command_name == "login":
            login_with_qr = getattr(self.music_source_provider, "login_with_qr", None)
            if not callable(login_with_qr):
                message = "Song console login command ignored: current music source has no QR login entrypoint."
                self._last_error = message
                self._update_console_state()
                self._log_warning(message)
                return
            self._log_info(f"Song console requested manual QR login from {source or 'unknown source'}")
            try:
                await login_with_qr(reason="manual song console command")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                self._update_console_state()
                self._log_warning(f"Song console login command failed: {exc}")
            else:
                self._last_error = ""
                self._update_console_state()
                self._log_info("Song console login command completed")
            return
        self._log_warning(f"Song console command is not supported yet: {command_name}")

    def _update_console_state(self) -> None:
        console_session = self.console_session
        if console_session is None:
            return
        current_request = self._current_request
        console_session.update_state(
            {
                "service_active": bool(self.settings.enabled and not self._stopping),
                "pending_count": self.pending_count,
                "playback_active": self._playback_active,
                "current_request_id": current_request.request_id if current_request is not None else "",
                "current_song_title": self._current_song_title,
                "current_song_keyword": current_request.song_keyword if current_request is not None else "",
                "last_error": self._last_error,
            }
        )

    def _log_info(self, message: str) -> None:
        if self.logger is not None and hasattr(self.logger, "info"):
            self.logger.info(message)

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)


def _tool_failure(prompt: str) -> dict[str, Any]:
    return {"success": False, "queued": False, "prompt": prompt, "message": prompt}


def _format_prompt(template: str, *, song_title: str, artist: str = "", requester: str = "") -> str:
    variables = {
        "song_title": str(song_title or "").strip(),
        "artist": str(artist or "").strip(),
        "requester": str(requester or "").strip(),
    }
    try:
        return str(template or "").format_map(_SafePromptVars(variables))
    except Exception:
        return variables["song_title"]


def _metadata_for(
    request: QueuedSongRequest,
    *,
    song: SongCandidate,
    result: RvcSongPipelineResult,
) -> dict[str, Any]:
    return {
        "stream_id": request.stream_id,
        "requester": request.requester,
        "song_keyword": request.song_keyword,
        "song_id": song.song_id,
        "song_title": song.title,
        "artist": song.artist_text,
        "source_provider": song.source,
        "source_page_url": song.page_url,
        "request_id": result.request_id,
        "audio_ref": str(result.final_wav_path),
    }


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


class _SafePromptVars(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"

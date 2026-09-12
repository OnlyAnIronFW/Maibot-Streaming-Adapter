"""RVC song conversion pipeline for Bilibili live song requests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

import asyncio
import contextlib
import os
import re
import shutil
import time

from .config import SongRequestConfig
from .music_source_provider import SongCandidate

try:
    from aiohttp import ClientSession, ClientTimeout

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False


@dataclass(frozen=True)
class RvcSongPipelineResult:
    """Generated RVC song artifact."""

    request_id: str
    song: SongCandidate
    original_url: str
    final_wav_path: Path
    work_dir: Path


class RvcSongPipeline:
    """Download, separate, convert, and remix a song through external commands."""

    def __init__(self, settings: SongRequestConfig, *, logger: Any = None) -> None:
        self.settings = settings
        self.logger = logger

    async def process(self, *, song: SongCandidate, song_url: str, request_id: str) -> RvcSongPipelineResult:
        normalized_url = str(song_url or "").strip()
        if not normalized_url:
            raise ValueError("song_url is required")
        work_dir = self._build_work_dir(request_id)
        work_dir.mkdir(parents=True, exist_ok=True)

        source_path = work_dir / f"source{_extension_from_url(normalized_url)}"
        input_wav = work_dir / "source.wav"
        separation_dir = work_dir / "separated"
        rvc_vocals_wav = work_dir / "rvc_vocals.wav"
        final_wav = work_dir / "final.wav"

        await self._download_audio(normalized_url, source_path)
        await self._run_ffmpeg_convert(source_path, input_wav)
        duration_ms = await self._probe_duration_ms(input_wav)
        max_duration_ms = int(self.settings.max_song_duration_sec) * 1000
        if duration_ms > max_duration_ms:
            raise RuntimeError(
                f"song is too long: {duration_ms / 1000:.1f}s > {self.settings.max_song_duration_sec}s"
            )

        variables = self._template_variables(
            song=song,
            request_id=request_id,
            work_dir=work_dir,
            input_wav=input_wav,
            separation_dir=separation_dir,
            vocals_wav=work_dir / "vocals.wav",
            instrumental_wav=work_dir / "no_vocals.wav",
            rvc_vocals_wav=rvc_vocals_wav,
            final_wav=final_wav,
        )
        await self._run_template_command(self.settings.separation_command_template, variables)
        variables["stem_name"] = input_wav.stem
        vocals_wav = Path(_format_template(self.settings.separated_vocals_path_template, variables))
        instrumental_wav = Path(_format_template(self.settings.separated_instrumental_path_template, variables))
        if not vocals_wav.exists():
            raise FileNotFoundError(f"separated vocals wav not found: {vocals_wav}")
        if not instrumental_wav.exists():
            raise FileNotFoundError(f"separated instrumental wav not found: {instrumental_wav}")

        variables.update(
            {
                "vocals_wav": str(vocals_wav),
                "instrumental_wav": str(instrumental_wav),
                "rvc_vocals_wav": str(rvc_vocals_wav),
            }
        )
        await self._run_template_command(self.settings.rvc_command_template, variables)
        if not rvc_vocals_wav.exists():
            raise FileNotFoundError(f"RVC vocals wav not found: {rvc_vocals_wav}")

        await self._run_mix(rvc_vocals_wav, instrumental_wav, final_wav)
        if not final_wav.exists():
            raise FileNotFoundError(f"final mixed wav not found: {final_wav}")
        return RvcSongPipelineResult(
            request_id=request_id,
            song=song,
            original_url=normalized_url,
            final_wav_path=final_wav,
            work_dir=work_dir,
        )

    def _build_work_dir(self, request_id: str) -> Path:
        root = Path(str(self.settings.work_dir or "").strip()).expanduser() if self.settings.work_dir else _plugin_data_dir() / "rvc_song_requests"
        safe_request_id = _safe_filename(request_id or str(int(time.time() * 1000)))
        return root.resolve() / safe_request_id

    async def _download_audio(self, url: str, output_path: Path) -> None:
        local_path = _local_path_from_audio_ref(url)
        if local_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local_path, output_path)
            return
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError("aiohttp is required to download song audio")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        timeout = ClientTimeout(total=self.settings.request_timeout_sec, connect=self.settings.connect_timeout_sec)
        headers = {"User-Agent": self.settings.download_user_agent}
        async with ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as response:
                if response.status >= 400:
                    raise RuntimeError(f"song download returned {response.status}")
                with output_path.open("wb") as file:
                    async for chunk in response.content.iter_chunked(1024 * 256):
                        if chunk:
                            file.write(chunk)

    async def _run_ffmpeg_convert(self, input_path: Path, output_path: Path) -> None:
        command = (
            f'{_quote_command(self.settings.ffmpeg_command)} -y -i "{_escape_path(input_path)}" '
            f'-vn -ac 2 -ar 44100 "{_escape_path(output_path)}"'
        )
        await self._run_shell(command)

    async def _probe_duration_ms(self, input_path: Path) -> int:
        command = (
            f'{_quote_command(self.settings.ffprobe_command)} -v error -show_entries format=duration '
            f'-of default=noprint_wrappers=1:nokey=1 "{_escape_path(input_path)}"'
        )
        result = await self._run_shell(command, check=False)
        if result.returncode != 0:
            return 0
        try:
            return int(float(result.stdout.strip()) * 1000)
        except ValueError:
            return 0

    async def _run_mix(self, rvc_vocals_wav: Path, instrumental_wav: Path, final_wav: Path) -> None:
        command = (
            f'{_quote_command(self.settings.ffmpeg_command)} -y '
            f'-i "{_escape_path(rvc_vocals_wav)}" -i "{_escape_path(instrumental_wav)}" '
            '-filter_complex "[0:a]volume=0.80[a0];[1:a]volume=0.85[a1];'
            '[a0][a1]amix=inputs=2:duration=longest:normalize=0,'
            'alimiter=limit=0.95:level=disabled" '
            f'-ac 2 -ar 44100 "{_escape_path(final_wav)}"'
        )
        await self._run_shell(command)

    async def _run_template_command(self, command_template: str, variables: Mapping[str, Any]) -> None:
        command = _format_template(command_template, variables)
        if not command.strip():
            raise ValueError("command template is empty")
        await self._run_shell(command)

    async def _run_shell(self, command: str, *, check: bool = True) -> "_CommandResult":
        self._log_debug(f"Running song command: {_redact_command(command)}")
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=max(1.0, float(self.settings.command_timeout_sec)),
            )
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise RuntimeError(f"song command timed out after {self.settings.command_timeout_sec}s") from exc
        result = _CommandResult(
            returncode=int(process.returncode or 0),
            stdout=stdout.decode("utf-8", errors="ignore"),
            stderr=stderr.decode("utf-8", errors="ignore"),
        )
        if check and result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(f"song command failed with exit {result.returncode}: {message}")
        return result

    def _template_variables(
        self,
        *,
        song: SongCandidate,
        request_id: str,
        work_dir: Path,
        input_wav: Path,
        separation_dir: Path,
        vocals_wav: Path,
        instrumental_wav: Path,
        rvc_vocals_wav: Path,
        final_wav: Path,
    ) -> dict[str, str | int]:
        return {
            "request_id": request_id,
            "song_id": song.song_id,
            "song_title": song.title,
            "artist": song.artist_text,
            "plugin_dir": str(Path(__file__).resolve().parent),
            "plugin_data_dir": str(_plugin_data_dir()),
            "work_dir": str(work_dir),
            "input_wav": str(input_wav),
            "separation_dir": str(separation_dir),
            "stem_name": input_wav.stem,
            "vocals_wav": str(vocals_wav),
            "instrumental_wav": str(instrumental_wav),
            "rvc_vocals_wav": str(rvc_vocals_wav),
            "final_wav": str(final_wav),
            "rvc_model_path": self.settings.rvc_model_path,
            "rvc_index_path": self.settings.rvc_index_path,
            "rvc_pitch": self.settings.rvc_pitch,
        }

    def _log_debug(self, message: str) -> None:
        if self.logger is not None:
            self.logger.debug(message)


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str


class _SafeTemplateVars(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _format_template(template: str, variables: Mapping[str, Any]) -> str:
    return str(template or "").format_map(_SafeTemplateVars({key: str(value) for key, value in variables.items()}))


def _extension_from_url(url: str) -> str:
    local_path = _local_path_from_audio_ref(url)
    if local_path is not None:
        suffix = local_path.suffix.lower()
        if suffix:
            return suffix
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in {".mp3", ".m4a", ".flac", ".wav", ".aac", ".ogg", ".webm"}:
        return suffix
    return ".audio"


def _local_path_from_audio_ref(value: str) -> Path | None:
    normalized = str(value or "").strip().strip('"')
    if not normalized:
        return None
    raw_path = Path(normalized).expanduser()
    if raw_path.exists() and raw_path.is_file():
        return raw_path.resolve()
    parsed = urlparse(normalized)
    if parsed.scheme != "file":
        return None
    if os.name == "nt":
        path_text = unquote(parsed.path.lstrip("/"))
    else:
        path_text = unquote(parsed.path)
    path = Path(path_text).expanduser()
    if path.exists() and path.is_file():
        return path.resolve()
    return None


def _safe_filename(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return normalized.strip("._") or "song"


def _quote_command(command: str) -> str:
    normalized = str(command or "").strip()
    if not normalized:
        return normalized
    if os.path.sep in normalized or (os.path.altsep and os.path.altsep in normalized):
        return f'"{_escape_path(Path(normalized))}"'
    return normalized


def _escape_path(path: Path) -> str:
    return str(path).replace('"', '\\"')


def _redact_command(command: str) -> str:
    return re.sub(r"(?i)(cookie|token|secret|key)=\S+", r"\1=<redacted>", command)


def _plugin_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"

"""Manual command-line entrypoint for RVC song request testing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import argparse
import asyncio
import contextlib
import sys
import time

from .audio_output import LocalAudioOutputPlayer
from .config import LiveAdapterSettings, SongRequestConfig
from .music_source_provider import (
    LegacyNeteaseMusicSourceProvider,
    SongCandidate,
    build_music_source_provider,
    normalize_source_provider_name,
)
from .netease_client import NeteaseCloudMusicClient
from .rvc_song_pipeline import RvcSongPipeline
from .song_request_console import _extract_qr_url, _print_qr_banner

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

try:
    import tomlkit
except ImportError:  # pragma: no cover - project dependency in normal runtime
    tomlkit = None  # type: ignore[assignment]


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"


@dataclass(frozen=True)
class ManualRvcSongOptions:
    """Options for a manual RVC song request run."""

    song_keyword: str = ""
    artist: str = ""
    provider: str = "audius"
    request_id: str = ""
    config_path: Path = DEFAULT_CONFIG_PATH
    lookup_only: bool = False
    login_only: bool = False
    clear_token: bool = False
    force_login: bool = False
    respect_disable: bool = False
    play: bool = False
    verbose: bool = False


@dataclass(frozen=True)
class ManualRvcSongResult:
    """Result from a manual RVC song request run."""

    request_id: str
    song: SongCandidate | None
    song_url: str
    caption_text: str
    final_wav_path: Path | None = None
    lookup_only: bool = False
    login_only: bool = False


def parse_manual_args(argv: list[str] | None = None) -> ManualRvcSongOptions:
    parser = argparse.ArgumentParser(
        description="Manually test song lookup and RVC conversion without starting the live adapter.",
    )
    parser.add_argument("song_keyword", nargs="?", default="", help="Song name or NetEase song URL/id.")
    parser.add_argument("--song", dest="song_keyword_option", default="", help="Song name or NetEase song URL/id.")
    parser.add_argument("--artist", default="", help="Optional artist hint for search ranking.")
    parser.add_argument(
        "--provider",
        default="audius",
        choices=["audius", "netease", "direct", "local"],
        help="Music source provider. Defaults to audius.",
    )
    parser.add_argument("--request-id", default="", help="Stable request id used for the working directory.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the live adapter config.toml.")
    parser.add_argument("--lookup-only", action="store_true", help="Resolve song metadata and playable URL only.")
    parser.add_argument("--login-only", action="store_true", help="Run NetEase QR login and exit.")
    parser.add_argument("--clear-token", action="store_true", help="Delete the cached NetEase access token before running.")
    parser.add_argument("--force-login", action="store_true", help="Force a fresh NetEase QR login before lookup/RVC.")
    parser.add_argument(
        "--respect-disable",
        action="store_true",
        help="Honor song_request.enabled and song_request.hard_disable. By default manual tests ignore them.",
    )
    parser.add_argument("--play", action="store_true", help="Play the generated WAV through the configured TTS output device.")
    parser.add_argument("--verbose", action="store_true", help="Print debug-level command logs.")
    raw_argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(list(raw_argv))
    return ManualRvcSongOptions(
        song_keyword=str(args.song_keyword_option or args.song_keyword or "").strip(),
        artist=str(args.artist or "").strip(),
        provider=normalize_source_provider_name(str(args.provider or "audius")),
        request_id=str(args.request_id or "").strip(),
        config_path=Path(str(args.config or DEFAULT_CONFIG_PATH)).expanduser(),
        lookup_only=bool(args.lookup_only),
        login_only=bool(args.login_only),
        clear_token=bool(args.clear_token),
        force_login=bool(args.force_login),
        respect_disable=bool(args.respect_disable),
        play=bool(args.play),
        verbose=bool(args.verbose),
    )


def load_live_adapter_settings(config_path: Path | str = DEFAULT_CONFIG_PATH) -> LiveAdapterSettings:
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    if tomllib is not None:
        with path.open("rb") as file:
            payload = tomllib.load(file)
    else:
        if tomlkit is None:
            raise RuntimeError("tomllib or tomlkit is required to read config.toml")
        payload = tomlkit.parse(path.read_text(encoding="utf-8"))
    return LiveAdapterSettings.model_validate(payload)


async def run_manual_rvc_song_request(
    settings: SongRequestConfig,
    options: ManualRvcSongOptions,
    *,
    music_source_provider: Any = None,
    netease_client: Any = None,
    pipeline: Any = None,
    logger: Any = None,
) -> ManualRvcSongResult:
    """Run a manual song lookup/RVC conversion outside the live adapter."""

    if options.respect_disable and not settings.is_available():
        raise RuntimeError("song request and RVC are disabled by configuration")

    request_id = _manual_request_id(options)
    provider = music_source_provider
    if provider is None and netease_client is not None:
        provider = LegacyNeteaseMusicSourceProvider(netease_client)
    if provider is None:
        provider = build_music_source_provider(settings, provider_name=options.provider, logger=logger)
    await _maybe_start(provider)
    try:
        if options.clear_token or options.force_login:
            clear_cached_access_token = getattr(provider, "clear_cached_access_token", None)
            if callable(clear_cached_access_token):
                clear_cached_access_token()
        if options.login_only:
            login_with_qr = getattr(provider, "login_with_qr", None)
            if not callable(login_with_qr):
                raise RuntimeError("current music source provider does not support QR login")
            await login_with_qr(reason="manual RVC song CLI", force=True)
            return ManualRvcSongResult(
                request_id=request_id,
                song=None,
                song_url="",
                caption_text="",
                login_only=True,
            )

        if not options.song_keyword:
            raise ValueError("song keyword is required unless --login-only is used")
        if options.force_login:
            login_with_qr = getattr(provider, "login_with_qr", None)
            if not callable(login_with_qr):
                raise RuntimeError("current music source provider does not support QR login")
            await login_with_qr(reason="manual RVC song CLI", force=True)
        song = await _resolve_song(provider, options.song_keyword, artist_hint=options.artist)
        resolved_audio = await provider.resolve_audio(song)
        song_url = resolved_audio.audio_url
        if not song_url:
            raise RuntimeError(f"{options.provider} did not return a playable URL for: {song.title}")
        caption_text = _format_prompt(
            settings.subtitle_template,
            song_title=song.name,
            artist=song.artist_text,
            requester="manual",
        )
        if options.lookup_only:
            return ManualRvcSongResult(
                request_id=request_id,
                song=song,
                song_url=song_url,
                caption_text=caption_text,
                lookup_only=True,
            )

        active_pipeline = pipeline or RvcSongPipeline(settings, logger=logger)
        pipeline_result = await active_pipeline.process(song=song, song_url=song_url, request_id=request_id)
        return ManualRvcSongResult(
            request_id=request_id,
            song=song,
            song_url=song_url,
            caption_text=caption_text,
            final_wav_path=Path(pipeline_result.final_wav_path),
        )
    finally:
        await _maybe_stop(provider)


def build_netease_client(settings: SongRequestConfig, *, logger: Any = None) -> NeteaseCloudMusicClient:
    return NeteaseCloudMusicClient(
        base_url=settings.netease_api_base_url,
        app_id=settings.netease_app_id,
        app_secret=settings.netease_app_secret,
        public_key=settings.netease_public_key,
        private_key=settings.netease_private_key,
        access_token=settings.netease_access_token,
        token_cache_path=settings.netease_token_cache_path,
        device={
            "deviceId": settings.netease_device_id,
            "deviceType": settings.netease_device_type,
            "os": settings.netease_os,
            "appVer": settings.netease_app_ver,
            "channel": settings.netease_channel,
            "brand": settings.netease_brand,
            "model": settings.netease_model,
            "osVer": settings.netease_os_ver,
            "clientIp": settings.netease_client_ip,
            "flowFlag": settings.netease_flow_flag,
        },
        connect_timeout_sec=settings.connect_timeout_sec,
        request_timeout_sec=settings.request_timeout_sec,
        search_limit=settings.netease_search_limit,
        song_level=settings.netease_song_level,
        cookie=settings.netease_cookie,
        user_agent=settings.netease_user_agent,
        referer=settings.netease_referer,
        auto_qr_login_on_unauthorized=settings.netease_auto_qr_login_on_unauthorized,
        qr_login_timeout_sec=settings.netease_qr_login_timeout_sec,
        qr_login_poll_interval_sec=settings.netease_qr_poll_interval_sec,
        logger=logger,
    )


class ManualRvcSongLogger:
    """Small stdout logger that also renders NetEase QR login links."""

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = bool(verbose)

    def debug(self, message: str) -> None:
        if self.verbose:
            self._print("DEBUG", message)

    def info(self, message: str) -> None:
        self._print("INFO", message)

    def warning(self, message: str) -> None:
        self._print("WARNING", message)

    def error(self, message: str) -> None:
        self._print("ERROR", message)

    def _print(self, level: str, message: str) -> None:
        _console_print(f"[{level}] {message}")
        qr_url = _extract_qr_url(message)
        if qr_url:
            _print_qr_banner(qr_url)


async def _async_main(options: ManualRvcSongOptions) -> int:
    settings = load_live_adapter_settings(options.config_path)
    logger = ManualRvcSongLogger(verbose=options.verbose)
    result = await run_manual_rvc_song_request(settings.song_request, options, logger=logger)
    _print_result(result)
    if options.play and result.final_wav_path is not None:
        player = LocalAudioOutputPlayer(
            output_device=settings.tts.audio_output_device,
            volume=settings.tts.audio_output_volume,
            logger=logger,
        )
        played = await player.play(str(result.final_wav_path))
        if not played:
            return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    options = parse_manual_args(argv)
    try:
        return asyncio.run(_async_main(options))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        _console_print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


async def _resolve_song(provider: Any, keyword: str, *, artist_hint: str = "") -> SongCandidate:
    songs = await provider.search(keyword, artist_hint=artist_hint)
    if not songs:
        raise RuntimeError(f"{getattr(provider, 'source', 'music source')} returned no songs for: {keyword}")
    return songs[0]


async def _maybe_start(client: Any) -> None:
    start = getattr(client, "start", None)
    if callable(start):
        await start()


async def _maybe_stop(client: Any) -> None:
    stop = getattr(client, "stop", None)
    if callable(stop):
        with contextlib.suppress(Exception):
            await stop()


def _manual_request_id(options: ManualRvcSongOptions) -> str:
    if options.request_id:
        return options.request_id
    return f"manual-{int(time.time())}"


def _print_result(result: ManualRvcSongResult) -> None:
    _console_print("")
    _console_print("Manual RVC song result:")
    _console_print(f"  request_id: {result.request_id}")
    _console_print(f"  login_only: {result.login_only}")
    _console_print(f"  lookup_only: {result.lookup_only}")
    if result.song is not None:
        _console_print(f"  song: {result.song.name}")
        _console_print(f"  artist: {result.song.artist_text}")
        _console_print(f"  song_id: {result.song.song_id}")
    if result.song_url:
        _console_print(f"  song_url: {result.song_url}")
    if result.caption_text:
        _console_print(f"  caption: {result.caption_text}")
    if result.final_wav_path is not None:
        _console_print(f"  final_wav: {result.final_wav_path}")


def _console_print(message: str = "", *, file: Any = None) -> None:
    output = sys.stdout if file is None else file
    print(_safe_console_text(message, encoding=getattr(output, "encoding", None)), file=output, flush=True)


def _safe_console_text(value: Any, *, encoding: str | None = None) -> str:
    text = str(value)
    target_encoding = encoding or "utf-8"
    return text.encode(target_encoding, errors="replace").decode(target_encoding, errors="replace")


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


class _SafePromptVars(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


if __name__ == "__main__":
    raise SystemExit(main())

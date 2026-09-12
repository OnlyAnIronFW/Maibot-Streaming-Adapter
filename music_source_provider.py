"""Common music source provider contracts for RVC song requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse


@dataclass(frozen=True)
class SongCandidate:
    """Normalized song metadata returned by any music source."""

    song_id: str
    title: str
    artist_text: str = ""
    duration_ms: int = 0
    page_url: str = ""
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.title


@dataclass(frozen=True)
class ResolvedSongAudio:
    """A candidate plus the concrete audio URL/path to feed into the RVC pipeline."""

    song: SongCandidate
    audio_url: str


class MusicSourceProvider(Protocol):
    """Search and resolve playable audio from one backend."""

    async def start(self) -> None:
        """Open any provider resources."""

    async def stop(self) -> None:
        """Close any provider resources."""

    async def search(self, keyword: str, *, artist_hint: str = "") -> list[SongCandidate]:
        """Return normalized song candidates for a user query."""

    async def resolve_audio(self, song: SongCandidate) -> ResolvedSongAudio:
        """Return the final audio URL/path for a selected candidate."""


class MusicSourceLookupError(RuntimeError):
    """Raised when a music source cannot return a usable song/audio URL."""


class DirectMusicSourceProvider:
    """Treat the query itself as a direct HTTP(S) audio URL."""

    source = "direct"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def search(self, keyword: str, *, artist_hint: str = "") -> list[SongCandidate]:
        del artist_hint
        normalized = str(keyword or "").strip()
        if not _is_http_url(normalized):
            return []
        return [
            SongCandidate(
                song_id=normalized,
                title=_title_from_url(normalized),
                page_url=normalized,
                source=self.source,
                metadata={"direct_url": normalized},
            )
        ]

    async def resolve_audio(self, song: SongCandidate) -> ResolvedSongAudio:
        url = str(song.metadata.get("direct_url") or song.song_id or "").strip()
        if not _is_http_url(url):
            raise MusicSourceLookupError("direct provider requires an http(s) audio URL")
        return ResolvedSongAudio(song=song, audio_url=url)


class LocalFileMusicSourceProvider:
    """Treat the query as a local audio file path."""

    source = "local"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def search(self, keyword: str, *, artist_hint: str = "") -> list[SongCandidate]:
        del artist_hint
        path = Path(str(keyword or "").strip().strip('"')).expanduser()
        if not path.exists() or not path.is_file():
            return []
        resolved = path.resolve()
        return [
            SongCandidate(
                song_id=str(resolved),
                title=resolved.stem,
                page_url=str(resolved),
                source=self.source,
                metadata={"local_path": str(resolved)},
            )
        ]

    async def resolve_audio(self, song: SongCandidate) -> ResolvedSongAudio:
        path = Path(str(song.metadata.get("local_path") or song.song_id or "").strip().strip('"')).expanduser()
        if not path.exists() or not path.is_file():
            raise MusicSourceLookupError(f"local audio file not found: {path}")
        return ResolvedSongAudio(song=song, audio_url=str(path.resolve()))


class LegacyNeteaseMusicSourceProvider:
    """Adapter that keeps the old NetEase client usable as an opt-in legacy source."""

    source = "netease"

    def __init__(self, client: Any) -> None:
        self.client = client

    async def start(self) -> None:
        start = getattr(self.client, "start", None)
        if callable(start):
            await start()

    async def stop(self) -> None:
        stop = getattr(self.client, "stop", None)
        if callable(stop):
            await stop()

    async def search(self, keyword: str, *, artist_hint: str = "") -> list[SongCandidate]:
        from .netease_client import extract_netease_song_id

        song_id = extract_netease_song_id(keyword)
        if song_id is not None:
            song = await self.client.get_song_detail(song_id)
            return [_candidate_from_netease_song(song)] if song is not None else []
        songs = await self.client.search(keyword, artist_hint=artist_hint)
        return [_candidate_from_netease_song(song) for song in songs]

    async def resolve_audio(self, song: SongCandidate) -> ResolvedSongAudio:
        raw_song = song.metadata.get("netease_song")
        song_id = getattr(raw_song, "song_id", song.song_id)
        url = await self.client.get_song_url(song_id)
        if not url:
            raise MusicSourceLookupError(f"NetEase did not return a playable URL for: {song.title}")
        return ResolvedSongAudio(song=song, audio_url=url)

    async def login_with_qr(self, *, reason: str = "", force: bool = False) -> str:
        login = getattr(self.client, "login_with_qr", None)
        if not callable(login):
            raise MusicSourceLookupError("legacy NetEase client does not support QR login")
        try:
            return await login(reason=reason, force=force)
        except TypeError:
            return await login(reason=reason)

    def clear_cached_access_token(self) -> None:
        clear = getattr(self.client, "clear_cached_access_token", None)
        if callable(clear):
            clear()


def normalize_source_provider_name(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"", "audius"}:
        return "audius"
    if normalized in {"netease", "net_ease", "163", "ncm"}:
        return "netease"
    if normalized in {"direct", "url", "http", "https"}:
        return "direct"
    if normalized in {"local", "file", "path"}:
        return "local"
    return normalized


def build_music_source_provider(settings: Any, *, provider_name: str = "", logger: Any = None) -> MusicSourceProvider:
    """Build the configured music source provider."""

    normalized = normalize_source_provider_name(provider_name or getattr(settings, "source_provider", "audius"))
    if normalized == "audius":
        from .audius_client import AudiusMusicSourceProvider

        return AudiusMusicSourceProvider(
            base_url=getattr(settings, "audius_api_base_url", "https://api.audius.co/v1"),
            search_limit=getattr(settings, "audius_search_limit", 5),
            api_key=getattr(settings, "audius_api_key", ""),
            bearer_token=getattr(settings, "audius_bearer_token", ""),
            require_downloadable=getattr(settings, "audius_require_downloadable", False),
            connect_timeout_sec=getattr(settings, "connect_timeout_sec", 10.0),
            request_timeout_sec=getattr(settings, "request_timeout_sec", 120.0),
            logger=logger,
        )
    if normalized == "direct":
        return DirectMusicSourceProvider()
    if normalized == "local":
        return LocalFileMusicSourceProvider()
    if normalized == "netease":
        return LegacyNeteaseMusicSourceProvider(_build_netease_client(settings, logger=logger))
    raise ValueError(f"unsupported song source provider: {normalized}")


def _candidate_from_netease_song(song: Any) -> SongCandidate:
    return SongCandidate(
        song_id=str(song.song_id),
        title=str(song.name),
        artist_text=str(song.artist_text),
        duration_ms=int(song.duration_ms or 0),
        page_url=f"https://music.163.com/song?id={song.song_id}",
        source="netease",
        metadata={"netease_song": song},
    )


def _is_http_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _title_from_url(value: str) -> str:
    path_name = Path(urlparse(value).path).stem
    return path_name or "direct audio"


def _build_netease_client(settings: Any, *, logger: Any = None) -> Any:
    from .netease_client import NeteaseCloudMusicClient

    return NeteaseCloudMusicClient(
        base_url=getattr(settings, "netease_api_base_url", ""),
        app_id=getattr(settings, "netease_app_id", ""),
        app_secret=getattr(settings, "netease_app_secret", ""),
        public_key=getattr(settings, "netease_public_key", ""),
        private_key=getattr(settings, "netease_private_key", ""),
        access_token=getattr(settings, "netease_access_token", ""),
        token_cache_path=getattr(settings, "netease_token_cache_path", ""),
        device={
            "deviceId": getattr(settings, "netease_device_id", ""),
            "deviceType": getattr(settings, "netease_device_type", ""),
            "os": getattr(settings, "netease_os", ""),
            "appVer": getattr(settings, "netease_app_ver", ""),
            "channel": getattr(settings, "netease_channel", ""),
            "brand": getattr(settings, "netease_brand", ""),
            "model": getattr(settings, "netease_model", ""),
            "osVer": getattr(settings, "netease_os_ver", ""),
            "clientIp": getattr(settings, "netease_client_ip", ""),
            "flowFlag": getattr(settings, "netease_flow_flag", ""),
        },
        connect_timeout_sec=getattr(settings, "connect_timeout_sec", 10.0),
        request_timeout_sec=getattr(settings, "request_timeout_sec", 120.0),
        search_limit=getattr(settings, "netease_search_limit", 5),
        song_level=getattr(settings, "netease_song_level", "standard"),
        cookie=getattr(settings, "netease_cookie", ""),
        user_agent=getattr(settings, "netease_user_agent", ""),
        referer=getattr(settings, "netease_referer", ""),
        auto_qr_login_on_unauthorized=getattr(settings, "netease_auto_qr_login_on_unauthorized", False),
        qr_login_timeout_sec=getattr(settings, "netease_qr_login_timeout_sec", 180.0),
        qr_login_poll_interval_sec=getattr(settings, "netease_qr_poll_interval_sec", 3.0),
        logger=logger,
    )

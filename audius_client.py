"""Audius music source provider for RVC song requests."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .music_source_provider import MusicSourceLookupError, ResolvedSongAudio, SongCandidate

try:
    from aiohttp import ClientSession, ClientTimeout

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False


class AudiusMusicSourceProvider:
    """Resolve songs through the public Audius API."""

    source = "audius"

    def __init__(
        self,
        *,
        base_url: str = "https://api.audius.co/v1",
        search_limit: int = 5,
        api_key: str = "",
        bearer_token: str = "",
        require_downloadable: bool = False,
        connect_timeout_sec: float = 10.0,
        request_timeout_sec: float = 120.0,
        logger: Any = None,
    ) -> None:
        self.base_url = str(base_url or "https://api.audius.co/v1").strip().rstrip("/")
        self.search_limit = max(1, int(search_limit or 5))
        self.api_key = str(api_key or "").strip()
        self.bearer_token = str(bearer_token or "").strip()
        self.require_downloadable = bool(require_downloadable)
        self.connect_timeout_sec = float(connect_timeout_sec or 10.0)
        self.request_timeout_sec = float(request_timeout_sec or 120.0)
        self.logger = logger
        self._session: Any = None
        self._owns_session = False

    async def start(self) -> None:
        if self._session is not None:
            return
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError("aiohttp is required to use Audius song requests")
        timeout = ClientTimeout(total=self.request_timeout_sec, connect=self.connect_timeout_sec)
        self._session = ClientSession(timeout=timeout, headers=self._headers())
        self._owns_session = True

    async def stop(self) -> None:
        session = self._session
        self._session = None
        if self._owns_session and session is not None:
            close = getattr(session, "close", None)
            if callable(close):
                await close()
        self._owns_session = False

    async def search(self, keyword: str, *, artist_hint: str = "") -> list[SongCandidate]:
        normalized_keyword = str(keyword or "").strip()
        if not normalized_keyword:
            return []
        query = _query_with_artist(normalized_keyword, artist_hint)
        payload = await self._get_json(
            f"{self.base_url}/tracks/search",
            params={"query": query, "limit": self.search_limit},
        )
        records = payload.get("data")
        if not isinstance(records, list):
            return []
        candidates = [
            candidate
            for record in records
            if (candidate := self._candidate_from_record(record)) is not None
        ]
        return sorted(candidates, key=lambda candidate: _score_candidate(candidate, normalized_keyword, artist_hint), reverse=True)

    async def resolve_audio(self, song: SongCandidate) -> ResolvedSongAudio:
        track_id = quote(str(song.song_id or "").strip(), safe="")
        if not track_id:
            raise MusicSourceLookupError("Audius song id is empty")
        audio_url = await self._get_stream_url(
            f"{self.base_url}/tracks/{track_id}/stream",
            params={"no_redirect": "true"},
        )
        if not audio_url:
            raise MusicSourceLookupError(f"Audius did not return a stream URL for: {song.title}")
        return ResolvedSongAudio(song=song, audio_url=audio_url)

    def _candidate_from_record(self, record: Any) -> SongCandidate | None:
        if not isinstance(record, dict):
            return None
        if not _truthy(record.get("is_streamable"), default=True):
            return None
        is_downloadable = _truthy(record.get("is_downloadable"), default=False)
        access = record.get("access") if isinstance(record.get("access"), dict) else {}
        if self.require_downloadable and not (is_downloadable or _truthy(access.get("download"), default=False)):
            return None
        raw_id = record.get("id") or record.get("track_id")
        title = str(record.get("title") or "").strip()
        if raw_id is None or not title:
            return None
        user = record.get("user") if isinstance(record.get("user"), dict) else {}
        artist = str(user.get("name") or user.get("handle") or "").strip()
        duration_ms = _duration_ms(record.get("duration"))
        permalink = str(record.get("permalink") or "").strip()
        page_url = _audius_page_url(permalink)
        metadata = {
            "track_id": record.get("track_id"),
            "is_downloadable": is_downloadable,
        }
        return SongCandidate(
            song_id=str(raw_id),
            title=title,
            artist_text=artist,
            duration_ms=duration_ms,
            page_url=page_url,
            source=self.source,
            metadata=metadata,
        )

    async def _get_json(self, url: str, *, params: dict[str, Any]) -> dict[str, Any]:
        await self.start()
        assert self._session is not None
        async with self._session.get(url, params=params) as response:
            status = int(getattr(response, "status", 0) or 0)
            if status >= 400:
                raise MusicSourceLookupError(f"Audius API returned HTTP {status}: {url}")
            payload = await response.json(content_type=None)
        return payload if isinstance(payload, dict) else {}

    async def _get_stream_url(
        self,
        url: str,
        *,
        params: dict[str, Any],
    ) -> str:
        await self.start()
        assert self._session is not None
        async with self._session.get(url, params=params, allow_redirects=False) as response:
            status = int(getattr(response, "status", 0) or 0)
            if status >= 400:
                raise MusicSourceLookupError(f"Audius API returned HTTP {status}: {url}")
            if 300 <= status < 400:
                return str(getattr(response, "headers", {}).get("Location") or "").strip()
            payload = await response.json(content_type=None)
            return _extract_stream_url(payload)

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "MaiBot-Bilibili-Live-Adapter/1.0"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers


def _query_with_artist(keyword: str, artist_hint: str) -> str:
    artist = str(artist_hint or "").strip()
    if not artist:
        return keyword
    if artist.lower() in keyword.lower():
        return keyword
    return f"{keyword} {artist}".strip()


def _score_candidate(candidate: SongCandidate, keyword: str, artist_hint: str) -> tuple[int, int, int]:
    title = candidate.title.lower()
    artist = candidate.artist_text.lower()
    normalized_keyword = keyword.lower()
    normalized_artist = str(artist_hint or "").strip().lower()
    title_score = 2 if normalized_keyword in title else int(any(part in title for part in normalized_keyword.split()))
    artist_score = 2 if normalized_artist and normalized_artist in artist else 0
    duration_score = 1 if candidate.duration_ms > 0 else 0
    return (artist_score, title_score, duration_score)


def _duration_ms(value: Any) -> int:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0
    if parsed <= 0:
        return 0
    return int(parsed * 1000)


def _truthy(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _audius_page_url(permalink: str) -> str:
    if permalink.startswith("http://") or permalink.startswith("https://"):
        return permalink
    if permalink:
        return "https://audius.co/" + permalink.lstrip("/")
    return ""


def _extract_stream_url(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        url = data.get("url")
        if isinstance(url, str):
            return url.strip()
    url = payload.get("url")
    if isinstance(url, str):
        return url.strip()
    return ""

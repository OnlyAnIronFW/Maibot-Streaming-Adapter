"""NetEase Cloud Music OpenAPI client for RVC song requests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

import base64
import contextlib
import json
import platform
import re
import time

try:
    from aiohttp import ClientSession, ClientTimeout

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover
    hashes = None  # type: ignore[assignment]
    padding = None  # type: ignore[assignment]
    serialization = None  # type: ignore[assignment]
    CRYPTOGRAPHY_AVAILABLE = False

NETEASE_OPENAPI_BASE_URL = "https://openncm.music.163.com"
ANONYMOUS_LOGIN_PATH = "/openapi/music/basic/oauth2/login/anonymous"
QR_LOGIN_KEY_PATH = "/openapi/music/basic/user/oauth2/qrcodekey/get/v2"
QR_LOGIN_POLL_PATH = "/openapi/music/basic/oauth2/device/login/qrcode/get"
SEARCH_SONG_PATH = "/openapi/music/basic/search/song/get/v3"
SONG_DETAIL_PATH = "/openapi/music/basic/song/detail/get/v2"
SONG_PLAY_URL_PATH = "/openapi/music/basic/song/playurl/get/v2"
DEFAULT_NETEASE_REFERER = "https://music.163.com/"
DEFAULT_NETEASE_PUBLIC_IP_URL = "https://api.ipify.org?format=json"
QR_LOGIN_WAITING_STATUSES = {"801", "802"}
QR_LOGIN_SUCCESS_STATUSES = {"803"}
QR_LOGIN_EXPIRED_STATUSES = {"800"}
QR_LOGIN_REQUIRED_CODES = {300, "300", 301, "301", 1408, "1408"}


@dataclass(frozen=True)
class NeteaseSong:
    """A normalized NetEase song search result."""

    song_id: int | str
    name: str
    artists: list[str]
    duration_ms: int = 0

    @property
    def artist_text(self) -> str:
        return "/".join(self.artists)


@dataclass(frozen=True)
class NeteaseQrLoginSession:
    """NetEase official QR login session metadata."""

    qr_code_url: str
    uni_key: str


class NeteaseCloudMusicClient:
    """Small async client for NetEase Cloud Music OpenAPI."""

    def __init__(
        self,
        *,
        base_url: str = NETEASE_OPENAPI_BASE_URL,
        app_id: str = "",
        app_secret: str = "",
        public_key: str = "",
        private_key: str = "",
        access_token: str = "",
        device: Mapping[str, Any] | None = None,
        token_cache_path: str | Path = "",
        connect_timeout_sec: float = 10.0,
        request_timeout_sec: float = 120.0,
        search_limit: int = 5,
        song_level: str = "standard",
        cookie: str = "",
        user_agent: str = "ncm-0.1.1",
        referer: str = DEFAULT_NETEASE_REFERER,
        auto_qr_login_on_unauthorized: bool = False,
        qr_login_timeout_sec: float = 180.0,
        qr_login_poll_interval_sec: float = 3.0,
        logger: Any = None,
    ) -> None:
        self.base_url = str(base_url or NETEASE_OPENAPI_BASE_URL).rstrip("/")
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "").strip()
        self.public_key = str(public_key or "").strip()
        self.private_key = str(private_key or "").strip()
        self._access_token = str(access_token or "").strip()
        self.device = dict(device or {})
        self.token_cache_path = (
            Path(token_cache_path).expanduser() if str(token_cache_path or "").strip() else _plugin_data_dir() / "netease_openapi_token.json"
        )
        self.connect_timeout_sec = max(1.0, float(connect_timeout_sec or 10.0))
        self.request_timeout_sec = max(1.0, float(request_timeout_sec or 120.0))
        self.search_limit = max(1, int(search_limit or 5))
        self.song_level = str(song_level or "standard").strip() or "standard"
        self.cookie = str(cookie or "").strip()
        self.user_agent = str(user_agent or "ncm-0.1.1").strip()
        self.referer = str(referer or DEFAULT_NETEASE_REFERER).strip()
        self.auto_qr_login_on_unauthorized = bool(auto_qr_login_on_unauthorized)
        self.qr_login_timeout_sec = max(1.0, float(qr_login_timeout_sec or 180.0))
        self.qr_login_poll_interval_sec = max(0.0, float(qr_login_poll_interval_sec or 3.0))
        self.logger = logger
        self._session: Any = None
        self._resolved_public_ip = ""
        self._authorization_lock = asyncio.Lock()

    async def start(self) -> None:
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError("aiohttp is required for Netease song requests")
        if not self.base_url:
            raise ValueError("Netease API base_url is empty")
        if self._session is None:
            timeout = ClientTimeout(total=self.request_timeout_sec, connect=self.connect_timeout_sec)
            headers = {"User-Agent": self.user_agent, "Referer": self.referer}
            if self.cookie:
                headers["Cookie"] = self.cookie
            self._session = ClientSession(timeout=timeout, headers=headers)

    async def stop(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    async def search(self, keyword: str, *, artist_hint: str = "") -> list[NeteaseSong]:
        normalized_keyword = str(keyword or "").strip()
        if not normalized_keyword:
            return []
        payload = await self._request_json(
            SEARCH_SONG_PATH,
            {
                "keyword": normalized_keyword,
                "limit": self.search_limit,
                "offset": 0,
                "qualityFlag": True,
            },
        )
        songs = _extract_song_items(payload)
        normalized_songs = [_normalize_song(item) for item in songs]
        normalized_songs = [song for song in normalized_songs if song is not None]
        hint = str(artist_hint or "").strip().lower()
        if hint:
            normalized_songs.sort(key=lambda song: 0 if hint in song.artist_text.lower() else 1)
        return normalized_songs

    async def get_song_url(self, song_id: int | str) -> str | None:
        payload = await self._request_json(SONG_PLAY_URL_PATH, self._song_url_biz_content(song_id))
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            return None
        url = str(data.get("url") or "").strip()
        return url or None

    async def get_song_detail(self, song_id: int | str) -> NeteaseSong | None:
        payload = await self._request_json(SONG_DETAIL_PATH, {"songId": str(song_id), "withUrl": False})
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            return None
        return _normalize_song(data)

    async def create_qr_login_session(self) -> NeteaseQrLoginSession:
        payload = await self._request_json(
            QR_LOGIN_KEY_PATH,
            {"type": 2, "expiredKey": "300"},
            method="GET",
        )
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            raise RuntimeError("Netease QR login did not return session data")
        qr_code_url = str(data.get("qrCodeUrl") or data.get("url") or "").strip()
        uni_key = str(data.get("uniKey") or data.get("key") or "").strip()
        if not qr_code_url or not uni_key:
            raise RuntimeError("Netease QR login did not return qrCodeUrl/uniKey")
        return NeteaseQrLoginSession(qr_code_url=qr_code_url, uni_key=uni_key)

    async def wait_for_qr_login(
        self,
        session: NeteaseQrLoginSession,
        *,
        timeout_sec: float | None = None,
        poll_interval_sec: float | None = None,
    ) -> str:
        normalized_timeout = max(1.0, float(timeout_sec or self.qr_login_timeout_sec))
        normalized_poll_interval = max(0.0, float(poll_interval_sec or self.qr_login_poll_interval_sec))
        deadline = time.monotonic() + normalized_timeout
        while True:
            payload = await self._request_json(
                QR_LOGIN_POLL_PATH,
                {"key": session.uni_key, "clientId": self.app_id},
                method="GET",
            )
            data = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(data, Mapping):
                raise RuntimeError("Netease QR login status did not return data")
            access_token = str(data.get("accessToken") or "").strip()
            status = str(data.get("status") or "").strip()
            if access_token or status in QR_LOGIN_SUCCESS_STATUSES:
                if not access_token:
                    raise RuntimeError("Netease QR login succeeded without accessToken")
                self._access_token = access_token
                self._write_cached_access_token(data)
                return access_token
            if status in QR_LOGIN_EXPIRED_STATUSES:
                raise RuntimeError("Netease QR login code expired before confirmation")
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for NetEase QR login confirmation")
            await asyncio.sleep(normalized_poll_interval)

    async def login_with_qr(self, *, reason: str = "", force: bool = False) -> str:
        async with self._authorization_lock:
            if force:
                self.clear_cached_access_token()
            cached = self._read_cached_access_token()
            if cached and cached != self._access_token:
                self._access_token = cached
                return cached
            session = await self.create_qr_login_session()
            reason_suffix = f" ({reason})" if str(reason or "").strip() else ""
            self._log_warning(
                "NetEase OpenAPI requires account login"
                f"{reason_suffix}. Scan this QR link in NetEase Cloud Music: {session.qr_code_url}"
            )
            return await self.wait_for_qr_login(session)

    async def _request_json(
        self,
        path: str,
        biz_content: dict[str, Any],
        *,
        require_token: bool = True,
        method: str = "GET",
        allow_qr_retry: bool = True,
    ) -> dict[str, Any]:
        if self._session is None:
            await self.start()
        if self._session is None:
            raise RuntimeError("Netease HTTP session is unavailable")
        normalized_path = "/" + str(path or "").lstrip("/")
        device_payload = await self._resolve_device_payload()
        access_token = await self._ensure_access_token() if require_token else ""
        normalized_payload = await self._perform_json_request(
            normalized_path,
            biz_content,
            access_token=access_token,
            method=method,
            device_payload=device_payload,
        )
        code = normalized_payload.get("code")
        if code in QR_LOGIN_REQUIRED_CODES and require_token and allow_qr_retry and self.auto_qr_login_on_unauthorized:
            await self.login_with_qr(reason=f"{normalized_path} returned code {code}", force=True)
            return await self._request_json(
                path,
                biz_content,
                require_token=require_token,
                method=method,
                allow_qr_retry=False,
            )
        if code not in {200, "200", None}:
            message = normalized_payload.get("message") or normalized_payload.get("msg") or "unknown error"
            if code in QR_LOGIN_REQUIRED_CODES:
                raise RuntimeError(f"Netease OpenAPI returned code {code}: {message}. Complete NetEase QR login and retry.")
            raise RuntimeError(f"Netease OpenAPI returned code {code}: {message}")
        return normalized_payload

    async def _perform_json_request(
        self,
        normalized_path: str,
        biz_content: Mapping[str, Any],
        *,
        access_token: str = "",
        method: str = "GET",
        device_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("Netease HTTP session is unavailable")
        params = self._build_signed_params(
            biz_content,
            access_token=access_token,
            device_payload=device_payload,
        )
        request_method = str(method or "GET").strip().upper() or "GET"
        request_kwargs: dict[str, Any] = {"params": params}
        if request_method == "POST":
            request_kwargs["headers"] = {"Content-Type": "application/json"}
        async with self._session.request(request_method, f"{self.base_url}{normalized_path}", **request_kwargs) as response:
            payload = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"Netease OpenAPI returned HTTP {response.status}: {payload}")
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _build_signed_params(
        self,
        biz_content: Mapping[str, Any],
        *,
        access_token: str = "",
        timestamp_ms: int | None = None,
        device_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, str | int]:
        if not self.app_id:
            raise ValueError("Netease OpenAPI app_id is required")
        params: dict[str, str | int] = {
            "appId": self.app_id,
            "timestamp": int(timestamp_ms if timestamp_ms is not None else time.time() * 1000),
            "device": _compact_json(device_payload or self._device_payload()),
            "bizContent": _compact_json(dict(biz_content)),
            "signType": "RSA_SHA256",
        }
        normalized_token = str(access_token or "").strip()
        if normalized_token:
            params["accessToken"] = normalized_token
        params["sign"] = self._sign_content(format_openapi_parameters(params))
        return params

    async def _ensure_access_token(self) -> str:
        if self._access_token:
            return self._access_token
        cached = self._read_cached_access_token()
        if cached:
            self._access_token = cached
            return cached
        payload = await self._request_json(
            ANONYMOUS_LOGIN_PATH,
            {"clientId": self.app_id},
            require_token=False,
            method="POST",
        )
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            raise RuntimeError("Netease anonymous login did not return token data")
        token = str(data.get("accessToken") or "").strip()
        if not token:
            raise RuntimeError("Netease anonymous login did not return accessToken")
        self._access_token = token
        self._write_cached_access_token(data)
        return token

    def _read_cached_access_token(self) -> str:
        path = Path(self.token_cache_path)
        if not path.exists():
            return ""
        with contextlib.suppress(Exception):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                return ""
            if str(payload.get("appId") or "") != self.app_id:
                return ""
            if str(payload.get("deviceId") or "") != str(self._device_payload().get("deviceId") or ""):
                return ""
            return str(payload.get("accessToken") or "").strip()
        return ""

    def clear_cached_access_token(self) -> bool:
        self._access_token = ""
        path = Path(self.token_cache_path)
        if not path.exists():
            return False
        with contextlib.suppress(Exception):
            path.unlink()
            return True
        return False

    def _write_cached_access_token(self, token_data: Mapping[str, Any]) -> None:
        path = Path(self.token_cache_path)
        with contextlib.suppress(Exception):
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "appId": self.app_id,
                "deviceId": str(self._device_payload().get("deviceId") or ""),
                "accessToken": str(token_data.get("accessToken") or ""),
                "refreshToken": str(token_data.get("refreshToken") or ""),
                "savedAt": int(time.time()),
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _device_payload(self) -> dict[str, str]:
        defaults = {
            "deviceType": "openapi",
            "os": "ncmcli",
            "appVer": "0.1.1",
            "channel": "ncmcli",
            "model": _default_ncmcli_model(),
            "deviceId": "ncmcli_maibotlive001",
            "brand": "ncmcli",
            "osVer": _default_os_version(),
            "clientIp": "",
        }
        payload = {key: str(self.device.get(key) or default).strip() for key, default in defaults.items()}
        flow_flag = str(self.device.get("flowFlag") or "").strip()
        if flow_flag:
            payload["flowFlag"] = flow_flag
        return payload

    async def _resolve_device_payload(self) -> dict[str, str]:
        payload = self._device_payload()
        if _should_resolve_public_ip(payload.get("clientIp", "")):
            resolved_ip = await self._resolve_public_ip()
            if resolved_ip:
                payload["clientIp"] = resolved_ip
        return payload

    async def _resolve_public_ip(self) -> str:
        if self._resolved_public_ip:
            return self._resolved_public_ip
        if self._session is None:
            await self.start()
        if self._session is None:
            return ""
        try:
            async with self._session.get(
                DEFAULT_NETEASE_PUBLIC_IP_URL,
                headers={"Accept": "application/json"},
            ) as response:
                payload = await response.json(content_type=None)
        except Exception:
            return ""
        ip_address = str(payload.get("ip") or "").strip() if isinstance(payload, Mapping) else ""
        if ip_address:
            self._resolved_public_ip = ip_address
        return self._resolved_public_ip

    def _song_url_biz_content(self, song_id: int | str) -> dict[str, Any]:
        biz_content: dict[str, Any] = {"songId": str(song_id)}
        normalized_level = str(self.song_level or "").strip()
        if not normalized_level:
            return biz_content
        if normalized_level.isdigit():
            biz_content["bitrate"] = int(normalized_level)
        else:
            biz_content["level"] = normalized_level
        return biz_content

    def _sign_content(self, content: str) -> str:
        if not CRYPTOGRAPHY_AVAILABLE:
            raise RuntimeError("cryptography is required for Netease OpenAPI RSA_SHA256 signing")
        if not self.private_key:
            raise ValueError("Netease OpenAPI private_key is required")
        private_key = serialization.load_pem_private_key(_format_pem_key(self.private_key, "PRIVATE").encode(), None)
        signature = private_key.sign(content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode("utf-8")

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(message)


def format_openapi_parameters(params: Mapping[str, Any]) -> str:
    """Format parameters for NetEase OpenAPI RSA signing."""

    filtered_params = {}
    for key, value in params.items():
        if key == "sign" or value == "" or isinstance(value, bytes):
            continue
        filtered_params[str(key)] = value
    pairs = []
    for key, value in sorted(filtered_params.items(), key=lambda item: item[0]):
        if isinstance(value, bool):
            formatted_value = str(value).lower()
        else:
            formatted_value = str(value)
        pairs.append(f"{key}={formatted_value}")
    return "&".join(pairs)


def _extract_song_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if isinstance(data, Mapping):
        records = data.get("records")
        if isinstance(records, list):
            return [item for item in records if isinstance(item, Mapping)]
    result = payload.get("result") if isinstance(payload, Mapping) else None
    if isinstance(result, Mapping):
        songs = result.get("songs")
        if isinstance(songs, list):
            return [item for item in songs if isinstance(item, Mapping)]
    songs = payload.get("songs") if isinstance(payload, Mapping) else None
    if isinstance(songs, list):
        return [item for item in songs if isinstance(item, Mapping)]
    return []


def _normalize_song(item: Mapping[str, Any]) -> NeteaseSong | None:
    song_id = _optional_song_id(item.get("id") or item.get("songId"))
    name = str(item.get("name") or "").strip()
    if song_id is None or not name:
        return None
    duration_ms = _optional_int(item.get("duration")) or _optional_int(item.get("dt")) or 0
    artists = _extract_artists(item)
    return NeteaseSong(song_id=song_id, name=name, artists=artists, duration_ms=max(0, duration_ms))


def _extract_artists(item: Mapping[str, Any]) -> list[str]:
    for key in ("artists", "fullArtists", "ar"):
        raw_artists = item.get(key)
        if isinstance(raw_artists, list):
            artists = [
                str(artist.get("name") or "").strip()
                for artist in raw_artists
                if isinstance(artist, Mapping) and str(artist.get("name") or "").strip()
            ]
            if artists:
                return artists
    artist = item.get("artist")
    if isinstance(artist, Mapping):
        name = str(artist.get("name") or "").strip()
        if name:
            return [name]
    artist_name = str(item.get("artistName") or "").strip()
    if artist_name:
        return [artist_name]
    return []


def _optional_song_id(value: Any) -> int | str | None:
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_netease_song_id(value: str) -> int | None:
    """Extract a Netease song id from a song URL or plain id string."""

    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    parsed = urlparse(text)
    query_ids = parse_qs(parsed.query).get("id")
    if query_ids:
        song_id = _optional_int(query_ids[0])
        if song_id is not None:
            return song_id
    match = re.search(r"(?:song/|song\?id=)(\d+)", text)
    if match:
        return int(match.group(1))
    return None


def _compact_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))


def _format_pem_key(key_string: str, key_type: str) -> str:
    normalized_key = str(key_string or "").strip()
    if "BEGIN" in normalized_key:
        return normalized_key
    normalized_key = "".join(normalized_key.split())
    formatted_key = f"-----BEGIN {key_type} KEY-----\n"
    for index in range(0, len(normalized_key), 64):
        formatted_key += normalized_key[index : index + 64] + "\n"
    formatted_key += f"-----END {key_type} KEY-----"
    return formatted_key


def _plugin_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def _default_ncmcli_model() -> str:
    system_name = (platform.system() or "Windows").strip() or "Windows"
    machine_name = (platform.machine() or "x64").strip().replace(" ", "") or "x64"
    normalized_machine = machine_name.lower()
    if normalized_machine in {"amd64", "x86_64"}:
        machine_name = "x64"
    return f"{system_name}_{machine_name}_cli"


def _default_os_version() -> str:
    return str(platform.version() or "").strip() or "10.0"


def _should_resolve_public_ip(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"", "0.0.0.0", "127.0.0.1", "auto"}

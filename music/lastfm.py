from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import aiohttp

from config import Config
from .errors import LastfmError
from .models import Track

logger = logging.getLogger(__name__)

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"


def lastfm_signature(params: dict[str, str], secret: str) -> str:
    """Build Last.fm's api_sig.

    format/callback are transport details, not signed parameters.
    """

    payload = {
        key: str(value)
        for key, value in params.items()
        if key not in {"format", "callback", "api_sig"} and value is not None
    }
    raw = "".join(f"{key}{payload[key]}" for key in sorted(payload))
    return hashlib.md5((raw + secret).encode("utf-8")).hexdigest()


class LastfmClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._user_cache: tuple[float, dict[str, Any]] | None = None

    @property
    def can_scrobble(self) -> bool:
        return self.config.lastfm_can_scrobble

    @property
    def can_query(self) -> bool:
        return self.config.lastfm_can_query

    @property
    def username(self) -> str | None:
        return self.config.lastfm_username

    @property
    def profile_url(self) -> str | None:
        if not self.username:
            return None
        return f"https://www.last.fm/user/{self.username}"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=12)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "RuMusicBot/1.0 (+https://last.fm)"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _signed_post(self, method: str, payload: dict[str, str]) -> dict[str, Any]:
        if not self.config.lastfm_api_key or not self.config.lastfm_api_secret:
            raise LastfmError("Chưa cấu hình Last.fm API key/secret.")

        params = {
            "method": method,
            "api_key": self.config.lastfm_api_key,
            **payload,
        }
        params["api_sig"] = lastfm_signature(
            params, self.config.lastfm_api_secret
        )
        params["format"] = "json"

        session = await self._get_session()
        try:
            async with session.post(LASTFM_API_URL, data=params) as response:
                text = await response.text()
                if response.status >= 400:
                    raise LastfmError(
                        f"Last.fm HTTP {response.status}: {text[:180]}"
                    )
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise LastfmError("Last.fm trả về dữ liệu không hợp lệ.") from exc
                if isinstance(data, dict) and "error" in data:
                    raise LastfmError(
                        f"Last.fm: {data.get('message', 'lỗi không xác định')} "
                        f"(mã {data.get('error')})"
                    )
                return data
        except aiohttp.ClientError as exc:
            raise LastfmError(f"Không kết nối được Last.fm: {exc}") from exc
        except TimeoutError as exc:
            raise LastfmError("Last.fm phản hồi quá chậm.") from exc

    async def now_playing(self, track: Track) -> None:
        if not self.can_scrobble:
            return
        if not track.title or not track.artist or track.is_live:
            return

        payload = {
            "sk": self.config.lastfm_session_key or "",
            "artist": track.artist,
            "track": track.title,
        }
        if track.duration and track.duration > 0:
            payload["duration"] = str(int(track.duration))
        await self._signed_post("track.updateNowPlaying", payload)

    async def scrobble(self, track: Track, timestamp: int) -> None:
        if not self.can_scrobble:
            raise LastfmError("Chưa cấu hình Last.fm session key.")
        if not track.title or not track.artist:
            raise LastfmError("Bài hát thiếu tên hoặc nghệ sĩ.")
        if track.is_live:
            raise LastfmError("Không scrobble luồng live.")

        payload = {
            "sk": self.config.lastfm_session_key or "",
            "artist": track.artist,
            "track": track.title,
            "timestamp": str(int(timestamp)),
        }
        if track.duration and track.duration > 0:
            payload["duration"] = str(int(track.duration))
        await self._signed_post("track.scrobble", payload)

    async def get_user_info(self) -> dict[str, Any]:
        if not self.can_query:
            raise LastfmError("Chưa cấu hình Last.fm API key hoặc username.")

        now = time.monotonic()
        if self._user_cache and now - self._user_cache[0] < 60:
            return self._user_cache[1]

        assert self.config.lastfm_api_key is not None
        assert self.config.lastfm_username is not None
        params = {
            "method": "user.getInfo",
            "user": self.config.lastfm_username,
            "api_key": self.config.lastfm_api_key,
            "format": "json",
        }
        session = await self._get_session()
        try:
            async with session.get(LASTFM_API_URL, params=params) as response:
                text = await response.text()
                if response.status >= 400:
                    raise LastfmError(
                        f"Last.fm HTTP {response.status}: {text[:180]}"
                    )
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise LastfmError("Last.fm trả về dữ liệu không hợp lệ.") from exc
        except aiohttp.ClientError as exc:
            raise LastfmError(f"Không kết nối được Last.fm: {exc}") from exc
        except TimeoutError as exc:
            raise LastfmError("Last.fm phản hồi quá chậm.") from exc

        if not isinstance(data, dict) or "error" in data:
            message = data.get("message", "lỗi không xác định") if isinstance(data, dict) else "lỗi"
            raise LastfmError(f"Last.fm: {message}")
        user = data.get("user")
        if not isinstance(user, dict):
            raise LastfmError("Last.fm không trả về thông tin người dùng.")

        self._user_cache = (now, user)
        return user

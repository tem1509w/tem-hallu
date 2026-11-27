from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from config import Config
from .errors import SpotifyError

SPOTIFY_URL_RE = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[A-Za-z-]+/)?(track|album|playlist)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)
SPOTIFY_URI_RE = re.compile(
    r"^spotify:(track|album|playlist):([A-Za-z0-9]+)$", re.IGNORECASE
)


def parse_spotify_url(value: str) -> tuple[str, str] | None:
    text = value.strip()
    match = SPOTIFY_URL_RE.match(text) or SPOTIFY_URI_RE.match(text)
    if not match:
        return None
    return match.group(1).lower(), match.group(2)


@dataclass(slots=True)
class SpotifyTrack:
    title: str
    artists: str
    duration: int | None
    url: str
    thumbnail: str | None = None


@dataclass(slots=True)
class SpotifyCollection:
    kind: str
    title: str
    tracks: list[SpotifyTrack] = field(default_factory=list)
    truncated: bool = False


class SpotifyClient:
    API_ROOT = "https://api.spotify.com/v1"
    TOKEN_URL = "https://accounts.spotify.com/api/token"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._token_expires_at = 0.0

    @property
    def configured(self) -> bool:
        return self.config.spotify_configured

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=18),
                headers={"User-Agent": "RuMusicBot/1.0"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_token(self, *, force: bool = False) -> str:
        if not self.configured:
            raise SpotifyError(
                "Chưa cấu hình Spotify. Cần SPOTIFY_CLIENT_ID và "
                "SPOTIFY_CLIENT_SECRET trong .env."
            )
        now = time.monotonic()
        if not force and self._token and now < self._token_expires_at - 60:
            return self._token

        session = await self._get_session()
        assert self.config.spotify_client_id is not None
        assert self.config.spotify_client_secret is not None
        try:
            async with session.post(
                self.TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=aiohttp.BasicAuth(
                    self.config.spotify_client_id,
                    self.config.spotify_client_secret,
                ),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    message = payload.get("error_description") or payload.get("error")
                    raise SpotifyError(
                        f"Không lấy được Spotify access token: {message or response.status}"
                    )
        except aiohttp.ClientError as exc:
            raise SpotifyError(f"Không kết nối được Spotify: {exc}") from exc
        except TimeoutError as exc:
            raise SpotifyError("Spotify phản hồi quá chậm.") from exc

        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise SpotifyError("Spotify không trả về access token.")
        expires_in = int(payload.get("expires_in", 3600))
        self._token = token
        self._token_expires_at = now + expires_in
        return token

    async def _api_get(
        self, path: str, params: dict[str, str | int] | None = None
    ) -> dict[str, Any]:
        session = await self._get_session()
        for attempt in range(2):
            token = await self._get_token(force=attempt == 1)
            try:
                async with session.get(
                    f"{self.API_ROOT}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    if response.status == 401 and attempt == 0:
                        self._token = None
                        continue
                    payload = await response.json(content_type=None)
                    if response.status >= 400:
                        detail = ""
                        if isinstance(payload, dict):
                            detail = str(
                                payload.get("error", {}).get("message")
                                if isinstance(payload.get("error"), dict)
                                else payload.get("error", "")
                            )
                        if response.status == 404:
                            raise SpotifyError("Không tìm thấy nội dung Spotify.")
                        if response.status == 429:
                            raise SpotifyError(
                                "Spotify đang giới hạn tần suất. Thử lại sau ít phút."
                            )
                        raise SpotifyError(
                            f"Spotify API lỗi {response.status}"
                            + (f": {detail}" if detail else ".")
                        )
                    if not isinstance(payload, dict):
                        raise SpotifyError("Spotify trả về dữ liệu không hợp lệ.")
                    return payload
            except aiohttp.ClientError as exc:
                raise SpotifyError(f"Không kết nối được Spotify: {exc}") from exc
            except TimeoutError as exc:
                raise SpotifyError("Spotify phản hồi quá chậm.") from exc

        raise SpotifyError("Không thể xác thực với Spotify.")

    async def resolve(self, url: str, max_tracks: int) -> SpotifyCollection:
        parsed = parse_spotify_url(url)
        if parsed is None:
            raise SpotifyError("Link Spotify không hợp lệ.")
        kind, spotify_id = parsed
        if not self.configured:
            raise SpotifyError(
                "Link Spotify cần SPOTIFY_CLIENT_ID và SPOTIFY_CLIENT_SECRET "
                "để đọc metadata. Xem README phần Spotify."
            )

        if kind == "track":
            data = await self._api_get(f"/tracks/{spotify_id}")
            track = self._map_track(data)
            if track is None:
                raise SpotifyError("Không đọc được thông tin bài hát Spotify.")
            return SpotifyCollection(kind=kind, title=track.title, tracks=[track])

        if kind == "album":
            album = await self._api_get(f"/albums/{spotify_id}")
            fallback_image = self._first_image(album.get("images"))
            tracks, truncated = await self._paginate_tracks(
                f"/albums/{spotify_id}/tracks",
                max_tracks,
                fallback_image,
            )
            return SpotifyCollection(
                kind=kind,
                title=str(album.get("name") or "Spotify album"),
                tracks=tracks,
                truncated=truncated,
            )

        playlist = await self._api_get(
            f"/playlists/{spotify_id}",
            params={
                "fields": "name,images,tracks(total),external_urls",
            },
        )
        fallback_image = self._first_image(playlist.get("images"))
        tracks, truncated = await self._paginate_tracks(
            f"/playlists/{spotify_id}/tracks",
            max_tracks,
            fallback_image,
        )
        return SpotifyCollection(
            kind=kind,
            title=str(playlist.get("name") or "Spotify playlist"),
            tracks=tracks,
            truncated=truncated,
        )

    async def _paginate_tracks(
        self,
        path: str,
        max_tracks: int,
        fallback_image: str | None,
    ) -> tuple[list[SpotifyTrack], bool]:
        tracks: list[SpotifyTrack] = []
        offset = 0
        total: int | None = None
        truncated = False

        while len(tracks) < max_tracks:
            limit = min(100, max_tracks - len(tracks))
            page = await self._api_get(
                path,
                params={"limit": limit, "offset": offset},
            )
            items = page.get("items")
            if not isinstance(items, list):
                break
            if total is None:
                total = int(page.get("total") or len(items) + offset)
            if len(items) == 0:
                break

            for item in items:
                track_data = item.get("track", item) if isinstance(item, dict) else None
                if not isinstance(track_data, dict):
                    continue
                if track_data.get("type") not in {None, "track"}:
                    continue
                mapped = self._map_track(track_data, fallback_image)
                if mapped is not None:
                    tracks.append(mapped)
                if len(tracks) >= max_tracks:
                    break

            offset += len(items)
            if not page.get("next"):
                break
            await asyncio.sleep(0)

        if total is not None:
            truncated = total > len(tracks)
        return tracks, truncated

    def _map_track(
        self, data: dict[str, Any], fallback_image: str | None = None
    ) -> SpotifyTrack | None:
        title = data.get("name")
        if not isinstance(title, str) or not title.strip():
            return None
        artists_data = data.get("artists")
        artists: list[str] = []
        if isinstance(artists_data, list):
            for artist in artists_data:
                if isinstance(artist, dict) and artist.get("name"):
                    artists.append(str(artist["name"]))
        artist_text = ", ".join(artists) if artists else "Không rõ"

        album = data.get("album")
        thumbnail = fallback_image
        if isinstance(album, dict):
            thumbnail = self._first_image(album.get("images")) or thumbnail

        external = data.get("external_urls")
        spotify_url = None
        if isinstance(external, dict) and external.get("spotify"):
            spotify_url = str(external["spotify"])
        spotify_id = data.get("id")
        if not spotify_url and spotify_id:
            spotify_url = f"https://open.spotify.com/track/{spotify_id}"
        if not spotify_url:
            return None

        duration_ms = data.get("duration_ms")
        duration = (
            max(0, int(duration_ms) // 1000)
            if isinstance(duration_ms, (int, float))
            else None
        )
        return SpotifyTrack(
            title=title.strip(),
            artists=artist_text,
            duration=duration,
            url=spotify_url,
            thumbnail=thumbnail,
        )

    @staticmethod
    def _first_image(images: object) -> str | None:
        if not isinstance(images, list):
            return None
        for image in reversed(images):
            if not isinstance(image, dict):
                continue
            url = image.get("url")
            if isinstance(url, str) and url.startswith("https://"):
                return url
        return None

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import Config
from .errors import ResolverError
from .models import Resolution, Track, platform_label
from .spotify import SpotifyClient, parse_spotify_url
from .utils import truncate

logger = logging.getLogger(__name__)

class MusicResolver:
    def __init__(self, config: Config, spotify: SpotifyClient) -> None:
        self.config = config
        self.spotify = spotify
        self._cookies_warned = False

    async def resolve(
        self,
        query: str,
        *,
        requester_id: int | None,
        requester_name: str,
        playlist: bool = False,
    ) -> Resolution:
        query = query.strip()
        if not query:
            raise ResolverError("Bạn cần nhập tên bài hát hoặc link.")
        if len(query) > 500:
            raise ResolverError("Truy vấn quá dài (tối đa 500 ký tự).")
        if any(ord(char) < 32 for char in query):
            raise ResolverError("Truy vấn chứa ký tự không hợp lệ.")

        spotify_info = parse_spotify_url(query)
        if spotify_info is not None:
            return await self._resolve_spotify(
                query,
                kind=spotify_info[0],
                requester_id=requester_id,
                requester_name=requester_name,
                playlist=playlist,
            )

        is_url = query.startswith(("http://", "https://"))
        if is_url:
            await self._assert_public_url(query)
            if not playlist and self._looks_like_playlist_url(query):
                raise ResolverError(
                    "Đây có vẻ là link playlist/album. Hãy dùng /playlist để "
                    "thêm toàn bộ danh sách."
                )

        if not is_url and playlist:
            raise ResolverError(
                "Playlist cần một link hợp lệ (YouTube, SoundCloud, Bandcamp, "
                "Spotify...)."
            )

        if is_url:
            target = query
        else:
            target = f"ytsearch1:{query}"

        payload = await self._dump_info(
            target,
            playlist=playlist,
            no_playlist=is_url and not playlist,
            flat_playlist=playlist and is_url and self._looks_like_playlist_url(query),
        )
        return self._parse_payload(
            payload,
            requester_id=requester_id,
            requester_name=requester_name,
            playlist=playlist,
            is_search=not is_url,
        )

    async def extract_stream_url(self, track: Track) -> str:
        args = self._base_args()
        args.extend(
            [
                "--no-playlist",
                "--format",
                "bestaudio/best",
                "--get-url",
                "--socket-timeout",
                "15",
                track.url,
            ]
        )
        output = await self._run_process(
            args, timeout=self.config.stream_timeout_seconds
        )
        for line in output.splitlines():
            url = line.strip()
            if url.startswith(("http://", "https://")):
                return url
        raise ResolverError(
            f"Không lấy được luồng âm thanh cho **{truncate(track.title, 80)}**."
        )

    async def _resolve_spotify(
        self,
        url: str,
        *,
        kind: str,
        requester_id: int | None,
        requester_name: str,
        playlist: bool,
    ) -> Resolution:
        if kind != "track" and not playlist:
            raise ResolverError(
                "Đây là link album/playlist Spotify. Hãy dùng /playlist để thêm "
                "toàn bộ danh sách."
            )
        if not self.spotify.configured:
            raise ResolverError(
                "Bot chưa cấu hình Spotify API. Thêm SPOTIFY_CLIENT_ID và "
                "SPOTIFY_CLIENT_SECRET vào .env để dùng link Spotify."
            )

        collection = await self.spotify.resolve(
            url, self.config.max_playlist_size
        )
        if not collection.tracks:
            raise ResolverError("Không tìm thấy bài hát nào trong nội dung Spotify.")

        semaphore = asyncio.Semaphore(4)

        async def match_one(spotify_track) -> Track | None:
            query = f"{spotify_track.artists} - {spotify_track.title}"
            async with semaphore:
                try:
                    payload = await self._dump_info(
                        f"ytsearch1:{query}",
                        playlist=False,
                        no_playlist=False,
                        flat_playlist=False,
                        timeout=self.config.ytdlp_timeout_seconds,
                    )
                except ResolverError as exc:
                    logger.warning("Không ghép được Spotify '%s': %s", query, exc)
                    return None

            info = self._first_video(payload)
            if info is None:
                return None
            track = self._track_from_info(
                info, requester_id=requester_id, requester_name=requester_name
            )
            if track is None:
                return None

            track.title = spotify_track.title
            track.artist = spotify_track.artists
            track.duration = spotify_track.duration
            track.platform = "Spotify"
            track.source_url = spotify_track.url
            track.thumbnail = spotify_track.thumbnail or track.thumbnail
            track.is_live = False
            return track

        results = await asyncio.gather(
            *(match_one(item) for item in collection.tracks),
            return_exceptions=True,
        )
        tracks: list[Track] = []
        failed = 0
        for result in results:
            if isinstance(result, Track):
                tracks.append(result)
            else:
                if isinstance(result, Exception):
                    logger.warning("Lỗi ghép Spotify: %s", result)
                failed += 1

        if not tracks:
            raise ResolverError(
                "Không tìm được nguồn phát phù hợp cho nội dung Spotify này."
            )
        return Resolution(
            tracks=tracks,
            title=collection.title,
            truncated=collection.truncated,
            failed=failed,
            requested=len(collection.tracks),
        )

    def _parse_payload(
        self,
        payload: dict[str, Any],
        *,
        requester_id: int | None,
        requester_name: str,
        playlist: bool,
        is_search: bool,
    ) -> Resolution:
        entries = payload.get("entries")
        is_collection = isinstance(entries, list)

        if playlist and not is_collection:
            track = self._track_from_info(
                payload,
                requester_id=requester_id,
                requester_name=requester_name,
            )
            if track is None:
                raise ResolverError("Không đọc được thông tin bài hát.")
            return Resolution(
                tracks=[track],
                title=track.title,
                requested=1,
            )

        if is_collection:
            if not playlist and not is_search:
                raise ResolverError(
                    "Link này là playlist/album. Hãy dùng /playlist để thêm toàn bộ."
                )
            selected_entries = (
                entries[: self.config.max_playlist_size]
                if playlist
                else entries[:1]
            )
            tracks: list[Track] = []
            failed = 0
            for entry in selected_entries:
                if not isinstance(entry, dict):
                    failed += 1
                    continue
                track = self._track_from_info(
                    entry,
                    requester_id=requester_id,
                    requester_name=requester_name,
                )
                if track is None:
                    failed += 1
                else:
                    tracks.append(track)
            if not tracks:
                raise ResolverError("Không tìm thấy bài hát nào có thể phát.")
            total = payload.get("playlist_count")
            if not isinstance(total, int):
                total = len(entries)
            truncated = playlist and (
                total > self.config.max_playlist_size
                or len(entries) > self.config.max_playlist_size
            )
            return Resolution(
                tracks=tracks,
                title=payload.get("title") if playlist else None,
                truncated=truncated,
                failed=failed,
                requested=total,
            )

        track = self._track_from_info(
            payload,
            requester_id=requester_id,
            requester_name=requester_name,
        )
        if track is None:
            raise ResolverError("Không đọc được thông tin bài hát.")
        return Resolution(tracks=[track], title=track.title, requested=1)

    def _first_video(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        entries = payload.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    return entry
            return None
        return payload

    def _track_from_info(
        self,
        info: dict[str, Any],
        *,
        requester_id: int | None,
        requester_name: str,
    ) -> Track | None:
        title = info.get("track") or info.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        title = title.strip()

        artist = (
            info.get("artist")
            or info.get("creator")
            or info.get("album_artist")
            or info.get("uploader")
            or info.get("channel")
            or "Không rõ"
        )
        artist = str(artist).strip() or "Không rõ"

        webpage_url = info.get("webpage_url") or info.get("original_url")
        raw_url = info.get("url")
        if not webpage_url and isinstance(raw_url, str) and raw_url.startswith(
            ("http://", "https://")
        ):
            webpage_url = raw_url
        extractor = str(
            info.get("ie_key") or info.get("extractor_key") or info.get("extractor") or ""
        )
        video_id = info.get("id")
        if (
            not webpage_url
            and video_id
            and ("youtube" in extractor.lower() or not extractor)
        ):
            webpage_url = f"https://www.youtube.com/watch?v={video_id}"
        if not isinstance(webpage_url, str) or not webpage_url.startswith(
            ("http://", "https://")
        ):
            return None

        duration: int | None = None
        raw_duration = info.get("duration")
        if isinstance(raw_duration, (int, float)) and raw_duration > 0:
            duration = int(round(raw_duration))

        live_status = str(info.get("live_status") or "")
        is_live = bool(info.get("is_live")) or live_status == "is_live"

        thumbnail = info.get("thumbnail")
        if not isinstance(thumbnail, str) or not thumbnail.startswith(
            ("http://", "https://")
        ):
            thumbnail = self._pick_thumbnail(info.get("thumbnails"))

        return Track(
            title=title,
            artist=artist,
            url=webpage_url,
            platform=platform_label(extractor or None),
            duration=duration,
            thumbnail=thumbnail,
            is_live=is_live,
            requester_id=requester_id,
            requester_name=requester_name,
            identifier=str(video_id) if video_id else None,
        )

    async def _dump_info(
        self,
        target: str,
        *,
        playlist: bool,
        no_playlist: bool,
        flat_playlist: bool,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        args = self._base_args()
        args.extend(
            [
                "--dump-single-json",
                "--skip-download",
                "--socket-timeout",
                "15",
            ]
        )
        if no_playlist:
            args.append("--no-playlist")
        if playlist:
            args.extend(["--playlist-end", str(self.config.max_playlist_size)])
        if flat_playlist:
            args.append("--flat-playlist")
        args.append(target)

        output = await self._run_process(
            args, timeout=timeout or self.config.ytdlp_timeout_seconds
        )
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ResolverError("yt-dlp trả về dữ liệu không hợp lệ.") from exc
        if not isinstance(payload, dict):
            raise ResolverError("yt-dlp trả về dữ liệu không hợp lệ.")
        return payload

    def _base_args(self) -> list[str]:
        args = [
            self.config.ytdlp_path,
            "--ignore-config",
            "--no-warnings",
            "--no-progress",
            "--retries",
            "2",
            "--extractor-retries",
            "2",
        ]
        if self.config.ytdlp_cookies_file:
            if self.config.ytdlp_cookies_file.exists():
                args.extend(["--cookies", str(self.config.ytdlp_cookies_file)])
            elif not self._cookies_warned:
                self._cookies_warned = True
                logger.warning(
                    "YTDLP_COOKIES_FILE không tồn tại: %s",
                    self.config.ytdlp_cookies_file,
                )
        return args

    async def _run_process(self, args: list[str], *, timeout: int) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ResolverError(
                "Không tìm thấy yt-dlp. Hãy cài đặt bằng "
                "`pip install -U yt-dlp` và kiểm tra YTDLP_PATH."
            ) from exc
        except OSError as exc:
            raise ResolverError(f"Không chạy được yt-dlp: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise ResolverError(
                "Lấy thông tin nhạc quá lâu. Thử lại hoặc kiểm tra kết nối mạng."
            ) from exc
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.communicate()
            raise

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            detail = self._last_error_line(stderr_text)
            logger.warning(
                "yt-dlp thất bại (exit %s): %s", process.returncode, detail
            )
            raise ResolverError(
                "Không thể lấy nguồn nhạc."
                + (f" Chi tiết: {detail}" if detail else "")
            )
        if not stdout_text:
            raise ResolverError("yt-dlp không trả về dữ liệu.")
        return stdout_text

    async def _assert_public_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ResolverError("Chỉ chấp nhận link http/https hợp lệ.")

        host = parsed.hostname.strip().rstrip(".").lower()
        if (
            host in {"localhost", "localhost.localdomain"}
            or host.endswith((".local", ".internal", ".localhost"))
        ):
            raise ResolverError("Không chấp nhận địa chỉ mạng nội bộ.")

        try:
            address = ipaddress.ip_address(host.split("%")[0])
        except ValueError:
            address = None

        if address is not None:
            if not address.is_global:
                raise ResolverError("Không chấp nhận địa chỉ IP nội bộ.")
            return

        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM),
                timeout=5,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ResolverError(
                "Không phân giải được tên miền của link này."
            ) from exc

        if not infos:
            raise ResolverError("Không phân giải được tên miền của link này.")
        for info in infos:
            raw_ip = info[4][0].split("%")[0]
            try:
                resolved = ipaddress.ip_address(raw_ip)
            except ValueError:
                continue
            if not resolved.is_global:
                raise ResolverError("Link trỏ tới địa chỉ mạng nội bộ.")

    @staticmethod
    def _looks_like_playlist_url(url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
        query = parse_qs(parsed.query)
        if "youtube.com" in host or host == "youtu.be":
            if path == "/playlist" or path.endswith("/playlist"):
                return True
            if "list" in query and "v" not in query:
                return True
        if "soundcloud.com" in host and "/sets/" in path:
            return True
        if "bandcamp.com" in host and "/album/" in path:
            return True
        return False

    @staticmethod
    def _pick_thumbnail(thumbnails: object) -> str | None:
        if not isinstance(thumbnails, list):
            return None
        usable = [
            item
            for item in thumbnails
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and item["url"].startswith(("http://", "https://"))
        ]
        if not usable:
            return None
        usable.sort(key=lambda item: int(item.get("width") or 0))
        return str(usable[-1]["url"])

    @staticmethod
    def _last_error_line(stderr_text: str) -> str:
        lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
        if not lines:
            return ""
        return truncate(lines[-1], 180)

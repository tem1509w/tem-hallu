from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Coroutine

import discord
from discord.ext import commands

from config import Config
from .embeds import error_embed, now_playing_embed
from .errors import LastfmError, ResolverError, UserError, VoiceError
from .lastfm import LastfmClient
from .models import GuildSettings, Track
from .queue import GuildQueue, LoopMode
from .resolver import MusicResolver
from .storage import Database

logger = logging.getLogger(__name__)


def scrobble_threshold(
    duration: int | None, *, is_live: bool = False
) -> float | None:
    """Last.fm: at least 30s long and half played, capped at 4 minutes."""

    if is_live:
        return None
    if duration is None or duration <= 0:
        return 30.0
    if duration < 30:
        return None
    return min(duration / 2.0, 240.0)


def should_scrobble(
    duration: int | None, played_seconds: float, *, is_live: bool = False
) -> bool:
    threshold = scrobble_threshold(duration, is_live=is_live)
    return threshold is not None and played_seconds >= threshold


@dataclass(slots=True)
class PlaybackSession:
    track: Track
    started_at: int
    history_id: int | None = None
    position_base: float = 0.0
    played_seconds: float = 0.0
    segment_started_at: float | None = None
    scrobbled: bool = False
    scrobble_attempts: int = 0
    finished: bool = False
    scrobble_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    watcher: asyncio.Task[Any] | None = None

    def active_for(self, now: float | None = None) -> float:
        if self.segment_started_at is None:
            return 0.0
        moment = time.monotonic() if now is None else now
        return max(0.0, moment - self.segment_started_at)

    def played(self, now: float | None = None) -> float:
        return self.played_seconds + self.active_for(now)

    def position(self, now: float | None = None) -> float:
        position = self.position_base + self.active_for(now)
        if self.track.duration and self.track.duration > 0:
            return min(position, float(self.track.duration))
        return max(0.0, position)

    def accumulate(self, now: float | None = None) -> None:
        elapsed = self.active_for(now)
        if elapsed:
            self.played_seconds += elapsed
            self.position_base += elapsed
        self.segment_started_at = None


@dataclass(slots=True)
class EnqueueResult:
    added: int
    rejected: int
    started: bool
    current: Track | None


class GuildPlayer:
    def __init__(
        self,
        bot: commands.Bot,
        guild: discord.Guild,
        config: Config,
        database: Database,
        lastfm: LastfmClient,
        resolver: MusicResolver,
        settings: GuildSettings,
    ) -> None:
        self.bot = bot
        self.guild = guild
        self.config = config
        self.database = database
        self.lastfm = lastfm
        self.resolver = resolver
        self.queue = GuildQueue(config.max_queue_size)
        self.loop_mode = LoopMode.OFF
        self.auto_247 = settings.auto_247
        self.desired_channel_id = settings.voice_channel_id

        self._loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()
        self._voice: discord.VoiceClient | None = None
        self._session: PlaybackSession | None = None
        self._volume = config.default_volume
        self._paused = False
        self._audio_token = 0
        self._notify_channel_id: int | None = None
        self._reconnect_task: asyncio.Task[Any] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._closed = False

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------
    @property
    def voice(self) -> discord.VoiceClient | None:
        return self._voice

    @property
    def current(self) -> Track | None:
        return self._session.track if self._session else None

    @property
    def current_session(self) -> PlaybackSession | None:
        return self._session

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_connected(self) -> bool:
        return bool(self._voice and self._voice.is_connected())

    def current_position(self) -> float:
        return self._session.position() if self._session else 0.0

    def build_now_playing_embed(self) -> discord.Embed | None:
        if self._session is None:
            return None
        return now_playing_embed(
            self._session.track,
            color=self.config.embed_color,
            position=self.current_position(),
            queue_length=len(self.queue),
            loop_label=self.loop_mode.label,
            volume=self._volume,
        )

    # ------------------------------------------------------------------
    # Background task helpers / notifications
    # ------------------------------------------------------------------
    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = self._loop.create_task(coro)
        self._background.add(task)

        def done(completed: asyncio.Task[Any]) -> None:
            self._background.discard(completed)
            if completed.cancelled():
                return
            exc = completed.exception()
            if exc is not None:
                logger.error(
                    "Lỗi tác vụ nền của player guild %s",
                    self.guild.id,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(done)
        return task

    async def _notify(self, embed: discord.Embed) -> None:
        if self._notify_channel_id is None:
            return
        channel = self.guild.get_channel(self._notify_channel_id)
        if channel is None:
            channel = self.guild.get_thread(self._notify_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            logger.warning(
                "Không gửi được thông báo vào kênh %s (guild %s)",
                self._notify_channel_id,
                self.guild.id,
            )

    # ------------------------------------------------------------------
    # Voice helpers
    # ------------------------------------------------------------------
    async def _ensure_voice_locked(self, channel: discord.VoiceChannel) -> None:
        if not isinstance(channel, discord.VoiceChannel):
            raise VoiceError("Bot chỉ có thể tham gia voice channel thường.")
        me = self.guild.me
        if me is None:
            raise VoiceError("Không xác định được bot trong server.")
        permissions = channel.permissions_for(me)
        if not permissions.connect:
            raise VoiceError(f"Bot không có quyền kết nối vào **{channel.name}**.")
        if not permissions.speak:
            raise VoiceError(f"Bot không có quyền nói trong **{channel.name}**.")

        vc = self.guild.voice_client or self._voice
        if vc is not None and vc.is_connected():
            if vc.channel and vc.channel.id == channel.id:
                self._voice = vc
                return
            try:
                await vc.move_to(channel)
            except (
                asyncio.TimeoutError,
                discord.ClientException,
                discord.HTTPException,
            ) as exc:
                raise VoiceError(
                    f"Không thể chuyển bot sang **{channel.name}**."
                ) from exc
            self._voice = vc
            return

        if vc is not None:
            with contextlib.suppress(Exception):
                await vc.disconnect(force=True)
            self._voice = None

        try:
            self._voice = await channel.connect(
                timeout=30,
                reconnect=True,
                self_deaf=True,
            )
        except (asyncio.TimeoutError, discord.ClientException, discord.HTTPException) as exc:
            if isinstance(exc, discord.ClientException) and "already" in str(exc).lower():
                existing = self.guild.voice_client
                if existing is not None:
                    self._voice = existing
                    return
            raise VoiceError(
                f"Không thể kết nối vào **{channel.name}**. "
                "Kiểm tra quyền Connect/Speak của bot."
            ) from exc

    def _make_source(
        self, stream_url: str, *, start_at: float = 0.0
    ) -> discord.PCMVolumeTransformer:
        before = ["-nostdin", "-hide_banner", "-loglevel", "error"]
        if stream_url.startswith(("http://", "https://")):
            before.extend(
                [
                    "-reconnect",
                    "1",
                    "-reconnect_streamed",
                    "1",
                    "-reconnect_delay_max",
                    "5",
                ]
            )
        if start_at > 0:
            before.extend(["-ss", f"{start_at:.3f}"])
        try:
            source = discord.FFmpegPCMAudio(
                stream_url,
                before_options=" ".join(before),
                options="-vn",
                executable=self.config.ffmpeg_path,
            )
        except (OSError, discord.ClientException) as exc:
            raise VoiceError(
                "Không khởi động được FFmpeg. Kiểm tra FFMPEG_PATH và "
                "đã cài FFmpeg chưa."
            ) from exc
        return discord.PCMVolumeTransformer(source, volume=self._volume / 100.0)

    def _schedule_audio_end(self, token: int, error: Exception | None) -> None:
        def schedule() -> None:
            if self._closed:
                return
            self._spawn(self._on_audio_finished(token, error))

        try:
            self._loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            pass

    def _play_source_locked(
        self,
        session: PlaybackSession,
        stream_url: str,
        *,
        start_at: float = 0.0,
        paused: bool = False,
    ) -> None:
        if self._voice is None or not self._voice.is_connected():
            raise VoiceError("Bot chưa kết nối voice channel.")

        source = self._make_source(stream_url, start_at=start_at)
        self._audio_token += 1
        token = self._audio_token
        try:
            self._voice.play(
                source,
                after=lambda err: self._schedule_audio_end(token, err),
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                source.cleanup()
            raise VoiceError("Không thể bắt đầu phát nhạc trong voice channel.") from exc

        self._session = session
        self._paused = paused
        session.finished = False
        session.position_base = max(0.0, start_at)
        if paused:
            session.segment_started_at = None
            with contextlib.suppress(Exception):
                self._voice.pause()
        else:
            session.segment_started_at = time.monotonic()
        self._replace_watcher(session)

    def _replace_watcher(self, session: PlaybackSession) -> None:
        if session.watcher is not None and not session.watcher.done():
            session.watcher.cancel()
        session.watcher = None
        if session.scrobbled or not self.lastfm.can_scrobble:
            return
        session.watcher = self._spawn(self._watch_scrobble(session))

    # ------------------------------------------------------------------
    # Playback flow
    # ------------------------------------------------------------------
    async def enqueue(
        self,
        tracks: list[Track],
        voice_channel: discord.VoiceChannel,
        notify_channel_id: int,
    ) -> EnqueueResult:
        if not tracks:
            raise UserError("Không có bài hát nào để thêm.")
        async with self._lock:
            if self._closed:
                raise UserError("Player đang đóng.")
            if self.queue.room <= 0:
                raise UserError(
                    f"Hàng đợi đã đầy ({self.config.max_queue_size} bài)."
                )

            had_voice = self.is_connected
            await self._ensure_voice_locked(voice_channel)
            self._notify_channel_id = notify_channel_id
            added, rejected = self.queue.extend(tracks)
            if added == 0:
                return EnqueueResult(0, rejected, False, self.current)

            if self._session is not None and had_voice is False:
                await self._resume_or_drop_locked()
            elif self._session is not None and not self._is_audio_running_locked():
                await self._resume_or_drop_locked()

            had_session = self._session is not None
            if self._session is None:
                await self._play_next_locked(announce=False)
            started = not had_session and self._session is not None
            return EnqueueResult(added, rejected, started, self.current)

    def _is_audio_running_locked(self) -> bool:
        if self._voice is None or not self._voice.is_connected():
            return False
        return bool(self._voice.is_playing() or self._voice.is_paused())

    async def connect(self, voice_channel: discord.VoiceChannel) -> None:
        async with self._lock:
            await self._ensure_voice_locked(voice_channel)
            if self._session is not None and not self._is_audio_running_locked():
                await self._resume_or_drop_locked()

    async def _start_new_track_locked(self, track: Track) -> PlaybackSession:
        if self._voice is None or not self._voice.is_connected():
            raise VoiceError("Bot chưa kết nối voice channel.")

        try:
            stream_url = await self.resolver.extract_stream_url(track)
        except ResolverError:
            raise
        except Exception as exc:
            raise ResolverError(
                f"Không lấy được luồng phát cho **{track.title}**."
            ) from exc

        history_id: int | None = None
        try:
            history_id = await self.database.add_history(
                self.guild.id,
                track.requester_id,
                track.title,
                track.artist,
                track.platform,
                track.duration,
            )
        except Exception:
            logger.exception("Không ghi được lịch sử phát cho guild %s", self.guild.id)

        session = PlaybackSession(
            track=track,
            started_at=int(time.time()),
            history_id=history_id,
        )
        try:
            self._play_source_locked(session, stream_url)
        except Exception:
            session.finished = True
            if history_id is not None:
                with contextlib.suppress(Exception):
                    await self.database.finish_history(history_id, 0.0, False)
            self._session = None
            raise

        self._spawn(self._safe_now_playing(session))
        return session

    async def _play_next_locked(self, *, announce: bool) -> None:
        if self._session is not None:
            return
        if self._voice is None or not self._voice.is_connected():
            return

        failures = 0
        while True:
            track = self.queue.pop_next()
            if track is None:
                return
            try:
                await self._start_new_track_locked(track)
            except VoiceError as exc:
                logger.warning("Dừng queue do lỗi voice: %s", exc)
                await self._notify(error_embed(str(exc)))
                return
            except Exception as exc:
                failures += 1
                logger.warning(
                    "Bỏ qua '%s' do lỗi phát: %s", track.title, exc
                )
                if failures <= 3:
                    await self._notify(
                        error_embed(
                            f"Không thể phát **{track.title}**: {exc}"
                        )
                    )
                continue

            if announce:
                embed = self.build_now_playing_embed()
                if embed is not None:
                    await self._notify(embed)
            return

    async def _on_audio_finished(
        self, token: int, error: Exception | None
    ) -> None:
        async with self._lock:
            session = self._session
            if session is None or token != self._audio_token:
                return
            self._audio_token += 1
            session.accumulate()
            self._paused = False
            if error is not None:
                logger.warning(
                    "Luồng phát kết thúc với lỗi (guild %s, '%s'): %s",
                    self.guild.id,
                    session.track.title,
                    error,
                )
            await self._finalize_session_locked(session)
            self._session = None

            if self.loop_mode is LoopMode.TRACK:
                try:
                    await self._start_new_track_locked(session.track)
                except Exception as exc:
                    logger.warning(
                        "Không loop lại được '%s': %s", session.track.title, exc
                    )
                    await self._notify(
                        error_embed(
                            f"Không thể loop lại **{session.track.title}**: {exc}"
                        )
                    )
                else:
                    embed = self.build_now_playing_embed()
                    if embed is not None:
                        await self._notify(embed)
                    return

            if self.loop_mode is LoopMode.QUEUE:
                if not self.queue.add(session.track):
                    logger.warning("Hàng đợi đầy, bỏ qua loop queue một bài.")

            await self._play_next_locked(announce=True)

    async def _finalize_session_locked(
        self, session: PlaybackSession, *, schedule_scrobble: bool = True
    ) -> None:
        session.finished = True
        session.accumulate()
        watcher = session.watcher
        if watcher is not None and not watcher.done():
            if not session.scrobble_lock.locked():
                watcher.cancel()

        if session.history_id is not None:
            try:
                await self.database.finish_history(
                    session.history_id,
                    session.played_seconds,
                    session.scrobbled,
                )
            except Exception:
                logger.exception("Không cập nhật được lịch sử phát.")

        if (
            schedule_scrobble
            and not session.scrobbled
            and self.lastfm.can_scrobble
            and should_scrobble(
                session.track.duration,
                session.played_seconds,
                is_live=session.track.is_live,
            )
        ):
            self._spawn(
                self._retry_scrobble(session, session.played_seconds, attempts=2)
            )

    async def skip(self) -> Track | None:
        async with self._lock:
            session = self._session
            if session is None:
                raise UserError("Hiện không có bài hát nào đang phát.")
            self._audio_token += 1
            if self._voice is not None and self._voice.is_connected():
                with contextlib.suppress(Exception):
                    self._voice.stop()
            session.accumulate()
            await self._finalize_session_locked(session)
            self._session = None
            self._paused = False
            await self._play_next_locked(announce=True)
            return self.current

    async def stop(self) -> int:
        async with self._lock:
            session = self._session
            if session is None and self.queue.is_empty:
                raise UserError("Không có gì đang phát để dừng.")
            self._audio_token += 1
            if self._voice is not None and self._voice.is_connected():
                with contextlib.suppress(Exception):
                    self._voice.stop()
            if session is not None:
                session.accumulate()
                await self._finalize_session_locked(session)
            self._session = None
            self._paused = False
            removed = self.queue.clear()
            return removed

    async def pause(self) -> None:
        async with self._lock:
            if self._session is None:
                raise UserError("Hiện không có bài hát nào đang phát.")
            if self._voice is None or not self._voice.is_connected():
                raise VoiceError("Bot chưa kết nối voice channel.")
            if self._paused:
                raise UserError("Nhạc đang tạm dừng rồi.")
            self._voice.pause()
            self._session.accumulate()
            self._paused = True

    async def resume(self) -> None:
        async with self._lock:
            if self._session is None:
                raise UserError("Hiện không có bài hát nào đang phát.")
            if self._voice is None or not self._voice.is_connected():
                raise VoiceError("Bot chưa kết nối voice channel.")
            if not self._paused:
                raise UserError("Nhạc đang phát, không bị tạm dừng.")
            self._voice.resume()
            if self._session.segment_started_at is None:
                self._session.segment_started_at = time.monotonic()
            self._paused = False

    async def seek(self, seconds: int) -> int:
        async with self._lock:
            session = self._session
            if session is None:
                raise UserError("Hiện không có bài hát nào đang phát.")
            if self._voice is None or not self._voice.is_connected():
                raise VoiceError("Bot chưa kết nối voice channel.")
            if session.track.is_live:
                raise UserError("Không thể tua một luồng live.")
            if seconds < 0:
                raise UserError("Vị trí tua không hợp lệ.")
            if (
                session.track.duration
                and seconds >= session.track.duration
            ):
                raise UserError(
                    f"Bài hát chỉ dài {session.track.duration_text}."
                )

            was_paused = self._paused
            session.accumulate()
            self._audio_token += 1
            with contextlib.suppress(Exception):
                self._voice.stop()

            try:
                stream_url = await self.resolver.extract_stream_url(
                    session.track
                )
                self._play_source_locked(
                    session,
                    stream_url,
                    start_at=float(seconds),
                    paused=was_paused,
                )
            except Exception:
                logger.exception("Tua nhạc thất bại.")
                raise
            return seconds

    async def _resume_or_drop_locked(self) -> None:
        session = self._session
        if session is None:
            return
        try:
            await self._restart_session_locked()
        except Exception as exc:
            logger.warning("Không phát lại được bài hiện tại: %s", exc)
            await self._finalize_session_locked(session)
            self._session = None
            self._paused = False
            await self._notify(
                error_embed(
                    f"Không thể tiếp tục **{session.track.title}**: {exc}"
                )
            )
            await self._play_next_locked(announce=True)

    async def _restart_session_locked(self) -> None:
        session = self._session
        if session is None:
            raise VoiceError("Không có bài hát để phát lại.")
        if self._voice is None or not self._voice.is_connected():
            raise VoiceError("Bot chưa kết nối voice channel.")

        session.accumulate()
        start_at = session.position()
        if (
            session.track.duration
            and start_at >= max(0, session.track.duration - 1)
        ):
            await self._finalize_session_locked(session)
            self._session = None
            self._paused = False
            return

        stream_url = await self.resolver.extract_stream_url(session.track)
        self._play_source_locked(
            session,
            stream_url,
            start_at=start_at,
            paused=self._paused,
        )
        self._spawn(self._safe_now_playing(session))

    async def set_loop(self, mode: LoopMode) -> LoopMode:
        async with self._lock:
            self.loop_mode = mode
            return self.loop_mode

    async def set_volume(self, volume: int) -> int:
        async with self._lock:
            self._volume = max(0, min(200, volume))
            source = self._voice.source if self._voice else None
            if isinstance(source, discord.PCMVolumeTransformer):
                source.volume = self._volume / 100.0
            return self._volume

    async def remove(self, index: int) -> Track:
        async with self._lock:
            return self.queue.remove(index)

    async def clear_queue(self) -> int:
        async with self._lock:
            return self.queue.clear()

    async def shuffle(self) -> int:
        async with self._lock:
            return self.queue.shuffle()

    # ------------------------------------------------------------------
    # Last.fm
    # ------------------------------------------------------------------
    async def _safe_now_playing(self, session: PlaybackSession) -> None:
        if not self.lastfm.can_scrobble:
            return
        try:
            await self.lastfm.now_playing(session.track)
        except LastfmError as exc:
            logger.warning("Không cập nhật được Last.fm now playing: %s", exc)
        except Exception:
            logger.exception("Lỗi Last.fm now playing.")

    async def _watch_scrobble(self, session: PlaybackSession) -> None:
        try:
            while not self._closed and not session.scrobbled:
                if session.finished:
                    return
                played = session.played()
                if should_scrobble(
                    session.track.duration,
                    played,
                    is_live=session.track.is_live,
                ):
                    success = await self._try_scrobble(session, played)
                    if success or session.scrobbled:
                        return
                    if session.scrobble_attempts >= 3:
                        logger.warning(
                            "Đã thử scrobble 3 lần nhưng thất bại: %s - %s",
                            session.track.artist,
                            session.track.title,
                        )
                        return
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Lỗi vòng theo dõi scrobble.")

    async def _try_scrobble(
        self, session: PlaybackSession, played_seconds: float
    ) -> bool:
        if not self.lastfm.can_scrobble:
            return False
        if session.scrobbled:
            return True
        if not should_scrobble(
            session.track.duration,
            played_seconds,
            is_live=session.track.is_live,
        ):
            return False

        async with session.scrobble_lock:
            if session.scrobbled:
                return True
            if not should_scrobble(
                session.track.duration,
                played_seconds,
                is_live=session.track.is_live,
            ):
                return False
            session.scrobble_attempts += 1
            try:
                await self.lastfm.scrobble(
                    session.track, session.started_at
                )
            except LastfmError as exc:
                logger.warning(
                    "Scrobble thất bại cho '%s - %s': %s",
                    session.track.artist,
                    session.track.title,
                    exc,
                )
                return False
            except Exception:
                logger.exception("Lỗi scrobble không xác định.")
                return False

            session.scrobbled = True
            if session.history_id is not None:
                try:
                    await self.database.mark_scrobbled(session.history_id)
                except Exception:
                    logger.exception("Không cập nhật được trạng thái scrobble.")
            logger.info(
                "Đã scrobble: %s - %s",
                session.track.artist,
                session.track.title,
            )
            return True

    async def _retry_scrobble(
        self, session: PlaybackSession, played_seconds: float, attempts: int = 3
    ) -> bool:
        for attempt in range(max(1, attempts)):
            if session.scrobbled:
                return True
            if await self._try_scrobble(session, played_seconds):
                return True
            if attempt + 1 < attempts:
                await asyncio.sleep(2 * (attempt + 1))
        return False

    # ------------------------------------------------------------------
    # 24/7
    # ------------------------------------------------------------------
    async def set_247(
        self, enabled: bool, channel: discord.VoiceChannel | None
    ) -> None:
        async with self._lock:
            if enabled:
                if channel is None:
                    raise UserError("Bạn cần ở trong một voice channel để bật 24/7.")
                await self._ensure_voice_locked(channel)
                if self._session is not None and not self._is_audio_running_locked():
                    await self._resume_or_drop_locked()
                try:
                    await self.database.set_247(
                        self.guild.id, True, channel.id
                    )
                except Exception as exc:
                    raise UserError(
                        "Không lưu được trạng thái 24/7 vào database."
                    ) from exc
                self.auto_247 = True
                self.desired_channel_id = channel.id
            else:
                if self._voice is not None and self._voice.channel:
                    desired_channel_id = self._voice.channel.id
                else:
                    desired_channel_id = self.desired_channel_id
                try:
                    await self.database.set_247(
                        self.guild.id, False, desired_channel_id
                    )
                except Exception as exc:
                    raise UserError(
                        "Không lưu được trạng thái 24/7 vào database."
                    ) from exc
                self.auto_247 = False
                self.desired_channel_id = desired_channel_id
                task = self._reconnect_task
                self._reconnect_task = None
                if task is not None and not task.done():
                    task.cancel()

    async def start_247_after_restart(self) -> None:
        if not self.auto_247 or self.desired_channel_id is None:
            return
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._closed or not self.auto_247:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = self._spawn(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = 2
        max_delay = self.config.reconnect_max_delay_seconds
        try:
            while not self._closed and self.auto_247:
                if self._voice is not None and self._voice.is_connected():
                    return
                if self.desired_channel_id is None:
                    logger.warning("24/7 bật nhưng chưa có voice channel.")
                    return
                channel = self.guild.get_channel(self.desired_channel_id)
                if not isinstance(channel, discord.VoiceChannel):
                    logger.warning(
                        "Không tìm thấy voice channel 24/7 %s (guild %s)",
                        self.desired_channel_id,
                        self.guild.id,
                    )
                    await asyncio.sleep(delay)
                    delay = min(max_delay, max(10, delay * 2))
                    continue
                try:
                    async with self._lock:
                        if self._closed or not self.auto_247:
                            return
                        if (
                            self._voice is not None
                            and self._voice.is_connected()
                        ):
                            return
                        await self._ensure_voice_locked(channel)
                        if self._session is not None:
                            try:
                                await self._restart_session_locked()
                            except Exception as exc:
                                logger.warning(
                                    "Không phát lại được bài sau reconnect: %s",
                                    exc,
                                )
                                await self._finalize_session_locked(
                                    self._session
                                )
                                self._session = None
                                self._paused = False
                                await self._play_next_locked(announce=True)
                            else:
                                if self._session is None:
                                    await self._play_next_locked(announce=True)
                        else:
                            await self._play_next_locked(announce=True)
                    logger.info(
                        "Đã kết nối lại 24/7 vào %s (guild %s)",
                        channel.name,
                        self.guild.id,
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Reconnect 24/7 thất bại (guild %s): %s; thử lại sau %ss",
                        self.guild.id,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(max_delay, delay * 2)
        finally:
            if self._reconnect_task is asyncio.current_task():
                self._reconnect_task = None

    async def handle_voice_move(self, channel: discord.VoiceChannel) -> None:
        self._voice = self.guild.voice_client or self._voice
        if not self.auto_247:
            return
        if self.desired_channel_id == channel.id:
            return
        self.desired_channel_id = channel.id
        try:
            await self.database.update_247_channel(self.guild.id, channel.id)
        except Exception:
            logger.exception("Không lưu được voice channel 24/7 mới.")

    async def handle_voice_disconnect(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._voice = None
            if self._session is not None:
                self._session.accumulate()
            self._audio_token += 1
            if self.auto_247:
                logger.info(
                    "Bot mất kết nối voice (guild %s); bật reconnect 24/7.",
                    self.guild.id,
                )
                self._schedule_reconnect()
                return

            if self._session is not None:
                await self._finalize_session_locked(self._session)
                self._session = None
            self._paused = False
            self.queue.clear()

    async def leave(self) -> None:
        async with self._lock:
            self.auto_247 = False
            vc = self.guild.voice_client or self._voice
            if vc is not None and getattr(vc, "channel", None) is not None:
                self.desired_channel_id = vc.channel.id
            task = self._reconnect_task
            self._reconnect_task = None
            if task is not None and not task.done():
                task.cancel()
            try:
                await self.database.set_247(
                    self.guild.id, False, self.desired_channel_id
                )
            except Exception:
                logger.exception("Không lưu được trạng thái tắt 24/7.")

            self._audio_token += 1
            if self._session is not None:
                self._session.accumulate()
                await self._finalize_session_locked(self._session)
            self._session = None
            self._paused = False
            self.queue.clear()

            if vc is not None:
                with contextlib.suppress(Exception):
                    await vc.disconnect(force=True)
            self._voice = None

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._reconnect_task
        self._reconnect_task = None
        if task is not None and not task.done():
            task.cancel()

        async with self._lock:
            session = self._session
            self._audio_token += 1
            if self._voice is not None and self._voice.is_connected():
                with contextlib.suppress(Exception):
                    self._voice.stop()
            if session is not None:
                session.accumulate()
                await self._finalize_session_locked(
                    session, schedule_scrobble=False
                )
                if (
                    not session.scrobbled
                    and self.lastfm.can_scrobble
                    and should_scrobble(
                        session.track.duration,
                        session.played_seconds,
                        is_live=session.track.is_live,
                    )
                ):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            self._retry_scrobble(
                                session, session.played_seconds, attempts=1
                            ),
                            timeout=4,
                        )
            self._session = None
            self._paused = False
            vc = self.guild.voice_client or self._voice
            if vc is not None:
                with contextlib.suppress(Exception):
                    await vc.disconnect(force=True)
            self._voice = None

        for background in list(self._background):
            if not background.done():
                background.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

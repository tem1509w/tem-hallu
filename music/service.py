from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from config import Config
from .lastfm import LastfmClient
from .player import GuildPlayer
from .resolver import MusicResolver
from .storage import Database

logger = logging.getLogger(__name__)


class MusicService:
    def __init__(
        self,
        bot: commands.Bot,
        config: Config,
        database: Database,
        lastfm: LastfmClient,
        resolver: MusicResolver,
    ) -> None:
        self.bot = bot
        self.config = config
        self.database = database
        self.lastfm = lastfm
        self.resolver = resolver
        self._players: dict[int, GuildPlayer] = {}
        self._lock = asyncio.Lock()

    async def get_player(self, guild: discord.Guild) -> GuildPlayer:
        existing = self._players.get(guild.id)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self._players.get(guild.id)
            if existing is not None:
                return existing
            settings = await self.database.get_settings(guild.id)
            player = GuildPlayer(
                self.bot,
                guild,
                self.config,
                self.database,
                self.lastfm,
                self.resolver,
                settings,
            )
            self._players[guild.id] = player
            return player

    def get_existing(self, guild_id: int) -> GuildPlayer | None:
        return self._players.get(guild_id)

    async def forget_guild(self, guild_id: int) -> None:
        player = self._players.pop(guild_id, None)
        if player is not None:
            await player.shutdown()

    async def restore_247(self) -> None:
        rows = await self.database.list_enabled_247()
        if not rows:
            return
        restored = 0
        for guild_id, channel_id in rows:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                logger.warning("Không còn ở guild 24/7 %s", guild_id)
                continue
            if guild.get_channel(channel_id) is None:
                logger.warning(
                    "Không tìm thấy voice channel 24/7 %s (guild %s)",
                    channel_id,
                    guild_id,
                )
            player = await self.get_player(guild)
            player.auto_247 = True
            player.desired_channel_id = channel_id
            await player.start_247_after_restart()
            restored += 1
        if restored:
            logger.info("Đã khôi phục %s phiên 24/7.", restored)

    async def handle_voice_state(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.bot.user is None or member.id != self.bot.user.id:
            return
        player = self.get_existing(member.guild.id)
        if player is None:
            return
        if after.channel is not None:
            if isinstance(after.channel, discord.VoiceChannel):
                await player.handle_voice_move(after.channel)
            return
        await player.handle_voice_disconnect()

    async def close(self) -> None:
        players = list(self._players.values())
        self._players.clear()
        if not players:
            return
        results = await asyncio.gather(
            *(player.shutdown() for player in players),
            return_exceptions=True,
        )
        for player, result in zip(players, results):
            if isinstance(result, Exception):
                logger.error(
                    "Lỗi đóng player guild %s",
                    player.guild.id,
                    exc_info=(
                        type(result),
                        result,
                        result.__traceback__,
                    ),
                )

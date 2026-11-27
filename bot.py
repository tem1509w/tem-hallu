from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs.music import MusicCog
from cogs.stats import StatsCog
from config import Config
from music.embeds import error_embed
from music.errors import UserError
from music.lastfm import LastfmClient
from music.resolver import MusicResolver
from music.service import MusicService
from music.spotify import SpotifyClient
from music.storage import Database

logger = logging.getLogger(__name__)


def _unwrap(error: BaseException) -> BaseException:
    return getattr(error, "original", error)


def _error_message(error: BaseException) -> str:
    original = _unwrap(error)
    if isinstance(original, UserError):
        return str(original)
    if isinstance(original, commands.MissingPermissions):
        return "Bạn không có quyền sử dụng lệnh này."
    if isinstance(original, commands.BotMissingPermissions):
        return (
            "Bot thiếu quyền: "
            + ", ".join(original.missing_permissions)
        )
    if isinstance(original, commands.NoPrivateMessage):
        return "Lệnh này chỉ dùng được trong server."
    if isinstance(original, commands.MissingRequiredArgument):
        return f"Thiếu tham số `{original.param.name}`."
    if isinstance(original, commands.BadArgument):
        return "Tham số không hợp lệ."
    if isinstance(original, commands.CommandOnCooldown):
        return f"Thử lại sau {original.retry_after:.1f} giây."
    if isinstance(original, commands.CheckFailure):
        return "Bạn không đủ điều kiện để dùng lệnh này."
    if isinstance(original, discord.Forbidden):
        return "Bot không có quyền gửi tin nhắn hoặc embed ở kênh này."
    return "Đã xảy ra lỗi khi xử lý lệnh. Xem log của bot để biết chi tiết."


class MusicBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True

        super().__init__(
            command_prefix=commands.when_mentioned_or(config.prefix),
            intents=intents,
            help_command=None,
            case_insensitive=True,
            strip_after_prefix=True,
            allowed_mentions=discord.AllowedMentions.none(),
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="/play • /247",
            ),
            status=discord.Status.online,
        )
        self.config = config
        self.database = Database(config.database_path)
        self.lastfm = LastfmClient(config)
        self.spotify = SpotifyClient(config)
        self.resolver = MusicResolver(config, self.spotify)
        self.service = MusicService(
            self,
            config,
            self.database,
            self.lastfm,
            self.resolver,
        )
        self._startup_done = False
        self._closing = False

    async def setup_hook(self) -> None:
        await self.database.initialize()
        await self.add_cog(MusicCog(self))
        await self.add_cog(StatsCog(self))
        self.tree.on_error = self._on_app_command_error
        logger.info("Đã nạp %s slash command.", len(self.tree.get_commands()))

    async def on_ready(self) -> None:
        if self.user is not None:
            logger.info("Bot online: %s (%s)", self.user, self.user.id)
        if self._startup_done:
            return
        self._startup_done = True
        try:
            await self._sync_commands()
        except Exception:
            logger.exception("Đồng bộ slash command thất bại.")
        try:
            await self.service.restore_247()
        except Exception:
            logger.exception("Khôi phục 24/7 thất bại.")

    async def _sync_commands(self) -> None:
        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(
                "Đã đồng bộ %s command vào dev guild %s.",
                len(synced),
                self.config.dev_guild_id,
            )
            return

        synced = await self.tree.sync()
        logger.info("Đã đồng bộ %s slash command toàn cầu.", len(synced))

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.user is None or member.id != self.user.id:
            return
        try:
            await self.service.handle_voice_state(member, before, after)
        except Exception:
            logger.exception(
                "Lỗi xử lý voice state (guild %s)", member.guild.id
            )

    async def on_command_error(
        self, ctx: commands.Context, error: BaseException
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        original = _unwrap(error)
        if not isinstance(original, UserError):
            logger.error(
                "Lỗi command '%s' (guild %s): %s",
                getattr(ctx.command, "qualified_name", "?"),
                ctx.guild.id if ctx.guild else "DM",
                original,
                exc_info=(
                    type(original),
                    original,
                    original.__traceback__,
                ),
            )
        message = _error_message(error)
        try:
            if ctx.interaction and not ctx.interaction.response.is_done():
                await ctx.interaction.response.send_message(
                    embed=error_embed(message), ephemeral=True
                )
            else:
                await ctx.send(embed=error_embed(message))
        except discord.HTTPException:
            logger.debug("Không gửi được báo lỗi command.", exc_info=True)

    async def _on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        original = _unwrap(error)
        if not isinstance(original, UserError):
            logger.error(
                "Lỗi slash command '%s' (guild %s): %s",
                getattr(interaction.command, "qualified_name", "?"),
                interaction.guild_id,
                original,
                exc_info=(
                    type(original),
                    original,
                    original.__traceback__,
                ),
            )
        message = _error_message(error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=error_embed(message), ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    embed=error_embed(message), ephemeral=True
                )
        except discord.HTTPException:
            logger.debug("Không gửi được báo lỗi interaction.", exc_info=True)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        try:
            await self.service.forget_guild(guild.id)
        except Exception:
            logger.exception("Không đóng được player guild %s", guild.id)

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            await self.service.close()
            await self.lastfm.close()
            await self.spotify.close()
            await self.database.close()
        except Exception:
            logger.exception("Lỗi dọn dẹp tài nguyên bot.")
        finally:
            await super().close()

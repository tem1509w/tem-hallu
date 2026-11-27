from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord.ext import commands

from music.embeds import base_embed, history_embed
from music.errors import LastfmError
from music.utils import (
    format_listen_time,
    format_number,
    truncate,
)
from .helpers import defer_interaction, require_guild, send_embed

if TYPE_CHECKING:
    from bot import MusicBot

logger = logging.getLogger(__name__)


class StatsCog(commands.Cog, name="Stats"):
    def __init__(self, bot: "MusicBot") -> None:
        self.bot = bot
        self.config = bot.config
        self.database = bot.database
        self.lastfm = bot.lastfm

    @staticmethod
    def _top_tracks_text(rows: list[dict[str, object]]) -> str:
        if not rows:
            return "Chưa có dữ liệu."
        lines = []
        for index, row in enumerate(rows, start=1):
            title = truncate(str(row.get("title") or ""), 68)
            artist = truncate(str(row.get("artist") or "Không rõ"), 40)
            plays = int(row.get("plays") or 0)
            lines.append(f"`{index}.` **{title}** — {artist} · {plays} lượt")
        return "\n".join(lines)

    async def _fetch_lastfm_profile(self, ctx: commands.Context):
        if not self.lastfm.can_query:
            return None, None
        try:
            if ctx.interaction:
                return await self.lastfm.get_user_info(), None
            async with ctx.typing():
                return await self.lastfm.get_user_info(), None
        except LastfmError as exc:
            return None, str(exc)
        except Exception:
            logger.exception("Lỗi lấy Last.fm user info.")
            return None, "Không lấy được dữ liệu Last.fm."

    @commands.hybrid_command(
        name="lastfm",
        aliases=["fm"],
        description="Xem playcount Last.fm và thống kê scrobble.",
    )
    async def lastfm_command(self, ctx: commands.Context) -> None:
        guild = require_guild(ctx)
        if ctx.interaction:
            await defer_interaction(ctx)
        stats = await self.database.get_guild_stats(guild.id)
        user_info, remote_error = await self._fetch_lastfm_profile(ctx)

        profile_url = self.lastfm.profile_url
        if isinstance(user_info, dict):
            profile_url = str(user_info.get("url") or profile_url or "") or None

        embed = base_embed(
            title=(
                f"🎧 Last.fm • {self.lastfm.username}"
                if self.lastfm.username
                else "🎧 Thống kê nghe nhạc"
            ),
            color=self.config.embed_color,
        )
        if profile_url:
            embed.url = profile_url

        if isinstance(user_info, dict):
            playcount = user_info.get("playcount")
            try:
                total_scrobbles = format_number(int(playcount))
            except (TypeError, ValueError):
                total_scrobbles = str(playcount or "Không rõ")
        else:
            total_scrobbles = "Không truy vấn"

        embed.add_field(
            name="Tổng scrobble Last.fm",
            value=f"**{total_scrobbles}**",
            inline=True,
        )
        embed.add_field(
            name="Lượt phát trên server",
            value=f"**{format_number(stats.total_plays)}**",
            inline=True,
        )
        embed.add_field(
            name="Đã scrobble từ bot",
            value=f"**{format_number(stats.scrobbled_plays)}**",
            inline=True,
        )
        embed.add_field(
            name="Thời gian nghe",
            value=format_listen_time(stats.listen_seconds),
            inline=True,
        )
        embed.add_field(
            name="Scrobbling",
            value=(
                "Đang bật"
                if self.lastfm.can_scrobble
                else "Chưa cấu hình đầy đủ"
            ),
            inline=True,
        )
        embed.add_field(
            name="Server",
            value=guild.name,
            inline=True,
        )
        embed.add_field(
            name="Top bài hát",
            value=self._top_tracks_text(stats.top_tracks),
            inline=False,
        )

        if remote_error:
            embed.add_field(
                name="Lỗi Last.fm",
                value=truncate(remote_error, 300),
                inline=False,
            )
        if not self.lastfm.can_query:
            embed.add_field(
                name="Chưa cấu hình Last.fm",
                value=(
                    "Đặt `LASTFM_API_KEY` và `LASTFM_USERNAME` trong `.env` để "
                    "xem playcount. Để scrobble, cần thêm API secret và session key."
                ),
                inline=False,
            )
        if not self.lastfm.can_scrobble:
            embed.add_field(
                name="Scrobble chưa bật",
                value=(
                    "Cần `LASTFM_API_SECRET` + `LASTFM_SESSION_KEY`. Chạy "
                    "`python scripts/lastfm_auth.py` để lấy session key."
                ),
                inline=False,
            )

        embed.set_footer(
            text=(
                "Scrobble khi đã nghe ≥ 1/2 bài hoặc 4 phút; "
                "bài dưới 30 giây/live không scrobble."
            )
        )
        await send_embed(ctx, embed)

    @commands.hybrid_command(
        name="stats",
        description="Thống kê lượt phát và top bài hát của server.",
    )
    async def stats_command(self, ctx: commands.Context) -> None:
        guild = require_guild(ctx)
        stats = await self.database.get_guild_stats(guild.id)
        embed = base_embed(
            title="📊 Thống kê nhạc của server",
            description=guild.name,
            color=self.config.embed_color,
        )
        embed.add_field(
            name="Tổng lượt phát",
            value=f"**{format_number(stats.total_plays)}**",
            inline=True,
        )
        embed.add_field(
            name="Scrobble thành công",
            value=f"**{format_number(stats.scrobbled_plays)}**",
            inline=True,
        )
        embed.add_field(
            name="Tổng thời gian nghe",
            value=format_listen_time(stats.listen_seconds),
            inline=False,
        )
        embed.add_field(
            name="Top bài hát",
            value=self._top_tracks_text(stats.top_tracks),
            inline=False,
        )
        await send_embed(ctx, embed)

    @commands.hybrid_command(
        name="history",
        description="Xem lịch sử phát nhạc gần đây của server.",
    )
    async def history_command(self, ctx: commands.Context) -> None:
        guild = require_guild(ctx)
        rows = await self.database.recent_history(guild.id, limit=10)
        await send_embed(ctx, history_embed(rows, color=self.config.embed_color))


async def setup(bot: "MusicBot") -> None:
    await bot.add_cog(StatsCog(bot))

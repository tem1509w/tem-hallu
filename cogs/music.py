from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from music.embeds import base_embed, queue_embed, success_embed
from music.errors import UserError
from music.models import Resolution
from music.queue import LoopMode
from music.utils import format_duration, format_number, parse_timestamp, truncate
from .helpers import defer_interaction, require_guild, send_embed

if TYPE_CHECKING:
    from bot import MusicBot

logger = logging.getLogger(__name__)


class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: "MusicBot") -> None:
        self.bot = bot
        self.config = bot.config
        self.service = bot.service
        self.lastfm = bot.lastfm

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _check_voice_permissions(self, channel: discord.VoiceChannel) -> None:
        guild = channel.guild
        me = guild.me
        if me is None:
            raise UserError("Không xác định được bot trong server.")
        permissions = channel.permissions_for(me)
        missing = []
        if not permissions.connect:
            missing.append("Connect")
        if not permissions.speak:
            missing.append("Speak")
        if missing:
            raise UserError(
                f"Bot thiếu quyền **{', '.join(missing)}** trong "
                f"**{channel.name}**."
            )

    async def _get_player(self, ctx: commands.Context):
        guild = require_guild(ctx)
        return await self.service.get_player(guild)

    async def _get_voice_player(self, ctx: commands.Context):
        require_guild(ctx)
        member = ctx.author
        state = getattr(member, "voice", None)
        if state is None or state.channel is None:
            raise UserError("Bạn cần tham gia một voice channel trước.")
        channel = state.channel
        if not isinstance(channel, discord.VoiceChannel):
            raise UserError("Bot chỉ hỗ trợ voice channel thường.")
        self._check_voice_permissions(channel)
        player = await self._get_player(ctx)
        return player, channel

    async def _control_player(self, ctx: commands.Context):
        player, channel = await self._get_voice_player(ctx)
        if not player.is_connected or player.voice is None:
            raise UserError("Bot chưa ở trong voice channel.")
        if (
            player.voice.channel
            and player.voice.channel.id != channel.id
            and not ctx.author.guild_permissions.manage_guild
        ):
            raise UserError("Bạn cần ở cùng voice channel với bot để điều khiển.")
        return player

    async def _resolve(
        self, ctx: commands.Context, query: str, *, playlist: bool
    ) -> Resolution:
        async def work() -> Resolution:
            return await self.service.resolver.resolve(
                query,
                requester_id=ctx.author.id,
                requester_name=ctx.author.display_name,
                playlist=playlist,
            )

        if ctx.interaction:
            await defer_interaction(ctx)
            return await work()
        async with ctx.typing():
            return await work()

    def _check_247_conflict(
        self, player, channel: discord.VoiceChannel
    ) -> None:
        if (
            player.auto_247
            and player.voice is not None
            and player.voice.channel is not None
            and player.voice.channel.id != channel.id
        ):
            raise UserError(
                f"Bot đang bật 24/7 tại **{player.voice.channel.name}**. "
                "Hãy vào kênh đó, hoặc tắt 24/7 trước khi chuyển kênh."
            )

    @staticmethod
    def _append_playlist_note(
        embed: discord.Embed,
        resolution: Resolution,
        added: int,
        rejected: int,
    ) -> None:
        notes = [f"Đã thêm **{added}** bài"]
        if resolution.title:
            notes[0] += f" từ **{truncate(resolution.title, 120)}**"
        if resolution.truncated:
            notes.append("Playlist đã được cắt bớt theo giới hạn cấu hình.")
        if resolution.failed:
            notes.append(f"Bỏ qua {resolution.failed} bài không ghép được.")
        if rejected:
            notes.append(f"Bỏ qua {rejected} bài vì hàng đợi đầy.")
        embed.add_field(name="Playlist", value="\n".join(notes), inline=False)

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------
    @commands.hybrid_command(
        name="play",
        aliases=["p"],
        description="Phát nhạc từ YouTube, SoundCloud, Spotify, Bandcamp...",
    )
    @app_commands.describe(
        query="Tên bài hát hoặc link (YouTube, SoundCloud, Spotify, Bandcamp...)"
    )
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        player, channel = await self._get_voice_player(ctx)
        self._check_247_conflict(player, channel)

        resolution = await self._resolve(ctx, query, playlist=False)
        if not resolution.tracks:
            raise UserError("Không tìm thấy bài hát phù hợp.")

        result = await player.enqueue(
            resolution.tracks,
            channel,
            ctx.channel.id,
        )
        if player.current is None:
            raise UserError(
                "Không thể phát bài này. Hãy thử link hoặc từ khóa khác."
            )

        if result.started:
            embed = player.build_now_playing_embed()
            if embed is None:
                embed = success_embed("Đang phát nhạc.", self.config.embed_color)
            if result.rejected:
                embed.set_footer(
                    text=f"Bỏ qua {result.rejected} bài vì hàng đợi đầy."
                )
            await send_embed(ctx, embed)
            return

        track = resolution.tracks[0]
        embed = base_embed(
            title="📥 Đã thêm vào hàng đợi",
            description=f"**{truncate(track.title, 150)}**\n{track.artist_text}",
            color=self.config.embed_color,
        )
        if track.page_url.startswith(("http://", "https://")):
            embed.url = track.page_url
        embed.add_field(name="Nguồn", value=track.platform, inline=True)
        embed.add_field(name="Thời lượng", value=track.duration_text, inline=True)
        embed.add_field(
            name="Vị trí",
            value=f"#{format_number(max(1, len(player.queue)))}",
            inline=True,
        )
        if result.rejected:
            embed.add_field(
                name="Giới hạn",
                value=f"Bỏ qua {result.rejected} bài vì hàng đợi đầy.",
                inline=False,
            )
        await send_embed(ctx, embed)

    @commands.hybrid_command(
        name="playlist",
        aliases=["pl"],
        description="Thêm playlist/album từ YouTube, SoundCloud, Spotify...",
    )
    @app_commands.describe(
        query="Link playlist hoặc album (YouTube, SoundCloud, Bandcamp, Spotify...)"
    )
    async def playlist(self, ctx: commands.Context, *, query: str) -> None:
        player, channel = await self._get_voice_player(ctx)
        self._check_247_conflict(player, channel)

        resolution = await self._resolve(ctx, query, playlist=True)
        if not resolution.tracks:
            raise UserError("Không tìm thấy bài hát nào trong playlist.")

        result = await player.enqueue(
            resolution.tracks,
            channel,
            ctx.channel.id,
        )
        if player.current is None:
            raise UserError("Không thể phát playlist này.")

        if result.started:
            embed = player.build_now_playing_embed()
            if embed is None:
                embed = success_embed("Đang phát playlist.", self.config.embed_color)
            self._append_playlist_note(
                embed, resolution, result.added, result.rejected
            )
            await send_embed(ctx, embed)
            return

        embed = base_embed(
            title="📥 Đã thêm playlist",
            description=(
                f"**{truncate(resolution.title or 'Playlist', 180)}**\n"
                f"Đã xếp {result.added} bài vào hàng đợi."
            ),
            color=self.config.embed_color,
        )
        if result.rejected:
            embed.add_field(
                name="Giới hạn",
                value=f"Bỏ qua {result.rejected} bài vì hàng đợi đầy.",
                inline=False,
            )
        if resolution.truncated:
            embed.add_field(
                name="Cắt bớt",
                value="Playlist dài hơn giới hạn cấu hình nên đã được rút gọn.",
                inline=False,
            )
        if resolution.failed:
            embed.add_field(
                name="Bỏ qua",
                value=f"{resolution.failed} bài không ghép được nguồn phát.",
                inline=False,
            )
        await send_embed(ctx, embed)

    @commands.hybrid_command(name="pause", description="Tạm dừng nhạc.")
    async def pause(self, ctx: commands.Context) -> None:
        player = await self._control_player(ctx)
        await player.pause()
        await send_embed(
            ctx, success_embed("Đã tạm dừng nhạc.", self.config.embed_color)
        )

    @commands.hybrid_command(name="resume", description="Tiếp tục phát nhạc.")
    async def resume(self, ctx: commands.Context) -> None:
        player = await self._control_player(ctx)
        await player.resume()
        await send_embed(
            ctx, success_embed("Đã tiếp tục phát nhạc.", self.config.embed_color)
        )

    @commands.hybrid_command(name="skip", aliases=["next"], description="Bỏ qua bài hiện tại.")
    async def skip(self, ctx: commands.Context) -> None:
        player = await self._control_player(ctx)
        next_track = await player.skip()
        if next_track is None:
            await send_embed(
                ctx,
                success_embed(
                    "Đã bỏ qua bài cuối cùng. Hàng đợi đã hết.",
                    self.config.embed_color,
                ),
            )
        else:
            await send_embed(
                ctx,
                success_embed(
                    f"Đã bỏ qua. Đang phát **{truncate(next_track.title, 120)}**.",
                    self.config.embed_color,
                ),
            )

    @commands.hybrid_command(name="stop", description="Dừng phát và xóa hàng đợi.")
    async def stop(self, ctx: commands.Context) -> None:
        player = await self._control_player(ctx)
        removed = await player.stop()
        text = "Đã dừng phát nhạc."
        if removed:
            text += f" Đã xóa {removed} bài khỏi hàng đợi."
        await send_embed(ctx, success_embed(text, self.config.embed_color))

    @commands.hybrid_command(
        name="nowplaying",
        aliases=["np"],
        description="Xem bài đang phát và tiến độ.",
    )
    async def nowplaying(self, ctx: commands.Context) -> None:
        player = await self._get_player(ctx)
        embed = player.build_now_playing_embed()
        if embed is None:
            raise UserError("Hiện không có bài hát nào đang phát.")
        await send_embed(ctx, embed)

    # ------------------------------------------------------------------
    # Queue / controls
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="queue", aliases=["q"], description="Xem hàng đợi.")
    @app_commands.describe(page="Số trang hàng đợi")
    async def queue(self, ctx: commands.Context, page: int = 1) -> None:
        player = await self._get_player(ctx)
        if page < 1:
            raise UserError("Số trang phải lớn hơn 0.")
        embed = queue_embed(
            color=self.config.embed_color,
            current=player.current,
            upcoming=player.queue.snapshot(),
            page=page,
            page_size=self.config.queue_page_size,
            loop_label=player.loop_mode.label,
            volume=player.volume,
        )
        await send_embed(ctx, embed)

    @commands.hybrid_command(
        name="loop", description="Xem hoặc đổi chế độ lặp."
    )
    @app_commands.describe(mode="off = tắt, track = lặp một bài, queue = lặp hàng đợi")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Tắt", value="off"),
            app_commands.Choice(name="Một bài", value="track"),
            app_commands.Choice(name="Cả hàng đợi", value="queue"),
        ]
    )
    async def loop(
        self, ctx: commands.Context, mode: str | None = None
    ) -> None:
        player = await self._get_player(ctx)
        if mode is None:
            await send_embed(
                ctx,
                success_embed(
                    f"Loop hiện tại: **{player.loop_mode.label}**.",
                    self.config.embed_color,
                ),
            )
            return
        player = await self._control_player(ctx)
        loop_mode = LoopMode.parse(mode)
        await player.set_loop(loop_mode)
        await send_embed(
            ctx,
            success_embed(
                f"Đã đặt loop: **{loop_mode.label}**.",
                self.config.embed_color,
            ),
        )

    @commands.hybrid_command(name="shuffle", description="Xáo trộn hàng đợi.")
    async def shuffle(self, ctx: commands.Context) -> None:
        player = await self._control_player(ctx)
        count = await player.shuffle()
        await send_embed(
            ctx,
            success_embed(
                f"Đã xáo trộn {count} bài trong hàng đợi.",
                self.config.embed_color,
            ),
        )

    @commands.hybrid_command(name="volume", description="Xem hoặc đổi âm lượng.")
    @app_commands.describe(level="Âm lượng từ 0 đến 200")
    async def volume(
        self, ctx: commands.Context, level: int | None = None
    ) -> None:
        player = await self._get_player(ctx)
        if level is None:
            await send_embed(
                ctx,
                success_embed(
                    f"Âm lượng hiện tại: **{player.volume}%**.",
                    self.config.embed_color,
                ),
            )
            return
        if not 0 <= level <= 200:
            raise UserError("Âm lượng phải nằm trong khoảng 0-200.")
        player = await self._control_player(ctx)
        new_level = await player.set_volume(level)
        await send_embed(
            ctx,
            success_embed(
                f"Đã đặt âm lượng **{new_level}%**.",
                self.config.embed_color,
            ),
        )

    @commands.hybrid_command(name="seek", description="Tua bài đang phát.")
    @app_commands.describe(position="Ví dụ: 90, 1:30 hoặc 1:02:03")
    async def seek(self, ctx: commands.Context, position: str) -> None:
        player = await self._control_player(ctx)
        seconds = parse_timestamp(position)

        if ctx.interaction:
            await defer_interaction(ctx)
            target = await player.seek(seconds)
        else:
            async with ctx.typing():
                target = await player.seek(seconds)
        await send_embed(
            ctx,
            success_embed(
                f"Đã tua tới **{format_duration(target)}**.",
                self.config.embed_color,
            ),
        )

    @commands.hybrid_command(name="remove", description="Xóa một bài khỏi hàng đợi.")
    @app_commands.describe(index="Vị trí trong hàng đợi (bắt đầu từ 1)")
    async def remove(self, ctx: commands.Context, index: int) -> None:
        player = await self._control_player(ctx)
        track = await player.remove(index)
        await send_embed(
            ctx,
            success_embed(
                f"Đã xóa **{truncate(track.title, 120)}** khỏi hàng đợi.",
                self.config.embed_color,
            ),
        )

    @commands.hybrid_command(name="clear", description="Xóa toàn bộ bài đang chờ.")
    async def clear(self, ctx: commands.Context) -> None:
        player = await self._control_player(ctx)
        count = await player.clear_queue()
        await send_embed(
            ctx,
            success_embed(
                f"Đã xóa {count} bài khỏi hàng đợi. Bài đang phát vẫn tiếp tục.",
                self.config.embed_color,
            ),
        )

    # ------------------------------------------------------------------
    # Voice / 24-7
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="join", description="Mời bot vào voice channel của bạn.")
    async def join(self, ctx: commands.Context) -> None:
        player, channel = await self._get_voice_player(ctx)
        if (
            player.auto_247
            and player.voice is not None
            and player.voice.channel is not None
            and player.voice.channel.id != channel.id
            and not ctx.author.guild_permissions.manage_guild
        ):
            raise UserError(
                "Bot đang bật 24/7 ở kênh khác. Cần quyền Manage Server để chuyển."
            )
        await player.connect(channel)
        if player.auto_247:
            await player.handle_voice_move(channel)
        await send_embed(
            ctx,
            success_embed(
                f"Đã tham gia **{channel.name}**.",
                self.config.embed_color,
            ),
        )

    @commands.hybrid_command(name="leave", description="Rời voice channel và tắt 24/7.")
    async def leave(self, ctx: commands.Context) -> None:
        require_guild(ctx)
        player = await self._get_player(ctx)
        state = getattr(ctx.author, "voice", None)
        author_channel = state.channel if state else None
        manage = ctx.author.guild_permissions.manage_guild

        if player.auto_247 and not manage:
            raise UserError(
                "Bot đang bật 24/7. Cần quyền Manage Server để tắt và rời kênh."
            )
        if (
            player.voice is not None
            and player.voice.channel is not None
            and author_channel is not None
            and player.voice.channel.id != author_channel.id
            and not manage
        ):
            raise UserError("Bạn cần ở cùng voice channel với bot.")
        if not player.is_connected and not player.auto_247:
            raise UserError("Bot không ở trong voice channel.")
        await player.leave()
        await send_embed(
            ctx,
            success_embed(
                "Đã rời voice channel và tắt chế độ 24/7.",
                self.config.embed_color,
            ),
        )

    @commands.hybrid_command(
        name="247",
        aliases=["stay"],
        description="Bật/tắt chế độ duy trì voice 24/7.",
    )
    @app_commands.describe(action="on = bật, off = tắt, status = xem trạng thái")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Bật", value="on"),
            app_commands.Choice(name="Tắt", value="off"),
            app_commands.Choice(name="Trạng thái", value="status"),
        ]
    )
    async def mode_247(
        self, ctx: commands.Context, action: str = "status"
    ) -> None:
        guild = require_guild(ctx)
        player = await self._get_player(ctx)
        action = action.strip().lower()
        manage = ctx.author.guild_permissions.manage_guild

        if action in {"on", "off"} and not manage:
            raise UserError("Cần quyền Manage Server để bật/tắt 24/7.")

        if action == "on":
            state = getattr(ctx.author, "voice", None)
            target = state.channel if state else None
            if target is None and player.voice is not None:
                target = player.voice.channel
            if not isinstance(target, discord.VoiceChannel):
                raise UserError(
                    "Bạn cần ở trong voice channel (hoặc bot phải đang ở "
                    "một voice channel) để bật 24/7."
                )
            self._check_voice_permissions(target)
            await player.set_247(True, target)
            await send_embed(
                ctx,
                success_embed(
                    f"Đã bật 24/7 tại **{target.name}**. Bot sẽ tự kết nối lại "
                    "khi mất kết nối hoặc restart.",
                    self.config.embed_color,
                ),
            )
            return

        if action == "off":
            await player.set_247(False, None)
            await send_embed(
                ctx,
                success_embed(
                    "Đã tắt 24/7. Bot vẫn ở trong voice channel cho tới khi "
                    "dùng /leave hoặc bị ngắt kết nối.",
                    self.config.embed_color,
                ),
            )
            return

        if action != "status":
            raise UserError("Hành động phải là: on, off hoặc status.")

        desired = (
            guild.get_channel(player.desired_channel_id)
            if player.desired_channel_id
            else None
        )
        current_voice = (
            player.voice.channel.mention
            if player.voice is not None and player.voice.channel is not None
            else "Không kết nối"
        )
        embed = base_embed(
            title="♾️ Trạng thái 24/7",
            color=self.config.embed_color,
        )
        embed.add_field(
            name="Chế độ",
            value="**Bật**" if player.auto_247 else "**Tắt**",
            inline=True,
        )
        embed.add_field(
            name="Kênh đã lưu",
            value=desired.mention if desired is not None else "Chưa có",
            inline=True,
        )
        embed.add_field(name="Voice hiện tại", value=current_voice, inline=True)
        embed.add_field(
            name="Tự kết nối lại",
            value=(
                "Có, có backoff khi lỗi"
                if player.auto_247
                else "Không"
            ),
            inline=False,
        )
        embed.set_footer(
            text="24/7 được lưu vào SQLite và khôi phục sau khi bot khởi động lại."
        )
        await send_embed(ctx, embed)

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="help", description="Xem hướng dẫn sử dụng bot.")
    async def help_command(self, ctx: commands.Context) -> None:
        prefix = self.config.prefix
        embed = discord.Embed(
            title="🎧 Hướng dẫn sử dụng",
            description=(
                "Bot hỗ trợ **YouTube, YouTube Music, SoundCloud, Bandcamp, "
                "Vimeo, Twitch, Mixcloud, link audio trực tiếp** và **Spotify** "
                "(metadata + ghép nguồn phát). Slash command dùng `/`, prefix "
                f"dùng `{prefix}`."
            ),
            color=self.config.embed_color,
        )
        embed.add_field(
            name="Phát nhạc",
            value=(
                "`/play <tên hoặc link>` — phát bài hát\n"
                "`/playlist <link playlist>` — thêm playlist/album\n"
                f"`{prefix}play <tên>` — dùng bằng prefix"
            ),
            inline=False,
        )
        embed.add_field(
            name="Điều khiển",
            value=(
                "`/pause` `/resume` `/skip` `/stop` `/seek <thời gian>`\n"
                "`/volume [0-200]` `/loop [off|track|queue]`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Hàng đợi",
            value=(
                "`/queue [trang]` `/nowplaying` `/shuffle`\n"
                "`/remove <vị trí>` `/clear`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Voice & 24/7",
            value=(
                "`/join` `/leave`\n"
                "`/247 on` — tự kết nối lại sau mất mạng/restart\n"
                "`/247 off` `/247 status`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Last.fm",
            value=(
                "`/lastfm` — xem playcount Last.fm và thống kê server\n"
                "`/stats` — thống kê lượt phát local\n"
                "`/history` — lịch sử phát gần đây\n"
                "Bot tự scrobble khi bài đủ ngưỡng nghe (1/2 bài, tối đa 4 phút)."
            ),
            inline=False,
        )
        embed.add_field(
            name="Lưu ý",
            value=(
                "• Spotify không cho bot stream trực tiếp; bot đọc metadata rồi "
                "ghép sang nguồn phát tương ứng.\n"
                "• 24/7 lưu kênh voice, không lưu hàng đợi sau restart.\n"
                "• Cần quyền Manage Server để bật/tắt 24/7."
            ),
            inline=False,
        )
        await send_embed(ctx, embed, ephemeral=True)


async def setup(bot: "MusicBot") -> None:
    await bot.add_cog(MusicCog(bot))

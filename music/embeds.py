from __future__ import annotations

import discord

from .models import Track
from .utils import (
    format_duration,
    format_number,
    progress_bar,
    truncate,
)


def _is_http(url: str | None) -> bool:
    return bool(url and url.startswith(("http://", "https://")))


def base_embed(
    *, title: str | None = None, description: str | None = None, color: int
) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def success_embed(text: str, color: int) -> discord.Embed:
    return discord.Embed(description=f"✅ {text}", color=color)


def error_embed(text: str) -> discord.Embed:
    return discord.Embed(description=f"⚠️ {text}", color=0xED4245)


def info_embed(text: str, color: int) -> discord.Embed:
    return discord.Embed(description=text, color=color)


def now_playing_embed(
    track: Track,
    *,
    color: int,
    position: float = 0.0,
    queue_length: int = 0,
    loop_label: str = "Tắt",
    volume: int = 100,
) -> discord.Embed:
    embed = discord.Embed(
        title=truncate(f"🎶 {track.title}", 250),
        description=f"**{track.artist_text}**",
        color=color,
    )
    if _is_http(track.page_url):
        embed.url = track.page_url
    if _is_http(track.thumbnail):
        embed.set_thumbnail(url=track.thumbnail)

    if track.is_live:
        progress = "🔴 Đang phát trực tiếp"
    elif track.duration:
        progress = (
            f"`{format_duration(position)}` "
            f"{progress_bar(position, track.duration)} "
            f"`{track.duration_text}`"
        )
    else:
        progress = f"`{format_duration(position)}` đã phát"
    embed.add_field(name="Tiến độ", value=progress, inline=False)

    embed.add_field(name="Nguồn", value=track.platform, inline=True)
    embed.add_field(
        name="Yêu cầu bởi", value=truncate(track.requester_name, 100), inline=True
    )
    embed.add_field(
        name="Hàng đợi", value=f"{format_number(queue_length)} bài", inline=True
    )
    embed.add_field(name="Loop", value=loop_label, inline=True)
    embed.add_field(name="Âm lượng", value=f"{volume}%", inline=True)
    embed.set_footer(text="Dùng /queue để xem hàng đợi • /help để xem lệnh")
    return embed


def queue_embed(
    *,
    color: int,
    current: Track | None,
    upcoming: list[Track],
    page: int,
    page_size: int,
    loop_label: str,
    volume: int,
) -> discord.Embed:
    total_pages = max(1, (len(upcoming) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    page_tracks = upcoming[start : start + page_size]

    embed = discord.Embed(title="📜 Hàng đợi", color=color)
    if current is not None:
        embed.add_field(
            name="Đang phát",
            value=(
                f"**{truncate(current.title, 130)}** — "
                f"{truncate(current.artist, 80)} "
                f"`[{current.duration_text}]`"
            ),
            inline=False,
        )

    if page_tracks:
        lines = []
        for offset, track in enumerate(page_tracks, start=start + 1):
            lines.append(
                f"`{offset:02d}.` **{truncate(track.title, 105)}** — "
                f"{truncate(track.artist, 55)} `[{track.duration_text}]`"
            )
        embed.add_field(name="Tiếp theo", value="\n".join(lines), inline=False)
    elif current is None:
        embed.description = "Hàng đợi đang trống."
    else:
        embed.description = "Không còn bài nào phía sau."

    embed.set_footer(
        text=(
            f"Trang {page}/{total_pages} • {format_number(len(upcoming))} bài chờ "
            f"• Loop: {loop_label} • Âm lượng: {volume}%"
        )
    )
    return embed


def history_embed(
    rows: list[dict[str, object]], *, color: int
) -> discord.Embed:
    embed = discord.Embed(title="🕘 Lịch sử phát gần đây", color=color)
    if not rows:
        embed.description = "Chưa có lịch sử phát nhạc trên server này."
        return embed

    lines = []
    for index, row in enumerate(rows, start=1):
        played = format_duration(float(row.get("played_seconds") or 0))
        scrobble = "🎧" if row.get("scrobbled") else "•"
        lines.append(
            f"`{index:02d}.` {scrobble} **{truncate(str(row.get('title', '')), 80)}** — "
            f"{truncate(str(row.get('artist', '')), 45)} `[{played}]`"
        )
    embed.description = "\n".join(lines)
    embed.set_footer(text="🎧 = đã scrobble lên Last.fm • thời gian nghe thực tế")
    return embed

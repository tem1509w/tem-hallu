from __future__ import annotations

import discord
from discord.ext import commands

from music.embeds import error_embed
from music.errors import UserError


def require_guild(ctx: commands.Context) -> discord.Guild:
    if ctx.guild is None:
        raise UserError("Lệnh này chỉ dùng được trong server.")
    return ctx.guild


async def defer_interaction(ctx: commands.Context) -> None:
    if ctx.interaction and not ctx.interaction.response.is_done():
        await ctx.interaction.response.defer()


async def send_embed(
    ctx: commands.Context,
    embed: discord.Embed,
    *,
    ephemeral: bool = False,
) -> None:
    if ctx.interaction:
        if ctx.interaction.response.is_done():
            await ctx.interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await ctx.interaction.response.send_message(
                embed=embed, ephemeral=ephemeral
            )
        return
    await ctx.send(embed=embed)


async def send_error(ctx: commands.Context, message: str) -> None:
    await send_embed(ctx, error_embed(message), ephemeral=True)

from __future__ import annotations

from discord.ext import commands


class UserError(commands.CommandError):
    """An error safe to show directly to Discord users."""


class ResolverError(UserError):
    """A track or playlist could not be resolved."""


class VoiceError(UserError):
    """A voice-channel operation failed."""


class LastfmError(Exception):
    """Last.fm rejected a request or could not be reached."""


class SpotifyError(ResolverError):
    """Spotify metadata could not be loaded."""

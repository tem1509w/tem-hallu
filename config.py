from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when the environment is not usable."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = _env(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} phải là số nguyên.") from exc

    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} phải >= {minimum}.")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} phải <= {maximum}.")
    return value


def _env_optional_int(name: str) -> int | None:
    raw = _env(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} phải là số nguyên.") from exc


def _env_path(name: str) -> Path | None:
    raw = _env(name)
    return Path(raw).expanduser() if raw else None


def _parse_color(raw: str | None) -> int:
    if raw is None:
        return 0x5865F2
    value = raw.strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    elif value.startswith("#"):
        value = value[1:]
    if len(value) not in {6}:
        raise ConfigError("EMBED_COLOR phải có dạng #RRGGBB hoặc 0xRRGGBB.")
    try:
        color = int(value, 16)
    except ValueError as exc:
        raise ConfigError("EMBED_COLOR không hợp lệ.") from exc
    if not 0 <= color <= 0xFFFFFF:
        raise ConfigError("EMBED_COLOR không hợp lệ.")
    return color


@dataclass(frozen=True, slots=True)
class Config:
    token: str
    prefix: str
    database_path: Path
    ffmpeg_path: str
    ytdlp_path: str
    ytdlp_cookies_file: Path | None
    default_volume: int
    max_queue_size: int
    max_playlist_size: int
    queue_page_size: int
    embed_color: int
    log_level: str
    ytdlp_timeout_seconds: int
    stream_timeout_seconds: int
    reconnect_max_delay_seconds: int
    lastfm_api_key: str | None
    lastfm_api_secret: str | None
    lastfm_session_key: str | None
    lastfm_username: str | None
    spotify_client_id: str | None
    spotify_client_secret: str | None
    dev_guild_id: int | None

    @classmethod
    def from_env(cls) -> "Config":
        token = _env("DISCORD_TOKEN")
        if not token:
            raise ConfigError(
                "Thiếu DISCORD_TOKEN. Hãy tạo file .env từ .env.example."
            )

        prefix = _env("COMMAND_PREFIX", "m!") or "m!"
        if len(prefix) > 5:
            raise ConfigError("COMMAND_PREFIX tối đa 5 ký tự.")

        log_level = (_env("LOG_LEVEL", "INFO") or "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError(
                "LOG_LEVEL phải là DEBUG, INFO, WARNING, ERROR hoặc CRITICAL."
            )

        max_queue_size = _env_int(
            "MAX_QUEUE_SIZE", 500, minimum=1, maximum=5000
        )
        max_playlist_size = _env_int(
            "MAX_PLAYLIST_SIZE", 50, minimum=1, maximum=max_queue_size
        )

        database_path = Path(
            _env("DATABASE_PATH", "data/musicbot.sqlite3")
            or "data/musicbot.sqlite3"
        ).expanduser()

        return cls(
            token=token,
            prefix=prefix,
            database_path=database_path,
            ffmpeg_path=_env("FFMPEG_PATH", "ffmpeg") or "ffmpeg",
            ytdlp_path=_env("YTDLP_PATH", "yt-dlp") or "yt-dlp",
            ytdlp_cookies_file=_env_path("YTDLP_COOKIES_FILE"),
            default_volume=_env_int(
                "DEFAULT_VOLUME", 80, minimum=0, maximum=200
            ),
            max_queue_size=max_queue_size,
            max_playlist_size=max_playlist_size,
            queue_page_size=_env_int(
                "QUEUE_PAGE_SIZE", 10, minimum=1, maximum=25
            ),
            embed_color=_parse_color(_env("EMBED_COLOR")),
            log_level=log_level,
            ytdlp_timeout_seconds=_env_int(
                "YTDLP_TIMEOUT_SECONDS", 45, minimum=5, maximum=180
            ),
            stream_timeout_seconds=_env_int(
                "STREAM_TIMEOUT_SECONDS", 30, minimum=5, maximum=120
            ),
            reconnect_max_delay_seconds=_env_int(
                "RECONNECT_MAX_DELAY_SECONDS", 60, minimum=10, maximum=600
            ),
            lastfm_api_key=_env("LASTFM_API_KEY"),
            lastfm_api_secret=_env("LASTFM_API_SECRET"),
            lastfm_session_key=_env("LASTFM_SESSION_KEY"),
            lastfm_username=_env("LASTFM_USERNAME"),
            spotify_client_id=_env("SPOTIFY_CLIENT_ID"),
            spotify_client_secret=_env("SPOTIFY_CLIENT_SECRET"),
            dev_guild_id=_env_optional_int("DEV_GUILD_ID"),
        )

    @property
    def lastfm_can_scrobble(self) -> bool:
        return bool(
            self.lastfm_api_key
            and self.lastfm_api_secret
            and self.lastfm_session_key
            and self.lastfm_username
        )

    @property
    def lastfm_can_query(self) -> bool:
        return bool(self.lastfm_api_key and self.lastfm_username)

    @property
    def spotify_configured(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

from __future__ import annotations

import asyncio
import logging
import shutil
import sys

import discord
from dotenv import load_dotenv

from bot import MusicBot
from config import Config, ConfigError


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    logging.getLogger("discord").setLevel(logging.WARNING)


def log_dependency_warnings(config: Config) -> None:
    logger = logging.getLogger("startup")
    if shutil.which(config.ffmpeg_path) is None:
        logger.warning(
            "Không tìm thấy FFmpeg tại '%s'. Kiểm tra FFMPEG_PATH hoặc cài FFmpeg.",
            config.ffmpeg_path,
        )
    if shutil.which(config.ytdlp_path) is None:
        logger.warning(
            "Không tìm thấy yt-dlp tại '%s'. Cài bằng: pip install -U yt-dlp",
            config.ytdlp_path,
        )
    if not config.lastfm_can_scrobble:
        logger.warning(
            "Last.fm scrobbling chưa bật. Điền đủ LASTFM_API_KEY, "
            "LASTFM_API_SECRET, LASTFM_SESSION_KEY, LASTFM_USERNAME."
        )
    if not config.spotify_configured:
        logger.info(
            "Spotify chưa cấu hình; link Spotify sẽ bị từ chối cho tới khi có "
            "SPOTIFY_CLIENT_ID/SECRET."
        )


async def run() -> None:
    load_dotenv()
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Lỗi cấu hình: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    configure_logging(config.log_level)
    log_dependency_warnings(config)
    bot = MusicBot(config)
    try:
        await bot.start(config.token)
    except discord.LoginFailure:
        logging.getLogger("startup").error(
            "Đăng nhập thất bại: DISCORD_TOKEN không hợp lệ."
        )
    except discord.PrivilegedIntentsRequired:
        logging.getLogger("startup").error(
            "Thiếu privileged intent. Bật Message Content Intent trong "
            "Discord Developer Portal."
        )
    finally:
        if not bot.is_closed():
            await bot.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

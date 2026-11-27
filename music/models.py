from __future__ import annotations

from dataclasses import dataclass, field

from .utils import format_duration, truncate


PLATFORM_LABELS = {
    "youtube": "YouTube",
    "youtube:tab": "YouTube",
    "soundcloud": "SoundCloud",
    "bandcamp": "Bandcamp",
    "vimeo": "Vimeo",
    "twitch": "Twitch",
    "mixcloud": "Mixcloud",
    "dailymotion": "Dailymotion",
    "audius": "Audius",
    "direct": "URL trực tiếp",
}

AUDIO_EXTENSIONS = (
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".flac",
    ".webm",
)


def platform_label(extractor: str | None) -> str:
    if not extractor:
        return "Không rõ"
    key = extractor.lower()
    if key in PLATFORM_LABELS:
        return PLATFORM_LABELS[key]
    if key.startswith("youtube"):
        return "YouTube"
    if key.startswith("soundcloud"):
        return "SoundCloud"
    base = key.split(":")[0].replace("_", " ").strip()
    return base.title() if base else "Không rõ"


@dataclass(slots=True)
class Track:
    title: str
    artist: str
    url: str
    platform: str
    duration: int | None = None
    thumbnail: str | None = None
    is_live: bool = False
    requester_id: int | None = None
    requester_name: str = "Không rõ"
    source_url: str | None = None
    identifier: str | None = None

    @property
    def page_url(self) -> str:
        return self.source_url or self.url

    @property
    def display_title(self) -> str:
        return truncate(self.title, 180)

    @property
    def duration_text(self) -> str:
        return format_duration(self.duration, is_live=self.is_live)

    @property
    def artist_text(self) -> str:
        return truncate(self.artist or "Không rõ", 120)

    def history_values(self) -> dict[str, object]:
        return {
            "title": self.title,
            "artist": self.artist,
            "platform": self.platform,
            "duration": self.duration,
        }


@dataclass(slots=True)
class Resolution:
    tracks: list[Track] = field(default_factory=list)
    title: str | None = None
    truncated: bool = False
    failed: int = 0
    requested: int = 0


@dataclass(slots=True)
class GuildSettings:
    auto_247: bool = False
    voice_channel_id: int | None = None


@dataclass(slots=True)
class GuildStats:
    total_plays: int = 0
    scrobbled_plays: int = 0
    listen_seconds: float = 0.0
    top_tracks: list[dict[str, object]] = field(default_factory=list)

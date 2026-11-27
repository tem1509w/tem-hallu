from __future__ import annotations

from .errors import UserError


def format_duration(seconds: float | int | None, *, is_live: bool = False) -> str:
    if is_live:
        return "LIVE"
    if seconds is None:
        return "Không rõ"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_timestamp(value: str) -> int:
    """Parse 90, 1:30, or 1:02:03 into seconds."""

    text = value.strip()
    if not text:
        raise UserError("Thời gian không được để trống.")

    parts = text.split(":")
    if len(parts) > 3 or any(not part.isdigit() for part in parts):
        raise UserError("Thời gian không hợp lệ. Ví dụ: 90, 1:30, 1:02:03.")

    numbers = [int(part) for part in parts]
    if len(numbers) == 1:
        seconds = numbers[0]
    elif len(numbers) == 2:
        minutes, seconds = numbers
        if seconds >= 60:
            raise UserError("Phần giây phải nhỏ hơn 60.")
        seconds = minutes * 60 + seconds
    else:
        hours, minutes, seconds = numbers
        if minutes >= 60 or seconds >= 60:
            raise UserError("Phần phút và giây phải nhỏ hơn 60.")
        seconds = hours * 3600 + minutes * 60 + seconds

    if seconds < 0:
        raise UserError("Thời gian không hợp lệ.")
    return seconds


def truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def format_number(value: int | float) -> str:
    return f"{int(value):,}".replace(",", ".")


def format_listen_time(seconds: float | int) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} giờ {minutes} phút"
    if minutes:
        return f"{minutes} phút {secs} giây"
    return f"{secs} giây"


def progress_bar(position: float | int, duration: float | int | None, length: int = 14) -> str:
    if duration is None or duration <= 0:
        return "▬" * length
    ratio = max(0.0, min(1.0, float(position) / float(duration)))
    index = min(length - 1, int(ratio * length))
    return "▬" * index + "🔘" + "▬" * (length - index - 1)


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))

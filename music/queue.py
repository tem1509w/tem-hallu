from __future__ import annotations

import random
from collections import deque
from enum import Enum
from typing import Iterable

from .errors import UserError
from .models import Track


class LoopMode(str, Enum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"

    @classmethod
    def parse(cls, value: str) -> "LoopMode":
        try:
            return cls(value.strip().lower())
        except (ValueError, AttributeError) as exc:
            raise UserError("Chế độ loop phải là: off, track hoặc queue.") from exc

    @property
    def label(self) -> str:
        return {
            LoopMode.OFF: "Tắt",
            LoopMode.TRACK: "Một bài",
            LoopMode.QUEUE: "Cả hàng đợi",
        }[self]


class GuildQueue:
    def __init__(self, max_size: int) -> None:
        self.max_size = max_size
        self._tracks: deque[Track] = deque()

    def __len__(self) -> int:
        return len(self._tracks)

    @property
    def room(self) -> int:
        return max(0, self.max_size - len(self._tracks))

    @property
    def is_empty(self) -> bool:
        return not self._tracks

    def extend(self, tracks: Iterable[Track]) -> tuple[int, int]:
        """Add as many tracks as fit. Returns (added, rejected)."""

        added = 0
        rejected = 0
        for track in tracks:
            if len(self._tracks) >= self.max_size:
                rejected += 1
                continue
            self._tracks.append(track)
            added += 1
        return added, rejected

    def add(self, track: Track) -> bool:
        if len(self._tracks) >= self.max_size:
            return False
        self._tracks.append(track)
        return True

    def pop_next(self) -> Track | None:
        if not self._tracks:
            return None
        return self._tracks.popleft()

    def remove(self, index: int) -> Track:
        """Remove a 1-based queue position."""

        if index < 1 or index > len(self._tracks):
            raise UserError(
                f"Vị trí không hợp lệ. Hàng đợi hiện có {len(self._tracks)} bài."
            )
        tracks = list(self._tracks)
        track = tracks.pop(index - 1)
        self._tracks = deque(tracks)
        return track

    def clear(self) -> int:
        count = len(self._tracks)
        self._tracks.clear()
        return count

    def shuffle(self) -> int:
        tracks = list(self._tracks)
        random.shuffle(tracks)
        self._tracks = deque(tracks)
        return len(tracks)

    def snapshot(self) -> list[Track]:
        return list(self._tracks)

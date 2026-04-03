from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Lesson:
    """A single group lesson from the schedule."""

    name: str
    date: str  # e.g. "2026-04-03"
    time_start: str  # e.g. "09:00"
    time_end: str  # e.g. "09:55"
    instructor: str = ""
    location: str = ""  # room / studio name
    spots_available: int | None = None  # None = unknown
    spots_total: int | None = None
    bookable: bool = False  # True when there are open spots

    @property
    def uid(self) -> str:
        """Stable unique id for deduplication."""
        raw = f"{self.date}|{self.time_start}|{self.name}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    @property
    def display(self) -> str:
        spots = ""
        if self.spots_available is not None and self.spots_total is not None:
            spots = f" ({self.spots_available}/{self.spots_total} spots)"
        elif self.bookable:
            spots = " (spots available!)"
        instructor = f" - {self.instructor}" if self.instructor else ""
        location = f" @ {self.location}" if self.location else ""
        return (
            f"📋 *{self.name}*\n"
            f"📅 {self.date}  🕐 {self.time_start}–{self.time_end}\n"
            f"👤{instructor}{location}{spots}"
        )

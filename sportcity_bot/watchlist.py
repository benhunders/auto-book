"""Persistent watch list for lesson names the user wants to be notified about.

Stores the list as a simple JSON file so it survives bot restarts.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("watchlist.json")


class WatchList:
    """A set of lesson name patterns the user wants to track."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self._path = path
        self._names: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._names = {n.lower().strip() for n in data if isinstance(n, str)}
                logger.info("Loaded watch list: %s", self._names)
            except Exception as e:
                logger.warning("Failed to load watch list: %s", e)

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(sorted(self._names), indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("Failed to save watch list: %s", e)

    def add(self, name: str) -> bool:
        """Add a lesson name to watch. Returns True if it was new."""
        key = name.lower().strip()
        if not key:
            return False
        if key in self._names:
            return False
        self._names.add(key)
        self._save()
        return True

    def remove(self, name: str) -> bool:
        """Remove a lesson name. Returns True if it existed."""
        key = name.lower().strip()
        if key not in self._names:
            return False
        self._names.discard(key)
        self._save()
        return True

    def matches(self, lesson_name: str) -> bool:
        """Check if a lesson name matches any watched pattern."""
        if not self._names:
            return False
        lower = lesson_name.lower()
        return any(pattern in lower for pattern in self._names)

    @property
    def names(self) -> list[str]:
        return sorted(self._names)

    @property
    def is_empty(self) -> bool:
        return len(self._names) == 0

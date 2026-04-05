"""Monitoring loop that detects new lessons appearing on the schedule.

The core logic: every poll cycle, scrape the full schedule and compare
against previously seen lessons. If a lesson is new (wasn't in the
previous scrape) AND matches the user's watch list, send a notification.

This handles the use case where lessons are added ~2 weeks in advance
and the user wants to know the moment a specific class appears.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from telegram.ext import Application

from .config import Settings
from .models import Lesson
from .scraper import scrape_schedule
from .telegram_bot import send_lesson_notification
from .watchlist import WatchList

logger = logging.getLogger(__name__)

# File to persist seen lesson UIDs across bot restarts
SEEN_FILE = Path("seen_lessons.json")


class LessonMonitor:
    """Monitors the SportCity schedule for new lessons matching the watch list."""

    def __init__(
        self, app: Application, settings: Settings, watchlist: WatchList
    ) -> None:
        self.app = app
        self.settings = settings
        self.watchlist = watchlist
        # UIDs of lessons we've already seen (persisted to disk)
        self._seen_uids: set[str] = set()
        # UIDs we've already sent notifications for
        self._notified_uids: set[str] = set()
        self._notified_count = 0
        self._load_seen()

    def _load_seen(self) -> None:
        """Load previously seen lesson UIDs from disk."""
        if SEEN_FILE.exists():
            try:
                data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
                self._seen_uids = set(data.get("seen", []))
                self._notified_uids = set(data.get("notified", []))
                logger.info(
                    "Loaded %d seen lessons, %d notified",
                    len(self._seen_uids),
                    len(self._notified_uids),
                )
            except Exception as e:
                logger.warning("Failed to load seen lessons: %s", e)

    def _save_seen(self) -> None:
        """Persist seen lesson UIDs to disk."""
        try:
            SEEN_FILE.write_text(
                json.dumps(
                    {
                        "seen": sorted(self._seen_uids),
                        "notified": sorted(self._notified_uids),
                        "last_updated": datetime.now().isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("Failed to save seen lessons: %s", e)

    async def check_once(self) -> list[Lesson]:
        """Run a single check cycle. Returns newly found matching lessons."""
        logger.info("Running schedule check...")
        try:
            lessons = await scrape_schedule(
                self.settings.sportcity_schedule_url,
                weeks_ahead=self.settings.weeks_ahead,
                email=self.settings.sportcity_email,
                password=self.settings.sportcity_password,
            )
        except Exception as e:
            logger.error("Scrape failed: %s", e)
            return []

        # Update bot status and known lesson names for the picker UI
        self.app.bot_data["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.app.bot_data["lessons_found"] = len(lessons)
        self.app.bot_data["known_lesson_names"] = {l.name for l in lessons}

        current_uids = {l.uid for l in lessons}

        # Find NEW lessons that weren't in previous scrapes
        new_lessons = [l for l in lessons if l.uid not in self._seen_uids]

        if new_lessons:
            logger.info(
                "Found %d new lessons on schedule (total: %d)",
                len(new_lessons),
                len(lessons),
            )

        # Filter to only lessons matching the watch list
        matching = [
            l
            for l in new_lessons
            if self.watchlist.matches(l.name) and l.uid not in self._notified_uids
        ]

        # Send notifications for matching lessons
        for lesson in matching:
            try:
                await send_lesson_notification(
                    self.app,
                    self.settings.telegram_chat_id,
                    lesson,
                )
                self._notified_uids.add(lesson.uid)
                self._notified_count += 1
            except Exception as e:
                logger.error("Failed to send notification for %s: %s", lesson.name, e)

        self.app.bot_data["notified_count"] = self._notified_count

        # Update seen set with ALL current lessons
        self._seen_uids.update(current_uids)

        # Clean up: remove UIDs for lessons no longer on the schedule
        # (they've passed or been removed), but keep a buffer
        self._seen_uids = self._seen_uids & current_uids
        self._notified_uids = self._notified_uids & current_uids

        # Persist to disk
        self._save_seen()

        if matching:
            logger.info("Notified about %d matching lessons", len(matching))
        elif new_lessons:
            logger.info(
                "%d new lessons found but none match watch list (%s)",
                len(new_lessons),
                ", ".join(self.watchlist.names) or "(empty)",
            )
        else:
            logger.info("No new lessons found")

        return matching

    async def run_loop(self) -> None:
        """Run the monitoring loop indefinitely."""
        logger.info(
            "Starting monitor loop (interval=%ds, url=%s)",
            self.settings.poll_interval_seconds,
            self.settings.sportcity_schedule_url,
        )
        logger.info(
            "Watch list: %s",
            ", ".join(self.watchlist.names) or "(empty — use /watch in Telegram)",
        )

        while True:
            await self.check_once()
            logger.info(
                "Next check in %ds...", self.settings.poll_interval_seconds
            )
            await asyncio.sleep(self.settings.poll_interval_seconds)

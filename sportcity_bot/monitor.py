"""Monitoring loop that periodically scrapes the schedule and detects new open spots."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram.ext import Application

from .config import Settings
from .models import Lesson
from .scraper import scrape_schedule
from .telegram_bot import send_lesson_notification

logger = logging.getLogger(__name__)


class LessonMonitor:
    """Monitors the SportCity schedule for newly available lessons."""

    def __init__(self, app: Application, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        # Set of lesson UIDs we've already notified about
        self._notified: set[str] = set()
        # Previous scrape results for diffing
        self._previous_lessons: dict[str, Lesson] = {}

    def _matches_filter(self, lesson: Lesson) -> bool:
        """Check if a lesson matches the configured name filter."""
        filters = self.settings.lesson_filter_list
        if not filters:
            return True  # No filter = match everything
        return any(f in lesson.name.lower() for f in filters)

    def _is_newly_bookable(self, lesson: Lesson) -> bool:
        """Check if a lesson just became bookable (wasn't before)."""
        if not lesson.bookable:
            return False
        if lesson.uid in self._notified:
            return False
        prev = self._previous_lessons.get(lesson.uid)
        if prev is not None and prev.bookable:
            return False  # Was already bookable in previous scrape
        return True

    async def check_once(self) -> list[Lesson]:
        """Run a single check cycle. Returns list of newly bookable lessons."""
        logger.info("Running schedule check...")
        try:
            lessons = await scrape_schedule(self.settings.sportcity_schedule_url)
        except Exception as e:
            logger.error("Scrape failed: %s", e)
            return []

        # Update bot status data
        self.app.bot_data["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.app.bot_data["lessons_found"] = len(lessons)
        self.app.bot_data["bookable_found"] = sum(1 for l in lessons if l.bookable)

        # Find newly bookable lessons that match our filter
        newly_bookable = [
            lesson
            for lesson in lessons
            if self._matches_filter(lesson) and self._is_newly_bookable(lesson)
        ]

        # Send notifications
        for lesson in newly_bookable:
            try:
                await send_lesson_notification(
                    self.app,
                    self.settings.telegram_chat_id,
                    lesson,
                )
                self._notified.add(lesson.uid)
            except Exception as e:
                logger.error("Failed to send notification for %s: %s", lesson.name, e)

        # Update previous state
        self._previous_lessons = {l.uid: l for l in lessons}

        # Clean up old notifications (lessons that are no longer in the schedule)
        current_uids = {l.uid for l in lessons}
        self._notified = self._notified & current_uids

        if newly_bookable:
            logger.info("Found %d newly bookable lessons", len(newly_bookable))
        else:
            logger.info("No new bookable lessons found")

        return newly_bookable

    async def run_loop(self) -> None:
        """Run the monitoring loop indefinitely."""
        logger.info(
            "Starting monitor loop (interval=%ds, url=%s, filter=%s)",
            self.settings.poll_interval_seconds,
            self.settings.sportcity_schedule_url,
            self.settings.lesson_filter or "(all)",
        )

        while True:
            await self.check_once()
            logger.info("Sleeping %ds until next check...", self.settings.poll_interval_seconds)
            await asyncio.sleep(self.settings.poll_interval_seconds)

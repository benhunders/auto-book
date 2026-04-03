"""Standalone debug script to test the scraper without Telegram.

Usage:
    python -m sportcity_bot.debug_scrape [URL]
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from .scraper import scrape_schedule

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

DEFAULT_URL = "https://www.sportcity.nl/utrecht/leidsche-rijn-sportpark/groepslesrooster"


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"Scraping: {url}\n")

    lessons = await scrape_schedule(url)

    if not lessons:
        print("No lessons found!")
        print("Check /tmp/sportcity_schedule.png and /tmp/sportcity_schedule.html for debug info.")
        return

    print(f"Found {len(lessons)} lessons:\n")
    for lesson in lessons:
        status = "BOOKABLE" if lesson.bookable else "full/unknown"
        print(f"  [{status}] {lesson.name} | {lesson.date} {lesson.time_start}-{lesson.time_end}")
        if lesson.spots_available is not None:
            print(f"           Spots: {lesson.spots_available}/{lesson.spots_total}")
        print()


if __name__ == "__main__":
    asyncio.run(main())

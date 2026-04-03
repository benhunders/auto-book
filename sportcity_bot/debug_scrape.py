"""Standalone debug script to test the scraper without Telegram.

Usage:
    python -m sportcity_bot.debug_scrape [URL]

Saves debug output (screenshot, HTML, extraction data) to a temp directory.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from .scraper import DEBUG_DIR, scrape_schedule

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

DEFAULT_URL = "https://www.sportcity.nl/utrecht/leidsche-rijn-sportpark/groepslesrooster"


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"Scraping: {url}")
    print(f"Debug output: {DEBUG_DIR}\n")

    lessons = await scrape_schedule(url, debug=True)

    print(f"\n{'='*60}")

    if not lessons:
        print("No lessons found!")
        print(f"\nCheck debug files in: {DEBUG_DIR}")
        return

    print(f"Found {len(lessons)} lessons:\n")
    for lesson in lessons:
        status = "BOOKABLE" if lesson.bookable else "full"
        spots = ""
        if lesson.spots_available is not None:
            spots = f" [{lesson.spots_available}/{lesson.spots_total} spots]"
        loc = f" @ {lesson.location}" if lesson.location else ""
        instr = f" ({lesson.instructor})" if lesson.instructor else ""
        print(
            f"  [{status}] {lesson.date} {lesson.time_start}-{lesson.time_end} "
            f"{lesson.name}{instr}{loc}{spots}"
        )


if __name__ == "__main__":
    asyncio.run(main())

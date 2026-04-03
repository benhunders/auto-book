"""Standalone debug script to test the scraper without Telegram.

Usage:
    python -m sportcity_bot.debug_scrape [URL]

Saves debug output (screenshot, HTML, network log) to a temp directory.
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
    print(f"Debug output will be saved to: {DEBUG_DIR}\n")

    lessons = await scrape_schedule(url, debug=True)

    print(f"\n{'='*60}")

    if not lessons:
        print("No lessons found!")
        print(f"\nCheck these debug files:")
        print(f"  Screenshot: {DEBUG_DIR / 'screenshot.png'}")
        print(f"  Page HTML:  {DEBUG_DIR / 'page.html'}")
        print(f"  Network:    {DEBUG_DIR / 'network.log'}")
        print(f"\nPlease share the screenshot so we can see what the page looks like.")
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

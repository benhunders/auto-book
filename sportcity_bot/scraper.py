"""Scrape the SportCity group lesson schedule using Playwright.

The schedule page at sportcity.nl embeds a Virtuagym-powered timetable that
is rendered client-side with JavaScript. We use a headless Chromium browser
to load the page, wait for the schedule widget to appear, and extract the
lesson data from the DOM.

The scraper intentionally uses broad, resilient selectors and multiple
fallback strategies because the exact DOM structure may vary or change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta

from playwright.async_api import Page, async_playwright

from .models import Lesson

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JS extraction script injected into the page.  It tries several strategies
# to pull structured lesson data out of the Virtuagym schedule widget.
# ---------------------------------------------------------------------------
EXTRACT_LESSONS_JS = """
() => {
    const lessons = [];

    // --- Strategy 1: look for Virtuagym event blocks ---
    // Virtuagym widgets typically render events inside elements with class
    // names like "event", "schedule-event", "class-event", etc.
    const eventSelectors = [
        '.event', '.schedule-event', '.class-event',
        '[class*="event"]', '[class*="class-item"]',
        '[class*="lesson"]', '[class*="rooster"]',
        'td[class*="event"]', '.timetable-event',
        '[data-event-id]', '[data-class-id]',
    ];

    for (const selector of eventSelectors) {
        const elements = document.querySelectorAll(selector);
        for (const el of elements) {
            // Skip tiny/invisible elements
            if (el.offsetHeight < 10) continue;

            const text = el.innerText || el.textContent || '';
            if (text.trim().length < 3) continue;

            // Try to extract structured data from data attributes first
            const data = el.dataset || {};
            const name = data.eventName || data.className || data.name || '';
            const time = data.time || data.startTime || '';

            lessons.push({
                name: name,
                time: time,
                text: text.trim().substring(0, 500),
                html: el.innerHTML.substring(0, 1000),
                tag: el.tagName,
                classes: el.className,
                dataAttrs: JSON.stringify(data),
            });
        }
    }

    // --- Strategy 2: look for table rows in a timetable ---
    const tables = document.querySelectorAll('table');
    for (const table of tables) {
        const rows = table.querySelectorAll('tr');
        for (const row of rows) {
            const cells = row.querySelectorAll('td, th');
            if (cells.length >= 2) {
                const text = row.innerText || '';
                if (text.trim().length > 5) {
                    lessons.push({
                        name: '',
                        time: '',
                        text: text.trim().substring(0, 500),
                        html: row.innerHTML.substring(0, 1000),
                        tag: 'TR',
                        classes: row.className,
                        dataAttrs: '{}',
                    });
                }
            }
        }
    }

    // --- Strategy 3: look for iframes (Virtuagym may be in an iframe) ---
    const iframes = document.querySelectorAll('iframe');
    const iframeInfo = [];
    for (const iframe of iframes) {
        iframeInfo.push({
            src: iframe.src || '',
            id: iframe.id || '',
            name: iframe.name || '',
        });
    }

    // --- Strategy 4: capture any XHR/fetch data that looks like schedule JSON ---
    // (This checks for data stored in window/global variables)
    const globals = [];
    for (const key of Object.keys(window)) {
        try {
            const val = window[key];
            if (val && typeof val === 'object' && !Array.isArray(val)) {
                const str = JSON.stringify(val).substring(0, 200);
                if (/class|event|lesson|schedule|rooster/i.test(str)) {
                    globals.push({ key, preview: str });
                }
            }
        } catch(e) {}
    }

    return {
        lessons,
        iframes: iframeInfo,
        globals,
        title: document.title,
        url: window.location.href,
        bodyLength: document.body.innerHTML.length,
    };
}
"""


async def _accept_cookies(page: Page) -> None:
    """Try to dismiss cookie consent banners."""
    cookie_selectors = [
        "button:has-text('Accepteer')",
        "button:has-text('Accept')",
        "button:has-text('Akkoord')",
        "button:has-text('OK')",
        "[id*='cookie'] button",
        "[class*='cookie'] button",
        "[id*='consent'] button",
    ]
    for selector in cookie_selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


async def _find_and_enter_iframe(page: Page) -> Page | None:
    """If the schedule is inside an iframe, return a handle to its content."""
    iframes = page.frames
    for frame in iframes:
        url = frame.url
        if "virtuagym" in url or "classes" in url or "schedule" in url:
            logger.info("Found schedule iframe: %s", url)
            return frame  # type: ignore[return-value]
    return None


async def _intercept_api_calls(page: Page) -> list[dict]:
    """Capture any XHR/fetch responses that look like schedule data."""
    captured: list[dict] = []

    async def handle_response(response):
        url = response.url
        if any(
            kw in url.lower()
            for kw in ["class", "event", "schedule", "lesson", "rooster"]
        ):
            try:
                body = await response.json()
                captured.append({"url": url, "data": body})
                logger.info("Captured API response: %s", url)
            except Exception:
                pass

    page.on("response", handle_response)
    return captured


def _parse_lessons_from_raw(raw: dict) -> list[Lesson]:
    """Parse structured Lesson objects from the raw JS extraction data."""
    lessons: list[Lesson] = []
    seen_uids: set[str] = set()

    for item in raw.get("lessons", []):
        text = item.get("text", "")
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
            continue

        # Try to parse name, time, instructor from the text lines
        name = item.get("name") or (lines[0] if lines else "Unknown")
        time_str = item.get("time", "")
        instructor = ""
        time_start = ""
        time_end = ""

        # Look for time pattern HH:MM in the text
        time_pattern = re.compile(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})")
        single_time = re.compile(r"(\d{1,2}:\d{2})")

        for line in lines:
            m = time_pattern.search(line)
            if m:
                time_start = m.group(1)
                time_end = m.group(2)
                break

        if not time_start and time_str:
            m = time_pattern.search(time_str)
            if m:
                time_start = m.group(1)
                time_end = m.group(2)
            else:
                m = single_time.search(time_str)
                if m:
                    time_start = m.group(1)

        # Check for availability indicators
        bookable = False
        spots_available = None
        spots_total = None

        spots_pattern = re.compile(r"(\d+)\s*/\s*(\d+)")
        for line in lines:
            sm = spots_pattern.search(line)
            if sm:
                spots_available = int(sm.group(1))
                spots_total = int(sm.group(2))
                bookable = spots_available > 0
                break

        if not bookable:
            text_lower = text.lower()
            bookable = any(
                kw in text_lower
                for kw in ["beschikbaar", "available", "boek", "book", "inschrijven"]
            )
            # "vol" = full in Dutch
            if any(kw in text_lower for kw in ["vol", "full", "sold out", "volgeboekt"]):
                bookable = False

        # Use today's date as fallback — we'll refine with actual date from page
        date = datetime.now().strftime("%Y-%m-%d")

        lesson = Lesson(
            name=name,
            date=date,
            time_start=time_start or "??:??",
            time_end=time_end or "??:??",
            instructor=instructor,
            spots_available=spots_available,
            spots_total=spots_total,
            bookable=bookable,
        )

        if lesson.uid not in seen_uids:
            seen_uids.add(lesson.uid)
            lessons.append(lesson)

    return lessons


def _parse_lessons_from_api(api_data: list[dict]) -> list[Lesson]:
    """Parse lessons from intercepted API responses."""
    lessons: list[Lesson] = []
    seen: set[str] = set()

    for entry in api_data:
        data = entry.get("data", {})
        # Virtuagym API typically returns a list of events
        events = []
        if isinstance(data, list):
            events = data
        elif isinstance(data, dict):
            # Could be nested: {"data": [...], "result": [...], "events": [...]}
            for key in ("data", "result", "events", "classes", "items"):
                if isinstance(data.get(key), list):
                    events = data[key]
                    break

        for event in events:
            if not isinstance(event, dict):
                continue

            name = (
                event.get("name")
                or event.get("title")
                or event.get("class_name")
                or event.get("event_name")
                or "Unknown"
            )
            date_str = event.get("date") or event.get("start_date") or ""
            time_start = event.get("start_time") or event.get("time_from") or ""
            time_end = event.get("end_time") or event.get("time_until") or ""
            instructor = event.get("instructor") or event.get("trainer") or ""
            location = event.get("location") or event.get("room") or ""

            spots_available = event.get("spots_available") or event.get("free_spots")
            spots_total = event.get("spots_total") or event.get("max_participants")
            bookable = event.get("bookable", False)

            if spots_available is not None:
                try:
                    spots_available = int(spots_available)
                    bookable = spots_available > 0
                except (ValueError, TypeError):
                    spots_available = None

            if spots_total is not None:
                try:
                    spots_total = int(spots_total)
                except (ValueError, TypeError):
                    spots_total = None

            if not date_str:
                date_str = datetime.now().strftime("%Y-%m-%d")

            lesson = Lesson(
                name=str(name),
                date=str(date_str),
                time_start=str(time_start),
                time_end=str(time_end),
                instructor=str(instructor) if instructor else "",
                location=str(location) if location else "",
                spots_available=spots_available,
                spots_total=spots_total,
                bookable=bookable,
            )

            if lesson.uid not in seen:
                seen.add(lesson.uid)
                lessons.append(lesson)

    return lessons


async def scrape_schedule(url: str) -> list[Lesson]:
    """Scrape the SportCity group lesson schedule and return available lessons.

    Uses a headless Chromium browser to:
    1. Load the schedule page
    2. Dismiss cookie banners
    3. Intercept API calls for schedule data
    4. Extract lesson info from the rendered DOM
    5. Check for iframe-embedded schedules
    """
    logger.info("Scraping schedule from %s", url)
    all_lessons: list[Lesson] = []

    async with async_playwright() as pw:
        # Use CHROMIUM_PATH env var if set, otherwise let Playwright find it
        chromium_path = os.environ.get("CHROMIUM_PATH") or None
        browser = await pw.chromium.launch(
            headless=True,
            **({"executable_path": chromium_path} if chromium_path else {}),
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="nl-NL",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # Set up API interception before navigating
        api_data = await _intercept_api_calls(page)

        try:
            await page.goto(url, wait_until="networkidle", timeout=30_000)
        except Exception as e:
            logger.warning("Page load timeout/error (continuing anyway): %s", e)

        # Dismiss cookie banners
        await _accept_cookies(page)

        # Wait a bit for dynamic content to load
        await page.wait_for_timeout(3000)

        # Check for iframe-embedded schedule
        schedule_frame = await _find_and_enter_iframe(page)
        target = schedule_frame or page

        # Extract lesson data from the DOM
        try:
            raw = await target.evaluate(EXTRACT_LESSONS_JS)
            logger.info(
                "DOM extraction: %d lesson elements, %d iframes, %d globals, body=%d chars",
                len(raw.get("lessons", [])),
                len(raw.get("iframes", [])),
                len(raw.get("globals", [])),
                raw.get("bodyLength", 0),
            )

            # If there are iframes we haven't explored, log them
            for iframe in raw.get("iframes", []):
                logger.info("  iframe: src=%s", iframe.get("src", ""))

            dom_lessons = _parse_lessons_from_raw(raw)
            all_lessons.extend(dom_lessons)
        except Exception as e:
            logger.error("DOM extraction failed: %s", e)

        # Parse any intercepted API data
        api_lessons = _parse_lessons_from_api(api_data)
        # Merge, preferring API data (more structured)
        existing_uids = {l.uid for l in all_lessons}
        for lesson in api_lessons:
            if lesson.uid not in existing_uids:
                all_lessons.append(lesson)

        # Debug: save a screenshot for troubleshooting
        try:
            await page.screenshot(path="/tmp/sportcity_schedule.png", full_page=True)
            logger.info("Debug screenshot saved to /tmp/sportcity_schedule.png")
        except Exception:
            pass

        # Debug: save page HTML
        try:
            html = await page.content()
            with open("/tmp/sportcity_schedule.html", "w") as f:
                f.write(html)
            logger.info("Debug HTML saved to /tmp/sportcity_schedule.html")
        except Exception:
            pass

        await browser.close()

    logger.info("Scraped %d total lessons (%d bookable)", len(all_lessons), sum(1 for l in all_lessons if l.bookable))
    return all_lessons

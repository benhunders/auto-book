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
import tempfile
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page, async_playwright

from .models import Lesson

logger = logging.getLogger(__name__)

# Where to store debug output (screenshot + HTML)
DEBUG_DIR = Path(tempfile.gettempdir()) / "sportcity_debug"


# ---------------------------------------------------------------------------
# JS extraction script injected into the page.
# ---------------------------------------------------------------------------
EXTRACT_LESSONS_JS = """
() => {
    const lessons = [];

    // --- Strategy 1: look for Virtuagym event blocks ---
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
            if (el.offsetHeight < 10) continue;
            const text = el.innerText || el.textContent || '';
            if (text.trim().length < 3) continue;

            const data = el.dataset || {};
            lessons.push({
                name: data.eventName || data.className || data.name || '',
                time: data.time || data.startTime || '',
                text: text.trim().substring(0, 500),
                html: el.innerHTML.substring(0, 1000),
                tag: el.tagName,
                classes: el.className,
                dataAttrs: JSON.stringify(data),
            });
        }
    }

    // --- Strategy 2: table rows ---
    const tables = document.querySelectorAll('table');
    for (const table of tables) {
        for (const row of table.querySelectorAll('tr')) {
            const cells = row.querySelectorAll('td, th');
            if (cells.length >= 2) {
                const text = row.innerText || '';
                if (text.trim().length > 5) {
                    lessons.push({
                        name: '', time: '',
                        text: text.trim().substring(0, 500),
                        html: row.innerHTML.substring(0, 1000),
                        tag: 'TR', classes: row.className, dataAttrs: '{}',
                    });
                }
            }
        }
    }

    // --- Strategy 3: iframes ---
    const iframes = [];
    for (const iframe of document.querySelectorAll('iframe')) {
        iframes.push({ src: iframe.src || '', id: iframe.id || '', name: iframe.name || '' });
    }

    // --- Strategy 4: global JS variables ---
    const globals = [];
    for (const key of Object.keys(window)) {
        try {
            const val = window[key];
            if (val && typeof val === 'object' && !Array.isArray(val)) {
                const str = JSON.stringify(val).substring(0, 300);
                if (/class|event|lesson|schedule|rooster/i.test(str)) {
                    globals.push({ key, preview: str });
                }
            }
        } catch(e) {}
    }

    return {
        lessons, iframes, globals,
        title: document.title,
        url: window.location.href,
        bodyLength: document.body.innerHTML.length,
    };
}
"""


async def _accept_cookies(page: Page) -> None:
    """Try to dismiss cookie / consent banners aggressively."""
    # Cookiebot is used on sportcity.nl — try its specific button first
    selectors = [
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "#CybotCookiebotDialogBodyButtonAccept",
        "button:has-text('Alles accepteren')",
        "button:has-text('Alle cookies accepteren')",
        "button:has-text('Accepteer')",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('Akkoord')",
        "button:has-text('OK')",
        "[id*='cookie'] button",
        "[class*='cookie'] button",
        "[id*='consent'] button",
        "a:has-text('Accepteer')",
        "a:has-text('Accept')",
    ]
    for selector in selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                logger.info("Dismissed cookie banner with: %s", selector)
                await page.wait_for_timeout(1000)
                return
        except Exception:
            continue
    logger.info("No cookie banner found (or already dismissed)")


async def _wait_for_schedule(page: Page) -> None:
    """Wait for the schedule widget to appear on the page."""
    # Try various selectors that a schedule widget might match
    schedule_selectors = [
        "[class*='schedule']",
        "[class*='rooster']",
        "[class*='timetable']",
        "[class*='calendar']",
        "[class*='event']",
        "[class*='virtuagym']",
        "iframe[src*='virtuagym']",
        "iframe[src*='classes']",
        "table",
    ]
    for selector in schedule_selectors:
        try:
            await page.wait_for_selector(selector, timeout=5000)
            logger.info("Schedule widget detected with selector: %s", selector)
            return
        except Exception:
            continue
    logger.warning("No schedule widget detected after waiting")


async def _intercept_api_calls(page: Page) -> list[dict]:
    """Capture any XHR/fetch responses that look like schedule data."""
    captured: list[dict] = []

    async def handle_response(response):
        url = response.url
        if any(
            kw in url.lower()
            for kw in ["class", "event", "schedule", "lesson", "rooster", "virtuagym"]
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

        name = item.get("name") or (lines[0] if lines else "Unknown")
        time_str = item.get("time", "")
        instructor = ""
        time_start = ""
        time_end = ""

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
            if any(kw in text_lower for kw in ["vol", "full", "sold out", "volgeboekt"]):
                bookable = False

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
        events = []
        if isinstance(data, list):
            events = data
        elif isinstance(data, dict):
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


async def scrape_schedule(url: str, debug: bool = False) -> list[Lesson]:
    """Scrape the SportCity group lesson schedule and return available lessons."""
    logger.info("Scraping schedule from %s", url)
    all_lessons: list[Lesson] = []

    async with async_playwright() as pw:
        chromium_path = os.environ.get("CHROMIUM_PATH") or None
        browser = await pw.chromium.launch(
            headless=True,
            **({"executable_path": chromium_path} if chromium_path else {}),
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="nl-NL",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # Set up API interception before navigating
        api_data = await _intercept_api_calls(page)

        # Also capture ALL network requests for debugging
        network_log: list[str] = []

        def log_request(request):
            network_log.append(f">> {request.method} {request.url}")

        def log_response(response):
            network_log.append(f"<< {response.status} {response.url}")

        if debug:
            page.on("request", log_request)
            page.on("response", log_response)

        try:
            await page.goto(url, wait_until="networkidle", timeout=30_000)
        except Exception as e:
            logger.warning("Page load timeout/error (continuing anyway): %s", e)

        # Step 1: Dismiss cookie banners (critical — blocks content on sportcity.nl)
        await _accept_cookies(page)

        # Step 2: Wait for schedule widget to load after cookies are accepted
        await page.wait_for_timeout(3000)
        await _wait_for_schedule(page)

        # Step 3: Give extra time for dynamic content after schedule container appears
        await page.wait_for_timeout(3000)

        # Step 4: Explore all frames (main page + iframes)
        all_frames = page.frames
        logger.info("Page has %d frames total", len(all_frames))

        for i, frame in enumerate(all_frames):
            frame_url = frame.url
            logger.info("  Frame %d: %s", i, frame_url[:120] if frame_url else "(empty)")

            # Extract from every frame that might have content
            try:
                raw = await frame.evaluate(EXTRACT_LESSONS_JS)

                n_lessons = len(raw.get("lessons", []))
                n_iframes = len(raw.get("iframes", []))
                n_globals = len(raw.get("globals", []))
                body_len = raw.get("bodyLength", 0)

                if n_lessons > 0 or n_globals > 0 or body_len > 1000:
                    logger.info(
                        "  Frame %d extraction: %d elements, %d globals, body=%d chars",
                        i, n_lessons, n_globals, body_len,
                    )

                    for g in raw.get("globals", []):
                        logger.info("    Global: %s = %s", g["key"], g["preview"][:200])

                dom_lessons = _parse_lessons_from_raw(raw)
                existing_uids = {l.uid for l in all_lessons}
                for lesson in dom_lessons:
                    if lesson.uid not in existing_uids:
                        all_lessons.append(lesson)

            except Exception as e:
                logger.debug("  Frame %d extraction failed: %s", i, e)

        # Parse any intercepted API data
        api_lessons = _parse_lessons_from_api(api_data)
        existing_uids = {l.uid for l in all_lessons}
        for lesson in api_lessons:
            if lesson.uid not in existing_uids:
                all_lessons.append(lesson)

        # Save debug info
        if debug:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            try:
                await page.screenshot(
                    path=str(DEBUG_DIR / "screenshot.png"), full_page=True
                )
                logger.info("Screenshot saved to %s", DEBUG_DIR / "screenshot.png")
            except Exception as e:
                logger.warning("Screenshot failed: %s", e)

            try:
                html = await page.content()
                (DEBUG_DIR / "page.html").write_text(html, encoding="utf-8")
                logger.info("HTML saved to %s", DEBUG_DIR / "page.html")
            except Exception as e:
                logger.warning("HTML save failed: %s", e)

            try:
                (DEBUG_DIR / "network.log").write_text(
                    "\n".join(network_log), encoding="utf-8"
                )
                logger.info("Network log saved to %s", DEBUG_DIR / "network.log")
                logger.info("Captured %d API responses", len(api_data))
                for entry in api_data:
                    logger.info("  API: %s", entry["url"][:150])
            except Exception as e:
                logger.warning("Network log save failed: %s", e)

            if api_data:
                try:
                    (DEBUG_DIR / "api_responses.json").write_text(
                        json.dumps(api_data, indent=2, default=str),
                        encoding="utf-8",
                    )
                    logger.info("API responses saved to %s", DEBUG_DIR / "api_responses.json")
                except Exception:
                    pass

        await browser.close()

    logger.info(
        "Scraped %d total lessons (%d bookable)",
        len(all_lessons),
        sum(1 for l in all_lessons if l.bookable),
    )
    return all_lessons

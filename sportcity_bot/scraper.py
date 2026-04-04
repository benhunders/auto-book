"""Scrape the SportCity group lesson schedule.

SportCity uses PerfectGym as their gym management platform. The schedule data
can be obtained two ways:

1. **Direct API** (preferred): Hit the SportCity Next.js API or PerfectGym API
   to get structured JSON data — no browser needed.
2. **Playwright fallback**: Render the page in a headless browser and extract
   lesson data from the DOM text content.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from .models import Lesson

logger = logging.getLogger(__name__)

DEBUG_DIR = Path(tempfile.gettempdir()) / "sportcity_debug"

# Mapping of SportCity club slugs to PerfectGym club IDs.
# To find yours: open browser DevTools on the groepslesrooster page and look
# for requests to sportcity.perfectgym.com or check /api/club-details.
# This will be populated automatically on first run if possible.
CLUB_IDS: dict[str, int] = {}

# The SportCity internal API that the Next.js frontend calls
SPORTCITY_API = "https://www.sportcity.nl/api"
ELECTROLYTE_API = "https://electrolyte.sportcity.nl/api"
PERFECTGYM_API = "https://sportcity.perfectgym.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Referer": "https://www.sportcity.nl/",
}


# ---------------------------------------------------------------------------
# Strategy 1: Direct API calls (no browser needed)
# ---------------------------------------------------------------------------


async def _fetch_clubs(client: httpx.AsyncClient) -> list[dict]:
    """Fetch list of all SportCity clubs from their internal API."""
    try:
        resp = await client.get(f"{ELECTROLYTE_API}/website/clubs", headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "result", "clubs", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []
    except Exception as e:
        logger.warning("Failed to fetch clubs from electrolyte API: %s", e)

    # Fallback: try the Next.js API
    try:
        resp = await client.get(f"{SPORTCITY_API}/club-details", headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return []
    except Exception as e:
        logger.warning("Failed to fetch club details: %s", e)
        return []


async def _find_club_id(client: httpx.AsyncClient, club_slug: str) -> int | None:
    """Try to find the PerfectGym club ID for a given club slug."""
    if club_slug in CLUB_IDS:
        return CLUB_IDS[club_slug]

    clubs = await _fetch_clubs(client)
    for club in clubs:
        if not isinstance(club, dict):
            continue
        # Try to match by slug, name, or URL
        slug = club.get("slug", "") or club.get("url", "") or ""
        name = club.get("name", "") or ""
        club_id = club.get("id") or club.get("clubId") or club.get("club_id")
        pg_id = club.get("perfectGymId") or club.get("perfectgym_id") or club.get("pgClubId")

        if club_slug in slug.lower() or club_slug.replace("-", " ") in name.lower():
            resolved_id = pg_id or club_id
            if resolved_id:
                try:
                    resolved_id = int(resolved_id)
                    CLUB_IDS[club_slug] = resolved_id
                    logger.info("Found club ID %d for slug '%s'", resolved_id, club_slug)
                    return resolved_id
                except (ValueError, TypeError):
                    pass

    logger.warning("Could not find club ID for slug '%s'", club_slug)
    return None


async def _fetch_classes_from_perfectgym(
    client: httpx.AsyncClient, club_id: int
) -> list[dict]:
    """Fetch class schedule directly from PerfectGym API.

    Tries multiple API endpoint patterns since the exact path varies
    between PerfectGym versions and deployments.
    """
    today = datetime.now()
    start = today.strftime("%Y-%m-%dT00:00:00")
    end = (today + timedelta(days=7)).strftime("%Y-%m-%dT23:59:59")

    # Try multiple known PerfectGym API patterns
    endpoints = [
        f"{PERFECTGYM_API}/Api/v2/Classes/Classes",
        f"{PERFECTGYM_API}/Api/Classes/Classes",
        f"{PERFECTGYM_API}/Api/v2/GroupActivities/GroupActivities",
        f"{PERFECTGYM_API}/Api/GroupActivities/GroupActivities",
        f"{PERFECTGYM_API}/Api/v2/Calendar/Classes",
        f"{PERFECTGYM_API}/Api/Calendar/Classes",
    ]

    params = {"clubId": club_id, "startDate": start, "endDate": end}

    for url in endpoints:
        try:
            resp = await client.get(url, params=params, headers=HEADERS)
            if resp.status_code == 404:
                logger.debug("PerfectGym endpoint not found: %s", url)
                continue
            resp.raise_for_status()
            data = resp.json()
            logger.info("PerfectGym API success: %s", url)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("data", "result", "elements", "classes", "items"):
                    if isinstance(data.get(key), list):
                        return data[key]
        except httpx.HTTPStatusError:
            continue
        except Exception as e:
            logger.debug("PerfectGym endpoint error (%s): %s", url, e)
            continue

    logger.warning("No PerfectGym API endpoint returned classes")
    return []


async def _fetch_schedule_via_nextjs(
    client: httpx.AsyncClient, url: str
) -> str | None:
    """Fetch the schedule page via Next.js RSC POST (returns HTML/RSC payload)."""
    try:
        # The Next.js app makes a POST to the same URL for RSC data
        rsc_headers = {
            **HEADERS,
            "Next-Router-State-Tree": "",
            "RSC": "1",
            "Next-Url": url.replace("https://www.sportcity.nl", ""),
        }
        resp = await client.post(url, headers=rsc_headers)
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        logger.debug("Next.js RSC fetch failed: %s", e)
    return None


def _parse_perfectgym_classes(classes: list[dict]) -> list[Lesson]:
    """Convert PerfectGym API class data to Lesson objects."""
    lessons: list[Lesson] = []
    seen: set[str] = set()

    for cls in classes:
        if not isinstance(cls, dict):
            continue

        name = (
            cls.get("name")
            or cls.get("className")
            or cls.get("title")
            or "Unknown"
        )
        start_date = cls.get("startDate") or cls.get("timestamp") or ""
        end_date = cls.get("endDate") or ""

        # Parse date and times
        date_str = ""
        time_start = ""
        time_end = ""

        if start_date:
            try:
                dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                date_str = dt.strftime("%Y-%m-%d")
                time_start = dt.strftime("%H:%M")
            except (ValueError, TypeError):
                date_str = str(start_date)[:10]

        if end_date:
            try:
                dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                time_end = dt.strftime("%H:%M")
            except (ValueError, TypeError):
                pass

        instructor = cls.get("instructorName") or cls.get("instructor") or ""
        location = cls.get("roomName") or cls.get("room") or cls.get("location") or ""

        # Availability
        capacity = cls.get("capacity") or cls.get("maxParticipants")
        booked = cls.get("bookedCount") or cls.get("participantsCount") or 0
        is_bookable = cls.get("isBookable") or cls.get("bookable")
        waiting = cls.get("isWaitlistAvailable", False)

        spots_total = None
        spots_available = None
        bookable = False

        if capacity is not None:
            try:
                spots_total = int(capacity)
                spots_available = max(0, spots_total - int(booked))
                bookable = spots_available > 0
            except (ValueError, TypeError):
                pass

        if is_bookable is not None:
            bookable = bool(is_bookable)

        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")

        lesson = Lesson(
            name=str(name),
            date=date_str,
            time_start=time_start or "??:??",
            time_end=time_end or "??:??",
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


# ---------------------------------------------------------------------------
# Strategy 2: Playwright DOM extraction (fallback)
# ---------------------------------------------------------------------------

EXTRACT_SCHEDULE_JS = """
() => {
    // The SportCity schedule page renders lessons as list items/rows.
    // Each lesson shows: time range, name, duration, instructor, location.
    // We scan ALL text nodes looking for time patterns.
    const results = [];
    const allElements = document.querySelectorAll('*');

    for (const el of allElements) {
        if (el.children.length > 10) continue;

        const text = (el.innerText || el.textContent || '').trim();
        if (!text || text.length < 10 || text.length > 500) continue;

        const timeMatch = text.match(/(\\d{1,2}:\\d{2})\\s*[-–]\\s*(\\d{1,2}:\\d{2})/);
        if (!timeMatch) continue;

        const afterTime = text.substring(text.indexOf(timeMatch[0]) + timeMatch[0].length).trim();
        if (afterTime.length < 2) continue;

        // Track vertical position so we can map to date headers
        const rect = el.getBoundingClientRect();

        results.push({
            text: text,
            timeStart: timeMatch[1],
            timeEnd: timeMatch[2],
            afterTime: afterTime,
            y: rect.top,
            tag: el.tagName,
            classes: (el.className || '').substring(0, 200),
        });
    }

    // Look for date headers (e.g., "3 april", "vrijdag", "zaterdag")
    // They can appear as "3 april" or combined with day name
    const dateHeaders = [];
    const datePattern = /(\\d{1,2})\\s+(januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december)/i;
    for (const el of allElements) {
        const text = (el.innerText || el.textContent || '').trim();
        if (text.length > 50) continue;
        const m = text.match(datePattern);
        if (m) {
            dateHeaders.push({
                text: text,
                day: parseInt(m[1]),
                month: m[2].toLowerCase(),
                y: el.getBoundingClientRect().top,
            });
        }
    }

    return { results, dateHeaders, url: window.location.href };
}
"""

DUTCH_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "augustus": 8,
    "september": 9, "oktober": 10, "november": 11, "december": 12,
}


def _parse_dom_schedule(raw: dict) -> list[Lesson]:
    """Parse lessons from the DOM extraction results."""
    lessons: list[Lesson] = []
    seen: set[str] = set()

    # Build date mapping from headers: (y_position, date_string)
    now = datetime.now()
    date_map: list[tuple[float, str]] = []

    for dh in raw.get("dateHeaders", []):
        day = dh.get("day", 0)
        month_name = dh.get("month", "")
        month = DUTCH_MONTHS.get(month_name, 0)
        if day and month:
            year = now.year
            if month < now.month - 1:
                year += 1
            date_str = f"{year}-{month:02d}-{day:02d}"
            y = dh.get("y", 0)
            date_map.append((y, date_str))

    date_map.sort()
    fallback_date = now.strftime("%Y-%m-%d")
    logger.info("Date headers: %s", [(d, y) for y, d in date_map])

    def _date_for_y(y: float) -> str:
        """Find the date for a lesson based on its Y position relative to date headers."""
        result = fallback_date
        for header_y, date_str in date_map:
            if y >= header_y:
                result = date_str
            else:
                break
        return result

    for item in raw.get("results", []):
        text = item.get("text", "")
        time_start = item.get("timeStart", "")
        time_end = item.get("timeEnd", "")
        after_time = item.get("afterTime", "")
        lesson_y = item.get("y", 0)

        # Parse name: typically the first word(s) after the time
        # Format seen: "08:00 - 09:00  BodyPump\n60 min Conny Zaal 1"
        lines = [l.strip() for l in after_time.split("\n") if l.strip()]
        name = lines[0] if lines else "Unknown"
        instructor = ""
        location = ""

        if len(lines) >= 2:
            # Second line: "60 min Conny Zaal 1" or "45 min Marian Zaal 2"
            detail = lines[1]
            # Remove duration prefix like "60 min" or "45 min"
            detail = re.sub(r"^\d+\s*min\s*", "", detail).strip()
            # Try to split: last word(s) may be location (Zaal 1, SuperCycle Zaal, etc.)
            # Common location patterns: "Zaal 1", "Zaal 2", "SuperCycle Zaal",
            # "Functionele Zone SGT", "Online Groepslessen"
            loc_match = re.search(
                r"\s+((?:Zaal|Studio|SuperCycle|Functionele|Online)\s*.*)$",
                detail,
                re.IGNORECASE,
            )
            if loc_match:
                location = loc_match.group(1).strip()
                instructor = detail[: loc_match.start()].strip()
            else:
                instructor = detail

        # Assign correct date based on vertical position
        date = _date_for_y(lesson_y)

        lesson = Lesson(
            name=name,
            date=date,
            time_start=time_start,
            time_end=time_end,
            instructor=instructor,
            location=location,
            bookable=True,  # Listed on the public schedule = bookable
        )

        if lesson.uid not in seen:
            seen.add(lesson.uid)
            lessons.append(lesson)

    return lessons


async def _extract_current_week(page) -> dict:
    """Extract schedule data from the currently visible week."""
    # Scroll down to trigger lazy loading, then back up
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(1500)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(500)

    return await page.evaluate(EXTRACT_SCHEDULE_JS)


async def _click_next_week(page) -> bool:
    """Click the forward arrow to go to the next week. Returns True if successful."""
    # The arrow is next to "Week XX, DD mmm - DD mmm"
    # Look for a clickable forward arrow element
    next_selectors = [
        "button:has-text('→')",
        "a:has-text('→')",
        "[class*='next']",
        "[class*='forward']",
        "[aria-label*='next']",
        "[aria-label*='volgende']",
    ]

    # Also try: the right arrow near the week header
    # From the screenshot it's a simple "→" link/button near the week title
    for selector in next_selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                await page.wait_for_timeout(3000)  # Wait for new week to load
                return True
        except Exception:
            continue

    # Fallback: try to find any clickable element with an arrow-right SVG or "›" text
    # near the week header
    try:
        # Look for SVG arrows or link elements near "Week" text
        arrow = page.locator(
            "svg[class*='arrow'], svg[class*='right'], "
            "a[href*='week'], button[class*='arrow'], "
            "[class*='week'] a, [class*='week'] button, "
            "[class*='navigation'] a:last-child, "
            "[class*='navigation'] button:last-child"
        ).last
        if await arrow.is_visible(timeout=1000):
            await arrow.click()
            await page.wait_for_timeout(3000)
            return True
    except Exception:
        pass

    # Last resort: try keyboard navigation
    try:
        await page.keyboard.press("ArrowRight")
        await page.wait_for_timeout(3000)
        return True
    except Exception:
        pass

    return False


async def _scrape_with_playwright(
    url: str, weeks_ahead: int = 2, debug: bool = False
) -> list[Lesson]:
    """Scrape the schedule for the current week plus future weeks."""
    logger.info("Playwright: loading %s (weeks_ahead=%d)", url, weeks_ahead)

    all_lessons: list[Lesson] = []
    seen_uids: set[str] = set()

    async with async_playwright() as pw:
        chromium_path = os.environ.get("CHROMIUM_PATH") or None
        browser = await pw.chromium.launch(
            headless=True,
            **({"executable_path": chromium_path} if chromium_path else {}),
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="nl-NL",
            user_agent=HEADERS["User-Agent"],
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="networkidle", timeout=30_000)
        except Exception as e:
            logger.warning("Page load timeout (continuing): %s", e)

        # Dismiss Cookiebot banner
        for selector in [
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
            "#CybotCookiebotDialogBodyButtonAccept",
            "button:has-text('Accepteer')",
        ]:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    logger.info("Dismissed cookie banner")
                    await page.wait_for_timeout(1000)
                    break
            except Exception:
                continue

        # Wait for schedule content to render
        await page.wait_for_timeout(5000)

        # Scrape current week + future weeks
        for week_num in range(1 + weeks_ahead):
            week_label = f"week {week_num}" if week_num == 0 else f"week +{week_num}"

            raw = await _extract_current_week(page)
            n_matches = len(raw.get("results", []))
            n_dates = len(raw.get("dateHeaders", []))
            logger.info(
                "Extracted %s: %d lessons, %d date headers",
                week_label, n_matches, n_dates,
            )

            # Parse lessons from this week
            week_lessons = _parse_dom_schedule(raw)
            for lesson in week_lessons:
                if lesson.uid not in seen_uids:
                    seen_uids.add(lesson.uid)
                    all_lessons.append(lesson)

            logger.info(
                "  %s: %d unique lessons (running total: %d)",
                week_label, len(week_lessons), len(all_lessons),
            )

            # Navigate to next week (unless this is the last iteration)
            if week_num < weeks_ahead:
                if not await _click_next_week(page):
                    logger.warning("Could not navigate to next week, stopping")
                    break

            if debug and week_num == 0:
                DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                try:
                    await page.screenshot(
                        path=str(DEBUG_DIR / "screenshot.png"), full_page=True
                    )
                    logger.info("Screenshot: %s", DEBUG_DIR / "screenshot.png")
                except Exception:
                    pass
                try:
                    html = await page.content()
                    (DEBUG_DIR / "page.html").write_text(html, encoding="utf-8")
                except Exception:
                    pass
                try:
                    (DEBUG_DIR / "extraction.json").write_text(
                        json.dumps(raw, indent=2, default=str), encoding="utf-8"
                    )
                except Exception:
                    pass

        await browser.close()

    return all_lessons


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _extract_club_slug(url: str) -> str:
    """Extract the club slug from a SportCity URL.

    E.g. "https://www.sportcity.nl/utrecht/leidsche-rijn-sportpark/groepslesrooster"
    -> "leidsche-rijn-sportpark"
    """
    parts = url.rstrip("/").split("/")
    # URL pattern: /[city]/[club-slug]/groepslesrooster
    if "groepslesrooster" in parts:
        idx = parts.index("groepslesrooster")
        if idx >= 1:
            return parts[idx - 1]
    # Fallback: second-to-last segment
    if len(parts) >= 2:
        return parts[-2]
    return ""


async def scrape_schedule(
    url: str, weeks_ahead: int = 2, debug: bool = False
) -> list[Lesson]:
    """Scrape the SportCity schedule. Tries direct API first, falls back to Playwright.

    Args:
        url: The groepslesrooster page URL.
        weeks_ahead: How many additional weeks to scrape beyond the current one.
        debug: Save screenshots and extraction data for debugging.
    """
    logger.info("Scraping schedule from %s (%d weeks ahead)", url, weeks_ahead)
    club_slug = _extract_club_slug(url)
    logger.info("Club slug: %s", club_slug)

    # Strategy 1: Try PerfectGym API directly
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        club_id = await _find_club_id(client, club_slug)

        if club_id:
            classes = await _fetch_classes_from_perfectgym(client, club_id)
            if classes:
                lessons = _parse_perfectgym_classes(classes)
                if lessons:
                    logger.info(
                        "PerfectGym API: found %d lessons (%d bookable)",
                        len(lessons),
                        sum(1 for l in lessons if l.bookable),
                    )
                    return lessons

        logger.info("Direct API did not return lessons, falling back to Playwright")

    # Strategy 2: Playwright DOM extraction with week navigation
    lessons = await _scrape_with_playwright(url, weeks_ahead=weeks_ahead, debug=debug)
    logger.info(
        "Playwright: found %d lessons (%d bookable)",
        len(lessons),
        sum(1 for l in lessons if l.bookable),
    )
    return lessons

"""Interactive Telegram bot with button-based UI for lesson watching.

The user interacts entirely through buttons — no commands to memorize.
Tapping /start (or any message) shows the main menu with:

  [ Add lessons ]  [ Settings ]

- "Add lessons" shows a picker of all unique lesson types found on the
  schedule, grouped as buttons. Tap to toggle watch on/off.
- "Settings" shows current watch list, status, and remove buttons.

Phase 1: The booking callback acknowledges the tap and logs it.
Phase 2: Will wire up actual SportCity booking via their auth flow.
"""

from __future__ import annotations

import logging
from math import ceil

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .models import Lesson
from .watchlist import WatchList

logger = logging.getLogger(__name__)

# Callback data prefixes
BOOK = "book:"
ADD = "add:"
REMOVE = "rm:"
MENU = "menu"
MENU_ADD = "menu_add"
MENU_ADD_PAGE = "menu_add_p:"
MENU_SETTINGS = "menu_settings"
MENU_STATUS = "menu_status"
REFRESH = "refresh"


def build_app(token: str, watchlist: WatchList) -> Application:
    """Create and configure the Telegram bot application."""
    app = Application.builder().token(token).build()
    app.bot_data["watchlist"] = watchlist
    app.bot_data["known_lesson_names"] = set()

    # Commands
    app.add_handler(CommandHandler("start", _cmd_main_menu))
    app.add_handler(CommandHandler("help", _cmd_main_menu))
    # Legacy text commands still work
    app.add_handler(CommandHandler("watch", _cmd_watch_text))
    app.add_handler(CommandHandler("unwatch", _cmd_unwatch_text))

    # Button callbacks
    app.add_handler(CallbackQueryHandler(_cb_main_menu, pattern=f"^{MENU}$"))
    app.add_handler(CallbackQueryHandler(_cb_add_lessons, pattern=f"^{MENU_ADD}$"))
    app.add_handler(CallbackQueryHandler(_cb_add_lessons_page, pattern=f"^{MENU_ADD_PAGE}"))
    app.add_handler(CallbackQueryHandler(_cb_settings, pattern=f"^{MENU_SETTINGS}$"))
    app.add_handler(CallbackQueryHandler(_cb_status, pattern=f"^{MENU_STATUS}$"))
    app.add_handler(CallbackQueryHandler(_cb_add, pattern=f"^{ADD}"))
    app.add_handler(CallbackQueryHandler(_cb_remove, pattern=f"^{REMOVE}"))
    app.add_handler(CallbackQueryHandler(_cb_refresh, pattern=f"^{REFRESH}$"))
    app.add_handler(CallbackQueryHandler(_cb_book, pattern=f"^{BOOK}"))

    # Any other text message -> show main menu
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _cmd_main_menu))

    return app


# ---------------------------------------------------------------------------
# Main Menu
# ---------------------------------------------------------------------------

def _main_menu_keyboard(wl: WatchList) -> InlineKeyboardMarkup:
    count = len(wl.names)
    watching_label = f"Settings ({count} watched)" if count else "Settings"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Add lessons", callback_data=MENU_ADD),
            InlineKeyboardButton(watching_label, callback_data=MENU_SETTINGS),
        ],
        [
            InlineKeyboardButton("Status", callback_data=MENU_STATUS),
        ],
    ])


def _main_menu_text(wl: WatchList) -> str:
    if wl.is_empty:
        return (
            "Welcome to *SportCity Lesson Watcher*\n\n"
            "I'll notify you when lessons you want become available.\n\n"
            "Tap *Add lessons* to pick which classes to watch."
        )
    watched = ", ".join(wl.names)
    return (
        "*SportCity Lesson Watcher*\n\n"
        f"Watching: {watched}\n\n"
        "I'll notify you when new matching lessons appear on the schedule."
    )


async def _cmd_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wl: WatchList = context.application.bot_data["watchlist"]
    await update.message.reply_text(
        _main_menu_text(wl),
        parse_mode="Markdown",
        reply_markup=_main_menu_keyboard(wl),
    )


async def _cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    wl: WatchList = context.application.bot_data["watchlist"]
    await query.edit_message_text(
        _main_menu_text(wl),
        parse_mode="Markdown",
        reply_markup=_main_menu_keyboard(wl),
    )


# ---------------------------------------------------------------------------
# Add Lessons (picker from scraped lesson names)
# ---------------------------------------------------------------------------

LESSONS_PER_PAGE = 8


def _get_unique_lesson_names(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    """Get sorted unique lesson names from the most recent scrape."""
    return sorted(context.application.bot_data.get("known_lesson_names", set()))


def _add_lessons_keyboard(
    names: list[str], wl: WatchList, page: int = 0
) -> InlineKeyboardMarkup:
    """Build a paginated grid of lesson buttons. Watched ones get a checkmark."""
    total_pages = max(1, ceil(len(names) / LESSONS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    start = page * LESSONS_PER_PAGE
    page_names = names[start : start + LESSONS_PER_PAGE]

    rows: list[list[InlineKeyboardButton]] = []

    # Lesson buttons in rows of 2
    for i in range(0, len(page_names), 2):
        row = []
        for name in page_names[i : i + 2]:
            is_watched = wl.matches(name)
            label = f"{'✓ ' if is_watched else ''}{name}"
            # Toggle: if watched -> remove, else -> add
            cb = f"{REMOVE}{name}" if is_watched else f"{ADD}{name}"
            row.append(InlineKeyboardButton(label, callback_data=cb))
        rows.append(row)

    # Pagination row
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"{MENU_ADD_PAGE}{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"{MENU_ADD_PAGE}{page + 1}"))
        rows.append(nav)

    # Back button
    rows.append([
        InlineKeyboardButton("↻ Refresh list", callback_data=REFRESH),
        InlineKeyboardButton("◀ Back", callback_data=MENU),
    ])

    return InlineKeyboardMarkup(rows)


async def _cb_add_lessons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    wl: WatchList = context.application.bot_data["watchlist"]
    names = _get_unique_lesson_names(context)

    if not names:
        await query.edit_message_text(
            "No lessons found yet.\n\n"
            "The schedule hasn't been scraped yet, or no lessons were found.\n"
            "Tap *Refresh* to try again, or wait for the next automatic check.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↻ Refresh", callback_data=REFRESH)],
                [InlineKeyboardButton("◀ Back", callback_data=MENU)],
            ]),
        )
        return

    await query.edit_message_text(
        "Tap a lesson to watch/unwatch it.\n"
        "✓ = currently watching",
        reply_markup=_add_lessons_keyboard(names, wl, page=0),
    )


async def _cb_add_lessons_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    wl: WatchList = context.application.bot_data["watchlist"]
    names = _get_unique_lesson_names(context)
    page = int(query.data.removeprefix(MENU_ADD_PAGE))
    await query.edit_message_text(
        "Tap a lesson to watch/unwatch it.\n"
        "✓ = currently watching",
        reply_markup=_add_lessons_keyboard(names, wl, page=page),
    )


async def _cb_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    wl: WatchList = context.application.bot_data["watchlist"]
    name = query.data.removeprefix(ADD)
    wl.add(name)
    await query.answer(f"Now watching: {name}")
    # Re-render the picker on the current page
    names = _get_unique_lesson_names(context)
    # Figure out which page this name is on
    try:
        idx = names.index(name)
        page = idx // LESSONS_PER_PAGE
    except ValueError:
        page = 0
    await query.edit_message_reply_markup(
        reply_markup=_add_lessons_keyboard(names, wl, page=page),
    )


async def _cb_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    wl: WatchList = context.application.bot_data["watchlist"]
    name = query.data.removeprefix(REMOVE)
    wl.remove(name)
    await query.answer(f"Stopped watching: {name}")
    names = _get_unique_lesson_names(context)

    # If we came from the settings page, check if message starts with "Settings"
    msg_text = query.message.text or ""
    if msg_text.startswith("Settings") or msg_text.startswith("Currently watching"):
        # Re-render settings
        await _render_settings(query, wl, context)
        return

    # Otherwise re-render the picker
    try:
        idx = names.index(name)
        page = idx // LESSONS_PER_PAGE
    except ValueError:
        page = 0
    await query.edit_message_reply_markup(
        reply_markup=_add_lessons_keyboard(names, wl, page=page),
    )


async def _cb_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger an immediate schedule scrape to refresh the lesson list."""
    query = update.callback_query
    await query.answer("Refreshing schedule...")

    # Import here to avoid circular imports
    from .scraper import scrape_schedule
    from .config import settings

    try:
        lessons = await scrape_schedule(
            settings.sportcity_schedule_url,
            weeks_ahead=settings.weeks_ahead,
        )
        names = {l.name for l in lessons}
        context.application.bot_data["known_lesson_names"] = names
        await query.answer(f"Found {len(names)} lesson types!")
    except Exception as e:
        logger.error("Refresh failed: %s", e)
        await query.answer("Refresh failed, try again later.")
        return

    wl: WatchList = context.application.bot_data["watchlist"]
    sorted_names = sorted(names)
    await query.edit_message_text(
        f"Found {len(sorted_names)} lesson types.\n"
        "Tap a lesson to watch/unwatch it.\n"
        "✓ = currently watching",
        reply_markup=_add_lessons_keyboard(sorted_names, wl, page=0),
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

async def _render_settings(query, wl: WatchList, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the settings view."""
    bot_data = context.application.bot_data
    last_check = bot_data.get("last_check", "never")

    if wl.is_empty:
        await query.edit_message_text(
            "Settings\n\n"
            "You're not watching any lessons yet.\n"
            f"Last check: {last_check}\n\n"
            "Tap *Add lessons* to get started.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Add lessons", callback_data=MENU_ADD)],
                [InlineKeyboardButton("◀ Back", callback_data=MENU)],
            ]),
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    for name in wl.names:
        rows.append([
            InlineKeyboardButton(f"✕ {name}", callback_data=f"{REMOVE}{name}"),
        ])

    rows.append([
        InlineKeyboardButton("Add more", callback_data=MENU_ADD),
        InlineKeyboardButton("◀ Back", callback_data=MENU),
    ])

    await query.edit_message_text(
        f"Currently watching {len(wl.names)} lesson(s).\n"
        f"Last check: {last_check}\n\n"
        "Tap a lesson to stop watching it.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _cb_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    wl: WatchList = context.application.bot_data["watchlist"]
    await _render_settings(query, wl, context)


async def _cb_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    bot_data = context.application.bot_data
    wl: WatchList = bot_data["watchlist"]
    last_check = bot_data.get("last_check", "never")
    lessons_found = bot_data.get("lessons_found", 0)
    notified = bot_data.get("notified_count", 0)
    watched = ", ".join(wl.names) or "(none)"
    known = len(bot_data.get("known_lesson_names", set()))

    await query.edit_message_text(
        f"*Bot Status*\n\n"
        f"Last check: {last_check}\n"
        f"Lessons on schedule: {lessons_found}\n"
        f"Unique lesson types: {known}\n"
        f"Notifications sent: {notified}\n"
        f"Watching: {watched}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀ Back", callback_data=MENU)],
        ]),
    )


# ---------------------------------------------------------------------------
# Book callback (Phase 2 placeholder)
# ---------------------------------------------------------------------------

async def _cb_book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lesson_uid = query.data.removeprefix(BOOK)
    logger.info("Booking requested for lesson %s by user %s", lesson_uid, query.from_user.id)

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"Booking request received.\n"
        f"Phase 2: Automatic booking will be wired up here.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Legacy text commands (still work for power users)
# ---------------------------------------------------------------------------

async def _cmd_watch_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wl: WatchList = context.application.bot_data["watchlist"]
    if not context.args:
        await _cmd_main_menu(update, context)
        return
    name = " ".join(context.args)
    if wl.add(name):
        await update.message.reply_text(f"Now watching *{name}*.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Already watching *{name}*.", parse_mode="Markdown")


async def _cmd_unwatch_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wl: WatchList = context.application.bot_data["watchlist"]
    if not context.args:
        await _cmd_main_menu(update, context)
        return
    name = " ".join(context.args)
    if wl.remove(name):
        await update.message.reply_text(f"Stopped watching *{name}*.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"*{name}* is not on your watch list.", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Notification sender (called by monitor)
# ---------------------------------------------------------------------------

async def send_lesson_notification(
    app: Application,
    chat_id: str,
    lesson: Lesson,
) -> None:
    """Send a Telegram message about a new lesson with a Book button."""
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Book now?", callback_data=f"{BOOK}{lesson.uid}")]]
    )

    await app.bot.send_message(
        chat_id=chat_id,
        text=f"New lesson available!\n\n{lesson.display}",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    logger.info("Sent notification for: %s on %s", lesson.name, lesson.date)

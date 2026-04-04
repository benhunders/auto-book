"""Telegram bot with watch list management and lesson notifications.

Commands:
    /start          - Welcome message with usage instructions
    /watch <name>   - Add a lesson to the watch list
    /unwatch <name> - Remove a lesson from the watch list
    /list           - Show all watched lessons
    /status         - Show bot status (last check, lessons found)

Phase 1: The booking callback acknowledges the tap and logs it.
Phase 2: Will wire up actual SportCity booking via their auth flow.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from .models import Lesson
from .watchlist import WatchList

logger = logging.getLogger(__name__)

BOOK_PREFIX = "book:"


def build_app(token: str, watchlist: WatchList) -> Application:
    """Create and configure the Telegram bot application."""
    app = Application.builder().token(token).build()
    app.bot_data["watchlist"] = watchlist

    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_start))
    app.add_handler(CommandHandler("watch", _cmd_watch))
    app.add_handler(CommandHandler("unwatch", _cmd_unwatch))
    app.add_handler(CommandHandler("list", _cmd_list))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CallbackQueryHandler(_handle_book_callback, pattern=f"^{BOOK_PREFIX}"))
    return app


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wl: WatchList = context.application.bot_data["watchlist"]
    watched = ", ".join(wl.names) if not wl.is_empty else "(none yet)"
    await update.message.reply_text(
        "*SportCity Lesson Watcher*\n\n"
        "I monitor the schedule and notify you when lessons you're "
        "interested in become available to book.\n\n"
        "*Commands:*\n"
        "/watch `BodyPump` — add a lesson to watch\n"
        "/unwatch `BodyPump` — stop watching\n"
        "/list — show your watch list\n"
        "/status — bot status\n\n"
        f"Currently watching: {watched}",
        parse_mode="Markdown",
    )


async def _cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wl: WatchList = context.application.bot_data["watchlist"]
    if not context.args:
        await update.message.reply_text(
            "Usage: /watch `lesson name`\n\n"
            "Examples:\n"
            "  /watch BodyPump\n"
            "  /watch Yoga\n"
            "  /watch Spinning\n"
            "  /watch Small Group HIIT\n\n"
            "The name is matched loosely — `/watch yoga` will match "
            "'Yoga', 'Power Yoga', 'Yin Yoga', etc.",
            parse_mode="Markdown",
        )
        return

    name = " ".join(context.args)
    if wl.add(name):
        await update.message.reply_text(
            f"Added *{name}* to your watch list.\n\n"
            f"I'll notify you when a matching lesson appears on the schedule.\n\n"
            f"Watching: {', '.join(wl.names)}",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"*{name}* is already on your watch list.",
            parse_mode="Markdown",
        )


async def _cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wl: WatchList = context.application.bot_data["watchlist"]
    if not context.args:
        await update.message.reply_text(
            "Usage: /unwatch `lesson name`\n\n"
            f"Currently watching: {', '.join(wl.names) or '(nothing)'}",
            parse_mode="Markdown",
        )
        return

    name = " ".join(context.args)
    if wl.remove(name):
        remaining = ", ".join(wl.names) or "(nothing)"
        await update.message.reply_text(
            f"Removed *{name}* from your watch list.\n\n"
            f"Still watching: {remaining}",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"*{name}* is not on your watch list.\n\n"
            f"Currently watching: {', '.join(wl.names) or '(nothing)'}",
            parse_mode="Markdown",
        )


async def _cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wl: WatchList = context.application.bot_data["watchlist"]
    if wl.is_empty:
        await update.message.reply_text(
            "Your watch list is empty.\n\n"
            "Use /watch `lesson name` to add lessons.\n"
            "Example: /watch BodyPump",
            parse_mode="Markdown",
        )
        return

    lines = [f"  • {name}" for name in wl.names]
    await update.message.reply_text(
        f"*Your watch list:*\n" + "\n".join(lines) + "\n\n"
        "I'll notify you when any of these appear on the schedule.",
        parse_mode="Markdown",
    )


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_data = context.application.bot_data
    wl: WatchList = bot_data["watchlist"]
    last_check = bot_data.get("last_check", "never")
    lessons_found = bot_data.get("lessons_found", 0)
    watched = ", ".join(wl.names) or "(nothing)"
    notified_count = bot_data.get("notified_count", 0)
    await update.message.reply_text(
        f"*Bot Status*\n\n"
        f"Last check: {last_check}\n"
        f"Lessons on schedule: {lessons_found}\n"
        f"Notifications sent: {notified_count}\n"
        f"Watching: {watched}",
        parse_mode="Markdown",
    )


async def _handle_book_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle the 'Book now?' button press."""
    query = update.callback_query
    await query.answer()

    lesson_uid = query.data.removeprefix(BOOK_PREFIX)
    logger.info("Booking requested for lesson %s by user %s", lesson_uid, query.from_user.id)

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"Booking request received for lesson `{lesson_uid}`.\n"
        f"Phase 2: Actual booking will be triggered here.",
        parse_mode="Markdown",
    )


async def send_lesson_notification(
    app: Application,
    chat_id: str,
    lesson: Lesson,
) -> None:
    """Send a Telegram message about a new lesson with a Book button."""
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Book now?", callback_data=f"{BOOK_PREFIX}{lesson.uid}")]]
    )

    await app.bot.send_message(
        chat_id=chat_id,
        text=f"New lesson available!\n\n{lesson.display}",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    logger.info("Sent notification for lesson: %s on %s (%s)", lesson.name, lesson.date, lesson.uid)

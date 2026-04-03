"""Telegram bot that sends lesson notifications with inline 'Book now?' buttons.

Phase 1: The booking callback acknowledges the tap and logs it.
Phase 2: Will wire up actual SportCity booking via their auth flow.
"""

from __future__ import annotations

import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from .models import Lesson

logger = logging.getLogger(__name__)

# Callback data prefix for booking actions
BOOK_PREFIX = "book:"


def build_app(token: str) -> Application:
    """Create and configure the Telegram bot application."""
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CallbackQueryHandler(_handle_book_callback, pattern=f"^{BOOK_PREFIX}"))
    return app


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "SportCity Auto-Booker is running!\n\n"
        "I'll notify you when group lessons have open spots.\n"
        "Use /status to check the bot status."
    )


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_data = context.application.bot_data
    last_check = bot_data.get("last_check", "never")
    lessons_found = bot_data.get("lessons_found", 0)
    bookable_found = bot_data.get("bookable_found", 0)
    await update.message.reply_text(
        f"*Bot Status*\n"
        f"Last check: {last_check}\n"
        f"Lessons found: {lessons_found}\n"
        f"Bookable: {bookable_found}",
        parse_mode="Markdown",
    )


async def _handle_book_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle the 'Book now?' button press."""
    query = update.callback_query
    await query.answer()

    # Extract lesson uid from callback data
    lesson_uid = query.data.removeprefix(BOOK_PREFIX)

    # Phase 2: this is where we'll trigger the actual booking
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
    """Send a Telegram message about an available lesson with a Book button."""
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Book now?", callback_data=f"{BOOK_PREFIX}{lesson.uid}")]]
    )

    await app.bot.send_message(
        chat_id=chat_id,
        text=f"Open spot detected!\n\n{lesson.display}",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    logger.info("Sent notification for lesson: %s (%s)", lesson.name, lesson.uid)

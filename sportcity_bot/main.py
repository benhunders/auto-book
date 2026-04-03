"""Entry point for the SportCity auto-booking bot."""

from __future__ import annotations

import asyncio
import logging
import sys

from .config import settings
from .monitor import LessonMonitor
from .telegram_bot import build_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def run() -> None:
    """Start the Telegram bot and monitoring loop concurrently."""
    logger.info("Starting SportCity Auto-Booking Bot")
    logger.info("Schedule URL: %s", settings.sportcity_schedule_url)
    logger.info("Poll interval: %ds", settings.poll_interval_seconds)
    logger.info("Lesson filter: %s", settings.lesson_filter or "(all)")

    app = build_app(settings.telegram_bot_token)
    monitor = LessonMonitor(app, settings)

    # Initialize the application (sets up the bot)
    await app.initialize()
    await app.start()

    # Start polling for Telegram updates in the background
    await app.updater.start_polling(drop_pending_updates=True)

    logger.info("Bot is running. Send /start to the bot to verify.")

    try:
        # Run the monitoring loop (blocks forever)
        await monitor.run_loop()
    except asyncio.CancelledError:
        logger.info("Shutting down...")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    """CLI entry point."""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)


if __name__ == "__main__":
    main()

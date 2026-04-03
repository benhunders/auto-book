# SportCity Auto-Booking Bot

Monitors the SportCity group lesson schedule for open spots and sends Telegram notifications with one-tap booking.

## How it works

1. **Scrapes** the schedule page using a headless browser (Playwright) every 5 minutes
2. **Detects** newly available spots by comparing against previous state
3. **Notifies** you via Telegram with an inline "Book now?" button
4. **Books** the lesson when you tap the button (Phase 2 — not yet implemented)

## Setup

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token

### 2. Get your Chat ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. Copy your numeric chat ID

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your bot token and chat ID
```

### 4. Run

**With Docker (recommended):**

```bash
docker compose up -d
```

**Without Docker:**

```bash
pip install .
playwright install chromium
sportcity-bot
```

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | (required) |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID | (required) |
| `SPORTCITY_SCHEDULE_URL` | Schedule page URL | Leidsche Rijn Sportpark |
| `POLL_INTERVAL_SECONDS` | Check interval in seconds | 300 |
| `LESSON_FILTER` | Comma-separated lesson names to watch | (all lessons) |
| `SPORTCITY_EMAIL` | SportCity login email (Phase 2) | |
| `SPORTCITY_PASSWORD` | SportCity login password (Phase 2) | |

## Bot commands

- `/start` — Verify the bot is running
- `/status` — Check last scrape results

## Phase 2 roadmap

- [ ] Reverse-engineer SportCity/Virtuagym booking API flow
- [ ] Implement authenticated session management
- [ ] Wire "Book now?" button to actual booking
- [ ] Add confirmation message after successful booking

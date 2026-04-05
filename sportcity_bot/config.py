from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    telegram_bot_token: str
    telegram_chat_id: str

    sportcity_email: str = ""
    sportcity_password: str = ""

    sportcity_schedule_url: str = (
        "https://www.sportcity.nl/utrecht/leidsche-rijn-sportpark/groepslesrooster"
    )

    poll_interval_seconds: int = 300

    # How many weeks ahead to scrape (default: 3)
    weeks_ahead: int = 3


settings = Settings()  # type: ignore[call-arg]

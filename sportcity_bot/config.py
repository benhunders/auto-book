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

    # Comma-separated lesson names to watch (empty = all lessons)
    lesson_filter: str = ""

    @property
    def lesson_filter_list(self) -> list[str]:
        if not self.lesson_filter:
            return []
        return [name.strip().lower() for name in self.lesson_filter.split(",")]


settings = Settings()  # type: ignore[call-arg]

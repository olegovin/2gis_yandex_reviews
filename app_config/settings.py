from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://user:password@localhost:5432/reviews_db",
        description="Async PostgreSQL connection string",
    )

    # OpenAI
    OPENAI_API_KEY: str = Field(default="sk-...", description="OpenAI API key")
    OPENAI_MODEL: str = Field(default="gpt-4o-mini", description="OpenAI model name")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = Field(default="123456:ABC-...", description="Aiogram bot token")
    TELEGRAM_ADMIN_CHAT_ID: int = Field(default=0, description="Admin chat/user ID for notifications")

    # Scheduler
    PARSE_INTERVAL_MINUTES: int = Field(default=60, ge=5, description="Parsing interval in minutes")

    # Concurrency
    MAX_CONCURRENT_PARSERS: int = Field(default=3, ge=1, le=10, description="Semaphore limit for parallel parsers")

    # Playwright
    PLAYWRIGHT_HEADLESS: bool = Field(default=True, description="Run Chromium in headless mode")

    # Logging
    LOG_LEVEL: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")


settings = Settings()

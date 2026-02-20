"""
Application configuration loaded from environment variables.
"""

import os
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from .env or environment."""

    # ClickUp
    clickup_api_key: str = ""
    clickup_api_base: str = "https://api.clickup.com"

    # Service
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # Upload tuning
    upload_delay: float = 1.2          # seconds between ClickUp API calls
    max_content_size: int = 90_000     # ClickUp page content limit (~90 KB)
    api_retries: int = 5               # retry count for transient errors
    api_retry_base_delay: float = 3.0  # base delay for exponential backoff

    # Security – shared secret the SuperAgent must send
    api_secret: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()

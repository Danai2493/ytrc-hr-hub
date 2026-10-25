"""Settings come from environment variables only, so the same container image
runs on a laptop, an internal server or any cloud. See .env.example."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    app_name: str = "YTRC HR Hub"
    env: str = field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    # Any SQLAlchemy URL: postgresql+psycopg://, mysql+pymysql://, mssql+pyodbc://, sqlite:///
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./data/hrhub.db")
    )
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", "dev-only-change-me"))
    base_url: str = field(default_factory=lambda: os.getenv("BASE_URL", "http://localhost:8000"))
    session_hours: int = field(default_factory=lambda: int(os.getenv("SESSION_HOURS", "10")))

    # Google sign-in (OpenID Connect). Leave empty to disable the button.
    google_client_id: str = field(default_factory=lambda: os.getenv("GOOGLE_CLIENT_ID", ""))
    google_client_secret: str = field(default_factory=lambda: os.getenv("GOOGLE_CLIENT_SECRET", ""))
    # Only these Google Workspace domains may sign in, e.g. "ytrc.co.th"
    google_allowed_domains: list[str] = field(
        default_factory=lambda: _list("GOOGLE_ALLOWED_DOMAINS", "ytrc.co.th")
    )
    # App-only logins (operators without Google accounts, break-glass admin).
    local_login_enabled: bool = field(default_factory=lambda: _bool("LOCAL_LOGIN_ENABLED", True))

    # File storage for scanned forms and documents (mount a volume here).
    storage_dir: Path = field(
        default_factory=lambda: Path(os.getenv("STORAGE_DIR", "./data/files")).resolve()
    )

    # AI provider: none | mock | anthropic | openai | gemini
    ai_provider: str = field(default_factory=lambda: os.getenv("AI_PROVIDER", "mock"))
    ai_model: str = field(default_factory=lambda: os.getenv("AI_MODEL", ""))
    ai_api_key: str = field(default_factory=lambda: os.getenv("AI_API_KEY", ""))
    ai_base_url: str = field(default_factory=lambda: os.getenv("AI_BASE_URL", ""))
    # Field confidence below this goes to the HR review queue.
    ai_review_threshold: float = field(
        default_factory=lambda: float(os.getenv("AI_REVIEW_THRESHOLD", "0.85"))
    )

    # Mark the whole app as a demo (banner + SAMPLE data only).
    demo_mode: bool = field(default_factory=lambda: _bool("DEMO_MODE", False))
    default_language: str = field(default_factory=lambda: os.getenv("DEFAULT_LANGUAGE", "th"))
    timezone: str = field(default_factory=lambda: os.getenv("TZ_NAME", "Asia/Bangkok"))

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def is_production(self) -> bool:
        return self.env == "production"


settings = Settings()

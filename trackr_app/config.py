import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    database_url: str = _value("DATABASE_URL", "sqlite:///./trackr.db").replace("postgres://", "postgresql+psycopg://", 1).replace("postgresql://", "postgresql+psycopg://", 1)
    app_url: str = _value("APP_URL", "http://localhost:8000").rstrip("/")
    secret_key: str = _value("SECRET_KEY", "development-only-change-me")
    encryption_key: str = _value("ENCRYPTION_KEY")
    admin_email: str = _value("ADMIN_EMAIL").lower()
    resend_api_key: str = _value("RESEND_API_KEY")
    email_from: str = _value("EMAIL_FROM", "Trackr Alerts <alerts@example.com>")
    notion_client_id: str = _value("NOTION_CLIENT_ID")
    notion_client_secret: str = _value("NOTION_CLIENT_SECRET")
    notion_version: str = _value("NOTION_VERSION", "2025-09-03")
    season: str = _value("TRACKR_SEASON", "2027")


settings = Settings()

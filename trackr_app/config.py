import os
from dataclasses import dataclass
from email.utils import parseaddr

from dotenv import load_dotenv
from trackr_common import smtp_password

load_dotenv()


def _value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    environment: str = _value("ENVIRONMENT", _value("VERCEL_ENV", "development")).lower()
    database_url: str = _value("DATABASE_URL", "sqlite:///./trackr.db").replace("postgres://", "postgresql+psycopg://", 1).replace("postgresql://", "postgresql+psycopg://", 1)
    app_url: str = _value("APP_URL", "http://localhost:8000").rstrip("/")
    secret_key: str = _value("SECRET_KEY", "development-only-change-me")
    encryption_key: str = _value("ENCRYPTION_KEY")
    admin_email: str = _value("ADMIN_EMAIL").lower()
    smtp_server: str = _value("SMTP_SERVER", "smtp.gmail.com") or 'smtp.gmail.com'
    smtp_port: int = int(_value("SMTP_PORT", "587") or '587')
    smtp_user: str = _value("SMTP_USER")
    smtp_password: str = smtp_password(_value("SMTP_PASS_APP"), _value('SMTP_SERVER', 'smtp.gmail.com') or 'smtp.gmail.com')
    smtp_from: str = _value("SMTP_FROM", _value("FROM_ADDR", _value("SMTP_USER")))
    notion_client_id: str = _value("NOTION_CLIENT_ID")
    notion_client_secret: str = _value("NOTION_CLIENT_SECRET")
    notion_version: str = _value("NOTION_VERSION", "2025-09-03")
    season: str = _value("TRACKR_SEASON", "2027")
    notion_enabled: bool = _value('NOTION_SYNC_ENABLED', 'false').lower() == 'true'

    @property
    def notion_available(self):
        return self.notion_enabled and bool(self.notion_client_id and self.notion_client_secret)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def validate(self) -> None:
        if not self.is_production:
            return
        required = {
            "DATABASE_URL": self.database_url if not self.database_url.startswith("sqlite") else "",
            "APP_URL": self.app_url if self.app_url.startswith("https://") else "",
            "SECRET_KEY": self.secret_key if self.secret_key != "development-only-change-me" and len(self.secret_key) >= 32 else "",
            "ENCRYPTION_KEY": self.encryption_key,
            "ADMIN_EMAIL": self.admin_email,
            "SMTP_USER": self.smtp_user,
            "SMTP_PASS_APP": self.smtp_password,
            "SMTP_FROM": parseaddr(self.smtp_from)[1],

        }
        if self.notion_enabled or self.notion_client_id or self.notion_client_secret:
            required.update(NOTION_CLIENT_ID=self.notion_client_id, NOTION_CLIENT_SECRET=self.notion_client_secret)
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Invalid production configuration: {', '.join(missing)}")
        from cryptography.fernet import Fernet
        Fernet(self.encryption_key.encode())


settings = Settings()

import os
from dataclasses import dataclass
from email.utils import parseaddr
from urllib.parse import urlsplit

from dotenv import load_dotenv
from trackr_common import smtp_password

load_dotenv()


def _value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    environment: str = _value("ENVIRONMENT", _value("VERCEL_ENV", "development")).lower()
    database_url: str = _value("DATABASE_URL", "sqlite:///./trackr.db").replace("postgres://", "postgresql+psycopg://", 1).replace("postgresql://", "postgresql+psycopg://", 1)
    allow_deployment_host: bool = _value('ALLOW_DEPLOYMENT_HOST', 'false').lower() == 'true'
    allowed_hosts: str = _value('ALLOWED_HOSTS')
    auth_allowed_origins: str = _value('AUTH_ALLOWED_ORIGINS')
    preview_isolated: bool = _value('PREVIEW_ISOLATED', 'false').lower() == 'true'
    migration_database_url: str = _value('MIGRATION_DATABASE_URL').replace('postgres://', 'postgresql+psycopg://', 1).replace('postgresql://', 'postgresql+psycopg://', 1)
    db_pool_size: int = int(_value('DB_POOL_SIZE', '2'))
    db_max_overflow: int = int(_value('DB_MAX_OVERFLOW', '3'))
    db_pool_timeout: int = int(_value('DB_POOL_TIMEOUT', '5'))
    db_connect_timeout: int = int(_value('DB_CONNECT_TIMEOUT', '5'))
    db_lock_timeout_ms: int = int(_value('DB_LOCK_TIMEOUT_MS', '2000'))
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
    def trusted_hosts(self):
        hosts = {urlsplit(self.app_url).hostname}
        if self.allow_deployment_host and os.getenv('VERCEL_URL'):
            hosts.add(os.environ['VERCEL_URL'].lower())
        hosts.update(item.strip().lower() for item in self.allowed_hosts.split(',') if item.strip())
        if self.environment == 'development' and os.getenv('VERCEL') != '1':
            hosts.update(('localhost', '127.0.0.1', 'testserver'))
        return sorted(host for host in hosts if host)

    @property
    def notion_available(self):
        return self.notion_enabled and bool(self.notion_client_id and self.notion_client_secret)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def validate(self) -> None:
        if not (self.is_production or self.environment == 'preview' or os.getenv('VERCEL') == '1'):
            return
        if (self.environment == 'preview' or os.getenv('VERCEL_ENV') == 'preview') and not self.preview_isolated:
            raise RuntimeError('Preview requires an isolated database and secrets (PREVIEW_ISOLATED=true)')
        origin = urlsplit(self.app_url)
        if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password
                or origin.path or origin.query or origin.fragment):
            raise RuntimeError('APP_URL must be an HTTPS origin without credentials or a path')
        if any(value <= 0 for value in (self.db_pool_size, self.db_pool_timeout,
                self.db_connect_timeout, self.db_lock_timeout_ms)) or self.db_max_overflow < 0:
            raise RuntimeError('Database limits must be positive and overflow nonnegative')
        for host in self.allowed_hosts.split(','):
            host = host.strip()
            if host and any(character in host for character in ('*', '/', ':', '@', ' ')):
                raise RuntimeError('ALLOWED_HOSTS must contain explicit hostnames')
        for value in self.auth_allowed_origins.split(','):
            if not value.strip():
                continue
            candidate = urlsplit(value.strip())
            if (candidate.scheme != 'https' or candidate.hostname not in self.trusted_hosts
                    or candidate.username or candidate.password or candidate.path not in ('', '/')
                    or candidate.query or candidate.fragment):
                raise RuntimeError('AUTH_ALLOWED_ORIGINS must contain HTTPS origins on allowed hosts')
        required = {
            "DATABASE_URL": self.database_url if self.database_url.startswith("postgresql+psycopg://") else "",
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

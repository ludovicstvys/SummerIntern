import base64
import hashlib
import secrets
from datetime import timedelta

from cryptography.fernet import Fernet

from .config import settings
from .models import utcnow


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def expires_in(minutes: int):
    return utcnow() + timedelta(minutes=minutes)


def _fernet() -> Fernet:
    if settings.encryption_key:
        key = settings.encryption_key.encode()
    else:
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())
    return Fernet(key)


def encrypt(value: str | None) -> str | None:
    return _fernet().encrypt(value.encode()).decode() if value else None


def decrypt(value: str | None) -> str | None:
    return _fernet().decrypt(value.encode()).decode() if value else None


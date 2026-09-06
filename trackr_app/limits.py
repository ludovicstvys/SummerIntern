"""Database-backed fixed-window limits shared by all web instances."""
import hashlib
import hmac
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .config import settings
from .models import AuthLimit, utcnow


def allow_login(db, email: str, ip: str) -> bool:
    now = utcnow()
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    allowed = True
    # A per-minute cooldown, five emails per quarter hour, and an IP budget.
    for scope, value, seconds, maximum in (("email-minute", email, 60, 1), ("email", email, 900, 5), ("ip", ip, 900, 20)):
        bucket = int(now.timestamp()) // seconds
        key = hmac.new(settings.secret_key.encode(), f"{scope}:{value}:{bucket}".encode(), hashlib.sha256).hexdigest()
        stmt = insert(AuthLimit).values(key=key, count=1, expires_at=now + timedelta(seconds=seconds * 2))
        count = db.scalar(stmt.on_conflict_do_update(index_elements=[AuthLimit.key], set_={"count": AuthLimit.count + 1}).returning(AuthLimit.count))
        allowed = allowed and count <= maximum
    db.execute(delete(AuthLimit).where(AuthLimit.expires_at < now))
    db.commit()
    return allowed

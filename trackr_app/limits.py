"""Database-backed fixed-window limits shared by all web instances."""
import hashlib
import hmac
from datetime import timedelta

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .config import settings
from .models import AuthLimit, utcnow


def allow_login(db, email: str, ip: str) -> bool:
    return _allow(db, 'mail', (("email-minute", email, 60, 1), ("email", email, 900, 5), ("ip", ip, 900, 20)))


def allow_password_login(db, email: str, ip: str) -> bool:
    return reserve_password_login(db, email, ip)[0]


def reserve_password_login(db, email, ip):
    # One remote client cannot exhaust another client's per-account budget.
    return _allow(db, 'password', (("email-ip", email + ':' + ip, 900, 10), ("ip", ip, 900, 50)), receipt=True)


def refund_password_login(db, keys):
    for key in keys:
        db.execute(update(AuthLimit).where(AuthLimit.key == key, AuthLimit.count > 0)
                   .values(count=AuthLimit.count - 1))


def _allow(db, namespace, budgets, receipt=False):
    now = utcnow()
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    allowed = True
    keys = []
    for scope, value, seconds, maximum in budgets:
        bucket = int(now.timestamp()) // seconds
        key = hmac.new(settings.secret_key.encode(), f"{namespace}:{scope}:{value}:{bucket}".encode(), hashlib.sha256).hexdigest()
        keys.append(key)
        stmt = insert(AuthLimit).values(key=key, count=1, expires_at=now + timedelta(seconds=seconds * 2))
        count = db.scalar(stmt.on_conflict_do_update(index_elements=[AuthLimit.key], set_={"count": AuthLimit.count + 1}).returning(AuthLimit.count))
        allowed = allowed and count <= maximum
    db.execute(delete(AuthLimit).where(AuthLimit.expires_at < now))
    db.commit()
    return (allowed, keys) if receipt else allowed

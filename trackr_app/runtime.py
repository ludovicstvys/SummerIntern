"""Short database gates and persistent invocation leases; no network-time locks."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
import uuid
import os

from sqlalchemy import delete, select, text
from .config import settings
from .health import WORKER_COMPATIBLE_SCHEMAS, WEB_COMPATIBLE_SCHEMAS
from .models import WorkerState, utcnow
from .operations import lock_state

_active = ContextVar('runtime_lease', default=False)


@contextmanager
def invocation(db, auth=False):
    if _active.get():
        yield
        return
    if settings.is_production or settings.environment == 'preview' or os.getenv('VERCEL') == '1':
        revision = db.scalar(text('SELECT version_num FROM alembic_version'))
        if revision not in (WEB_COMPATIBLE_SCHEMAS if auth else WORKER_COMPATIBLE_SCHEMAS):
            db.rollback()
            raise RuntimeError('WorkerSchemaIncompatible')
    gate = lock_state(db, 'runtime/gate')
    if gate.last_error:
        db.rollback()
        raise RuntimeError('WorkersPausedForMigration')
    identity = 'runtime/active/' + uuid.uuid4().hex
    db.add(WorkerState(key=identity, last_success_at=utcnow() + timedelta(minutes=5)))
    db.commit()
    token = _active.set(True)
    try:
        yield
    finally:
        _active.reset(token)
        db.rollback()
        db.execute(delete(WorkerState).where(WorkerState.key == identity))
        db.commit()


def guarded(auth=False):
    def decorate(function):
        from functools import wraps
        @wraps(function)
        def wrapped(db, *args, **kwargs):
            with invocation(db, auth=auth):
                return function(db, *args, **kwargs)
        return wrapped
    return decorate

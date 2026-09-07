"""Small shared helpers for safe diagnostics and serialized work."""
from datetime import timedelta
from sqlalchemy import select
from .models import WorkerState, utcnow


def insert_for(db, model):
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(model)


def lock_state(db, key):
    db.execute(insert_for(db, WorkerState).values(key=key).on_conflict_do_nothing(index_elements=['key']))
    return db.scalar(select(WorkerState).where(WorkerState.key == key).with_for_update().execution_options(populate_existing=True))


def error_code(exc):
    code = getattr(exc, 'smtp_code', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
    return f'{type(exc).__name__}:{code}' if code else type(exc).__name__


def next_retry(attempts):
    return utcnow() + timedelta(minutes=min(60, 2 ** attempts))

"""Per-source leases fence off stale acquisition results before SQL application."""
from datetime import timedelta
from .models import SourceSnapshot, utcnow
from .operations import insert_for
from .security import new_token
from .sessions import aware


def reserve(db, key):
    db.execute(insert_for(db, SourceSnapshot).values(key=key).on_conflict_do_nothing(index_elements=['key']))
    state = db.get(SourceSnapshot, key, with_for_update=True, populate_existing=True)
    if state.lease_until and aware(state.lease_until) > utcnow():
        db.commit()
        return None
    state.generation += 1
    state.lease_token, state.lease_until = new_token(), utcnow()+timedelta(minutes=5)
    claim = (state.generation, state.lease_token)
    db.commit()
    return claim


def owned(db, key, claim):
    state = db.get(SourceSnapshot, key, with_for_update=True, populate_existing=True)
    return state if state and (state.generation, state.lease_token) == claim else None

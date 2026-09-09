"""Bounded retention with permanent delivery receipts to preserve deduplication."""
from datetime import timedelta
from sqlalchemy import select, delete, inspect
from .models import DurableJob, AuthMail, Delivery, LegacyTask, NotionSync, MagicLink, PasswordToken, UserSession, OAuthNonce, utcnow
from .operations import insert_for
from .runtime import guarded


def runtime_available(db):
    if 'runtime_available' not in db.info:
        db.info['runtime_available'] = inspect(db.connection()).has_table('durable_jobs')
    return db.info['runtime_available']


def completed(db, model, identity):
    if not runtime_available(db):
        return
    key = f'retention/{model.__tablename__}/{identity}'
    stmt = insert_for(db, DurableJob).values(key=key, kind='retention', status='done', completed_at=utcnow())
    db.execute(stmt.on_conflict_do_update(index_elements=['key'], set_={'completed_at': utcnow()}))


@guarded()
def purge(db, batch=500):
    counts = {}
    cutoff = utcnow() - timedelta(days=7)
    for model in (MagicLink, PasswordToken, UserSession, OAuthNonce):
        identity = model.token_hash if model is OAuthNonce else model.id
        ids = db.scalars(select(identity).where(model.expires_at < cutoff).order_by(identity).limit(batch)).all()
        if ids:
            db.execute(delete(model).where(identity.in_(ids)))
        db.commit(); counts[model.__tablename__] = len(ids)
    from .models import WorkerState
    db.execute(delete(WorkerState).where(WorkerState.key.startswith('runtime/active/'), WorkerState.last_success_at < cutoff))
    db.commit()
    cutoff = utcnow() - timedelta(days=30)
    for model, status, clock in [(Delivery, 'sent', Delivery.sent_at), (NotionSync, 'synced', NotionSync.synced_at)]:
        ids = db.scalars(select(model.id).where(model.status == status, clock < cutoff).order_by(model.id).limit(batch)).all()
        if model is Delivery:
            for task in db.scalars(select(Delivery).where(Delivery.id.in_(ids))):
                db.execute(insert_for(db, DurableJob).values(key=f'receipt/{task.user_id}/{task.offer_id}',
                    kind='receipt', status='archived').on_conflict_do_nothing(index_elements=['key']))
        if ids:
            db.execute(delete(model).where(model.id.in_(ids)))
        db.commit(); counts[model.__tablename__] = len(ids)
    for model, column in [(AuthMail, AuthMail.id), (LegacyTask, LegacyTask.key)]:
        # Pre-migration completed rows get a conservative fresh retention clock.
        prefix = f'retention/{model.__tablename__}/'
        from sqlalchemy import cast, String
        missing = db.scalars(select(column).where(model.status == 'sent', ~select(DurableJob.key)
            .where(DurableJob.key == prefix + cast(column, String)).exists()).order_by(column).limit(batch)).all()
        for identity in missing:
            completed(db, model, identity)
        markers = db.scalars(select(DurableJob).where(DurableJob.kind == 'retention',
            DurableJob.key.startswith(prefix), DurableJob.completed_at < cutoff).limit(batch)).all()
        count = 0
        for marker in markers:
            identity = marker.key[len(prefix):]
            if model is AuthMail:
                identity = int(identity)
            count += db.execute(delete(model).where(column == identity, model.status == 'sent')).rowcount
            db.delete(marker)
        db.commit(); counts[model.__tablename__] = count
    keys = db.scalars(select(DurableJob.key).where(DurableJob.status.in_(['done', 'cancelled']),
        DurableJob.kind != 'retention', DurableJob.completed_at < cutoff).limit(batch)).all()
    if keys:
        db.execute(delete(DurableJob).where(DurableJob.key.in_(keys)))
    db.commit(); counts['durable_jobs'] = len(keys)
    return counts

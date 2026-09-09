"""Durable jobs whose claims survive a serverless or scheduled process exit."""
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from sqlalchemy import select, or_
from .models import DurableJob, NotionConnection, User, utcnow
from .operations import insert_for, error_code, next_retry
from .runtime import guarded
from .security import new_token
from .sessions import aware


def enqueue_job(db, key, kind, payload):
    encoded = json.dumps(payload, sort_keys=True)
    db.execute(insert_for(db, DurableJob).values(key=key, kind=kind, payload=encoded)
        .on_conflict_do_nothing(index_elements=['key']))
    job = db.get(DurableJob, key, with_for_update=True, populate_existing=True)
    if kind == 'match' and job.status == 'pending' and job.payload == encoded:
        return job
    if kind == 'match' and job.status in ('pending', 'processing') and json.loads(job.payload).get('updated'):
        payload['updated'] = True
        encoded = json.dumps(payload, sort_keys=True)
    job.payload, job.status, job.cursor = encoded, 'pending', 0
    job.attempts, job.last_error, job.completed_at = 0, None, None
    # A superseded network attempt must finish/expire before replacement begins.
    return job


@guarded()
def process_jobs(db, kind='notion-create', deadline=None):
    deadline = deadline or time.monotonic() + 90
    ids = db.scalars(select(DurableJob.key).where(DurableJob.kind == kind,
        DurableJob.status.in_(['pending', 'processing']),
        or_(DurableJob.lease_until.is_(None), DurableJob.lease_until <= utcnow()),
        or_(DurableJob.next_attempt_at.is_(None), DurableJob.next_attempt_at <= utcnow()))
        .order_by(DurableJob.created_at, DurableJob.key).limit(100)).all()
    db.commit()
    count = 0
    for key in ids:
        if time.monotonic() >= deadline:
            break
        try:
            if kind == 'notion-create':
                count += create_notion(db, key)
            elif kind == 'match':
                count += match_batch(db, key)
        except Exception as exc:
            db.rollback()
            job = db.get(DurableJob, key, with_for_update=True)
            if job and (job.status == 'pending' or (kind == 'match' and job.status == 'processing' and job.lease_token == db.info.get('match_claim'))):
                if job.status == 'pending':
                    job.attempts += 1
                job.lease_until = None
                job.status = 'failed' if job.attempts >= 5 else 'pending'
                job.last_error, job.next_attempt_at = error_code(exc), next_retry(job.attempts)
            db.commit()
    return count


def create_notion(db, key):
    from .notion import create_remote_database
    # Resolve identity before acquiring user -> job locks, consistently with web.
    initial = db.get(DurableJob, key)
    payload = json.loads(initial.payload)
    user = db.get(User, payload['user_id'], with_for_update=True, populate_existing=True)
    job = db.get(DurableJob, key, with_for_update=True, populate_existing=True)
    connection = db.get(NotionConnection, payload['connection_id'], populate_existing=True)
    from .sessions import aware
    if job.lease_until and aware(job.lease_until) > utcnow():
        db.commit(); return 0
    if job.status == 'processing':
        job.status, job.last_error = 'uncertain', 'CreationInterrupted'
        if connection:
            connection.setup_status = 'uncertain'
        db.commit(); return 0
    if job.status != 'pending':
        db.commit(); return 0
    if not user or not user.is_active or not connection or connection.user_id != user.id:
        job.status, job.completed_at = 'cancelled', utcnow()
        db.commit(); return 0
    if connection.data_source_id:
        job.status, job.completed_at = 'done', utcnow()
        db.commit(); return 0
    token = new_token()
    job.status, job.lease_token, job.lease_until = 'processing', token, utcnow()+timedelta(minutes=5)
    job.attempts += 1
    connection.setup_status = 'creating'
    snapshot = SimpleNamespace(access_token_encrypted=connection.access_token_encrypted)
    db.commit()
    failure, database_id, source_id, rejected = None, None, None, False
    try:
        database_id, source_id = create_remote_database(snapshot, payload['parent_page_id'])
    except Exception as exc:
        failure, database_id = error_code(exc), getattr(exc, 'database_id', None)
        rejected = getattr(getattr(exc, 'response', None), 'status_code', None) in (400, 401, 403, 404, 422) and not database_id
    user = db.get(User, payload['user_id'], with_for_update=True, populate_existing=True)
    job = db.get(DurableJob, key, with_for_update=True, populate_existing=True)
    connection = db.get(NotionConnection, payload['connection_id'], populate_existing=True)
    if job.status != 'processing' or job.lease_token != token:
        db.commit(); return 0
    if not user or not user.is_active or not connection:
        job.status, job.completed_at = 'cancelled', utcnow()
    elif failure:
        job.status = 'failed' if rejected else 'uncertain'
        job.last_error = failure
        connection.setup_status, connection.last_error = job.status, failure
        if database_id:
            connection.database_id = database_id
    else:
        connection.database_id, connection.data_source_id = database_id, source_id
        connection.setup_status, connection.last_error = 'ready', None
        job.status, job.completed_at = 'done', utcnow()
        enqueue_job(db, f'match-user/{user.id}', 'match', {'user_id': user.id, 'baseline': True})
    job.lease_until = None
    db.commit()
    return int(not failure)


def match_batch(db, key):
    from .models import Offer, Preference, UserOffer, NotionSync, Delivery
    from .preferences import offer_matches, offer_is_open
    job = db.get(DurableJob, key, with_for_update=True, populate_existing=True)
    if job.status not in ('pending', 'processing') or (job.lease_until and aware(job.lease_until) > utcnow()):
        db.commit(); return 0
    payload = json.loads(job.payload)
    if job.attempts >= 5:
        job.status, job.last_error = 'failed', job.last_error or 'MatchingLeaseExhausted'
        db.commit(); return 0
    token = new_token()
    job.attempts += 1
    db.info['match_claim'] = token
    job.status, job.lease_token, job.lease_until = 'processing', token, utcnow()+timedelta(minutes=5)
    cursor = job.cursor
    db.commit()
    if payload.get('reconcile') and payload.get('phase') != 'offers':
        from .preferences import reconcile_delivery
        user = db.get(User, payload['user_id'], with_for_update=True, populate_existing=True)
        pref = db.scalar(select(Preference).where(Preference.user_id == payload['user_id']))
        batch = db.scalars(select(Delivery).where(Delivery.user_id == payload['user_id'], Delivery.id > cursor)
            .order_by(Delivery.id).limit(100)).all()
        if user and user.is_active and pref:
            for delivery in batch:
                reconcile_delivery(db, pref, delivery)
        job = db.get(DurableJob, key, with_for_update=True, populate_existing=True)
        if job.status == 'processing' and job.lease_token == token:
            job.status, job.lease_until, job.attempts = 'pending', None, 0
            if batch:
                job.cursor = batch[-1].id
            else:
                payload['phase'] = 'offers'
                job.payload, job.cursor = json.dumps(payload, sort_keys=True), 0
        db.commit()
        return max(1, len(batch))
    if 'offer_id' in payload:
        users = db.scalars(select(User.id).where(User.id > cursor, User.is_active.is_(True))
            .order_by(User.id).limit(100)).all()
        pairs = [(uid, payload['offer_id']) for uid in users]
    else:
        offers = db.scalars(select(Offer.id).where(Offer.id > cursor)
            .order_by(Offer.id).limit(100)).all()
        pairs = [(payload['user_id'], oid) for oid in offers]
    for uid, oid in pairs:
        # Producers enqueue after source updates; this batch is entirely local.
        user = db.get(User, uid, with_for_update=True, populate_existing=True)
        pref = db.scalar(select(Preference).where(Preference.user_id == uid))
        offer = db.get(Offer, oid)
        if not user or not user.is_active or not pref or pref.status != 'active' or not offer or not offer_is_open(offer) or not offer_matches(offer, pref):
            continue
        matched = db.scalar(select(UserOffer).where(UserOffer.user_id == uid, UserOffer.offer_id == oid))
        if not matched:
            cutoff = pref.activated_at or (job.created_at if payload.get('baseline') else None)
            baseline = cutoff is not None and aware(offer.first_seen_at) <= aware(cutoff)
            db.add(UserOffer(user_id=uid, offer_id=oid, baseline=baseline))
            if not baseline:
                db.add(Delivery(user_id=uid, offer_id=oid, mode=pref.delivery_mode))
        if user.notion and user.notion.data_source_id:
            sync = db.scalar(select(NotionSync).where(NotionSync.connection_id == user.notion.id, NotionSync.offer_id == oid))
            if sync is None:
                db.add(NotionSync(connection_id=user.notion.id, offer_id=oid))
            elif payload.get('updated'):
                sync.status, sync.attempts = 'pending', 0
        db.flush()
    job = db.get(DurableJob, key, with_for_update=True, populate_existing=True)
    if job.status != 'processing' or job.lease_token != token:
        db.commit(); return len(pairs)
    job.status, job.lease_until, job.attempts = 'pending', None, 0
    if pairs:
        job.cursor = pairs[-1][0 if 'offer_id' in payload else 1]
    else:
        job.status, job.completed_at = 'done', utcnow()
    db.commit()
    return len(pairs)

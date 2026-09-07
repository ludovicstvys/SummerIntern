"""CSV compatibility with durable, independent Notion and email tasks.

Platform accounts always use the platform email pipeline. Legacy email remains
available for addresses that have not been migrated to accounts.
"""
import hashlib
import json
import os
import time
import uuid
from sqlalchemy import select, or_
from trackr_common import canonical_offer_url, deduplicate_offers, scrape_open_programmes, write_csv, filter_offers_by_start_term
from .database import SessionLocal
from .models import LegacyTask, User, utcnow
from .operations import insert_for, lock_state, error_code, next_retry


def enqueue(db, source, channel, recipient, offer, label, force=''):
    identity = '|'.join(('legacy-email' if channel == 'email' else source, channel, recipient, canonical_offer_url(offer['offer_url']), force))
    key = hashlib.sha256(identity.encode()).hexdigest()
    payload = json.dumps({'offer': offer, 'label': label}, sort_keys=True)
    db.execute(insert_for(db, LegacyTask).values(key=key, source=source, channel=channel, recipient=recipient, payload=payload).on_conflict_do_nothing(index_elements=['key']))
    task = db.get(LegacyTask, key, populate_existing=True, with_for_update=True)
    if channel == 'notion' and task.payload != payload:
        task.payload, task.status, task.attempts = payload, 'pending', 0
        task.next_attempt_at = None


def process_tasks(db, source, adapter, budget=90):
    deadline = time.monotonic() + budget
    keys = db.scalars(select(LegacyTask.key).where(LegacyTask.source == source, LegacyTask.status == 'pending', or_(LegacyTask.next_attempt_at.is_(None), LegacyTask.next_attempt_at <= utcnow())).order_by(LegacyTask.created_at)).all()
    db.commit()
    errors = 0
    for key in keys:
        if time.monotonic() >= deadline:
            break
        # Match producer lock order; no concurrent update can replace the payload
        # while a worker is synchronizing an older version.
        lock_state(db, 'legacy/' + source)
        task = db.get(LegacyTask, key, populate_existing=True, with_for_update=True)
        if task.status != 'pending':
            db.commit(); continue
        if task.channel == 'email':
            migrated = db.scalar(select(User).where(User.email == task.recipient).with_for_update())
            if migrated or os.getenv('LEGACY_EMAIL_ENABLED', 'true').lower() != 'true':
                task.status = 'cancelled'; db.commit(); continue
        payload = json.loads(task.payload)
        try:
            if task.channel == 'notion':
                adapter.sync_to_notion([payload['offer']])
            else:
                if not adapter.send_email([payload['offer']], programme_label=payload['label'], recipients=[task.recipient], idempotency_key=task.key):
                    raise RuntimeError('SMTP configuration missing')
            task.status, task.last_error, task.next_attempt_at = 'sent', None, None
        except Exception as exc:
            task.attempts += 1
            task.status = 'failed' if task.attempts >= 5 else 'pending'
            task.last_error, task.next_attempt_at = error_code(exc), next_retry(task.attempts)
            errors += 1
        db.commit()
    return errors


def run_collector(params, output_file, label, start_term=None):
    import test as adapter
    source = '/'.join((params['season'], params['region'], params['type']))
    failed = False
    with SessionLocal() as db:
        try:
            lock_state(db, 'legacy/' + source)
            offers = deduplicate_offers(scrape_open_programmes(params))
            previous = adapter.read_process_csv(output_file)
            new_urls = {canonical_offer_url(o['offer_url']) for o in adapter.detect_new_offers(offers, previous)}
            selected = filter_offers_by_start_term(offers, start_term) if start_term else offers
            force = uuid.uuid4().hex if os.getenv('FORCE_EMAIL_ALL', '').lower() in ('1', 'true', 'yes') else ''
            email_enabled = os.getenv('LEGACY_EMAIL_ENABLED', 'true').lower() == 'true'
            recipients = adapter.read_email_recipients() if email_enabled else []
            for offer in selected:
                if adapter.NOTION_TOKEN and adapter.NOTION_DATA_SOURCE_ID:
                    enqueue(db, source, 'notion', '', offer, label)
                if force or canonical_offer_url(offer['offer_url']) in new_urls:
                    for recipient in recipients:
                        enqueue(db, source, 'email', recipient.lower(), offer, label, force)
            # Commit outbox BEFORE CSV. A retry after either boundary preserves jobs.
            db.commit()
            if email_enabled and not recipients:
                raise RuntimeError('Legacy recipients missing; configure TO_ADDRS or disable legacy email')
            write_csv(offers, output_file)
        except Exception as exc:
            db.rollback()
            print('Legacy collection failed: ' + error_code(exc))
            failed = True
        # An upstream failure must not prevent previously queued tasks from retrying.
        failed = bool(process_tasks(db, source, adapter)) or failed
        failed = bool(db.scalar(select(LegacyTask.key).where(LegacyTask.source == source, LegacyTask.status == 'failed').limit(1))) or failed
    return int(failed)

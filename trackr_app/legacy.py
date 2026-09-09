from .runtime import guarded
"""CSV compatibility with durable, independent Notion and email tasks.

Platform accounts always use the platform email pipeline. Legacy email remains
available for addresses that have not been migrated to accounts.
"""
import hashlib
import json
import os
import time
import uuid
from datetime import timedelta
from pathlib import Path
from sqlalchemy import select, or_
from trackr_common import canonical_offer_url, deduplicate_offers, scrape_open_programmes, write_csv, filter_offers_by_start_term
from .database import SessionLocal
from .models import LegacyTask, User, SourceSnapshot, utcnow
from .operations import insert_for, lock_state, error_code, next_retry


def legacy_notion_enabled():
    """A separate circuit breaker for the shared, legacy Notion database."""
    return os.getenv('LEGACY_NOTION_ENABLED', 'true').lower() == 'true'


def enqueue(db, source, channel, recipient, offer, label, force=''):
    identity = '|'.join(('legacy-email' if channel == 'email' else source, channel, recipient, canonical_offer_url(offer['offer_url']), force))
    key = hashlib.sha256(identity.encode()).hexdigest()
    payload = json.dumps({'offer': offer, 'label': label}, sort_keys=True)
    db.execute(insert_for(db, LegacyTask).values(key=key, source=source, channel=channel, recipient=recipient, payload=payload).on_conflict_do_nothing(index_elements=['key']))
    task = db.get(LegacyTask, key, populate_existing=True, with_for_update=True)
    if channel == 'notion' and task.payload != payload:
        task.payload, task.status, task.attempts = payload, 'pending', 0
        # Preserve an outstanding lease/backoff when replacing the payload.


@guarded(auth=False)
def process_tasks(db, source, adapter, budget=90):
    deadline = time.monotonic() + budget
    due = or_(LegacyTask.next_attempt_at.is_(None), LegacyTask.next_attempt_at <= utcnow())
    keys = db.scalars(select(LegacyTask.key).where(LegacyTask.source == source,
        LegacyTask.status.in_(['pending', 'processing']), due)
        .order_by(LegacyTask.created_at, LegacyTask.key).limit(100)).all()
    db.commit()
    errors = 0
    notion_context = None
    for key in keys:
        if time.monotonic() >= deadline:
            break
        try:
            lock_state(db, 'legacy/' + source)
            task = db.scalar(select(LegacyTask).where(LegacyTask.key == key,
                LegacyTask.status.in_(['pending', 'processing']),
                or_(LegacyTask.next_attempt_at.is_(None), LegacyTask.next_attempt_at <= utcnow()))
                .with_for_update().execution_options(populate_existing=True))
            if not task:
                db.commit(); continue
            if task.attempts >= 5:
                task.status, task.last_error = 'failed', task.last_error or 'LegacyLeaseExhausted'
                db.commit(); errors += 1; continue
            if task.channel == 'email':
                migrated = db.scalar(select(User).where(User.email == task.recipient).with_for_update())
                if migrated or os.getenv('LEGACY_EMAIL_ENABLED', 'true').lower() != 'true':
                    task.status = 'cancelled'; db.commit(); continue
            if task.channel == 'notion' and not legacy_notion_enabled():
                db.commit(); continue
            task.status, task.next_attempt_at = 'processing', utcnow() + timedelta(minutes=5)
            task.attempts += 1
            lease, raw_payload, channel, recipient = task.next_attempt_at, task.payload, task.channel, task.recipient
            db.commit()
            failure = None
            try:
                payload = json.loads(raw_payload)
                if channel == 'notion':
                    if notion_context is None and hasattr(adapter, 'prepare_notion_sync'):
                        notion_context = adapter.prepare_notion_sync()
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Legacy invocation budget exhausted')
                    if notion_context is None:
                        adapter.sync_to_notion([payload['offer']])
                    else:
                        adapter.sync_to_notion([payload['offer']], context=notion_context)
                else:
                    if not adapter.send_email([payload['offer']], programme_label=payload['label'],
                            recipients=[recipient], idempotency_key=key):
                        raise RuntimeError('SMTP configuration missing')
            except Exception as exc:
                failure = error_code(exc)
                errors += 1
            lock_state(db, 'legacy/' + source)
            task = db.scalar(select(LegacyTask).where(LegacyTask.key == key,
                LegacyTask.status == 'processing', LegacyTask.next_attempt_at == lease,
                LegacyTask.payload == raw_payload).with_for_update().execution_options(populate_existing=True))
            if not task:
                db.commit(); continue
            if failure:
                task.status = 'failed' if task.attempts >= 5 or failure in ('JSONDecodeError', 'KeyError') else 'pending'
                task.last_error, task.next_attempt_at = failure, next_retry(task.attempts)
            else:
                task.status, task.last_error, task.next_attempt_at = 'sent', None, None
                task.attempts = max(0, task.attempts - 1)
                from .maintenance import completed
                completed(db, LegacyTask, task.key)
            db.commit()
        except Exception as exc:
            db.rollback()
            errors += 1
            print('Legacy task deferred: ' + error_code(exc))
    return errors


def run_collector(params, output_file, label, start_term=None):
    import test as adapter
    source = '/'.join((params['season'], params['region'], params['type']))
    failed = False
    with SessionLocal() as db:
        from .runtime import invocation
        with invocation(db):
            try:
                from .sources import reserve, owned
                claim = reserve(db, 'legacy/' + source)
                if claim is None:
                    return 0
                snapshot = owned(db, 'legacy/' + source, claim)
                first = snapshot.payload is None
                previous = json.loads(snapshot.payload) if not first else adapter.read_process_csv(output_file)
                missing_baseline = snapshot.updated_at is None and not Path(output_file).exists()
                if first:
                    snapshot.payload = json.dumps(previous)
                db.commit()
                acquired = scrape_open_programmes(params)
                if getattr(acquired, 'complete', True) is False:
                    raise RuntimeError('Incomplete legacy snapshot')
                offers = deduplicate_offers(acquired)
                if not offers and not getattr(offers, 'complete', False):
                    raise RuntimeError('Ambiguous empty legacy snapshot')
                lock_state(db, 'legacy/' + source)
                snapshot = owned(db, 'legacy/' + source, claim)
                if snapshot is None:
                    db.rollback(); return 0
                new_urls = {canonical_offer_url(o['offer_url']) for o in adapter.detect_new_offers(offers, previous)}
                selected = filter_offers_by_start_term(offers, start_term) if start_term else offers
                force = uuid.uuid4().hex if os.getenv('FORCE_EMAIL_ALL', '').lower() in ('1', 'true', 'yes') else ''
                email_enabled = os.getenv('LEGACY_EMAIL_ENABLED', 'true').lower() == 'true'
                recipients = adapter.read_email_recipients() if email_enabled else []
                for offer in selected:
                    if legacy_notion_enabled() and adapter.NOTION_TOKEN and adapter.NOTION_DATA_SOURCE_ID and canonical_offer_url(offer['offer_url']) in new_urls:
                        enqueue(db, source, 'notion', '', offer, label)
                    if force or (not missing_baseline and canonical_offer_url(offer['offer_url']) in new_urls):
                        for recipient in recipients:
                            enqueue(db, source, 'email', recipient.lower(), offer, label, force)
                if not email_enabled or recipients:
                    snapshot.payload, snapshot.updated_at = json.dumps(offers), utcnow()
                snapshot.lease_until = None
                # PostgreSQL snapshot and outbox advance together; CSV is only an export.
                db.commit()
                if email_enabled and not recipients:
                    raise RuntimeError('Legacy recipients missing; configure TO_ADDRS or disable legacy email')
                if os.getenv('LEGACY_EXPORT_CSV', 'false').lower() == 'true':
                    write_csv(offers, output_file)
            except Exception as exc:
                db.rollback()
                if 'claim' in locals() and claim:
                    snapshot = owned(db, 'legacy/' + source, claim)
                    if snapshot:
                        snapshot.lease_until, snapshot.last_error = None, error_code(exc)
                    db.commit()
                print('Legacy collection failed: ' + error_code(exc))
                failed = True
            # An upstream failure must not prevent previously queued tasks from retrying.
            failed = bool(process_tasks(db, source, adapter)) or failed
            failed = bool(db.scalar(select(LegacyTask.key).where(LegacyTask.source == source, LegacyTask.status == 'failed').limit(1))) or failed
    return int(failed)

SUMMER_SNAPSHOT_FILES = ('processus_ouverts.csv', 'processus_ouverts_fr_summer.csv', 'processus_ouverts_hk_summer.csv')
LAST_EMAIL_SNAPSHOT_FILES = (
    'processus_ouverts.csv', 'processus_ouverts_fr_off_cycle.csv',
    'processus_ouverts_uk_off_cycle.csv',
)
LAST_EMAIL_OFFERS = {
    ('Private Equity - Infrastructure - Internship', 'InfraVia Capital Partners', 'France'),
    ('2027 Sales & Trading Off-Cycle Internship', 'Morgan Stanley', 'France'),
    ('Global Banking and Advisory 2027 Summer Internship Programme', 'Société Générale', 'UK'),
    ('2027 Internal Audit Summer Analyst Programme', 'Morgan Stanley', 'UK'),
    ('2027 Sales & Trading Summer Analyst Programme', 'Morgan Stanley', 'UK'),
    ('2027 Global Investment Banking - M&A Summer Internship', 'Baird', 'UK'),
    ('2027 Global Capital Markets Summer Analyst Programme', 'Morgan Stanley', 'UK'),
    ('2027 Summer Analyst Programme: Japanese Corporate Banking', 'MUFG', 'UK'),
    ('2027 Investment Banking Summer Analyst Programme', 'Morgan Stanley', 'UK'),
    ('Global Markets 2027 Summer Internship Programme', 'Société Générale', 'UK'),
    ('2027 Summer Internship Program', 'Brevan Howard', 'UK'),
    ('2027 Summer Analyst Programme: Internal Audit', 'MUFG', 'UK'),
    ('2027 Summer Analyst Programme: Capital Markets', 'MUFG', 'UK'),
    ('Investments Summer Analyst Internship 2027', 'InfraRed Capital Partners', 'UK'),
    ('2027 Summer Analyst Programme: Global Corporate Investment Banking', 'MUFG', 'UK'),
    ('2027 Investment Banking Off-Cycle Internship', 'Morgan Stanley', 'UK'),
    ('2027 Global Capital Markets Off-Cycle Internship', 'Morgan Stanley', 'UK'),
}


def _offer_value(offer, lower, csv):
    return offer.get(lower) or offer.get(csv) or ''


def _csv_offer(offer):
    """Translate the durable CSV headers back to the collector payload shape."""
    fields = {
        'name': 'Name', 'company': 'Company', 'company_id': 'Company ID',
        'offer_url': 'Offer URL', 'region': 'Region', 'categories': 'Categories',
        'opening_date': 'Opening Date', 'closing_date': 'Closing Date',
        'stage': 'Stage', 'rolling': 'Rolling', 'needs_cv': 'Needs CV',
        'needs_cover_letter': 'Needs Cover Letter',
        'company_description': 'Company Description', 'notes': 'Notes',
    }
    return {key: offer.get(key) if key in offer else offer.get(header) for key, header in fields.items()}


def reconcile_summer_snapshot(adapter, snapshot_dir='.', apply=False):
    """Compare the preserved 2026-09-07 Summer CSV snapshot with Notion.

    This is deliberately read-first.  Ambiguous historical records are only
    reported, never patched or archived.
    """
    context = adapter.prepare_notion_sync(include_historical=True)
    existing = context['existing_offers']
    historical = context.get('historical_offers', [])
    candidates = []
    seen = set()
    for filename in SUMMER_SNAPSHOT_FILES:
        path = Path(snapshot_dir) / filename
        for row in adapter.read_process_csv(path):
            offer = _csv_offer(row)
            url = canonical_offer_url(_offer_value(offer, 'offer_url', 'Offer URL'))
            if url and url not in seen:
                seen.add(url)
                candidates.append(offer)

    missing, ambiguous = [], []
    for offer in candidates:
        url = canonical_offer_url(_offer_value(offer, 'offer_url', 'Offer URL'))
        if url in existing:
            continue
        name = _offer_value(offer, 'name', 'Name').strip().casefold()
        company = _offer_value(offer, 'company', 'Company').strip().casefold()
        matches = [
            page for page in historical
            if str(page.get('name') or '').casefold() == name
            and str(page.get('company') or '').casefold() == company
        ]
        if matches:
            ambiguous.append({'offer_url': url, 'name': name, 'company': company, 'page_ids': [page['page_id'] for page in matches]})
        else:
            missing.append(offer)
    result = {'snapshot_files': list(SUMMER_SNAPSHOT_FILES), 'candidates': len(candidates), 'missing': len(missing), 'ambiguous': ambiguous, 'created': 0}
    if apply and missing:
        adapter.sync_to_notion(missing, context=context)
        result['created'] = len(missing)
    return result


def sync_last_email_offers(adapter, snapshot_dir='.', apply=False):
    """Sync precisely the 17 opportunities listed in the 7 September email."""
    context = adapter.prepare_notion_sync()
    selected = []
    for filename in LAST_EMAIL_SNAPSHOT_FILES:
        for row in adapter.read_process_csv(Path(snapshot_dir) / filename):
            offer = _csv_offer(row)
            signature = (offer['name'], offer['company'], offer['region'])
            if signature in LAST_EMAIL_OFFERS:
                selected.append(offer)
    selected_by_url = {canonical_offer_url(offer['offer_url']): offer for offer in selected}
    if len(selected_by_url) != len(LAST_EMAIL_OFFERS):
        raise RuntimeError(f'Last-email snapshot mismatch: found {len(selected_by_url)} of {len(LAST_EMAIL_OFFERS)} offers')
    missing = [offer for url, offer in selected_by_url.items() if url not in context['existing_offers']]
    result = {'mail_offers': len(selected_by_url), 'already_in_notion': len(selected_by_url) - len(missing), 'missing': len(missing), 'created': 0}
    if apply and missing:
        adapter.sync_to_notion(missing, context=context)
        result['created'] = len(missing)
    return result


def cancel_legacy_notion_window(db, start, end, apply=False):
    """Quarantine only pending tasks generated by the known faulty run."""
    tasks = db.scalars(select(LegacyTask).where(LegacyTask.channel == 'notion', LegacyTask.status == 'pending', LegacyTask.created_at >= start, LegacyTask.created_at <= end).with_for_update()).all()
    if apply:
        for task in tasks:
            task.status, task.last_error, task.next_attempt_at = 'cancelled', 'remediated_duplicate_run', None
        db.commit()
    return {'matched': len(tasks), 'cancelled': len(tasks) if apply else 0, 'applied': apply}

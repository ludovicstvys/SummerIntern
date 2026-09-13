import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
import pytest
from sqlalchemy import select, text
from tests.test_audit import db, user, client, offer
from tests.test_postgres import pg
from trackr_app.models import OAuthNonce, DurableJob, WorkerState, SourceSnapshot, Delivery, AuthMail, Offer, OfferSource, PasswordToken, utcnow
from trackr_app.durable import enqueue_job, process_jobs
from trackr_app.preferences import offer_matches
from trackr_app.scraper import scrape_all
from trackr_app.sessions import aware


TRACKER = [{'region': 'France', 'type': 'summer-internships'}]


def _offer_payload(offer, **changes):
    payload = {
        'offer_url': offer.offer_url,
        'name': offer.name,
        'company': offer.company,
        'categories': json.loads(offer.categories or '[]'),
        'opening_date': offer.opening_date.isoformat() if offer.opening_date else None,
        'closing_date': offer.closing_date.isoformat() if offer.closing_date else None,
        'stage': offer.stage,
        'rolling': offer.rolling,
        'needs_cv': offer.needs_cv,
        'needs_cover_letter': offer.needs_cover_letter,
        'company_id': offer.company_id,
        'company_description': offer.company_description,
        'notes': offer.notes,
    }
    payload.update(changes)
    return payload


def _scrape_one(db, monkeypatch, payload, tracker=None):
    monkeypatch.setenv('TRACKR_SOURCE_INDEX', '0')
    with patch('trackr_app.scraper.TRACKERS', tracker or TRACKER), patch(
        'trackr_app.scraper.scrape_open_programmes', return_value=[payload],
    ):
        scrape_all(db)


def test_oauth_nonce_is_single_use_and_session_bound(client, db, user):
    from trackr_app.models import UserSession
    from trackr_app.security import token_hash, expires_in
    with patch('trackr_app.main.settings', SimpleNamespace(notion_available=True)):
        response = client.get('/notion/connect', follow_redirects=False)
        state = parse_qs(urlsplit(response.headers['location']).query)['state'][0]
        db.add(UserSession(user_id=user.id, token_hash=token_hash('other-session'), csrf_token='other', expires_at=expires_in(30)))
        db.commit()
        client.cookies.set('trackr_session', 'other-session')
        with patch('trackr_app.main.exchange_code') as exchange:
            assert client.get('/notion/callback', params={'state': state, 'code': 'code'}, follow_redirects=False).status_code == 400
            exchange.assert_not_called()
        client.cookies.set('trackr_session', 'audit-session')
        with patch('trackr_app.main.exchange_code', return_value={'access_token': 'test', 'workspace_id': 'workspace'}) as exchange:
            assert client.get('/notion/callback', params={'state': state, 'code': 'code'}, follow_redirects=False).status_code == 303
            assert client.get('/notion/callback', params={'state': state, 'code': 'code'}, follow_redirects=False).status_code == 400
            assert exchange.call_count == 1
    assert db.query(OAuthNonce).count() == 0


def test_source_generation_rejects_stale_application(db):
    from trackr_app.sources import reserve, owned
    first = reserve(db, 'source')
    assert reserve(db, 'source') is None
    source = db.get(SourceSnapshot, 'source')
    source.lease_until = utcnow() - timedelta(seconds=1); db.commit()
    second = reserve(db, 'source')
    assert second[0] == first[0] + 1
    assert owned(db, 'source', first) is None
    assert owned(db, 'source', second) is not None


def test_matching_baseline_is_durable_and_sends_no_old_offers(db, user, offer):
    enqueue_job(db, 'baseline', 'match', {'user_id': user.id, 'baseline': True, 'reconcile': True})
    db.get(DurableJob, 'baseline').last_error = 'OperationalError'
    db.commit()
    for _ in range(5):
        process_jobs(db, 'match')
    from trackr_app.models import UserOffer
    assert db.query(UserOffer).one().baseline
    assert db.query(Delivery).count() == 0
    job = db.get(DurableJob, 'baseline')
    assert job.status == 'done' and job.last_error is None


def test_unchanged_scrape_does_not_requeue_completed_match(db, offer, monkeypatch):
    completed_at = utcnow()
    job = DurableJob(key=f'match-offer/{offer.id}', kind='match',
        payload=json.dumps({'offer_id': offer.id, 'updated': False}, sort_keys=True),
        status='done', cursor=73, completed_at=completed_at)
    db.add(job); db.commit(); db.refresh(job)
    completed_at = job.completed_at

    _scrape_one(db, monkeypatch, _offer_payload(offer))

    db.refresh(job)
    assert (job.status, job.cursor, job.completed_at) == ('done', 73, completed_at)


def test_unchanged_scrape_preserves_pending_match_progress(db, offer, monkeypatch):
    retry_at = utcnow() + timedelta(minutes=10)
    job = DurableJob(key=f'match-offer/{offer.id}', kind='match',
        payload=json.dumps({'offer_id': offer.id, 'updated': True}, sort_keys=True),
        status='pending', cursor=73, attempts=2, last_error='OperationalError',
        next_attempt_at=retry_at)
    db.add(job); db.commit(); db.refresh(job)
    retry_at = job.next_attempt_at

    _scrape_one(db, monkeypatch, _offer_payload(offer))

    db.refresh(job)
    assert (job.status, job.cursor, job.attempts, job.last_error,
        job.next_attempt_at) == ('pending', 73, 2, 'OperationalError', retry_at)


def test_real_offer_change_restarts_pending_match(db, offer, monkeypatch):
    job = DurableJob(key=f'match-offer/{offer.id}', kind='match',
        payload=json.dumps({'offer_id': offer.id, 'updated': True}, sort_keys=True),
        status='pending', cursor=73, attempts=2, last_error='OperationalError',
        next_attempt_at=utcnow() + timedelta(minutes=10))
    db.add(job); db.commit()

    _scrape_one(db, monkeypatch, _offer_payload(offer, name='Renamed internship'))

    db.refresh(job)
    assert (job.status, job.cursor, job.attempts, job.last_error,
        job.next_attempt_at) == ('pending', 0, 0, None, None)
    assert json.loads(job.payload) == {'offer_id': offer.id, 'updated': True}


def test_new_source_combination_with_same_labels_restarts_matching(db, user, offer, monkeypatch):
    user.preference.program_types = '["off-cycle"]'
    user.preference.regions = '["France"]'
    offer.sources.extend([
        OfferSource(region='France', programme_type='summer', season='2027', is_open=True),
        OfferSource(region='UK', programme_type='off-cycle', season='2027', is_open=True),
    ])
    job = DurableJob(key=f'match-offer/{offer.id}', kind='match',
        payload=json.dumps({'offer_id': offer.id, 'updated': False}, sort_keys=True),
        status='done', cursor=user.id, completed_at=utcnow())
    db.add(job); db.commit()
    labels = offer.region_label, offer.programme_label
    assert labels == ('France / UK', 'off-cycle / summer')
    assert not offer_matches(offer, user.preference)

    _scrape_one(db, monkeypatch, _offer_payload(offer), tracker=[{
        'region': 'France', 'type': 'off-cycle-internships', 'season': '2027',
    }])

    db.refresh(job)
    assert (job.status, job.cursor, job.completed_at) == ('pending', 0, None)
    assert (offer.region_label, offer.programme_label) == labels
    assert offer_matches(offer, user.preference)
    assert process_jobs(db, 'match') > 0
    delivery = db.query(Delivery).filter_by(user_id=user.id, offer_id=offer.id).one()
    assert delivery.status == 'pending'


def test_superseded_match_failure_only_releases_old_lease(db, offer):
    key = f'match-offer/{offer.id}'
    enqueue_job(db, key, 'match', {'offer_id': offer.id, 'updated': False})
    db.commit()

    def supersede_then_fail(session, claimed_key):
        job = session.get(DurableJob, claimed_key, with_for_update=True)
        token = 'old-worker-token'
        session.info['match_claim'] = token
        job.status, job.lease_token = 'processing', token
        job.lease_until = utcnow() + timedelta(minutes=5)
        job.attempts = 1
        session.commit()
        enqueue_job(session, key, 'match', {
            'offer_id': offer.id, 'updated': True,
        }, supersede=True)
        session.commit()
        raise RuntimeError('old generation failed')

    with patch('trackr_app.durable.match_batch', side_effect=supersede_then_fail):
        assert process_jobs(db, 'match') == 0

    job = db.get(DurableJob, key, populate_existing=True)
    assert json.loads(job.payload) == {'offer_id': offer.id, 'updated': True}
    assert (job.status, job.cursor, job.attempts) == ('pending', 0, 0)
    assert (job.last_error, job.next_attempt_at, job.completed_at) == (None, None, None)
    assert (job.lease_token, job.lease_until) == (None, None)


def test_match_worker_continues_after_finalizing_first_hundred_jobs(db, user):
    for index in range(101):
        offer = Offer(canonical_url=f'https://example.com/backlog/{index}',
            offer_url=f'https://example.com/backlog/{index}', name=f'Intern {index}',
            region='France', programme_type='summer')
        db.add(offer); db.flush()
        enqueue_job(db, f'match-offer/{offer.id}', 'match', {
            'offer_id': offer.id, 'updated': False,
        })
    db.commit()

    for _ in range(6):
        if not process_jobs(db, 'match'):
            break

    assert db.query(DurableJob).filter_by(kind='match', status='pending').count() == 0
    assert db.query(Delivery).count() == 101


def test_migration_gate_blocks_claims_and_resumes_on_failure(pg):
    from sqlalchemy.orm import Session
    from scripts.migrate_coordinated import migrate
    from trackr_app.runtime import invocation
    def fail(connection):
        with Session(pg) as db:
            with pytest.raises(RuntimeError, match='Paused'):
                with invocation(db):
                    pytest.fail('A worker acquired a lease during migration')
        raise ValueError('migration failed')
    with pytest.raises(ValueError):
        migrate(pg, migrate_call=fail)
    with Session(pg) as db:
        assert db.get(WorkerState, 'runtime/gate').last_error is None
        with invocation(db):
            assert db.scalar(select(WorkerState).where(WorkerState.key.startswith('runtime/active/')))


def test_additive_schema_keeps_bridge_web_compatible(pg):
    from alembic.config import Config
    from alembic import command
    from fastapi.testclient import TestClient
    from trackr_app.main import app
    with pg.begin() as connection:
        config = Config('alembic.ini'); config.attributes['connection'] = connection
        command.downgrade(config, '20260908_0006')
    with patch('trackr_app.main.engine', pg), patch('trackr_app.main.settings', SimpleNamespace(is_production=True)):
        client = TestClient(app)
        response = client.get('/health')
        assert response.status_code == 200
        assert response.json()['schema'] == '20260908_0006'
        with pg.begin() as connection:
            config.attributes['connection'] = connection
            command.upgrade(config, 'head')
        assert client.get('/health').status_code == 200


def test_retention_preserves_failures_and_delivery_receipts(db, user, offer):
    from trackr_app.maintenance import purge
    from trackr_app.preferences import activate_preference
    old = utcnow()-timedelta(days=40)
    db.add(PasswordToken(user_id=user.id, token_hash='old', expires_at=old))
    db.add(Delivery(user_id=user.id, offer_id=offer.id, mode='immediate', status='sent', sent_at=old))
    db.add(DurableJob(key='unresolved', kind='notion-create', status='failed', created_at=old))
    db.commit()
    purge(db)
    assert db.query(PasswordToken).count() == db.query(Delivery).count() == 0
    assert db.get(DurableJob, 'unresolved').status == 'failed'
    assert db.get(DurableJob, f'receipt/{user.id}/{offer.id}')
    cancelled = Delivery(user_id=user.id, offer_id=offer.id, mode='daily_digest', status='cancelled')
    db.add(cancelled); db.commit()
    activate_preference(db, user.preference)
    assert cancelled.status == 'cancelled'


def test_admin_retry_is_targeted_and_uncertain_is_not_retried(client, db):
    db.add_all([DurableJob(key='failed', kind='match', status='failed'),
        DurableJob(key='uncertain', kind='notion-create', status='uncertain')]); db.commit()
    assert client.get('/admin/jobs?kind=durable').status_code == 200
    assert client.post('/admin/jobs/retry', data={'csrf_token': 'csrf', 'kind': 'durable', 'job_id': 'failed'}, follow_redirects=False).status_code == 303
    assert db.get(DurableJob, 'failed').status == 'pending'
    assert client.post('/admin/jobs/retry', data={'csrf_token': 'csrf', 'kind': 'durable', 'job_id': 'uncertain'}, follow_redirects=False).status_code == 409


def test_snapshot_export_requires_admin(client, db):
    db.add(SourceSnapshot(key='legacy/example', payload='[{"name":"Example", "offer_url":"https://example.com"}]')); db.commit()
    response = client.get('/admin/export.csv?source=legacy/example')
    assert response.status_code == 200 and 'https://example.com' in response.text
    client.cookies.clear()
    assert client.get('/admin/export.csv?source=legacy/example', follow_redirects=False).status_code == 303

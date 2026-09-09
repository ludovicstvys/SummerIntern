from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
import pytest
from sqlalchemy import select, text
from tests.test_audit import db, user, client, offer
from tests.test_postgres import pg
from trackr_app.models import OAuthNonce, DurableJob, WorkerState, SourceSnapshot, Delivery, AuthMail, PasswordToken, utcnow
from trackr_app.durable import enqueue_job, process_jobs
from trackr_app.sessions import aware


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

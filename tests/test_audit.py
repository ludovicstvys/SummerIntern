import json
from datetime import time, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from trackr_app.database import Base, get_db
from trackr_app.main import app
from trackr_app.models import Delivery, MagicLink, NotionConnection, NotionSync, Offer, Preference, User, UserOffer, UserSession, utcnow
from trackr_app.security import token_hash, expires_in, encrypt
from trackr_app.preferences import activate_preference
from trackr_app.workers import process_digests, process_immediate_alerts
from trackr_app.notion import save_connection, create_offer_database, process_notion_queue
from trackr_app.scraper import scrape_all


@pytest.fixture
def db():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session
    engine.dispose()


@pytest.fixture
def user(db):
    user = User(email='person@example.com', role='admin')
    db.add(user); db.flush()
    db.add(Preference(user_id=user.id, status='active', program_types='["summer"]', regions='["France"]'))
    db.add(UserSession(user_id=user.id, token_hash=token_hash('audit-session'), csrf_token='csrf', expires_at=expires_in(30)))
    db.commit()
    return user


@pytest.fixture
def client(db, user):
    def override():
        yield db
    app.dependency_overrides[get_db] = override
    with patch('trackr_app.main.engine', db.bind):
        client = TestClient(app)
        client.cookies.set('trackr_session', 'audit-session')
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def offer(db):
    offer = Offer(canonical_url='https://example.com/job', offer_url='https://example.com/job', name='Intern', company='Example', region='France', programme_type='summer')
    db.add(offer); db.commit()
    return offer


def form(**changes):
    return dict(csrf_token='csrf', program_types='summer', regions='UK', delivery_mode='daily_digest', digest_time='08:00', timezone='Europe/Paris', **changes)


def test_preview_preserves_active_preferences_and_activation_applies(client, user, db):
    response = client.post('/preferences/preview', data=form())
    assert response.status_code == 200
    db.refresh(user.preference)
    assert user.preference.status == 'active'
    assert json.loads(user.preference.regions) == ['France']
    assert 'name="regions" value="UK"' in response.text
    response = client.post('/preferences/activate', data=form(), follow_redirects=False)
    assert response.status_code == 303
    db.refresh(user.preference)
    assert json.loads(user.preference.regions) == ['UK']
    assert user.preference.delivery_mode == 'daily_digest'


@pytest.mark.parametrize('path', ['/preferences/preview', '/preferences/activate'])
@pytest.mark.parametrize('value', ['invalid', '25:00', '08:00+02:00'])
def test_invalid_time_is_form_error(client, user, path, value):
    data = form(); data['digest_time'] = value
    response = client.post(path, data=data)
    assert response.status_code == 422
    assert 'HH:MM' in response.text
    assert user.preference.status == 'active'


def test_login_limits_apply_to_known_and_unknown_addresses(client, db, user):
    with patch('trackr_app.main.send_magic_link') as send:
        responses = [client.post('/auth/request', data={'email': user.email}, follow_redirects=False) for _ in range(3)]
        unknown = client.post('/auth/request', data={'email': 'unknown@example.com'}, follow_redirects=False)
    assert send.call_count == 1
    assert all(response.headers['location'] == unknown.headers['location'] for response in responses)
    assert db.query(MagicLink).count() == 1


def test_ip_limit_covers_distinct_invited_addresses(client, db):
    for i in range(21): db.add(User(email=f'p{i}@example.com'))
    db.commit()
    with patch('trackr_app.main.send_magic_link') as send:
        for i in range(21): client.post('/auth/request', data={'email': f'p{i}@example.com'})
    assert send.call_count == 20


def test_production_logs_never_include_magic_link(client, user, capsys):
    with patch('trackr_app.main.settings', SimpleNamespace(app_url='https://example.com', is_production=True)), patch('trackr_app.main.send_magic_link', side_effect=RuntimeError('secret-url')):
        client.post('/auth/request', data={'email': user.email})
    output = capsys.readouterr().out
    assert 'secret-url' not in output and '/auth/consume/' not in output


def test_deactivation_revokes_sessions_and_cancels_jobs(client, db, offer):
    target = User(email='disabled@example.com'); db.add(target); db.flush()
    db.add(UserSession(user_id=target.id, token_hash=token_hash('target'), csrf_token='target', expires_at=expires_in(30)))
    db.add(MagicLink(user_id=target.id, token_hash=token_hash('magic'), expires_at=expires_in(15)))
    delivery = Delivery(user_id=target.id, offer_id=offer.id, mode='immediate'); db.add(delivery)
    connection = NotionConnection(user_id=target.id, access_token_encrypted=encrypt('token'), data_source_id='source'); db.add(connection); db.flush()
    job = NotionSync(connection_id=connection.id, offer_id=offer.id); db.add(job); db.commit()
    assert client.post(f'/admin/users/{target.id}/toggle', data={'csrf_token':'csrf'}, follow_redirects=False).status_code == 303
    assert not db.get(User, target.id).is_active
    assert db.query(UserSession).filter_by(user_id=target.id).count() == 0
    assert db.query(MagicLink).filter_by(user_id=target.id).count() == 0
    db.refresh(delivery); db.refresh(job)
    assert delivery.status == job.status == 'cancelled'


def test_activation_reconciles_pending_and_preserves_sent(db, user, offer):
    delivery = Delivery(user_id=user.id, offer_id=offer.id, mode='immediate')
    db.add(delivery); db.commit()
    user.preference.delivery_mode = 'daily_digest'
    activate_preference(db, user.preference)
    assert delivery.status == 'cancelled'
    new = db.query(Delivery).filter_by(mode='daily_digest').one()
    assert new.status == 'pending'
    user.preference.delivery_mode = 'immediate'
    activate_preference(db, user.preference)
    assert new.status == 'cancelled' and delivery.status == 'pending'
    delivery.status = 'sent'; db.commit()
    user.preference.delivery_mode = 'daily_digest'
    activate_preference(db, user.preference)
    assert new.status == 'cancelled' and delivery.status == 'sent'


def test_activation_cancels_removed_matches(db, user, offer):
    delivery = Delivery(user_id=user.id, offer_id=offer.id, mode='immediate'); db.add(delivery); db.commit()
    user.preference.regions = '["UK"]'
    activate_preference(db, user.preference)
    assert delivery.status == 'cancelled'


@pytest.mark.parametrize('mode,worker', [('immediate', process_immediate_alerts), ('daily_digest', process_digests)])
def test_disabled_account_never_sends(db, user, offer, mode, worker):
    user.is_active = False; user.preference.delivery_mode = mode; user.preference.digest_time = time(0)
    db.add(Delivery(user_id=user.id, offer_id=offer.id, mode=mode)); db.commit()
    with patch('trackr_app.workers.send_email') as send: worker(db)
    send.assert_not_called()


def test_smtp_failure_retries_and_exhausts(db, user, offer):
    delivery = Delivery(user_id=user.id, offer_id=offer.id, mode='immediate'); db.add(delivery); db.commit()
    with patch('trackr_app.workers.send_email', side_effect=RuntimeError('secret')) as send:
        for _ in range(6): process_immediate_alerts(db)
    db.refresh(delivery)
    assert send.call_count == 5 and delivery.status == 'failed' and delivery.attempts == 5
    assert delivery.last_error == 'RuntimeError'


def test_stale_processing_claim_is_retried(db, user, offer):
    delivery = Delivery(user_id=user.id, offer_id=offer.id, mode='immediate', status='processing', processing_started_at=utcnow()-timedelta(minutes=20)); db.add(delivery); db.commit()
    with patch('trackr_app.workers.send_email', return_value='message'):
        assert process_immediate_alerts(db) == 1
    assert delivery.status == 'sent'


def connection(db, user, offer):
    conn = NotionConnection(user_id=user.id, access_token_encrypted=encrypt('old'), workspace_id='workspace', database_id='database', data_source_id='source')
    db.add(conn); db.flush()
    job = NotionSync(connection_id=conn.id, offer_id=offer.id, notion_page_id='old-page', status='synced')
    db.add(job); db.commit()
    return conn, job


def test_notion_reconnect_preserves_same_workspace_resets_different(db, user, offer):
    conn, job = connection(db, user, offer)
    save_connection(db, user.id, {'access_token':'new', 'workspace_id':'workspace'})
    assert conn.data_source_id == 'source' and db.query(NotionSync).count() == 1
    save_connection(db, user.id, {'access_token':'newer', 'workspace_id':'other'})
    assert conn.database_id is None and conn.data_source_id is None and db.query(NotionSync).count() == 0


def test_notion_new_database_resets_old_page_references(db, user, offer):
    conn, job = connection(db, user, offer)
    response = Mock(); response.json.return_value = {'id':'new-db', 'data_sources':[{'id':'new-source'}]}
    with patch('trackr_app.notion.requests.post', return_value=response): create_offer_database(db, conn, 'parent')
    assert conn.data_source_id == 'new-source' and db.query(NotionSync).count() == 0


def test_notion_disabled_account_no_external_requests(db, user, offer):
    conn, job = connection(db, user, offer); job.status='pending'; user.is_active=False; db.commit()
    with patch('trackr_app.notion.requests.post') as post: assert process_notion_queue(db) == 0
    post.assert_not_called(); assert job.status == 'cancelled'


def test_unchanged_scrape_preserves_notion_failure_counter(db, user, offer):
    conn, job = connection(db, user, offer); job.status='failed'; job.attempts=5
    db.add(UserOffer(user_id=user.id, offer_id=offer.id)); db.commit()
    item = {'name':offer.name, 'company':offer.company, 'offer_url':offer.offer_url}
    with patch('trackr_app.scraper.TRACKERS', [{'region':'France','type':'summer-internships'}]), patch('trackr_app.scraper.scrape_open_programmes', return_value=[item]): scrape_all(db)
    assert job.status == 'failed' and job.attempts == 5


def test_scrape_failure_and_reappearance_do_not_close_offer(db, offer):
    other = {'name':'Other', 'offer_url':'https://example.com/other'}
    original = {'name':offer.name, 'offer_url':offer.offer_url}
    with patch('trackr_app.scraper.TRACKERS', [{'region':'France','type':'summer-internships'}]), patch('trackr_app.scraper.scrape_open_programmes', side_effect=[[other], RuntimeError(), [original,other], [other]]):
        scrape_all(db); assert offer.missing_collections == 1
        assert scrape_all(db)['failed_trackers'] == 1; assert offer.is_open
        scrape_all(db); assert offer.missing_collections == 0
        scrape_all(db); assert offer.missing_collections == 1 and offer.is_open


def test_partial_upstream_response_is_rejected():
    from trackr_common import scrape_open_programmes
    response=Mock(); response.json.return_value={'data':[{'name':'Example'}], 'has_more':True}
    with patch('trackr_common.requests.get', return_value=response), pytest.raises(RuntimeError, match='partial'):
        scrape_open_programmes({})


def test_health_rejects_wrong_schema(client, db):
    with patch('trackr_app.main.settings', SimpleNamespace(is_production=True)):
        assert client.get('/health').status_code == 503

"""Real PostgreSQL regression tests, always isolated in a disposable schema."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import time
from threading import Event
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from trackr_app.limits import allow_login
from trackr_app.models import User, Preference, Offer, Delivery
from trackr_app.workers import process_digests


@pytest.fixture
def pg():
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('TEST_DATABASE_URL is not configured')
    url = url.replace('postgres://','postgresql+psycopg://',1).replace('postgresql://','postgresql+psycopg://',1)
    admin = create_engine(url)
    schema = 'audit_test_' + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    # Set the schema on every connection; no test touches public tables.
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        with engine.begin() as connection:
            config = Config('alembic.ini'); config.attributes['connection'] = connection
            command.upgrade(config, 'head')
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_postgres_migrations_match_models(pg):
    with pg.connect() as connection:
        assert connection.scalar(text('select version_num from alembic_version')) == '20260907_0005'
    with pg.begin() as connection:
        config = Config('alembic.ini'); config.attributes['connection'] = connection
        command.check(config)


def test_postgres_login_limit_is_atomic(pg):
    Session = sessionmaker(bind=pg)
    def request(_):
        with Session() as db:
            return allow_login(db, 'same@example.com', '192.0.2.1')
    # Freeze the instant so the test cannot straddle a rate-limit bucket.
    from trackr_app.models import utcnow
    with patch('trackr_app.limits.utcnow', return_value=utcnow()), ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(request, range(8)))
    assert sum(results) == 1


def test_postgres_concurrent_digests_send_once(pg):
    Session = sessionmaker(bind=pg, expire_on_commit=False)
    with Session() as db:
        user = User(email='person@example.com'); db.add(user); db.flush()
        db.add(Preference(user_id=user.id, status='active', delivery_mode='daily_digest', digest_time=time(0)))
        offer = Offer(canonical_url='https://example.com/job', offer_url='https://example.com/job', name='Intern', region='France', programme_type='summer')
        db.add(offer); db.flush(); db.add(Delivery(user_id=user.id, offer_id=offer.id, mode='daily_digest')); db.commit()
        user_id = user.id
    entered, release = Event(), Event()
    def send(*args):
        entered.set()
        assert release.wait(10)
        return 'message'
    def worker():
        with Session() as db: return process_digests(db)
    with patch('trackr_app.workers.send_email', side_effect=send) as sender, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker)
        try:
            assert entered.wait(10)
            assert pool.submit(worker).result(timeout=10) == 0
        finally:
            release.set()
        assert first.result(timeout=10) == 1
    assert sender.call_count == 1
    # A later arrival must wait for tomorrow after today's successful digest.
    with Session() as db:
        offer = Offer(canonical_url='https://example.com/later', offer_url='https://example.com/later', name='Later', region='France', programme_type='summer')
        db.add(offer); db.flush(); db.add(Delivery(user_id=user_id, offer_id=offer.id, mode='daily_digest')); db.commit()
        assert process_digests(db) == 0


def test_postgres_concurrent_collectors_preserve_one_delivery(pg):
    from trackr_app.scraper import scrape_all
    from trackr_app.models import OfferSource
    Session = sessionmaker(bind=pg, expire_on_commit=False)
    with Session() as db:
        user = User(email='collector@example.com'); db.add(user); db.flush()
        db.add(Preference(user_id=user.id, status='active')); db.commit()
    entered, release = Event(), Event()
    calls = []
    def upstream(params):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
        return [{'name': 'Concurrent role', 'offer_url': 'https://example.com/concurrent'}]
    def collect():
        with Session() as db:
            return scrape_all(db)
    with patch('trackr_app.scraper.TRACKERS', [{'region': 'France', 'type': 'summer-internships'}]), patch('trackr_app.scraper.scrape_open_programmes', side_effect=upstream), ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(collect)
        try:
            assert entered.wait(10)
            second = pool.submit(collect)
        finally:
            release.set()
        assert first.result(timeout=10)['failed_trackers'] == 0
        assert second.result(timeout=10)['failed_trackers'] == 0
    with Session() as db:
        assert db.query(Offer).count() == db.query(OfferSource).count() == db.query(Delivery).count() == 1


def test_postgres_concurrent_invites_create_one_account_and_mail(pg):
    from fastapi.testclient import TestClient
    from trackr_app.main import app
    from trackr_app.database import get_db
    from trackr_app.models import UserSession, Invitation
    from trackr_app.security import token_hash, expires_in
    Session = sessionmaker(bind=pg, expire_on_commit=False)
    with Session() as db:
        admin = User(email='admin@example.com', role='admin'); db.add(admin); db.flush()
        db.add(UserSession(user_id=admin.id, token_hash=token_hash('session'), csrf_token='csrf', expires_at=expires_in(30))); db.commit()
    def dependency():
        with Session() as db:
            yield db
    def invite():
        client = TestClient(app)
        client.cookies.set('trackr_session', 'session')
        return client.post('/admin/invite', data={'email': 'invited@example.com', 'csrf_token': 'csrf'}, follow_redirects=False).status_code
    entered, release = Event(), Event()
    def sender(*args):
        entered.set(); assert release.wait(10)
    app.dependency_overrides[get_db] = dependency
    try:
        with patch('trackr_app.main.send_magic_link', side_effect=sender) as send, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(invite)
            try:
                assert entered.wait(10)
                second = pool.submit(invite)
            finally:
                release.set()
            assert first.result(timeout=10) == second.result(timeout=10) == 303
        assert send.call_count == 1
        with Session() as db:
            assert db.query(User).filter_by(email='invited@example.com').count() == 1
            assert db.query(Invitation).count() == 1
    finally:
        app.dependency_overrides.clear()


def test_postgres_upgrade_preserves_existing_data_without_resending_invites(pg):
    from trackr_app.models import Invitation, OfferSource
    Session = sessionmaker(bind=pg, expire_on_commit=False)
    with Session() as db:
        admin = User(email='migration@example.com', role='admin'); db.add(admin); db.flush()
        db.add(Invitation(email='old-invite@example.com', invited_by_id=admin.id, delivery_status='sent'))
        offer = Offer(canonical_url='https://example.com/migration', offer_url='https://example.com/migration', name='Existing', region='France', programme_type='summer')
        db.add(offer); db.commit()
        offer_id = offer.id
    with pg.begin() as connection:
        config = Config('alembic.ini'); config.attributes['connection'] = connection
        command.downgrade(config, '20260906_0003')
        command.upgrade(config, 'head')
        command.check(config)
    with Session() as db:
        assert db.get(Offer, offer_id).name == 'Existing'
        source = db.query(OfferSource).one()
        assert source.offer_id == offer_id and source.region == 'France' and source.season == '2027'
        assert db.query(Invitation).one().delivery_status == 'sent'


@pytest.mark.parametrize('second_token', ['first', 'second'])
def test_postgres_concurrent_password_resets_have_one_winner(pg, second_token):
    from fastapi.testclient import TestClient
    from tests.auth_helpers import auth_form
    from trackr_app.auth import passwords
    from trackr_app.database import get_db
    from trackr_app.main import app
    from trackr_app.models import PasswordToken, UserSession
    from trackr_app.security import token_hash, expires_in
    Session = sessionmaker(bind=pg, expire_on_commit=False)
    with Session() as db:
        user = User(email='reset@example.com'); db.add(user); db.flush()
        for raw in ('first', 'second'):
            db.add(PasswordToken(user_id=user.id, token_hash=token_hash(raw), expires_at=expires_in(15)))
        db.commit(); user_id = user.id
    def dependency():
        with Session() as db:
            yield db
    app.dependency_overrides[get_db] = dependency
    entered, release = Event(), Event()
    original_hash = passwords.hash
    def slow_hash(password):
        entered.set(); assert release.wait(10)
        return original_hash(password)
    # Do not enter TestClient lifespan: it bootstraps the application's real database.
    def reset(raw):
        client = TestClient(app)
        try:
            data = auth_form(client, password='new password phrase', confirmation='new password phrase')
            return client.post('/auth/password/reset/' + raw, data=data, follow_redirects=False)
        finally:
            client.close()
    try:
        with patch.object(passwords, 'hash', side_effect=slow_hash), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(reset, 'first')
            try:
                assert entered.wait(10)
                second = pool.submit(reset, second_token)
            finally:
                release.set()
            results = [first.result(timeout=10), second.result(timeout=10)]
        assert sum(r.headers['location'] == '/dashboard' for r in results) == 1
        with Session() as db:
            assert db.query(UserSession).count() == 1
            assert db.query(PasswordToken).count() == 0
            assert passwords.verify('new password phrase', db.get(User, user_id).password_hash)
    finally:
        app.dependency_overrides.clear()


def test_postgres_session_renewal_is_atomic(pg):
    from datetime import timedelta
    from types import SimpleNamespace
    from trackr_app.models import UserSession, utcnow
    from trackr_app.security import token_hash
    from trackr_app.sessions import current_user, aware
    Session = sessionmaker(bind=pg, expire_on_commit=False)
    now = utcnow()
    with Session() as db:
        user = User(email='session@example.com'); db.add(user); db.flush()
        db.add(UserSession(user_id=user.id, token_hash=token_hash('persistent'), csrf_token='csrf',
            created_at=now-timedelta(days=10), expires_at=now+timedelta(days=20)))
        db.commit()
    def renew(_):
        request = SimpleNamespace(cookies={'trackr_session': 'persistent'}, state=SimpleNamespace())
        with Session() as db:
            assert current_user(request, db) is not None
        return hasattr(request.state, 'session_cookie')
    with patch('trackr_app.sessions.utcnow', return_value=now), ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(renew, range(8)))
    assert sum(results) == 1
    with Session() as db:
        assert aware(db.query(UserSession).one().expires_at) == now+timedelta(days=90)

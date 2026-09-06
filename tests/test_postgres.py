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
        assert connection.scalar(text('select version_num from alembic_version')) == '20260906_0003'
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

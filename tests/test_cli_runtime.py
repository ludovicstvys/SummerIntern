from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from trackr_app import cli
from trackr_app.database import Base
from trackr_app.models import WorkerState


@pytest.fixture
def session_factory():
    engine = create_engine(
        'sqlite://',
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def _lease_is_active(db):
    return db.scalar(select(WorkerState.key).where(
        WorkerState.key.startswith('runtime/active/')).limit(1)) is not None


def test_run_command_keeps_lease_through_state_updates(session_factory):
    events = []
    original_lock_state = cli.lock_state

    def observed_lock_state(db, key):
        if key.startswith('execution/'):
            events.append(('state', _lease_is_active(db)))
        return original_lock_state(db, key)

    def worker(_command):
        with session_factory() as db:
            events.append(('worker', _lease_is_active(db)))
        return 0

    with patch('trackr_app.cli.SessionLocal', session_factory), patch(
        'trackr_app.cli.lock_state', side_effect=observed_lock_state,
    ), patch('trackr_app.cli._run_command', side_effect=worker):
        assert cli.run_command('process-matches') == 0

    assert events == [('state', True), ('worker', True), ('state', True)]
    with session_factory() as db:
        assert not _lease_is_active(db)
        assert db.get(WorkerState, 'execution/process-matches').last_error is None


def test_run_command_keeps_lease_during_failure_recording(session_factory):
    final_state_had_lease = []
    original_lock_state = cli.lock_state

    def observed_lock_state(db, key):
        if key == 'execution/scrape-all':
            final_state_had_lease.append(_lease_is_active(db))
        return original_lock_state(db, key)

    with patch('trackr_app.cli.SessionLocal', session_factory), patch(
        'trackr_app.cli.lock_state', side_effect=observed_lock_state,
    ), patch('trackr_app.cli._run_command', side_effect=LookupError('failed')):
        with pytest.raises(LookupError, match='failed'):
            cli.run_command('scrape-all')

    assert final_state_had_lease == [True, True]
    with session_factory() as db:
        assert not _lease_is_active(db)
        assert db.get(WorkerState, 'execution/scrape-all').last_error == 'LookupError'


def test_maintenance_command_runs_inside_lease(session_factory):
    observed = []

    def maintenance(_args):
        with session_factory() as db:
            observed.append(_lease_is_active(db))
        return 7

    args = SimpleNamespace(command='check-operations')
    with patch('trackr_app.cli.SessionLocal', session_factory), patch(
        'trackr_app.cli.maintenance', side_effect=maintenance,
    ):
        assert cli.run_maintenance(args) == 7

    assert observed == [True]
    with session_factory() as db:
        assert not _lease_is_active(db)


@pytest.mark.parametrize(
    ('command', 'expected_auth'),
    [
        ('process-auth-mail', True),
        ('process-invitations', True),
        ('process-immediate-alerts', False),
    ],
)
def test_cli_selects_schema_compatibility(command, expected_auth, session_factory):
    observed = []

    @contextmanager
    def invocation(_db, auth=False):
        observed.append(auth)
        yield

    with patch('trackr_app.cli.SessionLocal', session_factory), patch(
        'trackr_app.cli.invocation', side_effect=invocation,
    ), patch('trackr_app.cli._run_command', return_value=0):
        assert cli.run_command(command) == 0

    assert observed == [expected_auth]

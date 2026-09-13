import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml
from sqlalchemy.orm import sessionmaker

from tests.test_audit import db, offer, user
from trackr_app.models import AuthMail, Invitation, NotionConnection, NotionSync
from trackr_app.runtime import HARD_TIMEOUT_SECONDS, WORK_BUDGET_SECONDS


ROOT = Path(__file__).resolve().parents[1]


def test_worker_soft_budget_precedes_cli_and_shell_timeouts():
    assert WORK_BUDGET_SECONDS + 10 <= HARD_TIMEOUT_SECONDS

    command_timeouts = []
    for filename in ('collection.yml', 'platform-jobs.yml', 'email-delivery.yml'):
        workflow = yaml.load(
            (ROOT / '.github/workflows' / filename).read_text(),
            Loader=yaml.BaseLoader,
        )
        command_timeouts.extend(
            int(match.group(1))
            for step in workflow['jobs']['process']['steps']
            if (match := re.match(
                r'timeout (\d+)s python -m trackr_app\.cli ',
                step.get('run', ''),
            ))
        )
    assert command_timeouts
    assert min(command_timeouts) >= HARD_TIMEOUT_SECONDS + 10


def test_match_command_passes_the_soft_deadline(db):
    import trackr_app.cli as cli

    factory = sessionmaker(bind=db.bind, expire_on_commit=False)
    observed = []

    def process_jobs(session, kind, deadline):
        observed.append((kind, deadline))
        return 0

    clock = SimpleNamespace(monotonic=Mock(return_value=100.0))
    with patch('trackr_app.cli.SessionLocal', factory), \
            patch('trackr_app.cli.time', clock), \
            patch('trackr_app.durable.process_jobs', side_effect=process_jobs):
        assert cli._run_command('process-matches') == 0

    assert observed == [
        ('match', 100.0 + WORK_BUDGET_SECONDS),
        ('match', 100.0 + WORK_BUDGET_SECONDS),
    ]


def test_notion_worker_passes_the_soft_deadline(db, user, offer):
    import trackr_app.notion as notion

    connection = NotionConnection(
        user_id=user.id,
        access_token_encrypted='unused',
        data_source_id='source-id',
    )
    db.add(connection)
    db.flush()
    db.add(NotionSync(connection_id=connection.id, offer_id=offer.id))
    db.commit()
    observed = []

    def process_job(session, job_id, user_id, deadline):
        observed.append(deadline)
        return True

    clock = SimpleNamespace(monotonic=Mock(return_value=100.0))
    with patch('trackr_app.notion.time', clock), \
            patch('trackr_app.notion._process_notion_job', side_effect=process_job):
        assert notion.process_notion_queue(db) == 1

    assert observed == [100.0 + WORK_BUDGET_SECONDS]


@pytest.mark.parametrize(
    ('function_name', 'mode'),
    (
        ('process_immediate_alerts', 'immediate'),
        ('process_digests', 'daily_digest'),
    ),
)
def test_delivery_workers_pass_the_soft_deadline(db, function_name, mode):
    import trackr_app.workers as workers

    observed = []

    def user_ids(session, selected_mode, deadline):
        observed.append((selected_mode, deadline))
        return []

    clock = SimpleNamespace(monotonic=Mock(return_value=100.0))
    with patch('trackr_app.workers.time', clock), \
            patch('trackr_app.workers._user_ids', side_effect=user_ids):
        assert getattr(workers, function_name)(db) == 0

    assert observed == [(mode, 100.0 + WORK_BUDGET_SECONDS)]


def test_scraper_stops_before_starting_work_past_soft_deadline(db, monkeypatch):
    import trackr_app.scraper as scraper

    monkeypatch.delenv('TRACKR_SOURCE_INDEX', raising=False)
    tracker = {'region': 'France', 'type': 'summer-internships'}
    clock = SimpleNamespace(monotonic=Mock(side_effect=(100.0, 181.0, 181.0)))
    with patch('trackr_app.scraper.TRACKERS', [tracker]), \
            patch('trackr_app.scraper.time', clock), \
            patch('trackr_app.scraper.scrape_open_programmes') as fetch:
        assert scraper.scrape_all(db)['failed_trackers'] == 0

    fetch.assert_not_called()


def test_auth_worker_stops_before_starting_work_past_soft_deadline(db):
    import trackr_app.auth_mail as auth_mail

    db.add(AuthMail(email_encrypted='unused', email_hash='hash', kind='magic'))
    db.commit()
    clock = SimpleNamespace(monotonic=Mock(side_effect=(100.0, 181.0)))
    with patch('trackr_app.auth_mail.time', clock), \
            patch('trackr_app.auth_mail.process_auth_mail') as process:
        assert auth_mail.process_auth_queue(db) == 0

    process.assert_not_called()


def test_invitation_worker_stops_before_starting_work_past_soft_deadline(db, user):
    import trackr_app.invitations as invitations

    db.add(Invitation(email='invite@example.com', invited_by_id=user.id))
    db.commit()
    clock = SimpleNamespace(monotonic=Mock(side_effect=(100.0, 181.0)))
    with patch('trackr_app.invitations.time', clock), \
            patch('trackr_app.invitations.deliver_invitation') as deliver:
        assert invitations.process_invitations(db) == 0

    deliver.assert_not_called()

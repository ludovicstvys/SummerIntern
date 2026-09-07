import json
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import select
from tests.test_audit import db, user, offer, client
from trackr_app.models import Delivery, Invitation, LegacyTask, MagicLink, NotionConnection, Offer, OfferSource, User
from trackr_app.invitations import deliver_invitation, process_invitations
from trackr_app.legacy import enqueue, process_tasks
from trackr_app.preferences import offer_matches, offer_is_open
from trackr_app.scraper import scrape_all
from trackr_app.security import encrypt
from trackr_app.models import utcnow
from trackr_app.workers import process_immediate_alerts
from trackr_common import OfferSnapshot, canonical_offer_url, scrape_open_programmes, smtp_password, write_csv


def test_gmail_password_normalization_is_provider_specific():
    assert smtp_password('abcd\u00a0efgh ijkl mnop', 'smtp.gmail.com') == 'abcdefghijklmnop'
    assert smtp_password('ordinary password', 'smtp.example.com') == 'ordinary password'


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'data:text/html,test', '/relative', 'https://example.com:bad/job', 'https://user:pass@example.com/job'])
def test_invalid_offer_urls_are_rejected(url):
    assert canonical_offer_url(url) == ''


def test_confirmed_empty_snapshot_can_close_last_offer(db, offer):
    response = Mock(); response.json.return_value = {'data': [], 'total': 0, 'has_more': False}
    with patch('trackr_common.requests.get', return_value=response), patch('trackr_app.scraper.TRACKERS', [{'region': 'France', 'type': 'summer-internships'}]):
        assert scrape_all(db)['closed'] == 0
        assert scrape_all(db)['closed'] == 1
    assert not offer.is_open


def test_atomic_csv_failure_preserves_previous_snapshot(tmp_path):
    path = tmp_path/'offers.csv'; path.write_text('original')
    with pytest.raises(KeyError):
        write_csv([{'name': 'malformed'}], path)
    assert path.read_text() == 'original'
    write_csv(OfferSnapshot(), path)
    assert path.read_text().startswith('Name,Company')


def test_one_membership_closing_keeps_other_region_open(db, user):
    trackers = [{'region': region, 'type': 'summer-internships'} for region in ('France', 'UK')]
    item = {'name': 'Shared', 'offer_url': 'https://example.com/shared'}
    with patch('trackr_app.scraper.TRACKERS', trackers), patch('trackr_app.scraper.scrape_open_programmes', return_value=[item]):
        scrape_all(db)
    with patch('trackr_app.scraper.TRACKERS', trackers[:1]), patch('trackr_app.scraper.scrape_open_programmes', return_value=OfferSnapshot()):
        scrape_all(db); scrape_all(db)
    shared = db.scalar(select(Offer).where(Offer.canonical_url == item['offer_url']))
    assert shared.is_open
    assert not offer_matches(shared, user.preference)
    user.preference.regions = '["UK"]'
    assert offer_matches(shared, user.preference)


def test_smtp_backoff_defers_retry(db, user, offer):
    item = Delivery(user_id=user.id, offer_id=offer.id, mode='immediate'); db.add(item); db.commit()
    with patch('trackr_app.workers.send_email', side_effect=RuntimeError()) as send:
        process_immediate_alerts(db); process_immediate_alerts(db)
    assert send.call_count == 1 and item.next_attempt_at is not None


def test_failed_invitation_retries_with_fresh_link(client, db):
    with patch('trackr_app.main.send_magic_link', side_effect=RuntimeError()):
        client.post('/admin/invite', data={'csrf_token': 'csrf', 'email': 'retry@example.com'})
    invitation = db.query(Invitation).one()
    assert invitation.delivery_status == 'pending' and invitation.attempts == 1
    assert db.query(MagicLink).count() == 0
    invitation.next_attempt_at = utcnow() - timedelta(seconds=1); db.commit()
    with patch('trackr_app.invitations.send_magic_link') as send:
        assert process_invitations(db) == 1
    assert send.call_count == 1 and invitation.delivery_status == 'sent'
    assert db.query(MagicLink).count() == 1


def test_disabled_invitation_is_cancelled(db, user):
    invitation = Invitation(email=user.email, invited_by_id=user.id)
    user.is_active = False; db.add(invitation); db.commit()
    sender = Mock()
    assert not deliver_invitation(db, invitation.id, sender)
    assert invitation.delivery_status == 'cancelled'
    sender.assert_not_called()


def test_legacy_email_survives_notion_failure_and_deduplicates(db, monkeypatch):
    monkeypatch.setenv('LEGACY_EMAIL_ENABLED', 'true')
    item = {'name': 'Intern', 'offer_url': 'https://example.com/role'}
    enqueue(db, 'source', 'email', 'legacy@example.com', item, 'internship')
    enqueue(db, 'source', 'notion', '', item, 'internship')
    db.commit()
    adapter = SimpleNamespace(send_email=Mock(return_value=True), sync_to_notion=Mock(side_effect=RuntimeError()))
    assert process_tasks(db, 'source', adapter) == 1
    assert db.query(LegacyTask).filter_by(channel='email').one().status == 'sent'
    enqueue(db, 'source', 'email', 'legacy@example.com', item, 'internship'); db.commit()
    process_tasks(db, 'source', adapter)
    assert adapter.send_email.call_count == 1


def test_legacy_smtp_failure_is_replayed_without_csv(db, monkeypatch):
    monkeypatch.setenv('LEGACY_EMAIL_ENABLED', 'true')
    item = {'name': 'Intern', 'offer_url': 'https://example.com/role'}
    enqueue(db, 'source', 'email', 'legacy@example.com', item, 'internship'); db.commit()
    adapter = SimpleNamespace(send_email=Mock(side_effect=RuntimeError()))
    assert process_tasks(db, 'source', adapter) == 1
    task = db.query(LegacyTask).one(); task.next_attempt_at = None; db.commit()
    adapter.send_email = Mock(return_value=True)
    assert process_tasks(db, 'source', adapter) == 0 and task.status == 'sent'
    assert adapter.send_email.call_args.kwargs['recipients'] == ['legacy@example.com']


@pytest.mark.parametrize('active', [True, False])
def test_platform_accounts_never_receive_legacy_email(db, user, monkeypatch, active):
    monkeypatch.setenv('LEGACY_EMAIL_ENABLED', 'true')
    user.is_active = active
    enqueue(db, 'source', 'email', user.email, {'name': 'Intern', 'offer_url': 'https://example.com/role'}, 'internship'); db.commit()
    adapter = Mock()
    process_tasks(db, 'source', adapter)
    adapter.send_email.assert_not_called()
    assert db.query(LegacyTask).one().status == 'cancelled'


def test_force_resend_has_new_identity(db):
    item = {'name': 'Intern', 'offer_url': 'https://example.com/role'}
    enqueue(db, 'source', 'email', 'legacy@example.com', item, 'internship')
    enqueue(db, 'source', 'email', 'legacy@example.com', item, 'internship', force='manual-run')
    db.commit()
    assert db.query(LegacyTask).count() == 2


def test_notion_setup_double_submit_creates_once(client, db, user):
    connection = NotionConnection(user_id=user.id, access_token_encrypted=encrypt('test'))
    db.add(connection); db.commit()
    def create(db, connection, page_id):
        connection.database_id, connection.data_source_id, connection.setup_status = 'db', 'source', 'ready'
    with patch('trackr_app.main.settings', SimpleNamespace(notion_available=True)), patch('trackr_app.main.create_offer_database', side_effect=create) as remote:
        for _ in range(2):
            assert client.post('/notion/setup', data={'csrf_token': 'csrf', 'page_id': 'parent'}, follow_redirects=False).status_code == 303
    assert remote.call_count == 1


def test_notion_uncertain_setup_does_not_create_again(client, db, user):
    db.add(NotionConnection(user_id=user.id, access_token_encrypted=encrypt('test'))); db.commit()
    with patch('trackr_app.main.settings', SimpleNamespace(notion_available=True)), patch('trackr_app.main.create_offer_database', side_effect=TimeoutError()) as remote:
        for _ in range(2):
            client.post('/notion/setup', data={'csrf_token': 'csrf', 'page_id': 'parent'}, follow_redirects=False)
    assert remote.call_count == 1 and user.notion.setup_status == 'uncertain'


def test_notion_unavailable_is_explicit(client):
    assert 'Temporarily unavailable' in client.get('/dashboard').text
    assert client.get('/notion/connect', follow_redirects=False).headers['location'].startswith('/dashboard')


def test_missing_legacy_recipients_preserve_csv_and_do_not_block_notion(db, tmp_path, monkeypatch):
    import test as adapter
    from sqlalchemy.orm import sessionmaker
    from trackr_app.legacy import run_collector
    monkeypatch.setenv('LEGACY_EMAIL_ENABLED', 'true')
    path = tmp_path/'offers.csv'; path.write_text('old snapshot')
    item = {'name': 'Role', 'offer_url': 'https://example.com/new'}
    with patch('trackr_app.legacy.SessionLocal', sessionmaker(bind=db.bind)), patch('trackr_app.legacy.scrape_open_programmes', return_value=[item]), patch.object(adapter, 'read_process_csv', return_value=[]), patch.object(adapter, 'read_email_recipients', return_value=[]), patch.object(adapter, 'NOTION_TOKEN', 'test'), patch.object(adapter, 'NOTION_DATA_SOURCE_ID', 'test'), patch.object(adapter, 'sync_to_notion') as sync:
        assert run_collector({'season': '2027', 'region': 'France', 'type': 'summer-internships'}, path, 'Summer') == 1
    assert path.read_text() == 'old snapshot'
    assert sync.call_count == 1

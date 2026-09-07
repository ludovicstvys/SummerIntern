"""Régressions corrigées de l'audit : services sortants simulés."""
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import select

from tests.test_audit import db, user, client, offer
from trackr_app.models import Delivery, User, UserOffer, UserSession
from trackr_app.preferences import activate_preference, queue_new_offer
from trackr_app.scraper import scrape_all
from trackr_app.workers import process_immediate_alerts
from trackr_common import scrape_open_programmes, canonical_offer_url


def test_invitation_login_activation_scrape_delivery_logout(client, db, user):
    with patch('trackr_app.main.send_magic_link') as mail:
        response = client.post('/admin/invite', data={'csrf_token': 'csrf', 'email': 'new@example.com'}, follow_redirects=False)
    assert response.status_code == 303
    target = db.scalar(select(User).where(User.email == 'new@example.com'))
    assert target.preference.status == 'draft'
    url = mail.call_args.args[1]
    client.cookies.clear()
    assert client.get('/auth/consume/' + url.rsplit('/', 1)[-1], follow_redirects=False).status_code == 303
    assert client.get('/dashboard').status_code == 200
    assert client.get('/admin').status_code == 403
    session = db.scalar(select(UserSession).where(UserSession.user_id == target.id))
    form = {'csrf_token': session.csrf_token, 'program_types': 'summer', 'regions': 'France', 'delivery_mode': 'immediate', 'digest_time': '08:00', 'timezone': 'Europe/Paris'}
    assert client.post('/preferences/activate', data={**form, 'csrf_token': 'wrong'}).status_code == 403
    assert client.post('/preferences/preview', data=form).status_code == 200
    assert client.post('/preferences/activate', data=form, follow_redirects=False).status_code == 303
    user.preference.status = 'draft'; db.commit()
    with patch('trackr_app.scraper.TRACKERS', [{'region': 'France', 'type': 'summer-internships'}]), patch('trackr_app.scraper.scrape_open_programmes', return_value=[{'name': 'Audit role', 'offer_url': 'https://example.com/audit'}]):
        assert scrape_all(db)['created'] == 1
        assert scrape_all(db)['created'] == 0
    with patch('trackr_app.workers.send_email', return_value='audit-message') as mail:
        assert process_immediate_alerts(db) == 1
        assert process_immediate_alerts(db) == 0
    assert mail.call_count == 1 and mail.call_args.args[0] == target.email
    assert client.post('/logout', data={'csrf_token': session.csrf_token}, follow_redirects=False).status_code == 303
    assert client.get('/dashboard', follow_redirects=False).headers['location'] == '/login'


def test_invitation_failure_is_visible(client):
    with patch('trackr_app.main.send_magic_link', side_effect=RuntimeError('SMTP unavailable')):
        response = client.post('/admin/invite', data={'csrf_token': 'csrf', 'email': 'failed@example.com'})
    assert 'delivery failed' in response.text.lower()


def test_shared_url_reaches_both_regions(db, user):
    user.preference.regions = '["UK"]'; db.commit()
    trackers = [{'region': r, 'type': 'summer-internships'} for r in ['France', 'UK']]
    with patch('trackr_app.scraper.TRACKERS', trackers), patch('trackr_app.scraper.scrape_open_programmes', return_value=[{'name': 'Shared role', 'offer_url': 'https://example.com/shared'}]):
        scrape_all(db)
    assert db.query(Delivery).filter_by(user_id=user.id).count() == 1


def test_newly_matching_offer_is_delivered(db, user, offer):
    user.preference.program_types = '["off-cycle"]'
    user.preference.start_terms = '["2027 Q1 Start"]'
    offer.programme_type = 'off-cycle'; offer.start_term = '2027 Q2 Start'; db.commit()
    with patch('trackr_app.scraper.TRACKERS', [{'region': 'France', 'type': 'off-cycle-internships'}]), patch('trackr_app.scraper.scrape_open_programmes', return_value=[{'name': offer.name, 'offer_url': offer.offer_url, 'categories': ['2027 Q1 Start']}]):
        scrape_all(db)
    assert db.query(Delivery).count() == 1


def test_restored_preferences_resume_unsent_offer(db, user, offer):
    queue_new_offer(db, offer); db.commit()
    user.preference.regions = '["UK"]'; activate_preference(db, user.preference)
    user.preference.regions = '["France"]'; activate_preference(db, user.preference)
    assert db.query(Delivery).one().status == 'pending'


def test_closed_offer_is_identified_or_hidden(client, db, user, offer):
    offer.is_open = False
    db.add(UserOffer(user_id=user.id, offer_id=offer.id)); db.commit()
    response = client.get('/dashboard')
    assert offer.offer_url not in response.text or 'Closed' in response.text


def test_expired_offer_is_not_open():
    response = Mock()
    response.json.return_value = [{'name': 'Expired role', 'url': 'https://example.com/expired', 'openingDate': '2020-01-01', 'closingDate': '2020-02-01'}]
    with patch('trackr_common.requests.get', return_value=response):
        assert scrape_open_programmes({}) == []


def test_unsafe_url_is_rejected():
    assert not canonical_offer_url('javascript:alert(1)')

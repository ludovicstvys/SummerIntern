"""User-facing browsing regressions with isolated data and no external services."""
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from trackr_app.config import settings
from trackr_app.database import Base, get_db
from trackr_app.main import app
from trackr_app.models import Offer, OfferSource, Preference, User, UserOffer, UserSession, utcnow
from trackr_app.security import token_hash


@pytest.fixture
def feed():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(email='reader@example.com')
        db.add(user)
        db.flush()
        pref = Preference(user_id=user.id, status='active', program_types=json.dumps(['summer', 'off-cycle']),
                          regions=json.dumps(['France', 'UK']), start_terms='[]')
        db.add(pref)
        db.add(UserSession(user_id=user.id, token_hash=token_hash('browser-test'), csrf_token='test-csrf', expires_at=utcnow()+timedelta(hours=1)))
        db.commit()

        def override():
            yield db
        app.dependency_overrides[get_db] = override
        client = TestClient(app)
        client.cookies.set('trackr_session', 'browser-test')

        def add(name='Analyst', *, company='Example Bank', sources=None, owner=None, **values):
            offer = Offer(canonical_url=f'https://example.com/{name}/{db.query(Offer).count()}',
                          offer_url='https://example.com/apply', name=name, company=company,
                          region='France', programme_type='summer', **values)
            db.add(offer)
            db.flush()
            if sources:
                for fields in sources:
                    db.add(OfferSource(offer_id=offer.id, season=settings.season, **fields))
            db.add(UserOffer(user_id=owner or user.id, offer_id=offer.id))
            db.commit()
            db.expire(offer, ['sources'])
            return offer
        try:
            yield client, db, pref, add
        finally:
            app.dependency_overrides.clear()
            client.close()
    engine.dispose()


def test_search_pagination_and_preferences_are_independent(feed):
    client, db, pref, add = feed
    for i in range(23):
        add(f'Analyst {i:02}', company='A & B')
    add('Unrelated', company='Elsewhere')
    original = (pref.program_types, pref.regions, pref.start_terms)
    response = client.get('/dashboard', params={'q': 'a & b', 'region': 'France', 'sort': 'latest'})
    assert response.status_code == 200
    assert len(response.context['cards']) == 20
    assert response.context['total'] == 23
    assert response.context['cards'][0]['offer'].name == 'Analyst 22'
    link = response.context['feed_url'](page=2)
    assert 'q=a+%26+b' in link and 'region=France' in link
    second = client.get(link)
    assert len(second.context['cards']) == 3
    assert 'Showing 21–23' in second.text
    assert 'name="page"' not in second.text
    assert client.get('/dashboard?page=999&q=a+%26+b').context['page'] == 2
    db.refresh(pref)
    assert original == (pref.program_types, pref.regions, pref.start_terms)


def test_multisource_filters_must_match_same_source_and_deadline(feed):
    client, db, pref, add = feed
    today = utcnow().date()
    add(sources=[dict(region='France', programme_type='summer', closing_date=today+timedelta(days=2)),
                 dict(region='UK', programme_type='off-cycle', start_term='Q1 Start', closing_date=today+timedelta(days=25))])
    assert client.get('/dashboard?region=France&programme=off-cycle').context['total'] == 0
    response = client.get('/dashboard?region=UK&programme=off-cycle&start_term=Q1+Start')
    assert response.context['total'] == 1
    assert response.context['cards'][0]['deadline'] == today+timedelta(days=25)
    assert 'Closing soon</span>' not in response.text
    pref.regions = '["France"]'
    db.commit()
    assert client.get('/dashboard?region=UK').context['total'] == 0


def test_closing_sort_unknown_dates_rolling_and_ties(feed):
    client, db, pref, add = feed
    today = utcnow().date()
    add('Later', closing_date=today+timedelta(days=12))
    add('Tie older', closing_date=today+timedelta(days=2))
    add('Tie newer', closing_date=today+timedelta(days=2))
    add('Rolling', rolling=True)
    add('Unknown')
    response = client.get('/dashboard?sort=closing')
    assert [c['offer'].name for c in response.context['cards']] == ['Tie newer', 'Tie older', 'Later', 'Unknown', 'Rolling']
    assert 'Rolling recruitment' in response.text
    assert 'Closing soon</span>' in response.text
    assert 'Not specified' in response.text
    assert client.get('/dashboard?sort=invalid').context['sort'] == 'latest'


def test_history_and_user_isolation(feed):
    client, db, pref, add = feed
    add('Current')
    add('Closed role', closing_date=utcnow().date()-timedelta(days=1), rolling=True)
    outsider = User(email='other@example.com')
    db.add(outsider); db.commit()
    add('Private match', owner=outsider.id)
    current = client.get('/dashboard')
    assert current.context['total'] == 1
    history = client.get('/dashboard?history=true')
    assert history.context['total'] == 2
    assert 'Closed</span>' in history.text
    assert 'Private match' not in history.text
    assert 'outside your current preferences' in history.text


def test_escaped_details_and_explicit_documents_only(feed):
    client, db, pref, add = feed
    add(name='Long title ' * 30, notes='<script>alert(1)</script>', company_description='About\nour company', needs_cv=True)
    response = client.get('/dashboard')
    assert '<details class="opportunity-details">' in response.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text
    assert '<script>' not in response.text
    assert 'Documents requested:</strong> CV' in response.text
    assert 'Not required' not in response.text
    assert 'cover letter' not in response.text


def test_empty_states_and_disabled_alert_copy(feed, monkeypatch):
    client, db, pref, add = feed
    monkeypatch.setattr('trackr_app.main.settings', replace(settings, notion_enabled=False))
    assert 'No current matches' in client.get('/dashboard').text
    assert 'No previous matches yet' in client.get('/dashboard?history=true').text
    assert 'No opportunities match these filters' in client.get('/dashboard?q=nothing').text
    pref.status = 'draft'; db.commit()
    response = client.get('/dashboard')
    assert 'Set up your opportunity feed' in response.text
    assert 'Your email alerts remain active' not in response.text
    pref.status = 'disabled'; db.commit()
    assert 'Alerts disabled' in client.get('/dashboard').text


def test_preview_uses_cards_and_activation_still_works(feed):
    client, db, pref, add = feed
    add('Preview role', needs_cover_letter=True)
    data = dict(program_types='summer', regions='France', delivery_mode='immediate',
                timezone='Europe/Paris', digest_time='08:00', csrf_token='test-csrf')
    preview = client.post('/preferences/preview', data=data)
    assert preview.status_code == 200
    assert 'opportunity-card' in preview.text
    assert 'Show details' in preview.text
    assert 'cover letter' in preview.text
    assert 'Activate alert' in preview.text
    activated = client.post('/preferences/activate', data=data, follow_redirects=False)
    assert activated.status_code == 303
    assert activated.headers['location'].startswith('/dashboard?activated=')


def test_deadline_boundaries_and_future_opening(feed):
    client, db, pref, add = feed
    today = utcnow().date()
    for days in (0, 7, 8):
        add(f'Deadline {days}', closing_date=today+timedelta(days=days))
    add('Future opening', opening_date=today+timedelta(days=1))
    response = client.get('/dashboard?sort=closing')
    assert [(c['offer'].name, c['soon']) for c in response.context['cards']] == [
        ('Deadline 0', True), ('Deadline 7', True), ('Deadline 8', False)]


def test_history_filtered_to_closed_source_does_not_claim_open(feed):
    client, db, pref, add = feed
    add(sources=[dict(region='France', programme_type='summer', is_open=False),
                 dict(region='UK', programme_type='summer', is_open=True)])
    response = client.get('/dashboard?history=true&region=France')
    assert response.context['total'] == 1
    assert not response.context['cards'][0]['open']
    assert 'Closed</span>' in response.text

"""Regression checks for C01–C08 of the login audit."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from tests.auth_helpers import auth_form, consume
from tests.test_password_auth import auth_env, PASSWORD, login
from trackr_app.auth import FORM_COOKIE, return_signer, RETURN_COOKIE
from trackr_app.auth_mail import enqueue, process_auth_mail, process_auth_queue
from trackr_app.invitations import deliver_invitation
from trackr_app.models import AuthMail, Invitation, MagicLink, PasswordToken, User, UserSession, utcnow
from trackr_app.security import token_hash
from trackr_app.sessions import revoke_user_auth


def test_get_does_not_consume_or_switch_account(auth_env):
    client, factory, user_id = auth_env
    login(client)
    original = client.cookies['trackr_session']
    with factory() as db:
        other = User(email='other@example.com')
        db.add(other); db.flush()
        other_id = other.id
        db.add(MagicLink(user_id=other.id, token_hash=token_hash('other-link'),
                         expires_at=utcnow() + timedelta(minutes=15)))
        db.commit()
    for _ in range(2):
        response = client.get('/auth/consume/other-link', follow_redirects=False)
        assert response.status_code == 200
        assert 'other@example.com' in response.text and 'switch the account' in response.text
        assert client.cookies['trackr_session'] == original
    assert client.post('/auth/consume/other-link').status_code == 403
    response = consume(client, '/auth/consume/other-link', follow_redirects=False)
    assert response.status_code == 303
    with factory() as db:
        session = db.scalar(select(UserSession).where(
            UserSession.token_hash == token_hash(client.cookies['trackr_session'])))
        assert session.user_id == other_id != user_id
    assert consume(client, '/auth/consume/other-link', follow_redirects=False).headers['location'].startswith('/login?')


def test_successful_logins_do_not_exhaust_password_budget(auth_env):
    client, _, _ = auth_env
    with patch('trackr_app.limits.utcnow', return_value=utcnow()):
        for _ in range(12):
            assert login(client).headers['location'] == '/dashboard'


def test_attacker_cannot_exhaust_other_ip_password_budget(auth_env):
    client, _, _ = auth_env
    with patch('trackr_app.limits.utcnow', return_value=utcnow()):
        data = auth_form(client, email='member@example.com', password='wrong')
        with patch('trackr_app.auth.client_ip', return_value='192.0.2.1'):
            for _ in range(10):
                client.post('/auth/login', data=data, follow_redirects=False)
            denied = login(client)
            assert denied.headers['Retry-After'] == '900'
            assert 'Too%20many' in denied.headers['location']
        with patch('trackr_app.auth.client_ip', return_value='192.0.2.2'):
            assert login(client).headers['location'] == '/dashboard'


def test_recent_form_gets_full_lifetime_and_other_tab_stays_valid(auth_env):
    client, _, _ = auth_env
    with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=100000):
        old = auth_form(client, email='member@example.com', password=PASSWORD)
    with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=103590):
        fresh = auth_form(client, email='member@example.com', password=PASSWORD)
        assert client.post('/auth/login', data=old, follow_redirects=False).status_code == 303
    with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=103601):
        assert client.post('/auth/login', data=fresh, follow_redirects=False).headers['location'] == '/dashboard'
        response = client.post('/auth/login', data=old)
        assert response.status_code == 403 and 'text/html' in response.headers['content-type']
        assert 'Return to the form' in response.text and PASSWORD not in response.text


def test_csrf_form_is_bound_to_browser(auth_env):
    client, _, _ = auth_env
    data = auth_form(client, email='member@example.com', password=PASSWORD)
    client.cookies.clear()
    auth_form(client)
    assert client.post('/auth/login', data=data).status_code == 403


@pytest.mark.parametrize('path,kind', [('/auth/password/request', 'password'), ('/auth/request', 'magic')])
def test_request_queues_known_and_unknown_without_smtp_or_user_lookup(auth_env, path, kind):
    client, factory, _ = auth_env
    with patch('trackr_app.auth_mail.send_password_link') as reset, patch('trackr_app.auth_mail.send_magic_link') as magic:
        known = client.post(path, data=auth_form(client, email='member@example.com'), follow_redirects=False)
        unknown = client.post(path, data=auth_form(client, email='absent@example.com'), follow_redirects=False)
        assert known.headers['location'] == unknown.headers['location']
        reset.assert_not_called(); magic.assert_not_called()
    with factory() as db:
        assert db.query(AuthMail).filter_by(status='pending', kind=kind).count() == 2
        assert db.query(PasswordToken).count() == db.query(MagicLink).count() == 0


@pytest.mark.parametrize('commit_number', [1, 2])
def test_invitation_commit_failure_never_sends_unpersisted_link(auth_env, commit_number):
    _, factory, user_id = auth_env
    sent = []
    with factory() as db:
        invitation = Invitation(email='member@example.com', invited_by_id=user_id)
        db.add(invitation); db.commit()
        real_commit = db.commit
        calls = 0
        def commit():
            nonlocal calls
            calls += 1
            if calls == commit_number:
                raise RuntimeError('simulated database failure')
            real_commit()
        with patch.object(db, 'commit', side_effect=commit):
            with pytest.raises(RuntimeError):
                deliver_invitation(db, invitation.id, sender=lambda email, url: sent.append(url))
        db.rollback()
    assert not sent


def test_queue_retries_with_fresh_persisted_token_and_retains_uncertain_one(auth_env):
    _, factory, _ = auth_env
    sent = []
    def sender(email, url):
        with factory() as verification:
            token = verification.scalar(select(PasswordToken).where(PasswordToken.token_hash == token_hash(url.rsplit('/', 1)[-1])))
            assert token is not None
        sent.append(url)
        if len(sent) == 1:
            raise RuntimeError('secret-url-must-not-be-logged')
    with factory() as db:
        job = enqueue(db, 'member@example.com', 'password'); db.commit()
        assert not process_auth_mail(db, job.id, sender=sender)
        assert job.status == 'pending' and job.last_error == 'RuntimeError'
        assert not process_auth_mail(db, job.id, sender=sender)
        job.next_attempt_at = utcnow() - timedelta(seconds=1); db.commit()
        assert process_auth_mail(db, job.id, sender=sender)
        assert len(sent) == 2 and sent[0] != sent[1]
        assert db.query(PasswordToken).count() == 2


def test_worker_recovers_crashed_delivery_lease(auth_env):
    _, factory, _ = auth_env
    with factory() as db:
        job = enqueue(db, 'member@example.com', 'password')
        job.status = 'processing'; job.attempts = 1
        job.processing_started_at = utcnow() - timedelta(minutes=6)
        db.commit()
        with patch('trackr_app.auth_mail.send_password_link') as sender:
            assert process_auth_queue(db) == 1
        assert sender.call_count == 1
        db.refresh(job)
        assert job.status == 'sent'


def test_revoke_cancels_unsent_reset_requests(auth_env):
    _, factory, user_id = auth_env
    with factory() as db:
        job = enqueue(db, 'member@example.com', 'password'); db.commit()
        revoke_user_auth(db, user_id); db.commit()
        with patch('trackr_app.auth_mail.send_password_link') as sender:
            assert not process_auth_mail(db, job.id)
        sender.assert_not_called()
        db.refresh(job)
        assert job.status == 'cancelled'


def test_recovery_and_magic_link_report_shared_mail_quota(auth_env):
    client, _, _ = auth_env
    data = auth_form(client, email='member@example.com')
    with patch('trackr_app.limits.utcnow', return_value=utcnow()), patch('trackr_app.auth_mail.send_magic_link') as magic, patch('trackr_app.auth_mail.send_password_link') as reset:
        client.post('/auth/request', data=data, follow_redirects=False)
        response = client.post('/auth/password/request', data=data, follow_redirects=False)
    magic.assert_not_called(); reset.assert_not_called()
    assert 'Too%20many%20requests' in response.headers['location']
    assert response.headers['Retry-After'] == '900'


def test_logout_after_revocation_clears_cookie_and_redirects(auth_env):
    client, factory, _ = auth_env
    login(client)
    with factory() as db:
        session = db.query(UserSession).one()
        csrf = session.csrf_token
        db.delete(session); db.commit()
    for _ in range(2):
        response = client.post('/logout', data={'csrf_token': csrf}, follow_redirects=False)
        assert response.status_code == 303 and response.headers['location'] == '/login'
        assert 'Max-Age=0' in response.headers['set-cookie']


def test_login_restores_original_destination(auth_env):
    client, _, _ = auth_env
    assert client.get('/preferences', follow_redirects=False).headers['location'] == '/login'
    assert login(client).headers['location'] == '/preferences'
    assert RETURN_COOKIE not in client.cookies


@pytest.mark.parametrize('destination', ['https://evil.example', '//evil.example', '/\\evil.example', '/notion/callback?code=old', '/logout'])
def test_return_destination_rejects_external_and_unsafe_paths(auth_env, destination):
    client, _, _ = auth_env
    client.cookies.set(RETURN_COOKIE, return_signer.dumps(destination))
    assert login(client).headers['location'] == '/dashboard'


def test_password_request_only_queues_before_http_response_body(auth_env):
    import anyio
    from urllib.parse import urlencode
    from trackr_app.main import app
    client, _, _ = auth_env
    data = auth_form(client, email='member@example.com')
    body = urlencode(data).encode()
    events = []
    received = False
    async def receive():
        nonlocal received
        if not received:
            received = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        await anyio.sleep_forever()
    async def send(message):
        events.append(message)
    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}, 'http_version': '1.1',
             'method': 'POST', 'scheme': 'http', 'path': '/auth/password/request',
             'raw_path': b'/auth/password/request', 'query_string': b'', 'root_path': '',
             'server': ('testserver', 80), 'client': ('192.0.2.8', 1234),
             'headers': [(b'content-type', b'application/x-www-form-urlencoded'),
                         (b'cookie', (FORM_COOKIE + '=' + client.cookies[FORM_COOKIE]).encode())]}
    async def run():
        with anyio.fail_after(5):
            await app(scope, receive, send)
    with patch('trackr_app.auth_mail.send_password_link') as smtp:
        anyio.run(run)
    assert any(event['type'] == 'http.response.body' and not event.get('more_body') for event in events)
    smtp.assert_not_called()
    with auth_env[1]() as db:
        assert db.query(AuthMail).one().status == 'pending'


def test_mail_retry_budget_is_bounded_and_unknown_accounts_never_receive_mail(auth_env):
    _, factory, user_id = auth_env
    with factory() as db:
        failed = enqueue(db, 'member@example.com', 'password')
        unknown = enqueue(db, 'missing@example.com', 'magic')
        old = enqueue(db, 'member@example.com', 'password')
        old.created_at = utcnow() - timedelta(hours=2)
        db.commit()
        with patch('trackr_app.auth_mail.send_magic_link') as magic, patch('trackr_app.auth_mail.send_password_link', side_effect=RuntimeError('smtp-secret')) as sender:
            assert not process_auth_mail(db, unknown.id)
            assert not process_auth_mail(db, old.id)
            magic.assert_not_called()
            for _ in range(5):
                failed.next_attempt_at = None; db.commit()
                assert not process_auth_mail(db, failed.id)
            assert failed.status == 'failed' and failed.attempts == 5
            assert not process_auth_mail(db, failed.id)
            assert sender.call_count == 5


def test_first_password_skip_preserves_destination(auth_env):
    client, factory, user_id = auth_env
    with factory() as db:
        db.get(User, user_id).password_hash = None
        db.add(MagicLink(user_id=user_id, token_hash=token_hash('first-password'), expires_at=utcnow()+timedelta(minutes=15)))
        db.commit()
    client.get('/preferences', follow_redirects=False)
    assert consume(client, '/auth/consume/first-password', follow_redirects=False).headers['location'] == '/auth/password'
    assert client.get('/auth/continue', follow_redirects=False).headers['location'] == '/preferences'


def test_monitoring_reports_delayed_and_failed_auth_mail(auth_env):
    from trackr_app.monitoring import operational_status
    _, factory, _ = auth_env
    with factory() as db:
        delayed = enqueue(db, 'member@example.com', 'password')
        delayed.created_at = utcnow() - timedelta(minutes=31)
        failed = enqueue(db, 'member@example.com', 'magic'); failed.status = 'failed'
        db.commit()
        status = operational_status(db)
    assert status['status'] == 'degraded'
    assert status['delayed_auth_emails'] == 1 and status['failed_tasks'] == 1

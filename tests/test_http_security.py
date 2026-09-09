from unittest.mock import patch
import ssl
import pytest
from tests.test_password_auth import auth_env
from tests import test_emailing
from trackr_app.config import Settings


def test_oversized_form_rejected_before_authentication(auth_env):
    client, _, _ = auth_env
    for headers in ({}, {'content-length': '1'}):
        response = client.post('/auth/login', content=b'x=' + b'a' * 65536,
            headers={'content-type': 'application/x-www-form-urlencoded', **headers})
        assert response.status_code == 413


def test_security_headers_on_login(auth_env):
    response = auth_env[0].get('/login')
    assert response.headers['x-frame-options'] == 'DENY'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert "frame-ancestors 'none'" in response.headers['content-security-policy']
    assert 'href="/static/app.css"' in response.text


def test_preview_requires_secure_database_and_keys():
    with pytest.raises(RuntimeError):
        Settings(environment='preview', database_url='sqlite://', secret_key='development-only-change-me').validate()


def test_auth_queue_skips_corrupt_ciphertext(auth_env):
    from trackr_app.auth_mail import enqueue, process_auth_queue
    with auth_env[1]() as db:
        bad = enqueue(db, 'member@example.com', 'password')
        bad.email_encrypted = 'broken'
        good = enqueue(db, 'member@example.com', 'password')
        db.commit()
        with patch('trackr_app.auth_mail.send_password_link') as send:
            assert process_auth_queue(db) == 1
        db.refresh(bad); db.refresh(good)
        assert bad.status == 'failed' and good.status == 'sent'
        assert send.call_count == 1


def test_smtp_context_validates_certificate():
    from unittest.mock import MagicMock
    from trackr_app.emailing import send_email
    fixture = test_emailing.SmtpTests(); fixture.setUp()
    with patch('trackr_app.emailing.settings', fixture.settings), patch('trackr_app.emailing.smtplib.SMTP') as smtp:
        smtp.return_value.__enter__.return_value.send_message.return_value = {}
        send_email('recipient@example.com', 'test', 'test', 'test')
        context = smtp.return_value.__enter__.return_value.starttls.call_args.kwargs['context']
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_production_origin_requires_explicit_alias():
    from types import SimpleNamespace
    from starlette.requests import Request
    from trackr_app.auth import valid_form_origin
    request = Request({'type': 'http', 'scheme': 'https', 'path': '/', 'headers': [(b'host', b'alias.example')], 'query_string': b''})
    settings = SimpleNamespace(app_url='https://canonical.example', environment='production', auth_allowed_origins='')
    with patch('trackr_app.auth.settings', settings):
        assert not valid_form_origin(request, 'https://alias.example')
        settings.auth_allowed_origins = 'https://alias.example'
        assert valid_form_origin(request, 'https://alias.example')
        assert not valid_form_origin(request, 'https://foreign.example')


def test_dashboard_hydrates_only_twenty_offers(auth_env):
    from sqlalchemy import event, insert
    from trackr_app.models import User, Offer, OfferSource, UserOffer, Preference
    from trackr_app.opportunities import browse_database
    from trackr_app.config import settings
    _, factory, user_id = auth_env
    with factory() as db:
        db.execute(insert(User), [{'email': f'load{i}@example.com'} for i in range(19)])
        db.execute(insert(Offer), [{'id': i+1, 'canonical_url': f'https://example.com/{i}',
            'offer_url': f'https://example.com/{i}', 'name': f'Offer {i}', 'region': 'France', 'programme_type': 'summer'} for i in range(10000)])
        db.execute(insert(OfferSource), [{'offer_id': i+1, 'region': 'France', 'programme_type': 'summer', 'season': settings.season} for i in range(10000)])
        db.execute(insert(UserOffer), [{'user_id': user_id, 'offer_id': i+1} for i in range(10000)])
        pref = Preference(user_id=user_id); db.add(pref); db.commit()
        count = []
        def loaded(target, context):
            count.append(target.id)
        event.listen(Offer, 'load', loaded)
        try:
            result = browse_database(db, user_id, pref, page=2)
        finally:
            event.remove(Offer, 'load', loaded)
        assert result['total'] == 10000 and len(result['cards']) == len(count) == 20


def test_artifact_checker_rejects_private_files(tmp_path):
    from scripts.check_artifact import check
    (tmp_path / 'app.py').write_text('')
    check(tmp_path)
    (tmp_path / '.env.production').write_text('')
    with pytest.raises(ValueError):
        check(tmp_path)


def test_chunked_form_without_content_length_is_rejected_before_app():
    import asyncio
    from trackr_app.http_security import HttpSecurity
    calls, responses = [], []
    async def app(scope, receive, send):
        calls.append(True)
    chunks = iter([
        {'type': 'http.request', 'body': b'x' * 32768, 'more_body': True},
        {'type': 'http.request', 'body': b'x' * 32769, 'more_body': False},
    ])
    async def receive():
        return next(chunks)
    async def send(message):
        responses.append(message)
    asyncio.run(HttpSecurity(app)({'type': 'http', 'headers': [
        (b'content-type', b'application/x-www-form-urlencoded')]}, receive, send))
    assert not calls
    assert responses[0]['status'] == 413


@pytest.mark.parametrize('failure', [ssl.SSLCertVerificationError('invalid certificate'),
    __import__('smtplib').SMTPNotSupportedError('STARTTLS unavailable')])
def test_smtp_tls_failure_prevents_login_and_delivery(failure):
    from trackr_app.emailing import send_email
    fixture = test_emailing.SmtpTests(); fixture.setUp()
    with patch('trackr_app.emailing.settings', fixture.settings), patch('trackr_app.emailing.smtplib.SMTP') as smtp:
        client = smtp.return_value.__enter__.return_value
        client.starttls.side_effect = failure
        with pytest.raises(type(failure)):
            send_email('recipient@example.com', 'test', 'test', 'test')
        client.login.assert_not_called()
        client.send_message.assert_not_called()

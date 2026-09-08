from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.auth_helpers import auth_form, consume
from trackr_app.auth import passwords
from trackr_app.database import Base, get_db
from trackr_app.main import app
from trackr_app.models import Invitation, MagicLink, PasswordToken, User, UserSession, utcnow
from trackr_app.security import token_hash
from trackr_app.sessions import aware

PASSWORD = 'a memorable password phrase'


@pytest.fixture
def auth_env():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(email='member@example.com', password_hash=passwords.hash(PASSWORD))
        db.add(user); db.commit()
        user_id = user.id
    def dependency():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = dependency
    client = TestClient(app)
    try:
        yield client, factory, user_id
    finally:
        client.close()
        app.dependency_overrides.clear()
        engine.dispose()


def login(client, **data):
    return client.post('/auth/login', data=auth_form(client, email='member@example.com', password=PASSWORD, **data), follow_redirects=False)


def issue_token(factory, user_id, raw='reset-secret', expired=False):
    with factory() as db:
        db.add(PasswordToken(user_id=user_id, token_hash=token_hash(raw), expires_at=utcnow() + timedelta(minutes=-1 if expired else 15)))
        db.commit()
    return '/auth/password/reset/' + raw


def test_password_login_and_persistent_cookie(auth_env):
    client, factory, _ = auth_env
    response = client.post('/auth/login', data=auth_form(client, email=' MEMBER@example.com ', password=PASSWORD), follow_redirects=False)
    assert response.headers['location'] == '/dashboard'
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=lax' in cookie and 'expires=' in cookie
    assert 'Max-Age=7775999' in cookie or 'Max-Age=7776000' in cookie
    assert response.headers['cache-control'] == 'no-store'
    with factory() as db:
        session = db.query(UserSession).one()
        assert session.token_hash == token_hash(client.cookies['trackr_session'])
        assert aware(session.expires_at) - aware(session.created_at) == timedelta(days=90)
    assert client.get('/login', follow_redirects=False).headers['location'] == '/dashboard'
    assert client.get('/dashboard').status_code == 200


@pytest.mark.parametrize('kind', ['unknown', 'wrong', 'disabled', 'no-password', 'too-long'])
def test_login_failures_are_generic(auth_env, kind):
    client, factory, user_id = auth_env
    with factory() as db:
        user = db.get(User, user_id)
        if kind == 'disabled': user.is_active = False
        if kind == 'no-password': user.password_hash = None
        db.commit()
    response = client.post('/auth/login', data=auth_form(client,
        email='missing@example.com' if kind == 'unknown' else 'member@example.com',
        password='wrong' if kind == 'wrong' else 'x' * 129 if kind == 'too-long' else PASSWORD), follow_redirects=False)
    assert response.headers['location'] == '/login?message=Incorrect%20email%20or%20password.'
    assert 'trackr_session' not in response.cookies
    with factory() as db:
        assert db.query(UserSession).count() == 0


@pytest.mark.parametrize('path', ['/auth/login', '/auth/request', '/auth/password/request', '/auth/password', '/auth/password/reset/anything'])
def test_auth_posts_require_csrf(auth_env, path):
    client, _, _ = auth_env
    data = dict(email='member@example.com', password=PASSWORD, confirmation=PASSWORD)
    assert client.post(path, data=data).status_code == 403
    data = auth_form(client, **data)
    assert client.post(path, data=data, headers={'origin': 'https://attacker.example'}).status_code == 403
    data['form_token'] = 'forged'
    assert client.post(path, data=data).status_code == 403


def test_expired_form_token_rejected(auth_env):
    client, _, _ = auth_env
    with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=1):
        data = auth_form(client, email='member@example.com', password=PASSWORD)
    assert client.post('/auth/login', data=data).status_code == 403


def test_login_limits_separate_from_email_limits(auth_env):
    client, factory, _ = auth_env
    data = auth_form(client, email='member@example.com', password='wrong')
    with patch('trackr_app.limits.utcnow', return_value=utcnow()):
        for _ in range(10):
            client.post('/auth/login', data=data, follow_redirects=False)
        data['password'] = PASSWORD
        assert client.post('/auth/login', data=data, follow_redirects=False).headers['location'].startswith('/login?')
        with patch('trackr_app.auth_mail.send_password_link') as sender:
            client.post('/auth/password/request', data=data, follow_redirects=False)
            assert sender.call_count == 1
    with factory() as db:
        assert db.query(UserSession).count() == 0


def test_password_email_and_generic_response(auth_env):
    client, factory, user_id = auth_env
    with patch('trackr_app.auth_mail.send_password_link') as sender:
        known = client.post('/auth/password/request', data=auth_form(client, email='member@example.com'), follow_redirects=False)
        unknown = client.post('/auth/password/request', data=auth_form(client, email='unknown@example.com'), follow_redirects=False)
        assert known.headers['location'] == unknown.headers['location']
        assert sender.call_count == 1
        url = sender.call_args.args[1]
    with factory() as db:
        token = db.query(PasswordToken).one()
        assert token.token_hash == token_hash(url.rsplit('/', 1)[-1])
        assert aware(token.expires_at) > utcnow() + timedelta(minutes=14)
        db.get(User, user_id).is_active = False; db.commit()
    with patch('trackr_app.auth_mail.send_password_link') as sender:
        client.post('/auth/password/request', data=auth_form(client, email='member@example.com'))
        sender.assert_not_called()


def test_mail_failure_is_generic_and_never_logs_token(auth_env, capsys):
    client, _, _ = auth_env
    with patch('trackr_app.auth_mail.send_password_link', side_effect=RuntimeError('secret-token')):
        response = client.post('/auth/password/request', data=auth_form(client, email='member@example.com'), follow_redirects=False)
    assert response.status_code == 303
    output = capsys.readouterr().out
    assert 'secret-token' not in output
    with auth_env[1]() as db:
        from trackr_app.models import AuthMail
        job = db.query(AuthMail).one()
        assert job.status == 'pending' and job.last_error == 'RuntimeError'


def test_reset_is_single_use_and_revokes_all_auth(auth_env):
    client, factory, user_id = auth_env
    login(client)
    old_cookie = client.cookies['trackr_session']
    path = issue_token(factory, user_id)
    issue_token(factory, user_id, 'other-reset')
    with factory() as db:
        db.add(MagicLink(user_id=user_id, token_hash=token_hash('magic'), expires_at=utcnow()+timedelta(minutes=15)))
        db.commit()
    assert client.get(path).status_code == 200
    assert client.get(path).status_code == 200
    with factory() as db:
        assert db.scalar(select(PasswordToken).where(PasswordToken.token_hash == token_hash('reset-secret'))).used_at is None
    replacement = 'a different memorable phrase'
    data = auth_form(client, password=replacement, confirmation=replacement)
    response = client.post(path, data=data, follow_redirects=False)
    assert response.headers['location'] == '/dashboard'
    assert client.cookies['trackr_session'] != old_cookie
    with factory() as db:
        assert passwords.verify(replacement, db.get(User, user_id).password_hash)
        assert db.query(PasswordToken).count() == db.query(MagicLink).count() == 0
        assert db.query(UserSession).count() == 1
    assert client.post(path, data=data, follow_redirects=False).headers['location'].startswith('/auth/password/request?')
    client.cookies.clear()
    client.cookies.set('trackr_session', old_cookie)
    assert client.get('/dashboard', follow_redirects=False).headers['location'] == '/login'


@pytest.mark.parametrize('kind', ['expired', 'disabled', 'unknown', 'used'])
def test_invalid_reset_links_rejected(auth_env, kind):
    client, factory, user_id = auth_env
    path = issue_token(factory, user_id, expired=kind == 'expired')
    with factory() as db:
        if kind == 'disabled': db.get(User, user_id).is_active = False
        if kind == 'used': db.query(PasswordToken).one().used_at = utcnow()
        db.commit()
    if kind == 'unknown': path += '-unknown'
    assert client.get(path, follow_redirects=False).headers['location'].startswith('/auth/password/request?')
    response = client.post(path, data=auth_form(client, password=PASSWORD, confirmation=PASSWORD), follow_redirects=False)
    assert response.headers['location'].startswith('/auth/password/request?')


@pytest.mark.parametrize('password,confirmation', [('x'*11, 'x'*11), ('x'*129, 'x'*129), (PASSWORD, 'mismatch')])
def test_password_validation_preserves_token(auth_env, password, confirmation):
    client, factory, user_id = auth_env
    path = issue_token(factory, user_id)
    response = client.post(path, data=auth_form(client, password=password, confirmation=confirmation))
    assert response.status_code == 200 and 'role="alert"' in response.text
    with factory() as db:
        assert db.query(PasswordToken).one().used_at is None
        assert passwords.verify(PASSWORD, db.get(User, user_id).password_hash)


@pytest.mark.parametrize('length', [12, 128])
def test_password_length_boundaries(auth_env, length):
    client, factory, user_id = auth_env
    path = issue_token(factory, user_id)
    password = 'é' * length
    response = client.post(path, data=auth_form(client, password=password, confirmation=password), follow_redirects=False)
    assert response.headers['location'] == '/dashboard'
    with factory() as db:
        assert passwords.verify(password, db.get(User, user_id).password_hash)


def test_existing_invite_prompts_password_and_preserves_access(auth_env):
    client, factory, user_id = auth_env
    with factory() as db:
        db.get(User, user_id).password_hash = None
        db.add(Invitation(email='member@example.com', invited_by_id=user_id))
        db.add(MagicLink(user_id=user_id, token_hash=token_hash('invite'), expires_at=utcnow()+timedelta(minutes=15)))
        db.commit()
    response = consume(client, '/auth/consume/invite', follow_redirects=False)
    assert response.headers['location'] == '/auth/password'
    assert 'Continue without setting a password' in client.get('/auth/password').text
    assert 'Set your password' in client.get('/dashboard').text
    response = client.post('/auth/password', data=auth_form(client, password=PASSWORD, confirmation=PASSWORD), follow_redirects=False)
    assert response.headers['location'] == '/dashboard'
    with factory() as db:
        assert db.query(Invitation).one().accepted_at is not None
        assert passwords.verify(PASSWORD, db.get(User, user_id).password_hash)
    response = client.post('/auth/password', data=auth_form(client, password=PASSWORD, confirmation=PASSWORD), follow_redirects=False)
    assert response.headers['location'] == '/auth/password/request'


def seed_session(factory, user_id, now, age=10, idle=20, renewed=None):
    with factory() as db:
        db.add(UserSession(user_id=user_id, token_hash=token_hash('persistent'), csrf_token='csrf',
            created_at=now-timedelta(days=age), renewed_at=renewed, expires_at=now+timedelta(days=idle)))
        db.commit()


def test_sliding_session_daily_renewal_and_cookie(auth_env):
    client, factory, user_id = auth_env
    now = utcnow()
    seed_session(factory, user_id, now)
    client.cookies.set('trackr_session', 'persistent')
    with patch('trackr_app.sessions.utcnow', return_value=now):
        response = client.get('/', follow_redirects=False)
        assert 'Max-Age=7776000' in response.headers['set-cookie']
        with factory() as db:
            session = db.query(UserSession).one()
            assert aware(session.expires_at) == now+timedelta(days=90)
            assert aware(session.renewed_at) == now
        assert 'set-cookie' not in client.get('/', follow_redirects=False).headers


@pytest.mark.parametrize('age,idle,disabled', [(10, 0, False), (365, 20, False), (10, 20, True)])
def test_invalid_sessions_never_renew(auth_env, age, idle, disabled):
    client, factory, user_id = auth_env
    now = utcnow()
    seed_session(factory, user_id, now, age=age, idle=idle)
    with factory() as db:
        db.get(User, user_id).is_active = not disabled; db.commit()
    client.cookies.set('trackr_session', 'persistent')
    with patch('trackr_app.sessions.utcnow', return_value=now):
        response = client.get('/dashboard', follow_redirects=False)
    assert response.headers['location'] == '/login'
    assert 'trackr_session' not in response.cookies
    with factory() as db:
        assert db.query(UserSession).one().renewed_at is None


def test_absolute_cap_and_revocation(auth_env):
    client, factory, user_id = auth_env
    now = utcnow()
    seed_session(factory, user_id, now, age=360)
    client.cookies.set('trackr_session', 'persistent')
    with patch('trackr_app.sessions.utcnow', return_value=now):
        response = client.get('/', follow_redirects=False)
        assert 'Max-Age=432000' in response.headers['set-cookie']
    with factory() as db:
        assert aware(db.query(UserSession).one().expires_at) == now+timedelta(days=5)
    assert client.post('/logout', data={'csrf_token': 'wrong'}).status_code == 403
    response = client.post('/logout', data={'csrf_token': 'csrf'}, follow_redirects=False)
    assert 'Max-Age=0' in response.headers['set-cookie']
    with factory() as db:
        assert db.query(UserSession).count() == 0
    client.cookies.clear(); client.cookies.set('trackr_session', 'persistent')
    assert client.get('/dashboard', follow_redirects=False).headers['location'] == '/login'


def test_secure_cookie_on_https(auth_env):
    client, _, _ = auth_env
    with patch('trackr_app.sessions.settings', SimpleNamespace(app_url='https://example.com')):
        assert 'Secure' in login(client).headers['set-cookie']


def test_sqlite_migration_preserves_existing_session(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'migration.db'))
    with engine.begin() as connection:
        config = Config('alembic.ini'); config.attributes['connection'] = connection
        command.upgrade(config, '20260907_0004')
        connection.execute(text("INSERT INTO users (id,email,role,is_active,created_at) VALUES (1,'old@example.com','subscriber',true,CURRENT_TIMESTAMP)"))
        connection.execute(text("INSERT INTO user_sessions (user_id,token_hash,csrf_token,expires_at,created_at) VALUES (1,'old-token','csrf','2099-01-01',CURRENT_TIMESTAMP)"))
        command.upgrade(config, 'head'); command.check(config)
        assert connection.scalar(text('SELECT password_hash FROM users')) is None
        assert connection.scalar(text('SELECT token_hash FROM user_sessions')) == 'old-token'
        command.downgrade(config, '20260907_0004')
        command.upgrade(config, 'head'); command.check(config)
    engine.dispose()


def test_renewal_cookie_matches_database_even_on_form_error(auth_env):
    client, factory, user_id = auth_env
    now = utcnow()
    seed_session(factory, user_id, now)
    client.cookies.set('trackr_session', 'persistent')
    with patch('trackr_app.sessions.utcnow', return_value=now):
        response = client.post('/preferences/activate', data={'csrf_token': 'invalid'})
    assert response.status_code in (403, 422)
    assert 'Max-Age=7776000' in response.headers['set-cookie']
    with factory() as db:
        assert aware(db.query(UserSession).one().expires_at) == now+timedelta(days=90)


def test_password_limit_applies_across_distinct_addresses(auth_env):
    client, _, _ = auth_env
    data = auth_form(client, password=PASSWORD)
    with patch('trackr_app.limits.utcnow', return_value=utcnow()), patch('trackr_app.auth.passwords.verify', return_value=False) as verify:
        for index in range(50):
            client.post('/auth/login', data={**data, 'email': f'missing{index}@example.com'}, follow_redirects=False)
        assert verify.call_count == 50
        client.post('/auth/login', data={**data, 'email': 'member@example.com'}, follow_redirects=False)
        assert verify.call_count == 50

"""Password sign-in and verified, one-use password setup/recovery."""
import hmac
import os
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pwdlib import PasswordHash
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .auth_mail import enqueue, deliver_in_background
from .limits import allow_login, reserve_password_login, refund_password_login
from .models import Invitation, PasswordToken, User, utcnow
from .security import new_token, token_hash
from .sessions import aware, create_session, current_user, revoke_user_auth, set_session_cookie, valid_session

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / 'templates')
passwords = PasswordHash.recommended()
DUMMY_HASH = passwords.hash(new_token())
form_signer = URLSafeTimedSerializer(settings.secret_key, salt='auth-form')
FORM_COOKIE = 'trackr_auth_csrf'
RECOVERY_MESSAGE = 'Request received. If the address is invited, we will email a link. Delivery may take a few minutes.'
LIMIT_MESSAGE = 'Too many requests. Please try again in 15 minutes.'
return_signer = URLSafeTimedSerializer(settings.secret_key, salt='auth-return')
RETURN_COOKIE = 'trackr_return'
RETURN_PATHS = {'/dashboard', '/preferences', '/admin', '/notion/setup', '/notion/connect'}
INVALID_LINK = 'This link is invalid or has expired. Please request a new one.'


def client_ip(request):
    ip = request.client.host if request.client else 'unknown'
    if os.getenv('VERCEL') == '1':
        try:
            ip = str(ip_address(request.headers.get('x-vercel-forwarded-for', ip).split(',')[0].strip()))
        except ValueError:
            pass
    return ip


class AuthFormError(HTTPException):
    pass


def check_form(request, value):
    cookie = request.cookies.get(FORM_COOKIE, '')
    origin = request.headers.get('origin')
    configured = urlsplit(settings.app_url)
    if origin and origin != f'{configured.scheme}://{configured.netloc}':
        raise AuthFormError(403, 'Invalid form origin. Please reopen the form.')
    try:
        # The cookie identifies the browser; each rendered form has its own hour.
        browser = form_signer.loads(cookie)
        submitted = form_signer.loads(value, max_age=3600)
        if not isinstance(browser, str) or not isinstance(submitted, str) or not hmac.compare_digest(browser, submitted):
            raise BadSignature('Browser mismatch')
    except (BadSignature, TypeError):
        raise AuthFormError(403, 'This form has expired or is invalid. Please reopen it and try again.')


def auth_page(request, template, **context):
    cookie = request.cookies.get(FORM_COOKIE, '')
    try:
        browser = form_signer.loads(cookie)
        if not isinstance(browser, str):
            raise BadSignature('Invalid browser')
    except BadSignature:
        browser = new_token()
        cookie = form_signer.dumps(browser)
    token = form_signer.dumps(browser)
    response = templates.TemplateResponse(request, template, {
        'request': request, 'user': None, 'form_token': token, **context})
    response.set_cookie(FORM_COOKIE, cookie, httponly=True, secure=settings.app_url.startswith('https'),
                        samesite='lax', max_age=3600, path='/')
    return response


def safe_destination(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value) or '\\' in value:
        return '/dashboard'
    parsed = urlsplit(value)
    return value if not parsed.scheme and not parsed.netloc and parsed.path in RETURN_PATHS else '/dashboard'


def login_redirect(request, default='/dashboard'):
    destination = default
    if default == '/dashboard':
        try:
            destination = safe_destination(return_signer.loads(request.cookies.get(RETURN_COOKIE, ''), max_age=3600))
        except BadSignature:
            pass
    response = RedirectResponse(destination, 303)
    if default == '/dashboard':
        response.delete_cookie(RETURN_COOKIE)
    return response


def login_required(request):
    response = RedirectResponse('/login', 303)
    if request.method == 'GET' and request.url.path in RETURN_PATHS:
        path = request.url.path + ('?' + request.url.query if request.url.query else '')
        response.set_cookie(RETURN_COOKIE, return_signer.dumps(safe_destination(path)),
            httponly=True, secure=settings.app_url.startswith('https'), samesite='lax', max_age=3600, path='/')
    return response


def limited_response(path):
    response = redirect_message(path, LIMIT_MESSAGE)
    response.headers['Retry-After'] = '900'
    return response


def redirect_message(path, message):
    return RedirectResponse(path + '?message=' + quote(message), 303)


def password_error(password, confirmation):
    if not 12 <= len(password) <= 128:
        return 'Use between 12 and 128 characters.'
    if password != confirmation:
        return 'The passwords do not match.'
    return None


def finish_password(db, user, password, request):
    user.password_hash = passwords.hash(password)
    revoke_user_auth(db, user.id)
    db.execute(update(Invitation).where(Invitation.email == user.email,
               Invitation.accepted_at.is_(None)).values(accepted_at=utcnow()))
    cookie = create_session(db, user.id)
    db.commit()
    response = login_redirect(request)
    set_session_cookie(response, *cookie)
    return response


@router.post('/auth/login')
def password_login(request: Request, email: str = Form(...), password: str = Form(...),
                   form_token: str = Form(''), db: Session = Depends(get_db)):
    check_form(request, form_token)
    email = email.strip().lower()
    failure = 'Incorrect email or password.'
    if len(email) > 320:
        return redirect_message('/login', failure)
    allowed, reservation = reserve_password_login(db, email, client_ip(request))
    if not allowed:
        return limited_response('/login')
    user = db.scalar(select(User).where(User.email == email).with_for_update())
    candidate = user.password_hash if user and user.is_active and user.password_hash else DUMMY_HASH
    verified = passwords.verify(password if len(password) <= 128 else '', candidate)
    if not verified or not user or not user.is_active or not user.password_hash or not 12 <= len(password) <= 128:
        return redirect_message('/login', failure)
    refund_password_login(db, reservation)
    cookie = create_session(db, user.id)
    db.commit()
    response = login_redirect(request)
    set_session_cookie(response, *cookie)
    return response


@router.get('/auth/password/request')
def password_request_page(request: Request):
    return auth_page(request, 'password_request.html')


@router.post('/auth/password/request')
def password_request(request: Request, background_tasks: BackgroundTasks, email: str = Form(...), form_token: str = Form(''),
                     db: Session = Depends(get_db)):
    check_form(request, form_token)
    email = email.strip().lower()
    response = redirect_message('/auth/password/request', RECOVERY_MESSAGE)
    if len(email) > 320:
        return response
    if not allow_login(db, email, client_ip(request)):
        return limited_response('/auth/password/request')
    job = enqueue(db, email, 'password')
    db.commit()
    background_tasks.add_task(deliver_in_background, job.id, db.get_bind())
    return response


def recovery_token(db, raw):
    token = db.scalar(select(PasswordToken).where(PasswordToken.token_hash == token_hash(raw)))
    if not token or token.used_at or aware(token.expires_at) <= utcnow():
        return None
    return token


@router.get('/auth/password/reset/{raw}')
def password_reset_page(raw: str, request: Request, db: Session = Depends(get_db)):
    token = recovery_token(db, raw)
    user = db.get(User, token.user_id) if token else None
    if not user or not user.is_active:
        return redirect_message('/auth/password/request', INVALID_LINK)
    return auth_page(request, 'password_form.html', action=f'/auth/password/reset/{raw}')


@router.post('/auth/password/reset/{raw}')
def password_reset(raw: str, request: Request, password: str = Form(...), confirmation: str = Form(...),
                   form_token: str = Form(''), db: Session = Depends(get_db)):
    check_form(request, form_token)
    token = recovery_token(db, raw)
    user = db.get(User, token.user_id, populate_existing=True, with_for_update=True) if token else None
    if not user or not user.is_active:
        return redirect_message('/auth/password/request', INVALID_LINK)
    error = password_error(password, confirmation)
    if error:
        return auth_page(request, 'password_form.html', action=f'/auth/password/reset/{raw}', error=error)
    consumed = db.execute(update(PasswordToken).where(PasswordToken.id == token.id,
        PasswordToken.used_at.is_(None), PasswordToken.expires_at > utcnow()
    ).values(used_at=utcnow()).execution_options(synchronize_session=False))
    if consumed.rowcount != 1:
        db.rollback()
        return redirect_message('/auth/password/request', INVALID_LINK)
    return finish_password(db, user, password, request)


@router.get('/auth/password')
def password_setup_page(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse('/login', 303)
    if user.password_hash:
        return RedirectResponse('/dashboard', 303)
    return auth_page(request, 'password_form.html', action='/auth/password', setup=True)


@router.post('/auth/password')
def password_setup(request: Request, password: str = Form(...), confirmation: str = Form(...),
                   form_token: str = Form(''), db: Session = Depends(get_db)):
    check_form(request, form_token)
    session = valid_session(request, db)
    user = db.get(User, session.user_id, populate_existing=True, with_for_update=True) if session else None
    # Recheck the session after acquiring the user lock, including concurrent resets.
    if not user or not user.is_active or not valid_session(request, db):
        return RedirectResponse('/login', 303)
    if user.password_hash:
        return RedirectResponse('/auth/password/request', 303)
    error = password_error(password, confirmation)
    if error:
        return auth_page(request, 'password_form.html', action='/auth/password', setup=True, error=error)
    return finish_password(db, user, password, request)


@router.get('/auth/continue')
def continue_after_setup(request: Request, db: Session = Depends(get_db)):
    if not current_user(request, db):
        return RedirectResponse('/login', 303)
    return login_redirect(request)

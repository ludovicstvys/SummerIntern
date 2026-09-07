"""Password sign-in and verified, one-use password setup/recovery."""
import hmac
import os
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pwdlib import PasswordHash
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .emailing import send_password_link
from .limits import allow_login, allow_password_login
from .models import Invitation, PasswordToken, User, utcnow
from .operations import error_code
from .security import expires_in, new_token, token_hash
from .sessions import aware, create_session, current_user, revoke_user_auth, set_session_cookie, valid_session

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / 'templates')
passwords = PasswordHash.recommended()
DUMMY_HASH = passwords.hash(new_token())
form_signer = URLSafeTimedSerializer(settings.secret_key, salt='auth-form')
FORM_COOKIE = 'trackr_auth_csrf'
RECOVERY_MESSAGE = 'If the address is invited, a password setup or reset link is on its way.'
INVALID_LINK = 'This link is invalid or has expired. Please request a new one.'


def client_ip(request):
    ip = request.client.host if request.client else 'unknown'
    if os.getenv('VERCEL') == '1':
        try:
            ip = str(ip_address(request.headers.get('x-vercel-forwarded-for', ip).split(',')[0].strip()))
        except ValueError:
            pass
    return ip


def check_form(request, value):
    cookie = request.cookies.get(FORM_COOKIE, '')
    origin = request.headers.get('origin')
    configured = urlsplit(settings.app_url)
    if origin and origin != f'{configured.scheme}://{configured.netloc}':
        raise HTTPException(403, 'Invalid form origin')
    if not value or not cookie or not hmac.compare_digest(value.encode(), cookie.encode()):
        raise HTTPException(403, 'Invalid CSRF token')
    try:
        form_signer.loads(value, max_age=3600)
    except BadSignature:
        raise HTTPException(403, 'Expired form. Please reload the page.')


def auth_page(request, template, **context):
    token = request.cookies.get(FORM_COOKIE)
    try:
        form_signer.loads(token or '', max_age=3600)
    except BadSignature:
        token = form_signer.dumps(new_token())
    response = templates.TemplateResponse(request, template, {
        'request': request, 'user': None, 'form_token': token, **context})
    response.set_cookie(FORM_COOKIE, token, httponly=True, secure=settings.app_url.startswith('https'),
                        samesite='lax', max_age=3600, path='/')
    return response


def redirect_message(path, message):
    return RedirectResponse(path + '?message=' + quote(message), 303)


def password_error(password, confirmation):
    if not 12 <= len(password) <= 128:
        return 'Use between 12 and 128 characters.'
    if password != confirmation:
        return 'The passwords do not match.'
    return None


def finish_password(db, user, password):
    user.password_hash = passwords.hash(password)
    revoke_user_auth(db, user.id)
    db.execute(update(Invitation).where(Invitation.email == user.email,
               Invitation.accepted_at.is_(None)).values(accepted_at=utcnow()))
    cookie = create_session(db, user.id)
    db.commit()
    response = RedirectResponse('/dashboard', 303)
    set_session_cookie(response, *cookie)
    return response


@router.post('/auth/login')
def password_login(request: Request, email: str = Form(...), password: str = Form(...),
                   form_token: str = Form(''), db: Session = Depends(get_db)):
    check_form(request, form_token)
    email = email.strip().lower()
    failure = 'Incorrect email or password.'
    if len(email) > 320 or not allow_password_login(db, email, client_ip(request)):
        return redirect_message('/login', failure)
    user = db.scalar(select(User).where(User.email == email).with_for_update())
    candidate = user.password_hash if user and user.is_active and user.password_hash else DUMMY_HASH
    verified = passwords.verify(password if len(password) <= 128 else '', candidate)
    if not verified or not user or not user.is_active or not user.password_hash or not 12 <= len(password) <= 128:
        return redirect_message('/login', failure)
    cookie = create_session(db, user.id)
    db.commit()
    response = RedirectResponse('/dashboard', 303)
    set_session_cookie(response, *cookie)
    return response


@router.get('/auth/password/request')
def password_request_page(request: Request):
    return auth_page(request, 'password_request.html')


@router.post('/auth/password/request')
def password_request(request: Request, email: str = Form(...), form_token: str = Form(''),
                     db: Session = Depends(get_db)):
    check_form(request, form_token)
    email = email.strip().lower()
    response = redirect_message('/auth/password/request', RECOVERY_MESSAGE)
    if len(email) > 320 or not allow_login(db, email, client_ip(request)):
        return response
    user = db.scalar(select(User).where(User.email == email, User.is_active.is_(True)).with_for_update())
    if user:
        raw = new_token()
        token = PasswordToken(user_id=user.id, token_hash=token_hash(raw), expires_at=expires_in(15))
        db.add(token)
        # Commit before delivery, so a received link always has a persisted record.
        db.commit()
        try:
            send_password_link(user.email, f'{settings.app_url}/auth/password/reset/{raw}')
        except Exception as exc:
            print(f'Password link delivery failed: {error_code(exc)}')
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
    return finish_password(db, user, password)


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
    return finish_password(db, user, password)

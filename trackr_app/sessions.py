"""Opaque, revocable database sessions shared by every sign-in method."""
from datetime import timedelta, timezone

from sqlalchemy import delete, or_, select, update

from .config import settings
from .models import AuthMail, MagicLink, PasswordToken, User, UserSession, utcnow
from .security import new_token, token_hash

COOKIE = 'trackr_session'
IDLE = timedelta(days=90)
ABSOLUTE = timedelta(days=365)


def aware(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc)


def valid_session(request, db):
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(raw)))
    now = utcnow()
    if not session or min(aware(session.expires_at), aware(session.created_at) + ABSOLUTE) <= now:
        return None
    user = db.get(User, session.user_id)
    return session if user and user.is_active else None


def current_user(request, db):
    session = valid_session(request, db)
    if not session:
        return None
    user = db.get(User, session.user_id)
    now = utcnow()
    previous = aware(session.renewed_at or session.created_at)
    if previous <= now - timedelta(days=1):
        expiry = min(now + IDLE, aware(session.created_at) + ABSOLUTE)
        renewed = db.execute(update(UserSession).where(
            UserSession.id == session.id, UserSession.expires_at > now,
            or_(UserSession.renewed_at <= now - timedelta(days=1),
                UserSession.renewed_at.is_(None)),
        ).values(expires_at=expiry, renewed_at=now).execution_options(synchronize_session=False))
        db.commit()
        if renewed.rowcount == 1:
            request.state.session_cookie = (request.cookies[COOKIE], expiry)
    return user


def create_session(db, user_id):
    now = utcnow()
    raw = new_token()
    session = UserSession(user_id=user_id, token_hash=token_hash(raw), csrf_token=new_token(),
                          created_at=now, renewed_at=now, expires_at=now + IDLE)
    db.add(session)
    return raw, session.expires_at


def set_session_cookie(response, raw, expiry):
    response.set_cookie(COOKIE, raw, httponly=True, secure=settings.app_url.startswith('https'),
                        samesite='lax', path='/', expires=expiry,
                        max_age=max(0, int((aware(expiry) - utcnow()).total_seconds())))


def revoke_user_auth(db, user_id):
    user = db.get(User, user_id)
    if user:
        db.execute(update(AuthMail).where(AuthMail.email_hash == token_hash(user.email),
                   AuthMail.status.in_(['pending', 'processing'])).values(status='cancelled'))
    for model in (UserSession, MagicLink, PasswordToken):
        db.execute(delete(model).where(model.user_id == user_id))


async def session_headers(request, call_next):
    response = await call_next(request)
    renewal = getattr(request.state, 'session_cookie', None)
    # A new login or logout owns its cookie; never overwrite it with a renewal.
    owns_cookie = any(value.startswith(COOKIE + '=') for value in response.headers.getlist('set-cookie'))
    if renewal and not owns_cookie:
        set_session_cookie(response, *renewal)
    if request.url.path.startswith(('/auth/', '/login')) or request.cookies.get(COOKIE) or response.headers.getlist('set-cookie'):
        response.headers['Cache-Control'] = 'no-store'
        # Preserve a concrete Origin for same-site form POSTs while keeping
        # authentication URLs out of cross-site requests and external links.
        response.headers['Referrer-Policy'] = 'same-origin'
    return response

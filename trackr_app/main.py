import json
import os
from datetime import time
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import quote
from zoneinfo import available_timezones

from email_validator import EmailNotValidError, validate_email
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select, text, delete, update
from sqlalchemy.orm import Session

from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .emailing import send_magic_link
from .models import Delivery, Invitation, MagicLink, NotionSync, Offer, OfferSource, Preference, User, UserOffer, UserSession, WorkerState, utcnow
from .notion import accessible_pages, create_offer_database, exchange_code, oauth_url, save_connection, recover_database
from .preferences import PROGRAM_TYPES, REGIONS, activate_preference, matching_offers, offer_matches, offer_is_open
from .opportunities import browse_database, browse_opportunities, opportunity_card, relevant_sources
from .operations import insert_for, error_code
from .invitations import deliver_invitation
from .security import token_hash, new_token
from .limits import allow_login
from .auth import (router as auth_router, auth_page, check_form, client_ip, AuthFormError,
                   login_redirect, login_required, limited_response, RECOVERY_MESSAGE, RETURN_COOKIE)
from .auth_mail import enqueue, deliver_in_background
from .sessions import current_user, create_session, set_session_cookie, session_headers, valid_session, revoke_user_auth
from .health import SCHEMA_REVISION, WEB_COMPATIBLE_SCHEMAS

PACKAGE_DIR = __import__("pathlib").Path(__file__).resolve().parent
def bootstrap() -> None:
    settings.validate()
    # The same migration history owns both local and production schemas.
    if settings.database_url.startswith("sqlite"):
        from .local_database import upgrade_local
        upgrade_local(engine)
    if settings.admin_email:
        with SessionLocal() as db:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
            user_id = db.scalar(insert(User).values(email=settings.admin_email, role="admin").on_conflict_do_nothing(index_elements=[User.email]).returning(User.id))
            if user_id:
                db.add(Preference(user_id=user_id))
            db.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    bootstrap()
    yield


app = FastAPI(title="Trackr Alerts", lifespan=lifespan)
from .http_security import HttpSecurity
from starlette.middleware.trustedhost import TrustedHostMiddleware
if settings.environment != 'development' or os.getenv('VERCEL') == '1':
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
app.add_middleware(HttpSecurity)
app.include_router(auth_router)
app.middleware("http")(session_headers)
app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
signer = URLSafeTimedSerializer(settings.secret_key, salt="notion-oauth")


@app.exception_handler(AuthFormError)
async def auth_form_error(request: Request, exc: AuthFormError):
    path = request.url.path
    retry = '/login' if path in ('/auth/login', '/auth/request') else path
    response = templates.TemplateResponse(request, 'auth_error.html', {
        'request': request, 'user': None, 'message': exc.detail, 'retry': retry}, status_code=403)
    return response


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user(request, db)
    if not user:
        response = login_required(request)
        raise HTTPException(303, headers={key: value for key, value in response.headers.items() if key in ("location", "set-cookie")})
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403)
    return user


def csrf(request: Request, db: Session, value: str) -> None:
    session = valid_session(request, db)
    if not session or not value or value != session.csrf_token:
        raise HTTPException(403, "Invalid CSRF token")


def context(request: Request, db: Session, user: User | None = None, **extra):
    session = None
    raw = request.cookies.get("trackr_session", "")
    if raw:
        session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(raw)))
    return {"request": request, "user": user, "csrf_token": session.csrf_token if session else "", 'notion_available': getattr(settings, 'notion_available', False), **extra}


@app.get("/health")
def health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            revision = connection.scalar(text("SELECT version_num FROM alembic_version")) if (settings.is_production or getattr(settings, "environment", "") == "preview" or os.getenv("VERCEL") == "1") else SCHEMA_REVISION
            if revision not in WEB_COMPATIBLE_SCHEMAS:
                raise RuntimeError("Schema version mismatch")
    except Exception:
        raise HTTPException(503, "Database unavailable")
    return {"status": "ok", "database": "ok", "schema": revision, "compatible_schemas": WEB_COMPATIBLE_SCHEMAS, "commit": os.getenv("VERCEL_GIT_COMMIT_SHA", os.getenv("APP_COMMIT", "local"))}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse("/dashboard" if current_user(request, db) else "/login", 303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if current_user(request, db):
        return RedirectResponse("/dashboard", 303)
    return auth_page(request, "login.html")


@app.post("/auth/request")
def request_link(request: Request, background_tasks: BackgroundTasks, email: str = Form(...), form_token: str = Form(""), db: Session = Depends(get_db)):
    check_form(request, form_token)
    normalized = email.strip().lower()
    if len(normalized) > 320:
        return RedirectResponse('/login?message=' + quote(RECOVERY_MESSAGE), 303)
    if not allow_login(db, normalized, client_ip(request)):
        return limited_response('/login')
    job = enqueue(db, normalized, 'magic')
    db.commit()
    background_tasks.add_task(deliver_in_background, job.id, db.get_bind())
    return RedirectResponse('/login?message=' + quote(RECOVERY_MESSAGE), 303)


@app.get("/auth/consume/{raw}")
def consume_link_page(raw: str, request: Request, db: Session = Depends(get_db)):
    link = db.scalar(select(MagicLink).where(MagicLink.token_hash == token_hash(raw)))
    user = db.get(User, link.user_id) if link else None
    if not link or link.used_at or link.expires_at.replace(tzinfo=link.expires_at.tzinfo or utcnow().tzinfo) <= utcnow() or not user or not user.is_active:
        return RedirectResponse('/login?message=' + quote('This link is invalid or has expired.'), 303)
    existing = current_user(request, db)
    return auth_page(request, 'confirm_login.html', action='/auth/consume/' + raw,
                     email=user.email, switching=bool(existing and existing.id != user.id))


@app.post('/auth/consume/{raw}')
def consume_link(raw: str, request: Request, form_token: str = Form(''), db: Session = Depends(get_db)):
    check_form(request, form_token)
    link = db.scalar(select(MagicLink).where(MagicLink.token_hash == token_hash(raw)))
    if not link or link.used_at or link.expires_at.replace(tzinfo=link.expires_at.tzinfo or utcnow().tzinfo) <= utcnow():
        return RedirectResponse("/login?message=" + quote("This link is invalid or has expired."), 303)
    link_user = db.get(User, link.user_id, populate_existing=True, with_for_update=True)
    if not link_user or not link_user.is_active:
        return RedirectResponse("/login?message=" + quote("This link is invalid or has expired."), 303)
    consumed = db.execute(update(MagicLink).where(MagicLink.id == link.id, MagicLink.used_at.is_(None), MagicLink.expires_at > utcnow()).values(used_at=utcnow()).execution_options(synchronize_session=False))
    if consumed.rowcount != 1:
        db.rollback()
        return RedirectResponse("/login?message=" + quote("This link is invalid or has expired."), 303)
    cookie = create_session(db, link.user_id)
    invitation = db.scalar(select(Invitation).where(Invitation.email == db.get(User, link.user_id).email, Invitation.accepted_at.is_(None)))
    if invitation:
        invitation.accepted_at = utcnow()
    db.commit()
    response = login_redirect(request, "/auth/password" if not link_user.password_hash else "/dashboard")
    set_session_cookie(response, *cookie)
    return response


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    if valid_session(request, db):
        csrf(request, db, csrf_token)
    raw = request.cookies.get("trackr_session", "")
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(raw)))
    if session:
        db.delete(session)
        db.commit()
    response = RedirectResponse("/login", 303)
    response.delete_cookie("trackr_session")
    response.delete_cookie(RETURN_COOKIE)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, page: int = 1, history: bool = False, q: str = "", region: str = "", programme: str = "", start_term: str = "", sort: str = "latest", user: User = Depends(require_user), db: Session = Depends(get_db)):
    preference = user.preference or Preference(user_id=user.id)
    if not user.preference:
        db.add(preference); db.commit(); db.refresh(preference)
    feed = browse_database(db, user.id, preference, page=page, history=history, q=q, region=region, programme=programme, start_term=start_term, sort=sort)
    return templates.TemplateResponse(request, "dashboard.html", context(request, db, user, preference=preference, **feed))


@app.get("/preferences", response_class=HTMLResponse)
def preferences_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    all_terms = sorted(set(db.scalars(select(OfferSource.start_term).where(OfferSource.start_term.is_not(None))).all()))
    return templates.TemplateResponse(request, "preferences.html", context(request, db, user, preference=user.preference, program_types=json.loads(user.preference.program_types), regions=json.loads(user.preference.regions), terms=json.loads(user.preference.start_terms), all_terms=all_terms, all_types=PROGRAM_TYPES, all_regions=REGIONS))


def preference_values(program_types, regions, start_terms, delivery_mode, digest_time, timezone):
    if not program_types or not regions or not set(program_types).issubset(PROGRAM_TYPES) or not set(regions).issubset(REGIONS) or delivery_mode not in ("immediate", "daily_digest"):
        raise ValueError("Choose at least one programme and region, and a valid delivery mode.")
    if timezone not in available_timezones():
        raise ValueError("Choose a valid timezone, for example Europe/Paris.")
    try:
        parsed_time = time.fromisoformat(digest_time)
        if parsed_time.tzinfo or len(digest_time) != 5:
            raise ValueError()
    except ValueError:
        raise ValueError("Enter a valid digest time in HH:MM format.")
    return dict(program_types=json.dumps(sorted(set(program_types))), regions=json.dumps(sorted(set(regions))), start_terms=json.dumps(sorted({term.strip() for term in start_terms if term.strip()})), delivery_mode=delivery_mode, digest_time=parsed_time, timezone=timezone)


def preference_error(request, db, user, message):
    pref = user.preference
    return templates.TemplateResponse(request, "preferences.html", context(request, db, user, error=message, preference=pref, program_types=json.loads(pref.program_types), regions=json.loads(pref.regions), terms=json.loads(pref.start_terms), all_terms=sorted(set(db.scalars(select(OfferSource.start_term).where(OfferSource.start_term.is_not(None))))), all_types=PROGRAM_TYPES, all_regions=REGIONS), status_code=422)


@app.post("/preferences/preview")
def preferences_preview(request: Request, program_types: list[str] = Form(default=[]), regions: list[str] = Form(default=[]), start_terms: list[str] = Form(default=[]), delivery_mode: str = Form("immediate"), digest_time: str = Form("08:00"), timezone: str = Form("Europe/Paris"), csrf_token: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    try:
        values = preference_values(program_types, regions, start_terms, delivery_mode, digest_time, timezone)
    except ValueError as exc:
        return preference_error(request, db, user, str(exc))
    pref = Preference(user_id=user.id, **values)
    from .preferences import matching_query
    from sqlalchemy import func
    query = matching_query(pref)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    offers = db.scalars(query.limit(20)).all()
    cards = [opportunity_card(offer, relevant_sources(offer, pref)) for offer in offers[:20]]
    return templates.TemplateResponse(request, "preview.html", context(request, db, user, preference=pref, offers=offers, total=total, cards=cards, program_types=json.loads(pref.program_types), regions=json.loads(pref.regions), start_terms=json.loads(pref.start_terms)))


@app.post("/preferences/activate")
def activate(request: Request, program_types: list[str] = Form(default=[]), regions: list[str] = Form(default=[]), start_terms: list[str] = Form(default=[]), delivery_mode: str = Form("immediate"), digest_time: str = Form("08:00"), timezone: str = Form("Europe/Paris"), csrf_token: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    try:
        values = preference_values(program_types, regions, start_terms, delivery_mode, digest_time, timezone)
    except ValueError as exc:
        return preference_error(request, db, user, str(exc))
    # Serialize preference changes with workers and account deactivation.
    db.refresh(user, with_for_update=True)
    if not user.is_active:
        raise HTTPException(403)
    db.refresh(user.preference)
    for key, value in values.items():
        setattr(user.preference, key, value)
    count = activate_preference(db, user.preference, defer=True)
    return RedirectResponse("/dashboard?message=Preferences%20saved.%20Matching%20offers%20are%20being%20updated.", 303)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    invitations = db.scalars(select(Invitation).order_by(Invitation.id.desc()).limit(50)).all()
    states = db.scalars(select(WorkerState).where(WorkerState.key != 'scrape-lock')).all()
    return templates.TemplateResponse(request, "admin.html", context(request, db, admin, users=users, invitations=invitations, states=states))


@app.get('/admin/operations')
def operations(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    from .monitoring import operational_status
    result = operational_status(db)
    return JSONResponse(result, status_code=200 if result['status'] == 'ok' else 503)


@app.post("/admin/invite")
def invite(request: Request, email: str = Form(...), csrf_token: str = Form(...), admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    try:
        normalized = validate_email(email, check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise HTTPException(422, str(exc))
    db.execute(insert_for(db, User).values(email=normalized).on_conflict_do_nothing(index_elements=['email']))
    user = db.scalar(select(User).where(User.email == normalized).with_for_update().execution_options(populate_existing=True))
    db.execute(insert_for(db, Preference).values(user_id=user.id).on_conflict_do_nothing(index_elements=['user_id']))
    user.is_active = True
    if user.preference and user.preference.status == 'active':
        activate_preference(db, user.preference, commit=False, defer=True)
    invitation = db.scalar(select(Invitation).where(Invitation.email == normalized, Invitation.accepted_at.is_(None)).order_by(Invitation.id.desc()))
    recently_sent = invitation and invitation.delivery_status == 'sent' and invitation.created_at.replace(tzinfo=invitation.created_at.tzinfo or utcnow().tzinfo) > utcnow()-timedelta(minutes=1)
    if invitation and ((invitation.delivery_status == 'pending' and invitation.last_error is None) or recently_sent):
        db.commit()
        return RedirectResponse('/admin', 303)
    if invitation is None:
        invitation = Invitation(email=normalized, invited_by_id=admin.id)
        db.add(invitation)
    else:
        invitation.delivery_status, invitation.attempts = 'pending', 0
        invitation.last_error, invitation.next_attempt_at = None, None
        invitation.created_at = utcnow()
    db.commit()
    deliver_invitation(db, invitation.id, sender=send_magic_link)
    return RedirectResponse("/admin", 303)


@app.post("/admin/users/{user_id}/toggle")
def toggle_user(user_id: int, request: Request, csrf_token: str = Form(...), admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    target = db.get(User, user_id)
    if not target or target.id == admin.id:
        raise HTTPException(400)
    db.refresh(target, with_for_update=True)
    target.is_active = not target.is_active
    if not target.is_active:
        db.execute(update(Invitation).where(Invitation.email == target.email, Invitation.accepted_at.is_(None)).values(delivery_status='cancelled'))
        revoke_user_auth(db, target.id)
        db.execute(update(Delivery).where(Delivery.user_id == target.id, Delivery.status.in_(["pending", "processing", "failed"])).values(status="cancelled", processing_started_at=None))
        if target.notion:
            db.execute(update(NotionSync).where(NotionSync.connection_id == target.notion.id, NotionSync.status.in_(["pending", "processing", "failed"])).values(status="cancelled"))
    elif target.preference and target.preference.status == 'active':
        activate_preference(db, target.preference, commit=False, defer=True)
    db.commit()
    return RedirectResponse("/admin", 303)


@app.get("/notion/connect")
def notion_connect(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not settings.notion_available:
        return RedirectResponse('/dashboard?message=Notion%20is%20temporarily%20unavailable', 303)
    from .models import OAuthNonce
    from .notion import require_runtime
    require_runtime(db)
    session = valid_session(request, db)
    nonce = new_token()
    db.add(OAuthNonce(token_hash=token_hash(nonce), user_id=user.id, session_id=session.id,
        expires_at=utcnow() + timedelta(minutes=10)))
    db.commit()
    return RedirectResponse(oauth_url(signer.dumps({'user_id': user.id, 'nonce': nonce})), 303)


@app.get("/notion/callback")
def notion_callback(request: Request, state: str, code: str = '', error: str = '', user: User = Depends(require_user), db: Session = Depends(get_db)):
    try:
        data = signer.loads(state, max_age=600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(400, "Invalid OAuth state")
    if data.get("user_id") != user.id:
        raise HTTPException(403)
    from .models import OAuthNonce
    from .notion import require_runtime
    require_runtime(db)
    session = valid_session(request, db)
    used = db.execute(delete(OAuthNonce).where(OAuthNonce.token_hash == token_hash(data.get('nonce', '')),
        OAuthNonce.user_id == user.id, OAuthNonce.session_id == session.id,
        OAuthNonce.expires_at > utcnow()))
    db.commit()
    if used.rowcount != 1:
        raise HTTPException(400, 'OAuth state expired or already used')
    if not settings.notion_available or error or not code:
        return RedirectResponse('/dashboard?message=Notion%20connection%20cancelled', 303)
    try:
        payload = exchange_code(code)
    except Exception:
        return RedirectResponse('/dashboard?message=Notion%20connection%20failed.%20Please%20try%20again.', 303)
    db.refresh(user, with_for_update=True)
    if not user.is_active:
        raise HTTPException(403)
    save_connection(db, user.id, payload)
    return RedirectResponse("/notion/setup", 303)


@app.get("/notion/setup", response_class=HTMLResponse)
def notion_setup(request: Request, cursor: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not settings.notion_available:
        return RedirectResponse('/dashboard', 303)
    if not user.notion:
        return RedirectResponse("/dashboard", 303)
    try:
        connection = user.notion
        db.commit()
        pages, next_cursor = accessible_pages(connection, cursor or None)
    except Exception as exc:
        user.notion.last_error = error_code(exc); db.commit(); pages, next_cursor = [], None
    return templates.TemplateResponse(request, "notion_setup.html", context(request, db, user, pages=pages, next_cursor=next_cursor))


@app.post("/notion/setup")
def notion_create(request: Request, page_id: str = Form(...), csrf_token: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    db.refresh(user, with_for_update=True)
    if not user.is_active:
        raise HTTPException(403)
    if not settings.notion_available:
        return RedirectResponse('/dashboard', 303)
    if not user.notion:
        return RedirectResponse("/notion/connect", 303)
    if user.notion.data_source_id:
        return RedirectResponse('/dashboard', 303)
    if user.notion.setup_status in ('pending', 'creating', 'uncertain'):
        return RedirectResponse('/notion/setup?message=Check%20Notion%20before%20retrying%20an%20uncertain%20creation.', 303)
    from .notion import require_runtime
    from .durable import enqueue_job
    require_runtime(db)
    connection = user.notion
    connection.setup_status, connection.parent_page_id = 'pending', page_id
    enqueue_job(db, f'notion-create/{connection.id}', 'notion-create',
        {'connection_id': connection.id, 'user_id': user.id, 'parent_page_id': page_id})
    db.commit()
    return RedirectResponse('/notion/setup', 303)


@app.post("/notion/disconnect")
def notion_disconnect(request: Request, csrf_token: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    db.refresh(user, with_for_update=True)
    if user.notion:
        db.execute(delete(NotionSync).where(NotionSync.connection_id == user.notion.id))
        db.delete(user.notion); db.commit()
    return RedirectResponse("/dashboard", 303)


@app.post('/notion/setup/recover')
def notion_recover(request: Request, database_id: str = Form(...), csrf_token: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    db.refresh(user, with_for_update=True)
    if not user.is_active or not settings.notion_available or not user.notion:
        raise HTTPException(403)
    from types import SimpleNamespace
    from .models import DurableJob, NotionConnection
    from .durable import enqueue_job
    from .notion import require_runtime
    require_runtime(db)
    connection = user.notion
    identity, credential = connection.id, connection.access_token_encrypted
    job = db.get(DurableJob, f'notion-create/{identity}', with_for_update=True)
    from .sessions import aware
    if job and job.status == 'processing' and job.lease_until and aware(job.lease_until) > utcnow():
        raise HTTPException(409, 'Creation is still in progress')
    snapshot = SimpleNamespace(parent_page_id=connection.parent_page_id, access_token_encrypted=credential)
    db.commit()
    try:
        recover_database(SimpleNamespace(flush=lambda: None), snapshot, database_id)
        db.refresh(user, with_for_update=True)
        connection = db.get(NotionConnection, identity, populate_existing=True)
        if not user.is_active or not connection or connection.access_token_encrypted != credential:
            raise HTTPException(409, 'Notion connection changed')
        connection.database_id, connection.data_source_id = snapshot.database_id, snapshot.data_source_id
        connection.setup_status, connection.last_error = 'ready', None
        job = db.get(DurableJob, f'notion-create/{identity}', with_for_update=True, populate_existing=True)
        if job:
            job.status, job.completed_at, job.lease_until = 'done', utcnow(), None
        enqueue_job(db, f'match-user/{user.id}', 'match', {'user_id': user.id, 'baseline': True})
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse('/notion/setup?message=Unable%20to%20verify%20this%20database.%20Check%20its%20ID%20and%20permissions.', 303)
    return RedirectResponse('/dashboard', 303)


@app.get('/admin/jobs', response_class=HTMLResponse)
def admin_jobs(request: Request, kind: str = 'auth', after: str = '', admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    from .admin_jobs import MODELS, failed_jobs
    if kind not in MODELS:
        raise HTTPException(404)
    try:
        jobs = failed_jobs(db, kind, after)
    except ValueError:
        raise HTTPException(422, 'Invalid cursor')
    return templates.TemplateResponse(request, 'jobs.html', context(request, db, admin,
        jobs=jobs, kind=kind, kinds=MODELS, next_after=jobs[-1]['id'] if len(jobs) == 50 else None))


@app.post('/admin/jobs/retry')
def retry_job(request: Request, kind: str = Form(...), job_id: str = Form(...), csrf_token: str = Form(...), admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    from .admin_jobs import MODELS, retry
    csrf(request, db, csrf_token)
    if kind not in MODELS:
        raise HTTPException(404)
    try:
        retry(db, kind, job_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc))
    return RedirectResponse('/admin/jobs?kind=' + kind, 303)


@app.get('/admin/export.csv')
def export_snapshot(source: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    import csv
    import io
    from .models import SourceSnapshot
    from trackr_common import CSV_COLUMNS
    from fastapi.responses import Response
    snapshot = db.get(SourceSnapshot, source)
    if not snapshot or snapshot.payload is None:
        raise HTTPException(404)
    rows = json.loads(snapshot.payload)
    db.commit()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        values = []
        for column in CSV_COLUMNS:
            value = row.get(column, row.get(column.lower().replace(' ', '_'), ''))
            if isinstance(value, list):
                value = ','.join(map(str, value))
            # Prevent spreadsheet formula execution in administrator exports.
            if isinstance(value, str) and value.startswith(('=', '+', '-', '@')):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return Response(output.getvalue(), media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="trackr-snapshot.csv"', 'Cache-Control': 'no-store'})

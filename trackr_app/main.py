import json
from datetime import timedelta
from urllib.parse import quote
from zoneinfo import available_timezones

from email_validator import EmailNotValidError, validate_email
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .emailing import send_magic_link
from .models import Invitation, MagicLink, NotionSync, Offer, Preference, User, UserOffer, UserSession, utcnow
from .notion import accessible_pages, create_offer_database, exchange_code, oauth_url, save_connection
from .preferences import PROGRAM_TYPES, REGIONS, activate_preference, matching_offers
from .security import expires_in, new_token, token_hash

app = FastAPI(title="Trackr Alerts")
app.mount("/static", StaticFiles(directory="trackr_app/static"), name="static")
templates = Jinja2Templates(directory="trackr_app/templates")
signer = URLSafeTimedSerializer(settings.secret_key, salt="notion-oauth")


@app.on_event("startup")
def bootstrap() -> None:
    # Alembic owns production schema changes; create_all makes local onboarding painless.
    if settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(engine)
    if settings.admin_email:
        with SessionLocal() as db:
            if not db.scalar(select(User).where(User.email == settings.admin_email)):
                user = User(email=settings.admin_email, role="admin")
                db.add(user)
                db.flush()
                db.add(Preference(user_id=user.id))
                db.commit()


def current_user(request: Request, db: Session) -> User | None:
    raw = request.cookies.get("trackr_session")
    if not raw:
        return None
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(raw)))
    if not session or session.expires_at.replace(tzinfo=session.expires_at.tzinfo or utcnow().tzinfo) <= utcnow():
        return None
    user = db.get(User, session.user_id)
    return user if user and user.is_active else None


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user(request, db)
    if not user:
        raise HTTPException(303, headers={"Location": "/login"})
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403)
    return user


def csrf(request: Request, db: Session, value: str) -> None:
    raw = request.cookies.get("trackr_session", "")
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(raw)))
    if not session or not value or value != session.csrf_token:
        raise HTTPException(403, "Invalid CSRF token")


def context(request: Request, db: Session, user: User | None = None, **extra):
    session = None
    raw = request.cookies.get("trackr_session", "")
    if raw:
        session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(raw)))
    return {"request": request, "user": user, "csrf_token": session.csrf_token if session else "", **extra}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse("/dashboard" if current_user(request, db) else "/login", 303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "login.html", context(request, db, message=request.query_params.get("message")))


@app.post("/auth/request")
def request_link(email: str = Form(...), db: Session = Depends(get_db)):
    normalized = email.strip().lower()
    user = db.scalar(select(User).where(User.email == normalized, User.is_active.is_(True)))
    if user:
        raw = new_token()
        db.add(MagicLink(user_id=user.id, token_hash=token_hash(raw), expires_at=expires_in(15)))
        db.commit()
        url = f"{settings.app_url}/auth/consume/{raw}"
        try:
            send_magic_link(user.email, url)
        except Exception as exc:
            print(f"Magic link delivery failed: {exc}; development link: {url}")
    return RedirectResponse("/login?message=" + quote("If the address is invited, a sign-in link is on its way."), 303)


@app.get("/auth/consume/{raw}")
def consume_link(raw: str, db: Session = Depends(get_db)):
    link = db.scalar(select(MagicLink).where(MagicLink.token_hash == token_hash(raw)))
    if not link or link.used_at or link.expires_at.replace(tzinfo=link.expires_at.tzinfo or utcnow().tzinfo) <= utcnow():
        return RedirectResponse("/login?message=" + quote("This link is invalid or has expired."), 303)
    link.used_at = utcnow()
    session_raw = new_token()
    db.add(UserSession(user_id=link.user_id, token_hash=token_hash(session_raw), csrf_token=new_token(), expires_at=expires_in(60 * 24 * 30)))
    invitation = db.scalar(select(Invitation).where(Invitation.email == db.get(User, link.user_id).email, Invitation.accepted_at.is_(None)))
    if invitation:
        invitation.accepted_at = utcnow()
    db.commit()
    response = RedirectResponse("/dashboard", 303)
    response.set_cookie("trackr_session", session_raw, httponly=True, secure=settings.app_url.startswith("https"), samesite="lax", max_age=60 * 60 * 24 * 30)
    return response


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    raw = request.cookies.get("trackr_session", "")
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(raw)))
    if session:
        db.delete(session)
        db.commit()
    response = RedirectResponse("/login", 303)
    response.delete_cookie("trackr_session")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    preference = user.preference or Preference(user_id=user.id)
    if not user.preference:
        db.add(preference); db.commit(); db.refresh(preference)
    rows = db.execute(select(UserOffer, Offer).join(Offer, UserOffer.offer_id == Offer.id).where(UserOffer.user_id == user.id).order_by(UserOffer.matched_at.desc()).limit(50)).all()
    return templates.TemplateResponse(request, "dashboard.html", context(request, db, user, preference=preference, offers=[row[1] for row in rows]))


@app.get("/preferences", response_class=HTMLResponse)
def preferences_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    all_terms = sorted(set(db.scalars(select(Offer.start_term).where(Offer.start_term.is_not(None))).all()))
    return templates.TemplateResponse(request, "preferences.html", context(request, db, user, preference=user.preference, program_types=json.loads(user.preference.program_types), regions=json.loads(user.preference.regions), terms=json.loads(user.preference.start_terms), all_terms=all_terms, all_types=PROGRAM_TYPES, all_regions=REGIONS))


@app.post("/preferences/preview")
def preferences_preview(request: Request, program_types: list[str] = Form(default=[]), regions: list[str] = Form(default=[]), start_terms: list[str] = Form(default=[]), delivery_mode: str = Form("immediate"), digest_time: str = Form("08:00"), timezone: str = Form("Europe/Paris"), csrf_token: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    if not set(program_types).issubset(PROGRAM_TYPES) or not set(regions).issubset(REGIONS) or delivery_mode not in ("immediate", "daily_digest"):
        raise HTTPException(422, "Invalid preferences")
    if timezone not in available_timezones():
        raise HTTPException(422, "Invalid timezone")
    pref = user.preference
    pref.program_types, pref.regions = json.dumps(program_types), json.dumps(regions)
    pref.start_terms = json.dumps([term.strip() for term in start_terms if term.strip()])
    pref.delivery_mode, pref.digest_time, pref.timezone, pref.status = delivery_mode, __import__("datetime").time.fromisoformat(digest_time), timezone, "draft"
    db.commit()
    offers = matching_offers(db, pref)
    return templates.TemplateResponse(request, "preview.html", context(request, db, user, preference=pref, offers=offers))


@app.post("/preferences/activate")
def activate(request: Request, csrf_token: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    count = activate_preference(db, user.preference)
    return RedirectResponse(f"/dashboard?activated={count}", 303)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    return templates.TemplateResponse(request, "admin.html", context(request, db, admin, users=users))


@app.post("/admin/invite")
def invite(request: Request, email: str = Form(...), csrf_token: str = Form(...), admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    try:
        normalized = validate_email(email, check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise HTTPException(422, str(exc))
    user = db.scalar(select(User).where(User.email == normalized))
    if not user:
        user = User(email=normalized)
        db.add(user); db.flush(); db.add(Preference(user_id=user.id))
    else:
        user.is_active = True
    db.add(Invitation(email=normalized, invited_by_id=admin.id))
    raw = new_token()
    db.add(MagicLink(user_id=user.id, token_hash=token_hash(raw), expires_at=expires_in(15)))
    db.commit()
    try:
        send_magic_link(user.email, f"{settings.app_url}/auth/consume/{raw}")
    except Exception as exc:
        print(f"Invitation delivery failed: {exc}")
    return RedirectResponse("/admin", 303)


@app.post("/admin/users/{user_id}/toggle")
def toggle_user(user_id: int, request: Request, csrf_token: str = Form(...), admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    target = db.get(User, user_id)
    if not target or target.id == admin.id:
        raise HTTPException(400)
    target.is_active = not target.is_active
    db.commit()
    return RedirectResponse("/admin", 303)


@app.get("/notion/connect")
def notion_connect(user: User = Depends(require_user)):
    if not settings.notion_client_id:
        raise HTTPException(503, "Notion OAuth is not configured")
    return RedirectResponse(oauth_url(signer.dumps({"user_id": user.id})), 303)


@app.get("/notion/callback")
def notion_callback(code: str, state: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    try:
        data = signer.loads(state, max_age=600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(400, "Invalid OAuth state")
    if data.get("user_id") != user.id:
        raise HTTPException(403)
    save_connection(db, user.id, exchange_code(code))
    return RedirectResponse("/notion/setup", 303)


@app.get("/notion/setup", response_class=HTMLResponse)
def notion_setup(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not user.notion:
        return RedirectResponse("/dashboard", 303)
    try:
        pages = accessible_pages(user.notion)
    except Exception as exc:
        user.notion.last_error = str(exc); db.commit(); pages = []
    return templates.TemplateResponse(request, "notion_setup.html", context(request, db, user, pages=pages))


@app.post("/notion/setup")
def notion_create(request: Request, page_id: str = Form(...), csrf_token: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    create_offer_database(db, user.notion, page_id)
    for offer in matching_offers(db, user.preference):
        if not db.scalar(select(NotionSync).where(NotionSync.connection_id == user.notion.id, NotionSync.offer_id == offer.id)):
            db.add(NotionSync(connection_id=user.notion.id, offer_id=offer.id))
    db.commit()
    return RedirectResponse("/dashboard", 303)


@app.post("/notion/disconnect")
def notion_disconnect(request: Request, csrf_token: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    csrf(request, db, csrf_token)
    if user.notion:
        db.delete(user.notion); db.commit()
    return RedirectResponse("/dashboard", 303)

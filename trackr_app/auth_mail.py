from .runtime import guarded
"""Durable auth delivery with short leases and fresh, persisted tokens per attempt."""
from datetime import timedelta
import time

from sqlalchemy import select, or_, update, case
from .config import settings
from .emailing import send_magic_link, send_password_link
from .models import AuthMail, Invitation, MagicLink, PasswordToken, User, utcnow
from .operations import error_code, next_retry
from .security import decrypt, encrypt, expires_in, new_token, token_hash
from .sessions import aware

LEASE = timedelta(minutes=5)


def enqueue(db, email, kind, invitation_id=None):
    job = AuthMail(email_encrypted=encrypt(email), email_hash=token_hash(email),
                   kind=kind, invitation_id=invitation_id)
    db.add(job)
    db.flush()
    return job


def _locked(db, job_id):
    # The global lock order is user -> job -> invitation, including revocation.
    job = db.get(AuthMail, job_id)
    if not job:
        return None, None
    email = decrypt(job.email_encrypted)
    user = db.scalar(select(User).where(User.email == email).with_for_update()
                     .execution_options(populate_existing=True))
    db.refresh(job, with_for_update=True)
    return job, user


def _invitation(db, job):
    return db.get(Invitation, job.invitation_id, populate_existing=True, with_for_update=True) if job.invitation_id else None


def _cancelled(job, user, invitation):
    return (not user or not user.is_active or
            (job.invitation_id and (not invitation or invitation.accepted_at or invitation.delivery_status == 'cancelled')) or
            (not job.invitation_id and aware(job.created_at) < utcnow() - timedelta(hours=1)))


@guarded(auth=True)
def process_auth_mail(db, job_id, sender=None):
    job, user = _locked(db, job_id)
    if not job:
        return False
    now = utcnow()
    stale = job.status == 'processing' and (not job.processing_started_at or aware(job.processing_started_at) <= now - LEASE)
    if job.status != 'pending' and not stale:
        db.commit()
        return False
    if job.next_attempt_at and aware(job.next_attempt_at) > now:
        db.commit()
        return False
    invitation = _invitation(db, job)
    if _cancelled(job, user, invitation):
        job.status = 'cancelled'
        if invitation and not invitation.accepted_at:
            invitation.delivery_status = 'cancelled'
        db.commit()
        return False
    if job.attempts >= 5:
        job.status = 'failed'
        job.last_error = job.last_error or 'DeliveryLeaseExhausted'
        if invitation:
            invitation.delivery_status = 'failed'
            invitation.last_error, invitation.attempts = job.last_error, job.attempts
        db.commit()
        return False
    raw = new_token()
    model = PasswordToken if job.kind == 'password' else MagicLink
    db.add(model(user_id=user.id, token_hash=token_hash(raw), expires_at=expires_in(15)))
    job.status, job.processing_started_at = 'processing', now
    job.attempts += 1
    attempt = job.attempts
    # Never deliver a token that can disappear in the first commit failure.
    db.commit()

    job, user = _locked(db, job_id)
    invitation = _invitation(db, job)
    if job.status != 'processing' or (job.attempts != attempt or not job.processing_started_at or aware(job.processing_started_at) != now) or _cancelled(job, user, invitation):
        if job.status == 'processing' and job.attempts == attempt:
            job.status = 'cancelled'
        db.commit()
        return False
    path = '/auth/password/reset/' if job.kind == 'password' else '/auth/consume/'
    recipient, kind = user.email, job.kind
    # Release all user/job locks and the connection before contacting SMTP.
    # Revocation after this check cannot recall an email, but deletes its token.
    db.commit()
    failure = None
    try:
        (sender or (send_password_link if kind == 'password' else send_magic_link))(
            recipient, settings.app_url + path + raw)
    except Exception as exc:
        # SMTP may have accepted the email: retain its already persisted token.
        failure = error_code(exc)

    job, user = _locked(db, job_id)
    if not job or job.status != 'processing' or (job.attempts != attempt or not job.processing_started_at or aware(job.processing_started_at) != now):
        db.commit()
        return False
    invitation = _invitation(db, job)
    if _cancelled(job, user, invitation):
        job.status = 'cancelled'
    else:
        job.status = ('failed' if attempt >= 5 else 'pending') if failure else 'sent'
        job.last_error = failure
        job.next_attempt_at = next_retry(attempt) if failure else None
    job.processing_started_at = None
    if invitation:
        invitation.delivery_status = job.status
        invitation.attempts = attempt if failure else max(0, attempt - 1)
        invitation.last_error, invitation.next_attempt_at = job.last_error, job.next_attempt_at
    if job.status == 'sent':
        from .maintenance import completed
        completed(db, AuthMail, job.id)
    sent = job.status == 'sent'
    db.commit()
    return sent


@guarded(auth=True)
def process_auth_queue(db, deadline=None):
    deadline = deadline or time.monotonic() + 90
    now = utcnow()
    ids = db.scalars(select(AuthMail.id).where(
        or_(AuthMail.status == 'pending',
            (AuthMail.status == 'processing') & (AuthMail.processing_started_at <= now - LEASE)),
        or_(AuthMail.next_attempt_at.is_(None), AuthMail.next_attempt_at <= now)
    ).order_by(AuthMail.invitation_id.is_not(None), AuthMail.id).limit(100)).all()
    db.commit()
    count = 0
    for job_id in ids:
        if time.monotonic() >= deadline:
            break
        try:
            count += process_auth_mail(db, job_id)
        except Exception as exc:
            db.rollback()
            from cryptography.fernet import InvalidToken
            if isinstance(exc, InvalidToken):
                db.execute(update(AuthMail).where(AuthMail.id == job_id,
                    AuthMail.status.in_(['pending', 'processing'])).values(
                    status='failed', last_error='InvalidToken', processing_started_at=None))
                invitation_id = db.scalar(select(AuthMail.invitation_id).where(AuthMail.id == job_id))
                if invitation_id:
                    db.execute(update(Invitation).where(Invitation.id == invitation_id, Invitation.accepted_at.is_(None),
                        Invitation.delivery_status.in_(['pending', 'processing'])).values(delivery_status='failed', last_error='InvalidToken'))
                db.commit()
            else:
                # A claim still in progress belongs to its attempt until expiry.
                # Only a pending job may acquire a new backoff here.
                try:
                    db.execute(update(AuthMail).where(AuthMail.id == job_id,
                        AuthMail.status == 'pending').values(
                        attempts=AuthMail.attempts + 1,
                        status=case((AuthMail.attempts >= 4, 'failed'), else_='pending'),
                        last_error=error_code(exc), next_attempt_at=next_retry(5)))
                    db.commit()
                except Exception:
                    db.rollback()
                print(f'Auth job deferred: {error_code(exc)}')
    return count

"""Invitation delivery uses the same durable queue as public auth requests."""
import time
from sqlalchemy import select, or_
from .auth_mail import enqueue, process_auth_mail
from .emailing import send_magic_link
from .models import AuthMail, Invitation, User, utcnow


def deliver_invitation(db, invitation_id, sender=None):
    invitation = db.get(Invitation, invitation_id)
    if not invitation:
        return False
    user = db.scalar(select(User).where(User.email == invitation.email).with_for_update()
                     .execution_options(populate_existing=True))
    job = db.scalar(select(AuthMail).where(AuthMail.invitation_id == invitation_id).with_for_update())
    db.refresh(invitation, with_for_update=True)
    if not user or not user.is_active:
        invitation.delivery_status = 'cancelled'
        if job:
            job.status = 'cancelled'
        db.commit()
        return False
    if invitation.accepted_at or invitation.delivery_status != 'pending':
        db.commit()
        return False
    if not job:
        job = enqueue(db, user.email, 'magic', invitation_id)
    elif job.status in ('failed', 'sent', 'cancelled'):
        # An explicit administrative resend/retry resets the existing outbox row.
        job.status, job.attempts, job.next_attempt_at = 'pending', 0, None
    else:
        job.next_attempt_at = invitation.next_attempt_at
    job_id = job.id
    db.commit()
    return process_auth_mail(db, job_id, sender=sender or send_magic_link)


def process_invitations(db):
    deadline = time.monotonic() + 90
    ids = db.scalars(select(Invitation.id).where(Invitation.delivery_status == 'pending',
        Invitation.accepted_at.is_(None), or_(Invitation.next_attempt_at.is_(None),
        Invitation.next_attempt_at <= utcnow())).order_by(Invitation.id).limit(20)).all()
    db.commit()
    count = 0
    for invitation_id in ids:
        if time.monotonic() >= deadline:
            break
        try:
            count += deliver_invitation(db, invitation_id)
        except Exception as exc:
            db.rollback()
            from .operations import error_code, next_retry
            from sqlalchemy import update, case
            db.execute(update(Invitation).where(Invitation.id == invitation_id,
                Invitation.delivery_status == 'pending').values(
                attempts=Invitation.attempts + 1,
                delivery_status=case((Invitation.attempts >= 4, 'failed'), else_='pending'),
                last_error=error_code(exc), next_attempt_at=next_retry(5)))
            db.commit()
    return count

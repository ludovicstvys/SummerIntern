"""Durable invitations; each retry generates a fresh 15-minute sign-in link."""
from sqlalchemy import select, or_
from .config import settings
from .emailing import send_magic_link
from .models import Invitation, MagicLink, User, utcnow
from .security import new_token, token_hash, expires_in
from .operations import error_code, next_retry
import time


def deliver_invitation(db, invitation_id, sender=None):
    invitation = db.get(Invitation, invitation_id)
    if not invitation:
        return False
    user = db.scalar(select(User).where(User.email == invitation.email).with_for_update().execution_options(populate_existing=True))
    db.refresh(invitation, with_for_update=True)
    if not user or not user.is_active:
        invitation.delivery_status = 'cancelled'; db.commit()
        return False
    if invitation.accepted_at or invitation.delivery_status != 'pending':
        db.commit()
        return False
    raw = new_token()
    link = MagicLink(user_id=user.id, token_hash=token_hash(raw), expires_at=expires_in(15))
    db.add(link)
    try:
        (sender or send_magic_link)(user.email, f'{settings.app_url}/auth/consume/{raw}')
        invitation.delivery_status, invitation.last_error = 'sent', None
        invitation.next_attempt_at = None
    except Exception as exc:
        db.expunge(link)
        invitation.attempts += 1
        invitation.last_error = error_code(exc)
        invitation.delivery_status = 'failed' if invitation.attempts >= 5 else 'pending'
        invitation.next_attempt_at = next_retry(invitation.attempts)
    db.commit()
    return invitation.delivery_status == 'sent'


def process_invitations(db):
    deadline = time.monotonic() + 90
    ids = db.scalars(select(Invitation.id).where(Invitation.delivery_status == 'pending', Invitation.accepted_at.is_(None), or_(Invitation.next_attempt_at.is_(None), Invitation.next_attempt_at <= utcnow())).order_by(Invitation.id).limit(20)).all()
    db.commit()
    completed = 0
    for invitation_id in ids:
        if time.monotonic() >= deadline:
            break
        completed += deliver_invitation(db, invitation_id)
    return completed

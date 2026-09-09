from .runtime import guarded
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update, case
from sqlalchemy.orm import Session

from .emailing import offer_email_html, send_email
from .models import Delivery, Offer, Preference, User, utcnow
from .notion import process_notion_queue
from .preferences import digest_is_due, offer_matches
from .preferences import offer_is_open
from .operations import error_code, next_retry
import time


def _send_group(db: Session, user: User, deliveries: list[Delivery], label: str) -> int:
    offers = [db.get(Offer, item.offer_id) for item in deliveries]
    offers = [offer for offer in offers if offer]
    if not offers:
        return False
    claims = [(item.id, item.processing_started_at) for item in deliveries]
    recipient = user.email
    key = f"alerts-{user.id}-{label}-" + "-".join(str(item.offer_id) for item in deliveries)
    subject = f"{len(offers)} new internship alert(s)"
    html = offer_email_html(offers, "New internship opportunities")
    # The user lock protects eligibility and claim creation, never network I/O.
    db.commit()
    failure, message_id = None, None
    try:
        message_id = send_email(recipient, subject, html, key)
    except Exception as exc:
        failure = error_code(exc)
    active_user = db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    completed = 0
    for identity, started_at in claims:
        item = db.scalar(select(Delivery).where(Delivery.id == identity,
            Delivery.status == 'processing', Delivery.processing_started_at == started_at)
            .with_for_update().execution_options(populate_existing=True))
        if not item:
            continue  # Revocation or a newer lease owns this row now.
        if not active_user or not active_user.is_active:
            item.status, item.processing_started_at = 'cancelled', None
            continue
        if failure:
            item.last_error = failure
            item.next_attempt_at = next_retry(item.attempts)
            item.status = 'failed' if item.attempts >= 5 else 'pending'
        else:
            item.status, item.provider_message_id, item.sent_at = 'sent', message_id, utcnow()
            item.last_error, item.next_attempt_at = None, None
            item.attempts = max(0, item.attempts - 1)
            completed += 1
        item.processing_started_at = None
    return completed


def _available():
    return or_((Delivery.status == "pending") & or_(Delivery.next_attempt_at.is_(None), Delivery.next_attempt_at <= utcnow()), (Delivery.status == "processing") & or_(Delivery.processing_started_at.is_(None), Delivery.processing_started_at < utcnow() - timedelta(minutes=15)))


def _claim(db: Session, deliveries: list[Delivery]) -> list[Delivery]:
    """Reserve under the user lock; the sender commits before network I/O."""
    claimed = []
    for delivery in deliveries:
        result = db.execute(update(Delivery).where(Delivery.id == delivery.id, Delivery.attempts < 5, _available()).values(status="processing", processing_started_at=utcnow(), attempts=Delivery.attempts + 1).execution_options(synchronize_session="fetch"))
        if result.rowcount == 1:
            claimed.append(delivery)
    return claimed


def _locked_user(db, user_id):
    user = db.scalar(select(User).where(User.id == user_id).with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if user and db.scalar(select(Delivery.id).where(Delivery.user_id == user_id,
            Delivery.status == 'processing', Delivery.processing_started_at >= utcnow() - timedelta(minutes=15)).limit(1)):
        return None
    return user


def _eligible(db, user, preference, deliveries):
    eligible = []
    for delivery in deliveries:
        offer = db.get(Offer, delivery.offer_id, populate_existing=True)
        if not user.is_active or not preference or preference.status != "active" or delivery.mode != preference.delivery_mode or not offer or not offer_is_open(offer) or not offer_matches(offer, preference):
            delivery.status = "cancelled"
            delivery.processing_started_at = None
        else:
            eligible.append(delivery)
    db.flush()
    return eligible


def _user_ids(db, mode, deadline):
    # Keyset batches bound memory even with a large subscriber base.
    cursor = 0
    db.execute(update(Delivery).where(Delivery.mode == mode, Delivery.status == 'processing',
        Delivery.attempts >= 5, _available()).values(status='failed',
        last_error='DeliveryLeaseExhausted', processing_started_at=None))
    db.commit()
    while time.monotonic() < deadline:
        ids = db.scalars(select(Delivery.user_id).where(Delivery.mode == mode,
            Delivery.user_id > cursor, Delivery.attempts < 5, _available())
            .distinct().order_by(Delivery.user_id).limit(100)).all()
        db.commit()
        if not ids:
            return
        yield from ids
        cursor = ids[-1]


@guarded(auth=False)
def process_immediate_alerts(db: Session) -> int:
    deadline = time.monotonic() + 90
    user_ids = _user_ids(db, "immediate", deadline)
    sent = 0
    for user_id in user_ids:
        if time.monotonic() >= deadline:
            break
        identities = []
        try:
            user = _locked_user(db, user_id)
            if not user:
                db.rollback()
                continue
            pref = db.scalar(select(Preference).where(Preference.user_id == user_id).execution_options(populate_existing=True))
            pending = db.scalars(select(Delivery).where(Delivery.user_id == user_id, Delivery.mode == "immediate", Delivery.attempts < 5, _available()).order_by(Delivery.id).limit(100).execution_options(populate_existing=True)).all()
            identities = [item.id for item in pending]
            claimed = _claim(db, _eligible(db, user, pref, pending))
            # At most 100 offers in this message; remaining rows stay durable.
            if claimed:
                sent += _send_group(db, user, claimed, "immediate")
            db.commit()
        except Exception as exc:
            _defer_group(db, identities, exc)
    return sent


@guarded(auth=False)
def process_digests(db: Session) -> int:
    deadline = time.monotonic() + 90
    user_ids = _user_ids(db, "daily_digest", deadline)
    sent = 0
    for user_id in user_ids:
        if time.monotonic() >= deadline:
            break
        identities = []
        try:
            user = _locked_user(db, user_id)
            if not user:
                db.rollback()
                continue
            preference = db.scalar(select(Preference).where(Preference.user_id == user_id).execution_options(populate_existing=True))
            if not user.is_active or not preference or preference.status != "active" or preference.delivery_mode != "daily_digest" or not digest_is_due(preference):
                db.commit()
                continue
            pending = db.scalars(select(Delivery).where(Delivery.user_id == user_id, Delivery.mode == "daily_digest", Delivery.attempts < 5, _available()).order_by(Delivery.id).limit(100).execution_options(populate_existing=True)).all()
            identities = [item.id for item in pending]
            claimed = _claim(db, _eligible(db, user, preference, pending))
            local_date = utcnow().astimezone(ZoneInfo(preference.timezone)).date()
            if claimed and _send_group(db, user, claimed, f"digest-{local_date.isoformat()}"):
                db.refresh(preference)
                remaining = db.scalar(select(Delivery.id).where(Delivery.user_id == user_id,
                    Delivery.mode == 'daily_digest', Delivery.status.in_(['pending', 'processing']),
                    Delivery.attempts < 5).limit(1))
                if not remaining:
                    preference.last_digest_date = local_date
                sent += 1
            # Do not consume the day's send slot when there is no work.
            db.commit()
        except Exception as exc:
            _defer_group(db, identities, exc)
    return sent


def sync_notion(db: Session) -> int:
    return process_notion_queue(db)


def _defer_group(db, identities, exc):
    db.rollback()
    if identities:
        db.execute(update(Delivery).where(Delivery.id.in_(identities), Delivery.status == 'pending').values(
            attempts=Delivery.attempts + 1, last_error=error_code(exc), next_attempt_at=next_retry(5),
            status=case((Delivery.attempts >= 4, 'failed'), else_='pending')))
        db.commit()
    print('Delivery group deferred: ' + error_code(exc))

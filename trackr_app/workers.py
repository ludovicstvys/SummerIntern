from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .emailing import offer_email_html, send_email
from .models import Delivery, Offer, Preference, User, utcnow
from .notion import process_notion_queue
from .preferences import digest_is_due, offer_matches


def _send_group(db: Session, user: User, deliveries: list[Delivery], label: str) -> bool:
    offers = [db.get(Offer, item.offer_id) for item in deliveries]
    offers = [offer for offer in offers if offer]
    if not offers:
        return False
    key = f"alerts-{user.id}-{label}-" + "-".join(str(item.offer_id) for item in deliveries)
    try:
        message_id = send_email(user.email, f"{len(offers)} new internship alert(s)", offer_email_html(offers, "New internship opportunities"), key)
    except Exception as exc:
        for item in deliveries:
            item.attempts += 1
            item.last_error = type(exc).__name__
            item.status = "failed" if item.attempts >= 5 else "pending"
            item.processing_started_at = None
        return False
    for item in deliveries:
        item.status = "sent"
        item.provider_message_id = message_id
        item.sent_at = utcnow()
        item.processing_started_at = None
        item.last_error = None
    return True


def _available():
    return or_(Delivery.status == "pending", (Delivery.status == "processing") & (Delivery.processing_started_at < utcnow() - timedelta(minutes=15)))


def _claim(db: Session, deliveries: list[Delivery]) -> list[Delivery]:
    """Reserve inside the user lock; commit only with the delivery outcome."""
    claimed = []
    for delivery in deliveries:
        result = db.execute(update(Delivery).where(Delivery.id == delivery.id, Delivery.attempts < 5, _available()).values(status="processing", processing_started_at=utcnow()).execution_options(synchronize_session="fetch"))
        if result.rowcount == 1:
            claimed.append(delivery)
    return claimed


def _locked_user(db, user_id):
    return db.scalar(select(User).where(User.id == user_id).with_for_update(skip_locked=True).execution_options(populate_existing=True))


def _eligible(db, user, preference, deliveries):
    eligible = []
    for delivery in deliveries:
        offer = db.get(Offer, delivery.offer_id, populate_existing=True)
        if not user.is_active or not preference or preference.status != "active" or delivery.mode != preference.delivery_mode or not offer or not offer.is_open or not offer_matches(offer, preference):
            delivery.status = "cancelled"
            delivery.processing_started_at = None
        else:
            eligible.append(delivery)
    db.flush()
    return eligible


def process_immediate_alerts(db: Session) -> int:
    user_ids = db.scalars(select(Delivery.user_id).where(Delivery.mode == "immediate", Delivery.attempts < 5, _available()).distinct()).all()
    db.commit()
    sent = 0
    for user_id in user_ids:
        user = _locked_user(db, user_id)
        if not user:
            db.rollback()
            continue
        pref = db.scalar(select(Preference).where(Preference.user_id == user_id).execution_options(populate_existing=True))
        pending = db.scalars(select(Delivery).where(Delivery.user_id == user_id, Delivery.mode == "immediate", Delivery.attempts < 5, _available()).order_by(Delivery.id).execution_options(populate_existing=True)).all()
        claimed = _claim(db, _eligible(db, user, pref, pending))
        # One bounded SMTP call per user, while the user lock prevents changes.
        if claimed and _send_group(db, user, claimed, "immediate"):
            sent += len(claimed)
        db.commit()
    return sent


def process_digests(db: Session) -> int:
    user_ids = db.scalars(select(Preference.user_id).where(Preference.status == "active", Preference.delivery_mode == "daily_digest")).all()
    db.commit()
    sent = 0
    for user_id in user_ids:
        user = _locked_user(db, user_id)
        if not user:
            db.rollback()
            continue
        preference = db.scalar(select(Preference).where(Preference.user_id == user_id).execution_options(populate_existing=True))
        if not user.is_active or preference.status != "active" or preference.delivery_mode != "daily_digest" or not digest_is_due(preference):
            db.commit()
            continue
        pending = db.scalars(select(Delivery).where(Delivery.user_id == user_id, Delivery.mode == "daily_digest", Delivery.attempts < 5, _available()).order_by(Delivery.id).execution_options(populate_existing=True)).all()
        claimed = _claim(db, _eligible(db, user, preference, pending))
        local_date = utcnow().astimezone(ZoneInfo(preference.timezone)).date()
        if claimed and _send_group(db, user, claimed, f"digest-{local_date.isoformat()}"):
            preference.last_digest_date = local_date
            sent += 1
        # Do not consume the day's send slot when there is no work.
        db.commit()
    return sent


def sync_notion(db: Session) -> int:
    return process_notion_queue(db)

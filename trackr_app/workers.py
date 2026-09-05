from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .emailing import offer_email_html, send_email
from .models import Delivery, Offer, Preference, User, utcnow
from .notion import process_notion_queue
from .preferences import digest_is_due


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
            item.last_error = str(exc)[:2000]
            item.status = "failed" if item.attempts >= 5 else "pending"
        db.commit()
        return False
    for item in deliveries:
        item.status = "sent"
        item.provider_message_id = message_id
        item.sent_at = utcnow()
        item.last_error = None
    db.commit()
    return True


def process_immediate_alerts(db: Session) -> int:
    pending = db.scalars(select(Delivery).where(Delivery.mode == "immediate", Delivery.status == "pending", Delivery.attempts < 5)).all()
    sent = 0
    for delivery in pending:
        user = db.get(User, delivery.user_id)
        if user and user.is_active and _send_group(db, user, [delivery], f"immediate-{delivery.id}"):
            sent += 1
    return sent


def process_digests(db: Session) -> int:
    preferences = db.scalars(select(Preference).where(Preference.status == "active", Preference.delivery_mode == "daily_digest")).all()
    sent = 0
    for preference in preferences:
        if not digest_is_due(preference):
            continue
        pending = db.scalars(select(Delivery).where(Delivery.user_id == preference.user_id, Delivery.mode == "daily_digest", Delivery.status == "pending", Delivery.attempts < 5)).all()
        if pending and _send_group(db, preference.user, pending, f"digest-{utcnow().date().isoformat()}"):
            sent += 1
    return sent


def sync_notion(db: Session) -> int:
    return process_notion_queue(db)

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Delivery, NotionSync, Offer, Preference, User, UserOffer, utcnow
from .config import settings
from trackr_common import dates_are_open

PROGRAM_TYPES = (
    "summer",
    "off-cycle",
    "spring-weeks",
    "industrial-placements",
    "graduate-programmes",
    "events",
)
REGIONS = ("France", "UK", "Hong Kong")


def json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def infer_start_term(categories: list[str]) -> str | None:
    for category in categories:
        if re.search(r"\bQ[1-4]\s+Start\b", category, re.I):
            return category.strip()
    return None


def offer_matches(offer: Offer, preference: Preference) -> bool:
    terms = json_list(preference.start_terms)
    sources = offer.sources or [offer]
    return any(
        source.region in json_list(preference.regions)
        and source.programme_type in json_list(preference.program_types)
        and (source is offer or (source.is_open and source.season == settings.season))
        and dates_are_open(source.opening_date, source.closing_date)
        and (source.programme_type != 'off-cycle' or not terms or source.start_term in terms)
        for source in sources
    )


def offer_is_open(offer):
    if offer.sources:
        return offer.is_open and any(s.is_open and s.season == settings.season and dates_are_open(s.opening_date, s.closing_date) for s in offer.sources)
    return offer.is_open and dates_are_open(offer.opening_date, offer.closing_date)


def matching_offers(db: Session, preference: Preference) -> list[Offer]:
    return [offer for offer in db.scalars(select(Offer).where(Offer.is_open.is_(True))).all() if offer_is_open(offer) and offer_matches(offer, preference)]


def activate_preference(db: Session, preference: Preference, commit=True) -> int:
    preference.status = "active"
    preference.activated_at = utcnow()
    # Keep already delivered history; reconcile only unfinished alerts.
    deliveries = db.scalars(select(Delivery).where(Delivery.user_id == preference.user_id)).all()
    sent_offers = {item.offer_id for item in deliveries if item.status == "sent"}
    by_mode = {(item.offer_id, item.mode): item for item in deliveries}
    for item in deliveries:
        if item.status not in ("pending", "processing", "failed", "cancelled"):
            continue
        offer = db.get(Offer, item.offer_id)
        if item.offer_id in sent_offers or not offer or not offer_is_open(offer) or not offer_matches(offer, preference):
            item.status = "cancelled"
            item.processing_started_at = None
        elif item.mode != preference.delivery_mode:
            previous_status = item.status
            item.status = "cancelled"
            item.processing_started_at = None
            target = by_mode.get((item.offer_id, preference.delivery_mode))
            if target is None:
                target = Delivery(user_id=preference.user_id, offer_id=item.offer_id, mode=preference.delivery_mode, attempts=item.attempts, status="failed" if previous_status == "failed" else "pending")
                db.add(target)
                by_mode[(item.offer_id, preference.delivery_mode)] = target
            elif target.status == "cancelled":
                target.status = "failed" if target.attempts >= 5 else "pending"
        elif item.status == 'cancelled':
            item.status = 'failed' if item.attempts >= 5 else 'pending'
            item.next_attempt_at = None
    offers = matching_offers(db, preference)
    for offer in offers:
        existing = db.scalar(select(UserOffer).where(UserOffer.user_id == preference.user_id, UserOffer.offer_id == offer.id))
        if not existing:
            db.add(UserOffer(user_id=preference.user_id, offer_id=offer.id, baseline=True))
        if preference.user.notion and preference.user.notion.data_source_id:
            queued = db.scalar(select(NotionSync).where(NotionSync.connection_id == preference.user.notion.id, NotionSync.offer_id == offer.id))
            if not queued:
                db.add(NotionSync(connection_id=preference.user.notion.id, offer_id=offer.id))
            elif queued.status == 'cancelled':
                queued.status = 'failed' if queued.attempts >= 5 else 'pending'
    if commit:
        db.commit()
    return len(offers)


def queue_new_offer(db: Session, offer: Offer) -> None:
    user_ids = db.scalars(select(Preference.user_id).where(Preference.status == 'active').order_by(Preference.user_id)).all()
    for user_id in user_ids:
        user = db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
        preference = db.scalar(select(Preference).where(Preference.user_id == user_id).execution_options(populate_existing=True))
        if not user.is_active or preference.status != 'active' or not offer_is_open(offer) or not offer_matches(offer, preference):
            continue
        matched = db.scalar(select(UserOffer).where(UserOffer.user_id == preference.user_id, UserOffer.offer_id == offer.id))
        if matched:
            continue
        db.add(UserOffer(user_id=preference.user_id, offer_id=offer.id, baseline=False))
        db.add(Delivery(user_id=preference.user_id, offer_id=offer.id, mode=preference.delivery_mode))
        if preference.user.notion and preference.user.notion.data_source_id:
            if not db.scalar(select(NotionSync).where(NotionSync.connection_id == preference.user.notion.id, NotionSync.offer_id == offer.id)):
                db.add(NotionSync(connection_id=preference.user.notion.id, offer_id=offer.id))


def queue_notion_update(db: Session, offer: Offer) -> None:
    """Requeue existing personal Notion pages when upstream metadata changes."""
    matches = db.scalars(select(UserOffer).where(UserOffer.offer_id == offer.id)).all()
    for match in matches:
        preference = db.scalar(select(Preference).where(Preference.user_id == match.user_id))
        if not preference or not preference.user.is_active or not preference.user.notion or not preference.user.notion.data_source_id:
            continue
        sync = db.scalar(select(NotionSync).where(NotionSync.connection_id == preference.user.notion.id, NotionSync.offer_id == offer.id).with_for_update().execution_options(populate_existing=True))
        if sync:
            sync.status = "pending"
            sync.attempts = 0
        else:
            db.add(NotionSync(connection_id=preference.user.notion.id, offer_id=offer.id))


def digest_is_due(preference: Preference, now: datetime | None = None) -> bool:
    now = now or utcnow()
    try:
        local = now.astimezone(ZoneInfo(preference.timezone))
    except ZoneInfoNotFoundError:
        return False
    if preference.last_digest_date == local.date():
        return False
    return local.time().replace(tzinfo=None) >= preference.digest_time

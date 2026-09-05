import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Delivery, NotionSync, Offer, Preference, UserOffer, utcnow

PROGRAM_TYPES = ("summer", "off-cycle")
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
    if offer.programme_type not in json_list(preference.program_types):
        return False
    if offer.region not in json_list(preference.regions):
        return False
    terms = json_list(preference.start_terms)
    return offer.programme_type != "off-cycle" or not terms or offer.start_term in terms


def matching_offers(db: Session, preference: Preference) -> list[Offer]:
    return [offer for offer in db.scalars(select(Offer).where(Offer.is_open.is_(True))).all() if offer_matches(offer, preference)]


def activate_preference(db: Session, preference: Preference) -> int:
    preference.status = "active"
    preference.activated_at = utcnow()
    offers = matching_offers(db, preference)
    for offer in offers:
        existing = db.scalar(select(UserOffer).where(UserOffer.user_id == preference.user_id, UserOffer.offer_id == offer.id))
        if not existing:
            db.add(UserOffer(user_id=preference.user_id, offer_id=offer.id, baseline=True))
        if preference.user.notion and preference.user.notion.data_source_id:
            queued = db.scalar(select(NotionSync).where(NotionSync.connection_id == preference.user.notion.id, NotionSync.offer_id == offer.id))
            if not queued:
                db.add(NotionSync(connection_id=preference.user.notion.id, offer_id=offer.id))
    db.commit()
    return len(offers)


def queue_new_offer(db: Session, offer: Offer) -> None:
    preferences = db.scalars(select(Preference).where(Preference.status == "active")).all()
    for preference in preferences:
        if not preference.user.is_active or not offer_matches(offer, preference):
            continue
        matched = db.scalar(select(UserOffer).where(UserOffer.user_id == preference.user_id, UserOffer.offer_id == offer.id))
        if matched:
            continue
        db.add(UserOffer(user_id=preference.user_id, offer_id=offer.id, baseline=False))
        db.add(Delivery(user_id=preference.user_id, offer_id=offer.id, mode=preference.delivery_mode))
        if preference.user.notion and preference.user.notion.data_source_id:
            db.add(NotionSync(connection_id=preference.user.notion.id, offer_id=offer.id))


def queue_notion_update(db: Session, offer: Offer) -> None:
    """Requeue existing personal Notion pages when upstream metadata changes."""
    matches = db.scalars(select(UserOffer).where(UserOffer.offer_id == offer.id)).all()
    for match in matches:
        preference = db.scalar(select(Preference).where(Preference.user_id == match.user_id))
        if not preference or not preference.user.notion or not preference.user.notion.data_source_id:
            continue
        sync = db.scalar(select(NotionSync).where(NotionSync.connection_id == preference.user.notion.id, NotionSync.offer_id == offer.id))
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
    target = preference.digest_time
    return local.hour == target.hour and local.minute == target.minute

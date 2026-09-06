import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from trackr_common import canonical_offer_url, deduplicate_offers, scrape_open_programmes

from .config import settings
from .models import Offer, utcnow
from .preferences import infer_start_term, queue_new_offer, queue_notion_update
from .notion import offer_properties

TRACKERS = [
    {"region": region, "industry": "Finance", "season": settings.season, "type": api_type}
    for region in ("France", "UK", "Hong Kong")
    for api_type in ("summer-internships", "off-cycle-internships")
]


def _date(value):
    return date.fromisoformat(value) if value else None


def scrape_all(db: Session) -> dict[str, int]:
    seen_urls: set[str] = set()
    created = updated = closed = failed = 0
    for params in TRACKERS:
        try:
            raw = deduplicate_offers(scrape_open_programmes(params))
            if not raw:
                raise RuntimeError("Tracker returned no usable open offers")
            for item in raw:
                if not item.get("name") or not canonical_offer_url(item.get("offer_url")):
                    raise ValueError("Incomplete offer in tracker response")
                _date(item.get("opening_date"))
                _date(item.get("closing_date"))
        except Exception as exc:
            print(f"Tracker failed for {params['region']} {params['type']}: {exc}")
            failed += 1
            continue
        programme_type = "summer" if params["type"] == "summer-internships" else "off-cycle"
        tracker_seen: set[str] = set()
        for item in raw:
            canonical = canonical_offer_url(item["offer_url"])
            if not canonical:
                continue
            seen_urls.add(canonical)
            tracker_seen.add(canonical)
            offer = db.scalar(select(Offer).where(Offer.canonical_url == canonical))
            is_new = offer is None
            if is_new:
                offer = Offer(canonical_url=canonical, offer_url=item["offer_url"], name=item["name"], region=params["region"], programme_type=programme_type)
                db.add(offer)
                created += 1
            else:
                updated += 1
            before = offer_properties(offer) if not is_new else None
            categories = item.get("categories") or []
            offer.offer_url = item["offer_url"]
            offer.name = item["name"]
            offer.company = item.get("company") or ""
            offer.company_id = str(item.get("company_id") or "") or None
            offer.region = params["region"]
            offer.programme_type = programme_type
            offer.categories = json.dumps(categories)
            offer.start_term = infer_start_term(categories) if programme_type == "off-cycle" else None
            offer.opening_date = _date(item.get("opening_date"))
            offer.closing_date = _date(item.get("closing_date"))
            offer.stage = item.get("stage") or "Unknown"
            offer.rolling = bool(item.get("rolling"))
            offer.needs_cv = bool(item.get("needs_cv"))
            offer.needs_cover_letter = bool(item.get("needs_cover_letter"))
            offer.company_description = item.get("company_description")
            offer.notes = item.get("notes")
            offer.is_open = True
            offer.missing_collections = 0
            offer.last_seen_at = utcnow()
            db.flush()
            if is_new:
                queue_new_offer(db, offer)
            elif before != offer_properties(offer):
                queue_notion_update(db, offer)
        stale_offers = db.scalars(
            select(Offer).where(
                Offer.region == params["region"],
                Offer.programme_type == programme_type,
                Offer.is_open.is_(True),
                Offer.canonical_url.not_in(tracker_seen),
            )
        ).all()
        for offer in stale_offers:
            offer.missing_collections += 1
            if offer.missing_collections >= 2:
                offer.is_open = False
                queue_notion_update(db, offer)
                closed += 1
        db.commit()
    return {"created": created, "updated": updated, "closed": closed, "failed_trackers": failed, "seen": len(seen_urls)}

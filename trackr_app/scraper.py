import json
import time
import os
from .runtime import guarded
from .sources import reserve, owned
from .durable import enqueue_job, process_jobs
from datetime import date
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from trackr_common import canonical_offer_url, deduplicate_offers, scrape_open_programmes
from .config import settings
from .models import Offer, OfferSource, utcnow
from .preferences import infer_start_term, queue_new_offer, queue_notion_update
from .notion import offer_properties
from .operations import lock_state, error_code

TRACKERS = [
    {'region': region, 'industry': 'Finance', 'season': settings.season, 'type': kind}
    for region in ('France', 'UK', 'Hong Kong')
    for kind in ('summer-internships', 'off-cycle-internships')
] + [
    {
        'region': 'UK',
        'season': settings.season,
        'type': 'spring-weeks',
        'source_type': 'spring-weeks',
        'endpoint': 'https://api.the-trackr.com/spring-weeks',
        'page_url': 'https://app.the-trackr.com/uk-finance/spring-weeks',
    },
    *[
        {
            'region': region,
            'industry': 'Finance',
            'season': settings.season,
            'type': kind,
            'page_url': f"https://app.the-trackr.com/{region_slug}-finance/{kind}",
        }
        for region, region_slug, kind in (
            ('UK', 'uk', 'industrial-placements'),
            ('UK', 'uk', 'graduate-programmes'),
            ('UK', 'uk', 'events'),
            ('France', 'france', 'graduate-programmes'),
        )
    ],
]


def _date(value):
    return date.fromisoformat(value) if value else None


@guarded()
def scrape_all(db: Session) -> dict[str, int]:
    seen = set()
    totals = dict(created=0, updated=0, closed=0, failed_trackers=0)
    deadline = time.monotonic() + 90
    trackers = [TRACKERS[int(os.environ['TRACKR_SOURCE_INDEX'])]] if 'TRACKR_SOURCE_INDEX' in os.environ else TRACKERS
    for params in trackers:
        if time.monotonic() >= deadline:
            break
        key = '/'.join((params.get('season', settings.season), params['region'], params['type']))
        delta = dict(created=0, updated=0, closed=0)
        try:
            # Serialize overlapping collectors before fetching, so an older snapshot
            # cannot overwrite a newer one. SMTP workers use separate user locks.
            claim = reserve(db, 'platform/' + key)
            if claim is None:
                continue
            acquired = scrape_open_programmes(params)
            if getattr(acquired, 'complete', True) is False:
                raise RuntimeError('Incomplete source snapshot')
            raw = deduplicate_offers(acquired)
            # Applying a snapshot changes shared offers and delivery queues, so
            # writers remain serialized. Fetches still run in parallel; allow a
            # bounded wait for the preceding writer rather than treating the
            # normal two-second request lock timeout as a tracker outage.
            if db.bind.dialect.name == 'postgresql':
                db.execute(text("SET LOCAL lock_timeout = '30s'"))
            lock_state(db, 'scrape-lock')
            snapshot = owned(db, 'platform/' + key, claim)
            if snapshot is None:
                db.rollback(); continue
            state = lock_state(db, 'source/' + key)
            if not raw and not getattr(raw, 'complete', False):
                raise RuntimeError('Tracker returned an ambiguous empty snapshot')
            for item in raw:
                if not item.get('name') or not canonical_offer_url(item.get('offer_url')):
                    raise ValueError('Incomplete offer')
                _date(item.get('opening_date')); _date(item.get('closing_date'))
            kind = params['type']
            if kind == 'summer-internships':
                kind = 'summer'
            elif kind == 'off-cycle-internships':
                kind = 'off-cycle'
            season = params.get('season', settings.season)
            tracker_seen = set()
            # Backfill fixtures/local databases created before the migration.
            for offer in db.scalars(select(Offer).where(~Offer.sources.any())).all():
                offer.sources.append(OfferSource(region=offer.region, programme_type=offer.programme_type,
                    season=settings.season, start_term=offer.start_term, is_open=offer.is_open,
                    opening_date=offer.opening_date, closing_date=offer.closing_date,
                    missing_collections=offer.missing_collections, last_seen_at=offer.last_seen_at))
            db.flush()
            changed = {}
            for item in raw:
                canonical = canonical_offer_url(item['offer_url'])
                tracker_seen.add(canonical)
                offer = db.scalar(select(Offer).where(Offer.canonical_url == canonical))
                is_new = offer is None
                if is_new:
                    offer = Offer(canonical_url=canonical, offer_url=canonical, name=item['name'], region=params['region'], programme_type=kind)
                    db.add(offer)
                before = offer_properties(offer) if not is_new else None
                source = next((s for s in offer.sources if (s.region, s.programme_type, s.season) == (params['region'], kind, season)), None)
                if source is None:
                    source = OfferSource(region=params['region'], programme_type=kind, season=season)
                    offer.sources.append(source)
                categories = item.get('categories') or []
                source.start_term = infer_start_term(categories) if kind == 'off-cycle' else None
                source.opening_date, source.closing_date = _date(item.get('opening_date')), _date(item.get('closing_date'))
                source.is_open, source.missing_collections, source.last_seen_at = True, 0, utcnow()
                offer.offer_url, offer.name = canonical, item['name']
                offer.company = item.get('company') or ''
                offer.company_id = str(item.get('company_id') or '') or None
                offer.categories = json.dumps(categories)
                offer.start_term = source.start_term
                offer.opening_date, offer.closing_date = _date(item.get('opening_date')), _date(item.get('closing_date'))
                offer.stage = item.get('stage') or 'Unknown'
                for attr in ('rolling', 'needs_cv', 'needs_cover_letter'):
                    setattr(offer, attr, bool(item.get(attr)))
                offer.company_description, offer.notes = item.get('company_description'), item.get('notes')
                offer.is_open, offer.missing_collections, offer.last_seen_at = True, 0, utcnow()
                db.flush()
                changed[offer.id] = (offer, before)
                delta['created' if is_new else 'updated'] += 1
            sources = db.scalars(select(OfferSource).where(OfferSource.region == params['region'], OfferSource.programme_type == kind, OfferSource.season == season, OfferSource.is_open.is_(True))).all()
            for source in sources:
                offer = db.get(Offer, source.offer_id)
                if offer.canonical_url in tracker_seen:
                    continue
                before = offer_properties(offer)
                source.missing_collections += 1
                if source.missing_collections >= 2:
                    source.is_open = False
                offer.missing_collections = source.missing_collections
                was_open = offer.is_open
                offer.is_open = any(s.is_open and s.season == settings.season for s in offer.sources)
                if was_open and not offer.is_open:
                    delta['closed'] += 1
                changed[offer.id] = (offer, before)
            # Stable user locking order inside queue_new_offer. Re-evaluate existing
            # offers as well, but retain the unique user/offer delivery history.
            for offer, before in changed.values():
                enqueue_job(db, f'match-offer/{offer.id}', 'match', {'offer_id': offer.id,
                    'updated': before is not None and before != offer_properties(offer)})
            snapshot.payload, snapshot.updated_at = json.dumps(raw), utcnow()
            snapshot.lease_until, snapshot.last_error = None, None
            state.last_success_at, state.last_error = utcnow(), None
            db.commit()
            seen.update(tracker_seen)
            for name in delta:
                totals[name] += delta[name]
        except Exception as exc:
            db.rollback()
            snapshot = owned(db, 'platform/' + key, claim) if 'claim' in locals() and claim else None
            if snapshot is None and claim:
                db.rollback(); continue
            if snapshot:
                snapshot.lease_until, snapshot.last_error = None, error_code(exc)
            state = lock_state(db, 'source/' + key)
            state.last_error = error_code(exc)
            db.commit()
            totals['failed_trackers'] += 1
            print(f'Tracker failed for {key}: {error_code(exc)}')
    if 'TRACKR_SOURCE_INDEX' not in os.environ:
        while time.monotonic() < deadline and process_jobs(db, 'match', deadline):
            pass
    return {**totals, 'seen': len(seen)}

"""Read-only presentation and browsing of a user's opportunity matches."""
from datetime import date
from urllib.parse import urlencode

from .config import settings
from .models import utcnow
from .preferences import json_list, offer_is_open
from trackr_common import dates_are_open

PROGRAMME_LABELS = {
    'summer': 'Summer internship', 'off-cycle': 'Off-cycle internship',
    'spring-weeks': 'Spring Week', 'industrial-placements': 'Industrial placement',
    'graduate-programmes': 'Graduate programme', 'events': 'Event',
}
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
PAGE_SIZE = 20


def date_label(value):
    return f'{value.day} {MONTHS[value.month - 1]} {value.year}' if value else 'Not specified'


def source_open(source):
    return source.is_open and (not hasattr(source, 'season') or source.season == settings.season) and dates_are_open(source.opening_date, source.closing_date)


def relevant_sources(offer, preference=None, history=False):
    sources = offer.sources or [offer]
    if history:
        return list(sources)
    terms = json_list(preference.start_terms) if preference else []
    return [s for s in sources if source_open(s) and (not preference or (
        s.region in json_list(preference.regions)
        and s.programme_type in json_list(preference.program_types)
        and (s.programme_type != 'off-cycle' or not terms or s.start_term in terms)
    ))]


def source_matches(source, filters):
    return all(not filters[key] or getattr(source, attr) == filters[key] for key, attr in (
        ('region', 'region'), ('programme', 'programme_type'), ('start_term', 'start_term')))


def opportunity_card(offer, sources=None):
    sources = list(sources if sources is not None else relevant_sources(offer))
    today = utcnow().date()
    deadlines = [s.closing_date for s in sources if source_open(s) and s.closing_date and offer.is_open]
    deadline = min(deadlines, default=None)
    opened = offer_is_open(offer) and any(source_open(s) for s in sources)
    details = [{
        'region': s.region, 'programme': PROGRAMME_LABELS.get(s.programme_type, s.programme_type),
        'start_term': s.start_term, 'opening': date_label(s.opening_date),
        'closing': date_label(s.closing_date), 'season': getattr(s, 'season', None),
        'open': bool(offer.is_open and source_open(s)),
    } for s in sources]
    return {
        'offer': offer, 'sources': details, 'open': opened, 'deadline': deadline,
        'deadline_label': date_label(deadline),
        'soon': deadline is not None and 0 <= (deadline - today).days <= 7,
        'regions': sorted({s.region for s in sources}),
        'programmes': sorted({PROGRAMME_LABELS.get(s.programme_type, s.programme_type) for s in sources}),
        'start_terms': sorted({s.start_term for s in sources if s.start_term}),
        'multiple_sources': len(sources) > 1,
    }


def browse_opportunities(offers, preference, *, page=1, history=False, q='', region='', programme='', start_term='', sort='latest'):
    filters = dict(q=q.strip(), region=region.strip(), programme=programme.strip(), start_term=start_term.strip())
    sort = sort if sort in ('latest', 'closing') else 'latest'
    scoped = [(offer, relevant_sources(offer, preference, history)) for offer in offers]
    options = {key: sorted({getattr(s, attr) for _, sources in scoped for s in sources if getattr(s, attr)})
               for key, attr in (('region', 'region'), ('programme', 'programme_type'), ('start_term', 'start_term'))}
    # Retain a stale/bookmarked filter visibly, even if it currently has no matches.
    for key in options:
        if filters[key] and filters[key] not in options[key]:
            options[key].append(filters[key])
            options[key].sort()
    cards = []
    for offer, sources in scoped:
        if filters['q'].casefold() not in f'{offer.company} {offer.name}'.casefold():
            continue
        selected = [s for s in sources if source_matches(s, filters)]
        if selected:
            cards.append(opportunity_card(offer, selected))
    if sort == 'closing':
        # Python's stable sort preserves the existing latest-match order on ties.
        cards.sort(key=lambda card: card['deadline'] or date.max)
    total = len(cards)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    params = {**{k: v for k, v in filters.items() if v}, 'sort': sort, 'history': 'true' if history else 'false'}

    def feed_url(**changes):
        return '/dashboard?' + urlencode({**params, **changes})

    return dict(cards=cards[(page-1)*PAGE_SIZE:page*PAGE_SIZE], filters=filters, options=options,
                sort=sort, total=total, page=page, pages=pages, history=history,
                first=(page-1)*PAGE_SIZE+1 if total else 0, last=min(page*PAGE_SIZE, total),
                has_filters=any(filters.values()), feed_url=feed_url,
                clear_url='/dashboard?history=' + ('true' if history else 'false'),
                programme_labels=PROGRAMME_LABELS)

import csv
import os
import tempfile
from pathlib import Path
from datetime import datetime, date, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


TRACKR_API_URL = "https://api.the-trackr.com/programmes"


class OfferSnapshot(list):
    """A validated complete response, including a legitimately empty result."""
    complete = True


def smtp_password(value, server):
    # Gmail displays application passwords in groups, sometimes with NBSPs.
    return ''.join(value.split()) if server.lower() == 'smtp.gmail.com' else value


def dates_are_open(opening, closing, today=None):
    today = today or datetime.now(timezone.utc).date()
    opening = date.fromisoformat(opening) if isinstance(opening, str) else opening
    closing = date.fromisoformat(closing) if isinstance(closing, str) else closing
    return (opening is None or opening <= today) and (closing is None or closing >= today)

CSV_COLUMNS = [
    "Name",
    "Company",
    "Company ID",
    "Offer URL",
    "Region",
    "Categories",
    "Opening Date",
    "Closing Date",
    "Stage",
    "Rolling",
    "Needs CV",
    "Needs Cover Letter",
    "Company Description",
    "Notes",
]


def iso_to_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return None


def extract_trackr_items(data):
    if isinstance(data, dict):
        for key in ("programmes", "vacancies", "internships", "offers", "jobs", "data"):
            items = data.get(key)
            if isinstance(items, list):
                return items
    if isinstance(data, list):
        return data
    return []


def scrape_open_programmes(params):
    response = requests.get(TRACKR_API_URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    internships = extract_trackr_items(payload)
    # A paginated or explicitly partial response is not a complete snapshot.
    if isinstance(payload, dict):
        metadata = [payload] + [payload[key] for key in ("pagination", "meta") if isinstance(payload.get(key), dict)]
        for meta in metadata:
            if any(meta.get(key) for key in ("has_more", "hasMore", "next_cursor", "nextCursor", "next", "partial")):
                raise RuntimeError("Trackr returned a partial snapshot")
            total = meta.get("total", meta.get("totalCount"))
            if isinstance(total, int) and total > len(internships):
                raise RuntimeError("Trackr returned fewer programmes than its total")
    if any(not isinstance(item, dict) for item in internships):
        raise RuntimeError("Trackr returned malformed programmes")

    # Trackr occasionally answers successfully with an empty payload. Treating
    # that as a genuine "zero offers" result would erase the reference CSV and
    # make every offer look new on the following run.
    explicit_empty = isinstance(payload, dict) and any(
        meta.get('total', meta.get('totalCount')) == 0
        for meta in [payload] + [payload[k] for k in ('pagination', 'meta') if isinstance(payload.get(k), dict)]
    ) and any(isinstance(payload.get(k), list) for k in ('programmes', 'vacancies', 'internships', 'offers', 'jobs', 'data'))
    if not internships and not explicit_empty:
        raise RuntimeError(
            "Trackr returned no programmes; keeping the existing CSV unchanged"
        )

    open_offers = OfferSnapshot()
    for item in internships:
        if not isinstance(item, dict) or item.get("openingDate") is None:
            continue

        if not canonical_offer_url(item.get("url")) or not (item.get("name") or '').strip() or not iso_to_date(item.get("openingDate")):
            raise RuntimeError("Trackr returned an incomplete open programme")
        if item.get('closingDate') and not iso_to_date(item['closingDate']):
            raise RuntimeError('Trackr returned an invalid closing date')
        if not dates_are_open(iso_to_date(item['openingDate']), iso_to_date(item.get('closingDate'))):
            continue
        company = item.get("company") or {}
        categories = item.get("categories") or []
        if not isinstance(company, dict) or not isinstance(categories, list) or any(not isinstance(c, str) for c in categories):
            raise RuntimeError('Trackr returned malformed programme metadata')
        open_offers.append(
            {
                "name": (item.get("name") or "").strip(),
                "company": company.get("name"),
                "company_id": company.get("id"),
                "offer_url": (item.get("url") or "").strip(),
                "region": item.get("region"),
                "categories": categories,
                "opening_date": iso_to_date(item.get("openingDate")),
                "closing_date": iso_to_date(item.get("closingDate")),
                "stage": item.get("currentStage") or "Unknown",
                "rolling": bool(item.get("rolling")),
                "needs_cv": bool(item.get("cv")),
                "needs_cover_letter": bool(item.get("coverLetter") == "Yes"),
                "company_description": company.get("description"),
                "notes": item.get("notes"),
            }
        )

    return open_offers


TRACKING_QUERY_PARAMETERS = {"source", "iis", "stype", "gh_src"}


def canonical_offer_url(value):
    url = (value or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ('https', 'http') or not parts.hostname or parts.username or parts.password:
            return ''
        parts.port  # Reject malformed ports as well.
        filtered_query = [
            (name, query_value)
            for name, query_value in parse_qsl(parts.query, keep_blank_values=True)
            if not name.lower().startswith("utm_")
            and name.lower() not in TRACKING_QUERY_PARAMETERS
        ]
        path = parts.path.rstrip("/") or "/"
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                path,
                urlencode(filtered_query, doseq=True),
                parts.fragment,
            )
        )
    except ValueError:
        return ''


def offer_key(offer):
    url = (offer.get("offer_url") or "").strip()
    if url:
        return f"url:{canonical_offer_url(url)}"
    company = (offer.get("company") or "").strip().lower()
    name = (offer.get("name") or "").strip().lower()
    return f"fallback:{company}:{name}"


def deduplicate_offers(open_offers):
    seen = set()
    deduped = OfferSnapshot() if getattr(open_offers, 'complete', False) else []
    skipped_duplicates = 0
    skipped_no_url = 0

    for offer in open_offers:
        url = (offer.get("offer_url") or "").strip()
        key = offer_key(offer)
        if not canonical_offer_url(url):
            skipped_no_url += 1
            continue
        if key in seen:
            skipped_duplicates += 1
            continue
        seen.add(key)
        deduped.append(offer)

    print(
        f"Deduplication: {len(deduped)} gardées, "
        f"{skipped_duplicates} doublons ignorés, {skipped_no_url} sans URL ignorées"
    )
    return deduped


def write_csv(open_offers, output_file):
    if not open_offers and not getattr(open_offers, 'complete', False):
        raise RuntimeError(
            f"Refusing to overwrite {output_file} with an empty offer list"
        )

    output_file = Path(output_file)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', newline='', encoding='utf-8', dir=output_file.parent, delete=False) as f:
            temporary = Path(f.name)
            writer = csv.writer(f)
            writer.writerow(CSV_COLUMNS)
            for offer in open_offers:
                writer.writerow(
                    [
                        offer["name"],
                        offer["company"],
                        offer["company_id"],
                        offer["offer_url"],
                        offer["region"],
                        ",".join(offer["categories"]) if isinstance(offer["categories"], list) else offer["categories"],
                        offer["opening_date"],
                        offer["closing_date"],
                        offer["stage"],
                        offer["rolling"],
                        offer["needs_cv"],
                        offer["needs_cover_letter"],
                        offer["company_description"],
                        offer["notes"],
                    ]
                )

            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, output_file)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"{len(open_offers)} offres exportées dans : {output_file}")
    return output_file


def log_run_summary(open_offers):
    companies = len({(offer.get("company") or "").strip() for offer in open_offers if offer.get("company")})
    stages = {}
    regions = {}
    for offer in open_offers:
        stage = offer.get("stage") or "Unknown"
        region = offer.get("region") or "Other"
        stages[stage] = stages.get(stage, 0) + 1
        regions[region] = regions.get(region, 0) + 1

    print(f"Run summary: {len(open_offers)} offres, {companies} entreprises")
    print(f"Stages: {stages}")
    print(f"Regions: {regions}")


def offer_has_start_term(offer, start_term):
    categories = offer.get("categories") or offer.get("Categories") or []
    if isinstance(categories, str):
        categories = [category.strip() for category in categories.split(",")]
    return start_term in categories


def filter_offers_by_start_term(open_offers, start_term):
    return [offer for offer in open_offers if offer_has_start_term(offer, start_term)]


def filter_email_offers(new_offers, notion_result):
    email_urls = notion_result["created_offer_urls"] | notion_result["opened_offer_urls"]
    return [offer for offer in new_offers if (offer.get("offer_url") or "").strip() in email_urls]

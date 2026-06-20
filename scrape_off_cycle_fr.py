import csv
import os
from datetime import datetime
import requests

from test import detect_new_offers, read_process_csv, send_email


TRACKR_API_URL = "https://api.the-trackr.com/programmes"
TRACKR_PARAMS = {
    "region": "FR",
    "industry": "Finance",
    "season": "2027",
    "type": "off-cycle-internships",
}
DEFAULT_OUTPUT_FILE = "processus_ouverts_fr_off_cycle.csv"

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
        for key in ("vacancies", "internships", "offers", "jobs", "data"):
            items = data.get(key)
            if isinstance(items, list):
                return items
    if isinstance(data, list):
        return data
    return []


def scrape_open_off_cycle_internships():
    response = requests.get(TRACKR_API_URL, params=TRACKR_PARAMS, timeout=30)
    response.raise_for_status()
    internships = extract_trackr_items(response.json())

    open_offers = []
    for item in internships:
        if not isinstance(item, dict) or item.get("openingDate") is None:
            continue

        company = item.get("company") or {}
        categories = item.get("categories") or []
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


def offer_key(offer):
    url = (offer.get("offer_url") or "").strip()
    if url:
        return f"url:{url}"
    company = (offer.get("company") or "").strip().lower()
    name = (offer.get("name") or "").strip().lower()
    return f"fallback:{company}:{name}"


def deduplicate_offers(open_offers):
    seen = set()
    deduped = []
    skipped_duplicates = 0
    skipped_no_url = 0

    for offer in open_offers:
        url = (offer.get("offer_url") or "").strip()
        key = offer_key(offer)
        if not url:
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


def write_csv(open_offers, output_file=DEFAULT_OUTPUT_FILE):
    with open(output_file, mode="w", newline="", encoding="utf-8") as f:
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


if __name__ == "__main__":
    output_file = os.getenv("OUTPUT_FILE", DEFAULT_OUTPUT_FILE)
    offers = deduplicate_offers(scrape_open_off_cycle_internships())
    previous_offers = read_process_csv(output_file)
    force_email_all = os.getenv("FORCE_EMAIL_ALL", "").strip().lower() in ("1", "true", "yes")
    new_offers = offers if force_email_all else detect_new_offers(offers, previous_offers)
    log_run_summary(offers)
    csv_file = write_csv(offers, output_file)
    if new_offers:
        print(f"{len(new_offers)} nouvelle(s) offre(s) off-cycle FR détectée(s), envoi email")
        send_email(new_offers, csv_file, "off-cycle internship(s) FR")
    else:
        print("Aucune nouvelle offre off-cycle FR détectée, email non envoyé")

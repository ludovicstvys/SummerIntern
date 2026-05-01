from playwright.sync_api import sync_playwright
import csv
import os
import requests
from datetime import datetime, timezone

NOTION_API_VERSION = "2022-06-28"
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "bffe10ddc8514e3dbf6591b8aadb9736")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")


def iso_to_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return None

def scrape_open_summer_internships():
    URL = "https://app.the-trackr.com/uk-finance/summer-internships"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page()

        internships = []
        def handle_resp(response):
            if response.request.resource_type == "xhr" and "internships" in response.url:
                try:
                    data = response.json()
                    if isinstance(data, dict):
                        lst = data.get("vacancies") or data.get("internships") or []
                    elif isinstance(data, list):
                        lst = data
                    else:
                        lst = []
                    if isinstance(lst, list):
                        internships.extend(lst)
                except:
                    pass

        page.on("response", handle_resp)
        page.goto(URL, wait_until="networkidle", timeout=60000)

        prev_h = page.evaluate("() => document.body.scrollHeight")
        while True:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1000)
            new_h = page.evaluate("() => document.body.scrollHeight")
            if new_h == prev_h:
                break
            prev_h = new_h

        browser.close()
    valid = [i for i in internships if isinstance(i, dict)]
    open_offers = []
    for i in valid:
        if i.get("openingDate") is not None:
            company  = i.get("company")
            title = (i.get("name") or "").strip()
            comp = (company or {}).get("name")
            company_id = (company or {}).get("id")
            categories = i.get("categories") or []
            url = (i.get("url")      or "").strip()
            open_offers.append({
                "name": title,
                "company": comp,
                "company_id": company_id,
                "offer_url": url,
                "region": i.get("region"),
                "categories": categories,
                "opening_date": iso_to_date(i.get("openingDate")),
                "closing_date": iso_to_date(i.get("closingDate")),
                "stage": i.get("currentStage") or "Unknown",
                "rolling": bool(i.get("rolling")),
                "needs_cv": bool(i.get("cv")),
                "needs_cover_letter": bool(i.get("coverLetter") == "Yes"),
                "company_description": (company or {}).get("description"),
                "notes": i.get("notes"),
            })

    return open_offers

def read_process_csv(csv_path):
    """
    Lit le fichier CSV et retourne une liste de dicts.
    Chaque dict correspond à une ligne, avec pour clés les en-têtes de colonnes.
    """
    processes=dict()
    with open(csv_path, mode="r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        processes = [row for row in reader]
    return processes


def ecriture_csv(open_offers, output_file="processus_ouverts.csv"):
    with open(output_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Name", "Company", "Company ID", "Offer URL", "Region", "Categories", "Opening Date", "Closing Date", "Stage", "Rolling", "Needs CV", "Needs Cover Letter", "Company Description", "Notes"])
        for offer in open_offers:
            writer.writerow([
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
            ])
    print(f"{len(open_offers)} offres exportées dans : {output_file}")
    return output_file


def notion_payload(offer):
    return {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": offer["name"] or "Untitled"}}]},
            "Company": {"rich_text": [{"text": {"content": offer["company"] or ""}}]},
            "Company ID": {"rich_text": [{"text": {"content": offer["company_id"] or ""}}]},
            "Offer URL": {"url": offer["offer_url"] or None},
            "Region": {"select": {"name": offer["region"] or "Other"}},
            "Categories": {"multi_select": [{"name": c} for c in (offer["categories"] or [])]},
            "Opening Date": {"date": {"start": offer["opening_date"]} if offer["opening_date"] else None},
            "Closing Date": {"date": {"start": offer["closing_date"]} if offer["closing_date"] else None},
            "Stage": {"select": {"name": offer["stage"] or "Unknown"}},
            "Rolling": {"checkbox": bool(offer["rolling"])},
            "Needs CV": {"checkbox": bool(offer["needs_cv"])},
            "Needs Cover Letter": {"checkbox": bool(offer["needs_cover_letter"])},
            "Company Description": {"rich_text": [{"text": {"content": offer["company_description"] or ""}}]},
            "Notes": {"rich_text": [{"text": {"content": offer["notes"] or ""}}]},
        },
    }


def fetch_existing_offers():
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }

    existing_offers = {}
    start_cursor = None

    while True:
        payload = {
            "page_size": 100,
            "filter": {
                "property": "Offer URL",
                "url": {"is_not_empty": True},
            },
        }
        if start_cursor:
            payload["start_cursor"] = start_cursor

        response = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            headers=headers,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        for page in data.get("results", []):
            properties = page.get("properties", {})
            url_prop = properties.get("Offer URL", {}).get("url")
            opening_prop = properties.get("Opening Date", {}).get("date")
            status_prop = properties.get("Status", {}).get("status")
            if url_prop:
                existing_offers[url_prop.strip()] = {
                    "page_id": page.get("id"),
                    "opening_date": (opening_prop or {}).get("start"),
                    "status": (status_prop or {}).get("name"),
                }

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    return existing_offers


def deduplicate_offers(open_offers):
    seen = set()
    deduped = []
    skipped_duplicates = 0
    skipped_no_url = 0
    for offer in open_offers:
        url = (offer.get("offer_url") or "").strip()
        if not url or url in seen:
            if not url:
                skipped_no_url += 1
            else:
                skipped_duplicates += 1
            continue
        seen.add(url)
        deduped.append(offer)
    print(f"Deduplication: {len(deduped)} gardées, {skipped_duplicates} doublons ignorés, {skipped_no_url} sans URL ignorées")
    return deduped


def sync_to_notion(open_offers):
    if not NOTION_TOKEN:
        raise RuntimeError("Missing NOTION_TOKEN environment variable")

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }

    existing_offers = fetch_existing_offers()
    created = 0
    updated = 0
    opened = 0
    skipped_no_url = 0
    for offer in open_offers:
        payload = notion_payload(offer)
        for key in ["Opening Date", "Closing Date"]:
            if payload["properties"][key] is None:
                del payload["properties"][key]
        offer_url = (offer.get("offer_url") or "").strip()
        if not offer_url:
            skipped_no_url += 1
            continue
        existing = existing_offers.get(offer_url)
        if existing:
            page_id = existing["page_id"]
            incoming_opening_date = offer.get("opening_date")
            previous_opening_date = existing.get("opening_date")
            if incoming_opening_date and not previous_opening_date:
                payload["properties"]["Status"] = {"status": {"name": "Opened"}}
                opened += 1
            response = requests.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=headers,
                json={"properties": payload["properties"]},
                timeout=30,
            )
            response.raise_for_status()
            updated += 1
            existing_offers[offer_url]["opening_date"] = incoming_opening_date or previous_opening_date
        else:
            if offer.get("opening_date"):
                payload["properties"]["Status"] = {"status": {"name": "Closed"}}
            response = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            if offer_url:
                existing_offers[offer_url] = {
                    "page_id": response.json().get("id"),
                    "opening_date": offer.get("opening_date"),
                    "status": "Closed",
                }
            created += 1

    print(f"Notion sync: {created} créées, {updated} mises à jour, {opened} passées à Opened, {skipped_no_url} sans URL ignorées")


def log_run_summary(open_offers):
    total = len(open_offers)
    companies = len({(offer.get("company") or "").strip() for offer in open_offers if (offer.get("company") or "").strip()})
    stages = {}
    regions = {}
    for offer in open_offers:
        stage = offer.get("stage") or "Unknown"
        region = offer.get("region") or "Other"
        stages[stage] = stages.get(stage, 0) + 1
        regions[region] = regions.get(region, 0) + 1

    print(f"Run summary: {total} offres, {companies} entreprises")
    print(f"Stages: {stages}")
    print(f"Regions: {regions}")

if __name__ == "__main__":
    offres = deduplicate_offers(scrape_open_summer_internships())
    log_run_summary(offres)
    ecriture_csv(offres)
    sync_to_notion(offres)
  

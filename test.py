from playwright.sync_api import sync_playwright
import csv
import smtplib
import os
import re
import requests
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from html import escape

def clean_env(name, default=None):
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else value


NOTION_API_VERSION = "2025-09-03"
NOTION_DATA_SOURCE_ID = clean_env("NOTION_DATA_SOURCE_ID")
TODO_DATA_SOURCE_ID = clean_env("TODO_DATA_SOURCE_ID")
NOTION_TOKEN = clean_env("NOTION_TOKEN")

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


def add_days(date_value, days):
    if not date_value:
        return None
    try:
        parsed = datetime.fromisoformat(date_value.replace("Z", "+00:00")).date()
        return (parsed + timedelta(days=days)).isoformat()
    except Exception:
        return None


def raise_for_notion(response, context):
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        print(f"{context} failed with {response.status_code}: {response.text}")
        if response.status_code == 404:
            print(
                "Notion access hint: the data source is either the wrong ID "
                "or it has not been shared with the integration attached to NOTION_TOKEN."
            )
        raise exc

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
    if not os.path.exists(csv_path):
        return []
    processes=dict()
    with open(csv_path, mode="r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        processes = [row for row in reader]
    return processes


def ecriture_csv(open_offers, output_file="processus_ouverts.csv"):
    with open(output_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
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


def offer_key(offer):
    url = (offer.get("offer_url") or offer.get("Offer URL") or "").strip()
    if url:
        return f"url:{url}"
    company = (offer.get("company") or offer.get("Company") or "").strip().lower()
    name = (offer.get("name") or offer.get("Name") or "").strip().lower()
    return f"fallback:{company}:{name}"


def detect_new_offers(open_offers, previous_rows):
    existing_keys = {offer_key(row) for row in previous_rows}
    return [offer for offer in open_offers if offer_key(offer) not in existing_keys]


def read_email_recipients(csv_path="email.csv"):
    recipients = []

    if os.path.exists(csv_path):
        with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as f:
            recipients.extend(extract_email_addresses(f.read()))

    env_recipients = clean_env("TO_ADDRS") or clean_env("MAIL_TO_ADDRS")
    if env_recipients:
        recipients.extend(extract_email_addresses(env_recipients))

    return sorted(set(recipients))


def extract_email_addresses(value):
    if not value:
        return []
    return re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value)


def format_offer_for_email(offer):
    parts = [
        offer.get("company") or "Unknown company",
        offer.get("name") or "Untitled",
    ]
    if offer.get("region"):
        parts.append(offer["region"])
    if offer.get("stage"):
        parts.append(offer["stage"])
    if offer.get("offer_url"):
        parts.append(offer["offer_url"])
    return " - ".join(parts)


def format_bool(value):
    return "Oui" if value else "Non"


def format_categories(categories):
    if isinstance(categories, list):
        return ", ".join(str(category) for category in categories if category)
    return categories or ""


def build_email_text(open_offers, programme_label="summer internship(s)"):
    lines = [
        "Bonjour,",
        "",
        f"{len(open_offers)} nouveau(x) {programme_label} ouvert(s) ont été détecté(s).",
        "",
    ]
    for index, offer in enumerate(open_offers, start=1):
        lines.extend(
            [
                f"{index}. {offer.get('company') or 'Unknown company'} - {offer.get('name') or 'Untitled'}",
                f"   Région: {offer.get('region') or 'Non précisée'}",
                f"   Stage: {offer.get('stage') or 'Unknown'}",
                f"   Catégories: {format_categories(offer.get('categories')) or 'Non précisées'}",
                f"   Ouverture: {offer.get('opening_date') or 'Non précisée'}",
                f"   Clôture: {offer.get('closing_date') or 'Non précisée'}",
                f"   CV requis: {format_bool(offer.get('needs_cv'))}",
                f"   Cover letter requise: {format_bool(offer.get('needs_cover_letter'))}",
                f"   Lien: {offer.get('offer_url') or 'Non disponible'}",
                "",
            ]
        )
    lines.append("Le CSV complet est joint à ce mail.")
    return "\n".join(lines)


def build_email_html(open_offers, programme_label="summer internship(s)"):
    rows = []
    for offer in open_offers:
        company = escape(offer.get("company") or "Unknown company")
        name = escape(offer.get("name") or "Untitled")
        url = offer.get("offer_url") or ""
        link = (
            f'<a href="{escape(url, quote=True)}" style="color:#0f766e;text-decoration:none;font-weight:600;">Postuler</a>'
            if url
            else "Non disponible"
        )
        requirements = []
        if offer.get("needs_cv"):
            requirements.append("CV")
        if offer.get("needs_cover_letter"):
            requirements.append("Cover letter")
        if offer.get("rolling"):
            requirements.append("Rolling")
        requirement_text = ", ".join(requirements) or "Aucun signalé"

        rows.append(
            f"""
            <tr>
              <td style="padding:14px 12px;border-bottom:1px solid #e5e7eb;">
                <div style="font-weight:700;color:#111827;">{company}</div>
                <div style="color:#374151;margin-top:3px;">{name}</div>
                <div style="color:#6b7280;font-size:13px;margin-top:6px;">{escape(format_categories(offer.get("categories")) or "Catégories non précisées")}</div>
              </td>
              <td style="padding:14px 12px;border-bottom:1px solid #e5e7eb;color:#374151;">{escape(offer.get("region") or "Non précisée")}</td>
              <td style="padding:14px 12px;border-bottom:1px solid #e5e7eb;color:#374151;">{escape(offer.get("stage") or "Unknown")}</td>
              <td style="padding:14px 12px;border-bottom:1px solid #e5e7eb;color:#374151;">
                <div>Ouverture: {escape(offer.get("opening_date") or "Non précisée")}</div>
                <div>Clôture: {escape(offer.get("closing_date") or "Non précisée")}</div>
              </td>
              <td style="padding:14px 12px;border-bottom:1px solid #e5e7eb;color:#374151;">{escape(requirement_text)}</td>
              <td style="padding:14px 12px;border-bottom:1px solid #e5e7eb;">{link}</td>
            </tr>
            """
        )

    return f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f9fafb;font-family:Arial,Helvetica,sans-serif;color:#111827;">
    <div style="max-width:980px;margin:0 auto;padding:28px 18px;">
      <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
        <div style="padding:22px 24px;background:#111827;color:#ffffff;">
          <div style="font-size:20px;font-weight:700;">Nouveaux {escape(programme_label)}</div>
          <div style="font-size:14px;color:#d1d5db;margin-top:6px;">{len(open_offers)} nouvelle(s) offre(s) détectée(s)</div>
        </div>
        <div style="padding:18px 24px;color:#374151;font-size:14px;">
          Bonjour,<br>
          Voici les nouvelles offres détectées par le scraper. Le CSV complet est joint à ce mail.
        </div>
        <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:14px;">
          <thead>
            <tr style="background:#f3f4f6;color:#374151;text-align:left;">
              <th style="padding:10px 12px;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;">Offre</th>
              <th style="padding:10px 12px;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;">Région</th>
              <th style="padding:10px 12px;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;">Stage</th>
              <th style="padding:10px 12px;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;">Dates</th>
              <th style="padding:10px 12px;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;">Requis</th>
              <th style="padding:10px 12px;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;">Lien</th>
            </tr>
          </thead>
          <tbody>
            {''.join(rows)}
          </tbody>
        </table>
      </div>
    </div>
  </body>
</html>
"""


def send_email(open_offers, csv_path=None, programme_label="summer internship(s)"):
    smtp_server = clean_env("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(clean_env("SMTP_PORT", "587"))
    smtp_user = clean_env("SMTP_USER")
    smtp_pass = clean_env("SMTP_PASS_APP") or clean_env("SMTP_PASS")
    from_addr = clean_env("FROM_ADDR") or smtp_user
    to_addrs = read_email_recipients()

    missing = [
        name
        for name, value in {
            "SMTP_USER": smtp_user,
            "SMTP_PASS_APP or SMTP_PASS": smtp_pass,
            "FROM_ADDR or SMTP_USER": from_addr,
            "email.csv, TO_ADDRS, or MAIL_TO_ADDRS": to_addrs,
        }.items()
        if not value
    ]
    if missing:
        print(f"Email skipped: missing {', '.join(missing)}")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"{len(open_offers)} nouveau(x) {programme_label} ouvert(s)"
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(build_email_text(open_offers, programme_label))
    msg.add_alternative(build_email_html(open_offers, programme_label), subtype="html")

    if csv_path:
        with open(csv_path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype="text",
                subtype="csv",
                filename=os.path.basename(csv_path),
            )

    with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg, from_addr=from_addr, to_addrs=to_addrs)

    print(f"Email envoyé à : {to_addrs}")
    return True


def notion_payload(offer):
    return {
        "parent": {"data_source_id": NOTION_DATA_SOURCE_ID},
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


def todo_payload(offer, opened_on, due_on):
    task_name = f"TODO - {offer['company'] or 'Unknown company'} - {offer['name'] or 'Untitled'}"
    payload = {
        "parent": {"data_source_id": TODO_DATA_SOURCE_ID},
        "properties": {
            "Task": {"title": [{"text": {"content": task_name}}]},
            "Company": {"rich_text": [{"text": {"content": offer["company"] or ""}}]},
            "Offer URL": {"url": offer["offer_url"] or None},
            "Trigger Stage": {"rich_text": [{"text": {"content": offer["stage"] or "Unknown"}}]},
            "Opened On": {"date": {"start": opened_on} if opened_on else None},
            "Due": {"date": {"start": due_on} if due_on else None},
            "Status": {"select": {"name": "To-do"}},
            "Notes": {"rich_text": [{"text": {"content": offer["notes"] or ""}}]},
        },
    }
    for key in ["Company", "Offer URL", "Trigger Stage", "Opened On", "Due", "Notes"]:
        if payload["properties"][key] is None:
            del payload["properties"][key]
    return payload


def fetch_existing_offers():
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }

    existing_offers = {}
    start_cursor = None

    while True:
        payload = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor

        response = requests.post(
            f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query",
            headers=headers,
            json=payload,
            timeout=30,
        )
        raise_for_notion(response, "Notion data source query")
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


def fetch_existing_todos():
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }

    existing_todos = {}
    start_cursor = None

    while True:
        payload = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor

        response = requests.post(
            f"https://api.notion.com/v1/data_sources/{TODO_DATA_SOURCE_ID}/query",
            headers=headers,
            json=payload,
            timeout=30,
        )
        raise_for_notion(response, "Notion todo data source query")
        data = response.json()

        for page in data.get("results", []):
            properties = page.get("properties", {})
            url_prop = properties.get("Offer URL", {}).get("url")
            due_prop = properties.get("Due", {}).get("date")
            if url_prop:
                existing_todos[url_prop.strip()] = {
                    "page_id": page.get("id"),
                    "due": (due_prop or {}).get("start"),
                }

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    return existing_todos


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
    todos_enabled = bool(TODO_DATA_SOURCE_ID)
    existing_todos = {}
    if todos_enabled:
        try:
            existing_todos = fetch_existing_todos()
        except requests.HTTPError:
            todos_enabled = False
            print(
                "Todo sync skipped: TODO_DATA_SOURCE_ID is not accessible. "
                "Check that the ID is correct and shared with the Notion integration."
            )
    else:
        print("Todo sync skipped: missing TODO_DATA_SOURCE_ID environment variable")
    created = 0
    updated = 0
    opened = 0
    todo_created = 0
    todo_updated = 0
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
            newly_opened = bool(incoming_opening_date and not previous_opening_date)
            if newly_opened:
                payload["properties"]["Status"] = {"status": {"name": "Opened"}}
                opened += 1
            response = requests.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=headers,
                json={"properties": payload["properties"]},
                timeout=30,
            )
            raise_for_notion(response, f"Notion page update {page_id}")
            updated += 1
            existing_offers[offer_url]["opening_date"] = incoming_opening_date or previous_opening_date
            if newly_opened and todos_enabled:
                due_date = add_days(incoming_opening_date, 2)
                todo_existing = existing_todos.get(offer_url)
                todo_request = todo_payload(offer, incoming_opening_date, due_date)
                if todo_existing:
                    todo_page_id = todo_existing["page_id"]
                    response = requests.patch(
                        f"https://api.notion.com/v1/pages/{todo_page_id}",
                        headers=headers,
                        json={"properties": todo_request["properties"]},
                        timeout=30,
                    )
                    raise_for_notion(response, f"Notion todo update {todo_page_id}")
                    todo_updated += 1
                else:
                    response = requests.post(
                        "https://api.notion.com/v1/pages",
                        headers=headers,
                        json=todo_request,
                        timeout=30,
                    )
                    raise_for_notion(response, "Notion todo create")
                    existing_todos[offer_url] = {
                        "page_id": response.json().get("id"),
                        "due": due_date,
                    }
                    todo_created += 1
        else:
            if offer.get("opening_date"):
                payload["properties"]["Status"] = {"status": {"name": "Closed"}}
            response = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=30)
            raise_for_notion(response, "Notion page create")
            if offer_url:
                existing_offers[offer_url] = {
                    "page_id": response.json().get("id"),
                    "opening_date": offer.get("opening_date"),
                    "status": "Closed",
                }
            created += 1

    print(
        "Notion sync: "
        f"{created} créées, {updated} mises à jour, {opened} passées à Opened, "
        f"{todo_created} todos créés, {todo_updated} todos mis à jour, "
        f"{skipped_no_url} sans URL ignorées"
    )


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
    previous_offers = read_process_csv("processus_ouverts.csv")
    new_offers = detect_new_offers(offres, previous_offers)
    log_run_summary(offres)
    csv_file = ecriture_csv(offres)
    if new_offers:
        print(f"{len(new_offers)} nouvelle(s) offre(s) détectée(s), envoi email")
        send_email(new_offers, csv_file)
    else:
        print("Aucune nouvelle offre détectée, email non envoyé")
    sync_to_notion(offres)
  

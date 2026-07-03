from playwright.sync_api import sync_playwright
import csv
import smtplib
import os
import re
import requests
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from html import escape
from html.parser import HTMLParser


def load_env_file(path=".env"):
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name and name not in os.environ:
                os.environ[name] = value


def clean_env(name, default=None):
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else value


load_env_file()

NOTION_API_VERSION = "2025-09-03"
NOTION_DATA_SOURCE_ID = clean_env("NOTION_DATA_SOURCE_ID")
TODO_DATA_SOURCE_ID = clean_env("TODO_DATA_SOURCE_ID")
NOTION_TOKEN = clean_env("NOTION_TOKEN")
_RESOLVED_DATA_SOURCE_IDS = {}
_OFFER_DESCRIPTION_CACHE = {}
NOTION_TEXT_LIMIT = 1900
OFFER_DESCRIPTION_TIMEOUT = 8
PLAYWRIGHT_DESCRIPTION_TIMEOUT_MS = 15000
DESCRIPTION_KEYWORDS = (
    "intern",
    "internship",
    "analyst",
    "programme",
    "program",
    "role",
    "team",
    "opportunity",
    "responsibilities",
    "requirements",
    "candidate",
    "graduate",
    "off-cycle",
    "summer",
    "investment",
    "markets",
    "finance",
)
DESCRIPTION_NOISE_PATTERNS = (
    "accept cookies",
    "cookie policy",
    "privacy policy",
    "terms of use",
    "sign in",
    "log in",
    "create alert",
    "equal opportunity employer",
    "powered by",
)

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


def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def short_id(value):
    value = (value or "").strip()
    if len(value) <= 12:
        return value or "<missing>"
    return f"{value[:8]}...{value[-4:]}"


def audit_log(message):
    print(f"[Notion audit] {message}")


def schema_summary(schema):
    return ", ".join(
        f"{name}:{definition.get('type')}"
        for name, definition in sorted(schema.items(), key=lambda item: item[0].lower())
    )


def offer_audit_label(offer):
    company = normalize_text(offer.get("company") or offer.get("Company") or "Unknown company")
    name = normalize_text(offer.get("name") or offer.get("Name") or "Untitled")
    url = (offer.get("offer_url") or offer.get("Offer URL") or "").strip()
    return f"{company} | {name} | url={url or '<missing>'}"


def page_audit_summary(page_data):
    if not isinstance(page_data, dict):
        return "page=<invalid response>"
    page_id = short_id(page_data.get("id"))
    url = page_data.get("url") or page_data.get("public_url") or "<no Notion URL>"
    return f"page_id={page_id}, notion_url={url}"


def resolve_data_source_id(configured_id, label):
    if not configured_id:
        return None
    if configured_id in _RESOLVED_DATA_SOURCE_IDS:
        resolved_id = _RESOLVED_DATA_SOURCE_IDS[configured_id]
        audit_log(f"{label}: reused resolved data source id {short_id(resolved_id)}")
        return resolved_id

    headers = notion_headers()
    audit_log(f"{label}: resolving configured id {short_id(configured_id)}")
    data_source_response = requests.get(
        f"https://api.notion.com/v1/data_sources/{configured_id}",
        headers=headers,
        timeout=30,
    )
    if data_source_response.ok:
        _RESOLVED_DATA_SOURCE_IDS[configured_id] = configured_id
        audit_log(f"{label}: configured id is directly accessible as data source {short_id(configured_id)}")
        return configured_id

    database_response = requests.get(
        f"https://api.notion.com/v1/databases/{configured_id}",
        headers=headers,
        timeout=30,
    )
    if database_response.ok:
        data_sources = database_response.json().get("data_sources") or []
        if data_sources:
            resolved_id = data_sources[0]["id"]
            print(f"{label}: resolved database ID to data source ID {resolved_id}")
            audit_log(
                f"{label}: database {short_id(configured_id)} resolved to data source {short_id(resolved_id)}"
            )
            _RESOLVED_DATA_SOURCE_IDS[configured_id] = resolved_id
            return resolved_id

    raise_for_notion(data_source_response, f"{label} data source retrieve")
    return configured_id


def fetch_data_source_schema(data_source_id):
    response = requests.get(
        f"https://api.notion.com/v1/data_sources/{data_source_id}",
        headers=notion_headers(),
        timeout=30,
    )
    raise_for_notion(response, "Notion data source retrieve")
    schema = response.json().get("properties") or {}
    audit_log(f"schema for data source {short_id(data_source_id)}: {schema_summary(schema)}")
    return schema


def prop_type(schema, name):
    return (schema.get(name) or {}).get("type")


def normalize_text(value):
    if value in (None, ""):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def truncate_text(value, limit=NOTION_TEXT_LIMIT):
    text = normalize_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def rich_text(content):
    text = truncate_text(content)
    return {"rich_text": [{"text": {"content": text}}]} if text else {"rich_text": []}


def title_text(content):
    return {"title": [{"text": {"content": str(content or "Untitled")}}]}


def date_property(value):
    return {"date": {"start": value}} if value else None


def status_property(schema, name, candidates):
    prop = schema.get(name) or {}
    options = ((prop.get("status") or {}).get("options") or [])
    option_names = {option.get("name") for option in options}
    for candidate in candidates:
        if candidate in option_names:
            return {"status": {"name": candidate}}
    return None


def select_property(schema, name, candidates):
    prop = schema.get(name) or {}
    options = ((prop.get("select") or {}).get("options") or [])
    option_names = {option.get("name") for option in options}
    for candidate in candidates:
        if candidate in option_names:
            return {"select": {"name": candidate}}
    return None


def plain_text_from_property(prop):
    if not prop:
        return None
    if prop.get("type") == "url":
        return prop.get("url")
    if prop.get("type") in ("rich_text", "title"):
        return "".join(part.get("plain_text", "") for part in prop.get(prop["type"], [])) or None
    if prop.get("type") == "date":
        date_value = prop.get("date") or {}
        return date_value.get("start")
    return None


def derived_role(offer):
    categories = offer.get("categories") or []
    category_text = " ".join(categories) if isinstance(categories, list) else str(categories)
    text = f"{offer.get('name') or ''} {category_text}".lower()
    if "off-cycle" in text or "q1 start" in text or "q2 start" in text or "q3 start" in text or "q4 start" in text:
        return "Off-cycle"
    if "summer" in text:
        return "Summer Analyst"
    return None


def set_if_schema(properties, schema, name, expected_type, value):
    if prop_type(schema, name) == expected_type and value is not None:
        properties[name] = value


def first_schema_property(schema, expected_type, candidates):
    for candidate in candidates:
        if prop_type(schema, candidate) == expected_type:
            return candidate
    return None


def score_description_text(text):
    normalized = normalize_text(text)
    lowered = normalized.lower()
    if len(normalized) < 80:
        return -10
    if any(pattern in lowered for pattern in DESCRIPTION_NOISE_PATTERNS):
        return -5
    keyword_hits = sum(1 for keyword in DESCRIPTION_KEYWORDS if keyword in lowered)
    length_score = min(len(normalized), 1200) / 250
    return keyword_hits * 3 + length_score


def best_visible_description(candidates):
    cleaned = []
    seen = set()
    for candidate in candidates:
        text = truncate_text(candidate)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    if not cleaned:
        return None
    best = max(cleaned, key=score_description_text)
    return best if score_description_text(best) > 0 else None


class OfferDescriptionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.title_parts = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = {name.lower(): value for name, value in attrs if name}
        if tag.lower() == "title":
            self.in_title = True
            return
        if tag.lower() != "meta":
            return

        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        content = attrs.get("content")
        if key and content:
            self.meta[key] = normalize_text(content)

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)

    def best_description(self):
        for key in ("og:description", "description", "twitter:description"):
            if self.meta.get(key):
                return self.meta[key]
        return None


def fetch_offer_link_description(url):
    url = (url or "").strip()
    if not url:
        return None
    if url in _OFFER_DESCRIPTION_CACHE:
        return _OFFER_DESCRIPTION_CACHE[url]

    description = None
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            allow_redirects=True,
            timeout=OFFER_DESCRIPTION_TIMEOUT,
        )
        content_type = response.headers.get("Content-Type", "")
        if response.ok and "html" in content_type.lower():
            parser = OfferDescriptionParser()
            parser.feed(response.text[:250000])
            description = parser.best_description()
    except requests.RequestException:
        description = None

    description = truncate_text(description)
    _OFFER_DESCRIPTION_CACHE[url] = description
    return description


def fetch_offer_rendered_description(url):
    url = (url or "").strip()
    if not url:
        return None

    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                )
            )
            page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_DESCRIPTION_TIMEOUT_MS)
            page.wait_for_timeout(2500)
            candidates = page.locator(
                "main, article, section, [role='main'], "
                "[class*='job'], [class*='description'], [class*='posting'], "
                "[data-automation-id*='description'], [data-testid*='description'], "
                "p, li"
            ).evaluate_all(
                """nodes => nodes
                    .map(node => node.innerText || node.textContent || '')
                    .map(text => text.replace(/\\s+/g, ' ').trim())
                    .filter(text => text.length >= 80)
                """
            )
            return best_visible_description(candidates)
    except Exception:
        return None
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass


def offer_notes_for_notion(offer):
    return (
        fetch_offer_link_description(offer.get("offer_url"))
        or fetch_offer_rendered_description(offer.get("offer_url"))
        or offer.get("notes")
        or offer.get("company_description")
        or ""
    )

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


def category_group_label(offer):
    return format_categories(offer.get("categories")) or "Catégorie non précisée"


def offers_by_category(open_offers):
    grouped = {}
    for offer in open_offers:
        grouped.setdefault(category_group_label(offer), []).append(offer)
    return sorted(grouped.items(), key=lambda item: item[0].lower())


def build_email_text(open_offers, programme_label="summer internship(s)"):
    lines = [
        "Bonjour,",
        "",
        f"{len(open_offers)} nouveau(x) {programme_label} ouvert(s) ont été détecté(s).",
        "",
    ]
    offer_number = 1
    for category, category_offers in offers_by_category(open_offers):
        lines.extend([f"## {category}", ""])
        for offer in category_offers:
            lines.extend(
                [
                    f"{offer_number}. {offer.get('company') or 'Unknown company'} - {offer.get('name') or 'Untitled'}",
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
            offer_number += 1
    lines.append("Le CSV complet est joint à ce mail.")
    return "\n".join(lines)


def build_email_html(open_offers, programme_label="summer internship(s)"):
    sections = []
    for category, category_offers in offers_by_category(open_offers):
        rows = []
        for offer in category_offers:
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

        sections.append(
            f"""
            <tr>
              <td colspan="6" style="padding:12px 12px;background:#e5e7eb;color:#111827;font-weight:700;border-top:1px solid #d1d5db;border-bottom:1px solid #d1d5db;">
                {escape(category)} ({len(category_offers)})
              </td>
            </tr>
            {''.join(rows)}
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
            {''.join(sections)}
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


def notion_payload(offer, data_source_id=None, schema=None):
    schema = schema or {}
    properties = {}
    categories = offer.get("categories") or []
    if isinstance(categories, str):
        categories = [category.strip() for category in categories.split(",") if category.strip()]
    categories_text = format_categories(categories)
    role = derived_role(offer)
    notes = offer_notes_for_notion(offer)

    set_if_schema(properties, schema, "Name", "title", title_text(offer.get("name")))
    set_if_schema(properties, schema, "Entreprise", "title", title_text(offer.get("company")))
    set_if_schema(properties, schema, "Company", "rich_text", rich_text(offer.get("company")))
    set_if_schema(properties, schema, "Company ID", "rich_text", rich_text(offer.get("company_id")))
    set_if_schema(properties, schema, "Job Title", "rich_text", rich_text(offer.get("name")))
    set_if_schema(properties, schema, "Offer URL", "url", {"url": offer.get("offer_url") or None})
    set_if_schema(properties, schema, "lien offre", "rich_text", rich_text(offer.get("offer_url")))
    set_if_schema(properties, schema, "Region", "select", {"select": {"name": offer.get("region") or "Other"}})
    set_if_schema(properties, schema, "Lieu", "rich_text", rich_text(offer.get("region")))
    set_if_schema(properties, schema, "Categories", "multi_select", {"multi_select": [{"name": c} for c in categories]})
    set_if_schema(properties, schema, "Start month", "rich_text", rich_text(categories_text))
    if role:
        set_if_schema(properties, schema, "Role", "multi_select", {"multi_select": [{"name": role}]})
    set_if_schema(properties, schema, "Opening Date", "date", date_property(offer.get("opening_date")))
    set_if_schema(properties, schema, "Date d'ouverture", "rich_text", rich_text(offer.get("opening_date")))
    set_if_schema(properties, schema, "Closing Date", "date", date_property(offer.get("closing_date")))
    set_if_schema(properties, schema, "Date de fermeture", "rich_text", rich_text(offer.get("closing_date")))
    set_if_schema(properties, schema, "Stage", "select", {"select": {"name": offer.get("stage") or "Unknown"}})
    set_if_schema(properties, schema, "Rolling", "checkbox", {"checkbox": bool(offer.get("rolling"))})
    set_if_schema(properties, schema, "Needs CV", "checkbox", {"checkbox": bool(offer.get("needs_cv"))})
    set_if_schema(properties, schema, "Needs Cover Letter", "checkbox", {"checkbox": bool(offer.get("needs_cover_letter"))})
    set_if_schema(properties, schema, "Company Description", "rich_text", rich_text(offer.get("company_description")))
    set_if_schema(properties, schema, "Notes", "rich_text", rich_text(notes))

    return {
        "parent": {"data_source_id": data_source_id or NOTION_DATA_SOURCE_ID},
        "properties": properties,
    }


def todo_payload(offer, opened_on, due_on, data_source_id=None, schema=None):
    schema = schema or {}
    title_property = first_schema_property(schema, "title", ["Task", "Name"])
    if not title_property:
        raise RuntimeError("Todo sync requires a title property named Task or Name")

    task_name = f"TODO - {offer['company'] or 'Unknown company'} - {offer['name'] or 'Untitled'}"
    properties = {
        title_property: {"title": [{"text": {"content": task_name}}]},
    }
    set_if_schema(properties, schema, "Company", "rich_text", rich_text(offer["company"]))
    set_if_schema(properties, schema, "Offer URL", "url", {"url": offer["offer_url"] or None})
    set_if_schema(properties, schema, "Trigger Stage", "rich_text", rich_text(offer["stage"] or "Unknown"))
    set_if_schema(properties, schema, "Opened On", "date", date_property(opened_on))

    due_property = first_schema_property(schema, "date", ["Due", "Due Date"])
    if due_property and due_on:
        properties[due_property] = {"date": {"start": due_on}}

    status = None
    if prop_type(schema, "Status") == "status":
        status = status_property(schema, "Status", ["To-do", "To do", "Not started", "À faire"])
    elif prop_type(schema, "Status") == "select":
        status = select_property(schema, "Status", ["To-do", "To do", "Not started", "À faire"])
    if status:
        properties["Status"] = status

    set_if_schema(properties, schema, "Notes", "rich_text", rich_text(offer["notes"]))

    payload = {
        "parent": {"data_source_id": data_source_id or TODO_DATA_SOURCE_ID},
        "properties": properties,
    }
    return payload


def fetch_existing_offers(data_source_id=None):
    headers = notion_headers()
    target_data_source_id = data_source_id or NOTION_DATA_SOURCE_ID

    existing_offers = {}
    start_cursor = None
    pages_seen = 0
    duplicate_urls = 0
    pages_without_url = 0
    page_number = 0

    while True:
        payload = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor
        page_number += 1

        response = requests.post(
            f"https://api.notion.com/v1/data_sources/{target_data_source_id}/query",
            headers=headers,
            json=payload,
            timeout=30,
        )
        raise_for_notion(response, "Notion data source query")
        data = response.json()
        results = data.get("results", [])
        pages_seen += len(results)
        audit_log(
            f"existing offers query page {page_number}: {len(results)} result(s), "
            f"has_more={bool(data.get('has_more'))}"
        )

        for page in results:
            properties = page.get("properties", {})
            url_prop = plain_text_from_property(properties.get("Offer URL")) or plain_text_from_property(properties.get("lien offre"))
            opening_prop = properties.get("Opening Date", {}).get("date")
            opening_value = (opening_prop or {}).get("start") or plain_text_from_property(properties.get("Date d'ouverture"))
            status_prop = properties.get("Status", {}).get("status")
            if url_prop:
                normalized_url = url_prop.strip()
                if normalized_url in existing_offers:
                    duplicate_urls += 1
                    audit_log(
                        "duplicate existing Offer URL in Notion: "
                        f"url={normalized_url}, previous_page={short_id(existing_offers[normalized_url]['page_id'])}, "
                        f"duplicate_page={short_id(page.get('id'))}"
                    )
                existing_offers[url_prop.strip()] = {
                    "page_id": page.get("id"),
                    "opening_date": opening_value,
                    "status": (status_prop or {}).get("name"),
                }
            else:
                pages_without_url += 1

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    audit_log(
        f"existing offers loaded: {len(existing_offers)} unique URL(s), "
        f"{pages_seen} page(s) scanned, {duplicate_urls} duplicate URL(s), "
        f"{pages_without_url} page(s) without Offer URL/lien offre"
    )
    return existing_offers


def fetch_existing_todos(data_source_id=None):
    headers = notion_headers()
    target_data_source_id = data_source_id or TODO_DATA_SOURCE_ID

    existing_todos = {}
    start_cursor = None
    pages_seen = 0
    pages_without_url = 0
    page_number = 0

    while True:
        payload = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor
        page_number += 1

        response = requests.post(
            f"https://api.notion.com/v1/data_sources/{target_data_source_id}/query",
            headers=headers,
            json=payload,
            timeout=30,
        )
        raise_for_notion(response, "Notion todo data source query")
        data = response.json()
        results = data.get("results", [])
        pages_seen += len(results)
        audit_log(
            f"existing todos query page {page_number}: {len(results)} result(s), "
            f"has_more={bool(data.get('has_more'))}"
        )

        for page in results:
            properties = page.get("properties", {})
            url_prop = plain_text_from_property(properties.get("Offer URL"))
            due_prop = (properties.get("Due") or properties.get("Due Date") or {}).get("date")
            if url_prop:
                existing_todos[url_prop.strip()] = {
                    "page_id": page.get("id"),
                    "due": (due_prop or {}).get("start"),
                }
            else:
                pages_without_url += 1

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    audit_log(
        f"existing todos loaded: {len(existing_todos)} unique URL(s), "
        f"{pages_seen} page(s) scanned, {pages_without_url} page(s) without Offer URL"
    )
    return existing_todos


def todo_schema_ready(schema):
    missing = []
    if not first_schema_property(schema, "title", ["Task", "Name"]):
        missing.append("Task or Name title")
    if prop_type(schema, "Offer URL") != "url":
        missing.append("Offer URL url")
    if missing:
        audit_log(f"todo schema not ready: missing {', '.join(missing)}")
        return False
    return True


def upsert_todo_for_offer(headers, offer, opened_on, todo_data_source_id, todo_schema, existing_todos):
    offer_url = (offer.get("offer_url") or "").strip()
    if not offer_url:
        audit_log(f"todo skipped: missing Offer URL | {offer_audit_label(offer)}")
        return 0, 0

    due_date = add_days(opened_on, 2)
    todo_existing = existing_todos.get(offer_url)
    todo_request = todo_payload(offer, opened_on, due_date, todo_data_source_id, todo_schema)

    if todo_existing:
        todo_page_id = todo_existing["page_id"]
        audit_log(
            f"todo action=update | page_id={short_id(todo_page_id)} | "
            f"due={due_date or '<empty>'} | payload_properties={sorted(todo_request['properties'].keys())} | "
            f"{offer_audit_label(offer)}"
        )
        response = requests.patch(
            f"https://api.notion.com/v1/pages/{todo_page_id}",
            headers=headers,
            json={"properties": todo_request["properties"]},
            timeout=30,
        )
        raise_for_notion(response, f"Notion todo update {todo_page_id}")
        audit_log(f"todo update ok | {page_audit_summary(response.json())} | {offer_audit_label(offer)}")
        return 0, 1

    audit_log(
        f"todo action=create | due={due_date or '<empty>'} | "
        f"payload_properties={sorted(todo_request['properties'].keys())} | {offer_audit_label(offer)}"
    )
    response = requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers,
        json=todo_request,
        timeout=30,
    )
    raise_for_notion(response, "Notion todo create")
    todo_page = response.json()
    existing_todos[offer_url] = {
        "page_id": todo_page.get("id"),
        "due": due_date,
    }
    audit_log(f"todo create ok | {page_audit_summary(todo_page)} | {offer_audit_label(offer)}")
    return 1, 0


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
    if not NOTION_DATA_SOURCE_ID:
        raise RuntimeError("Missing NOTION_DATA_SOURCE_ID environment variable")

    headers = notion_headers()
    audit_log(
        f"sync start: {len(open_offers)} offer(s), "
        f"NOTION_DATA_SOURCE_ID={short_id(NOTION_DATA_SOURCE_ID)}"
    )
    notion_data_source_id = resolve_data_source_id(NOTION_DATA_SOURCE_ID, "Notion internships")
    notion_schema = fetch_data_source_schema(notion_data_source_id)

    existing_offers = fetch_existing_offers(notion_data_source_id)
    created = 0
    updated = 0
    opened = 0
    skipped_no_url = 0
    created_offer_urls = set()
    opened_offer_urls = set()
    for offer in open_offers:
        payload = notion_payload(offer, notion_data_source_id, notion_schema)
        for key in ["Opening Date", "Closing Date"]:
            if key in payload["properties"] and payload["properties"][key] is None:
                del payload["properties"][key]
        offer_url = (offer.get("offer_url") or "").strip()
        audit_log(
            f"offer candidate: {offer_audit_label(offer)} | "
            f"payload_properties={sorted(payload['properties'].keys())}"
        )
        if not offer_url:
            skipped_no_url += 1
            audit_log(f"offer skipped: missing Offer URL | {offer_audit_label(offer)}")
            continue
        existing = existing_offers.get(offer_url)
        if existing:
            page_id = existing["page_id"]
            incoming_opening_date = offer.get("opening_date")
            previous_opening_date = existing.get("opening_date")
            newly_opened = bool(incoming_opening_date and not previous_opening_date)
            audit_log(
                f"offer action=update | page_id={short_id(page_id)} | "
                f"existing_status={existing.get('status') or '<empty>'} | "
                f"previous_opening={previous_opening_date or '<empty>'} | "
                f"incoming_opening={incoming_opening_date or '<empty>'} | "
                f"newly_opened={newly_opened} | {offer_audit_label(offer)}"
            )
            if newly_opened:
                status = status_property(notion_schema, "Status", ["Ouvert", "Opened"])
                if status:
                    payload["properties"]["Status"] = status
                opened += 1
                opened_offer_urls.add(offer_url)
            response = requests.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=headers,
                json={"properties": payload["properties"]},
                timeout=30,
            )
            raise_for_notion(response, f"Notion page update {page_id}")
            updated_page = response.json()
            audit_log(f"offer update ok | {page_audit_summary(updated_page)} | {offer_audit_label(offer)}")
            updated += 1
            existing_offers[offer_url]["opening_date"] = incoming_opening_date or previous_opening_date
        else:
            status = (
                status_property(notion_schema, "Status", ["Ouvert", "Opened"])
                if offer.get("opening_date")
                else status_property(notion_schema, "Status", ["Pas encore ouvert", "Closed"])
            )
            if status:
                payload["properties"]["Status"] = status
            else:
                audit_log(
                    "offer create warning: no matching Status option found for "
                    f"opening_date={offer.get('opening_date') or '<empty>'} | {offer_audit_label(offer)}"
                )
            audit_log(
                f"offer action=create | opening={offer.get('opening_date') or '<empty>'} | "
                f"status_property={'Status' in payload['properties']} | {offer_audit_label(offer)}"
            )
            response = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=30)
            raise_for_notion(response, "Notion page create")
            created_page = response.json()
            if offer_url:
                existing_offers[offer_url] = {
                    "page_id": created_page.get("id"),
                    "opening_date": offer.get("opening_date"),
                    "status": ((status or {}).get("status") or {}).get("name"),
                }
            created += 1
            created_offer_urls.add(offer_url)
            audit_log(f"offer create ok | {page_audit_summary(created_page)} | {offer_audit_label(offer)}")

    print(
        "Notion sync: "
        f"{created} créées, {updated} mises à jour, {opened} passées à Opened, "
        f"{skipped_no_url} sans URL ignorées"
    )
    return {
        "created_offer_urls": created_offer_urls,
        "opened_offer_urls": opened_offer_urls,
    }


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
    notion_result = sync_to_notion(offres)
    email_urls = notion_result["created_offer_urls"] | notion_result["opened_offer_urls"]
    email_offers = [offer for offer in new_offers if (offer.get("offer_url") or "").strip() in email_urls]
    if email_offers:
        print(f"{len(email_offers)} nouvelle(s) offre(s) détectée(s), envoi email")
        send_email(email_offers, csv_file)
    else:
        print("Aucune nouvelle offre détectée, email non envoyé")
  

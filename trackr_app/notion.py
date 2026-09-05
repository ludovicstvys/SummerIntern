import json
from urllib.parse import urlencode

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import NotionConnection, NotionSync, Offer, utcnow
from .security import decrypt, encrypt


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Notion-Version": settings.notion_version, "Content-Type": "application/json"}


def oauth_url(state: str) -> str:
    query = urlencode({"client_id": settings.notion_client_id, "response_type": "code", "owner": "user", "redirect_uri": f"{settings.app_url}/notion/callback", "state": state})
    return f"https://api.notion.com/v1/oauth/authorize?{query}"


def exchange_code(code: str) -> dict:
    response = requests.post(
        "https://api.notion.com/v1/oauth/token",
        auth=(settings.notion_client_id, settings.notion_client_secret),
        json={"grant_type": "authorization_code", "code": code, "redirect_uri": f"{settings.app_url}/notion/callback"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def save_connection(db: Session, user_id: int, payload: dict) -> NotionConnection:
    connection = db.scalar(select(NotionConnection).where(NotionConnection.user_id == user_id)) or NotionConnection(user_id=user_id, access_token_encrypted="")
    connection.access_token_encrypted = encrypt(payload["access_token"])
    connection.refresh_token_encrypted = encrypt(payload.get("refresh_token"))
    connection.workspace_id = payload.get("workspace_id")
    connection.workspace_name = payload.get("workspace_name")
    connection.last_error = None
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection


def accessible_pages(connection: NotionConnection) -> list[dict]:
    response = requests.post("https://api.notion.com/v1/search", headers=headers(decrypt(connection.access_token_encrypted)), json={"filter": {"property": "object", "value": "page"}, "page_size": 100}, timeout=30)
    response.raise_for_status()
    pages = []
    for page in response.json().get("results", []):
        title_parts = []
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                title_parts = prop.get("title") or []
                break
        title = "".join(part.get("plain_text", "") for part in title_parts)
        pages.append({"id": page["id"], "title": title or "Untitled page"})
    return pages


def create_offer_database(db: Session, connection: NotionConnection, parent_page_id: str) -> None:
    token = decrypt(connection.access_token_encrypted)
    schema = {
        "Name": {"title": {}}, "Company": {"rich_text": {}}, "Offer URL": {"url": {}},
        "Region": {"select": {}}, "Programme Type": {"select": {}}, "Start Term": {"rich_text": {}},
        "Categories": {"multi_select": {}}, "Opening Date": {"date": {}}, "Closing Date": {"date": {}},
        "Stage": {"select": {}}, "Rolling": {"checkbox": {}}, "Needs CV": {"checkbox": {}},
        "Needs Cover Letter": {"checkbox": {}}, "Notes": {"rich_text": {}}, "Status": {"select": {}},
    }
    response = requests.post(
        "https://api.notion.com/v1/databases", headers=headers(token),
        json={"parent": {"type": "page_id", "page_id": parent_page_id}, "title": [{"type": "text", "text": {"content": "Internship Opportunities"}}], "initial_data_source": {"properties": schema}}, timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    connection.database_id = result["id"]
    sources = result.get("data_sources") or []
    connection.data_source_id = sources[0]["id"] if sources else None
    if not connection.data_source_id:
        detail = requests.get(f"https://api.notion.com/v1/databases/{connection.database_id}", headers=headers(token), timeout=30)
        detail.raise_for_status()
        connection.data_source_id = detail.json()["data_sources"][0]["id"]
    connection.last_error = None
    db.commit()


def _rich(value):
    return {"rich_text": [{"text": {"content": str(value)[:1900]}}]} if value else {"rich_text": []}


def offer_properties(offer: Offer) -> dict:
    categories = json.loads(offer.categories or "[]")
    def date_prop(value): return {"date": {"start": value.isoformat()}} if value else {"date": None}
    return {
        "Name": {"title": [{"text": {"content": offer.name[:1900]}}]}, "Company": _rich(offer.company),
        "Offer URL": {"url": offer.canonical_url}, "Region": {"select": {"name": offer.region}},
        "Programme Type": {"select": {"name": offer.programme_type}}, "Start Term": _rich(offer.start_term),
        "Categories": {"multi_select": [{"name": str(item)[:100]} for item in categories]},
        "Opening Date": date_prop(offer.opening_date), "Closing Date": date_prop(offer.closing_date),
        "Stage": {"select": {"name": offer.stage[:100]}}, "Rolling": {"checkbox": offer.rolling},
        "Needs CV": {"checkbox": offer.needs_cv}, "Needs Cover Letter": {"checkbox": offer.needs_cover_letter},
        "Notes": _rich(offer.notes or offer.company_description), "Status": {"select": {"name": "Open" if offer.is_open else "Closed"}},
    }


def process_notion_queue(db: Session) -> int:
    jobs = db.scalars(select(NotionSync).where(NotionSync.status == "pending", NotionSync.attempts < 5)).all()
    completed = 0
    for job in jobs:
        connection = db.get(NotionConnection, job.connection_id)
        offer = db.get(Offer, job.offer_id)
        if not connection or not connection.data_source_id or not offer:
            continue
        token = decrypt(connection.access_token_encrypted)
        try:
            if not job.notion_page_id:
                lookup = requests.post(
                    f"https://api.notion.com/v1/data_sources/{connection.data_source_id}/query",
                    headers=headers(token),
                    json={"filter": {"property": "Offer URL", "url": {"equals": offer.canonical_url}}, "page_size": 1},
                    timeout=30,
                )
                lookup.raise_for_status()
                matches = lookup.json().get("results") or []
                if matches:
                    job.notion_page_id = matches[0]["id"]
            if job.notion_page_id:
                response = requests.patch(f"https://api.notion.com/v1/pages/{job.notion_page_id}", headers=headers(token), json={"properties": offer_properties(offer)}, timeout=30)
            else:
                response = requests.post("https://api.notion.com/v1/pages", headers=headers(token), json={"parent": {"data_source_id": connection.data_source_id}, "properties": offer_properties(offer)}, timeout=30)
            response.raise_for_status()
            job.notion_page_id = response.json()["id"]
            job.status, job.synced_at, job.last_error = "synced", utcnow(), None
            connection.last_error = None
            completed += 1
        except Exception as exc:
            job.attempts += 1
            job.last_error = str(exc)[:2000]
            job.status = "failed" if job.attempts >= 5 else "pending"
            connection.last_error = job.last_error
        db.commit()
    return completed

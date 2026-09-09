from .runtime import guarded
import json
import time
from urllib.parse import urlencode

import requests
from sqlalchemy import select, delete, or_, update
from sqlalchemy.orm import Session

from .config import settings
from .models import NotionConnection, NotionSync, Offer, User, utcnow
from .security import decrypt, encrypt
from .operations import error_code, next_retry


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
    if connection.id and connection.workspace_id != payload.get("workspace_id"):
        db.execute(delete(NotionSync).where(NotionSync.connection_id == connection.id))
        connection.database_id = None
        connection.data_source_id = None
        connection.setup_status, connection.parent_page_id = 'idle', None
    connection.access_token_encrypted = encrypt(payload["access_token"])
    connection.refresh_token_encrypted = encrypt(payload.get("refresh_token"))
    connection.workspace_id = payload.get("workspace_id")
    connection.workspace_name = payload.get("workspace_name")
    connection.last_error = None
    db.add(connection)
    db.flush()
    db.execute(update(NotionSync).where(NotionSync.connection_id == connection.id, NotionSync.status == 'failed').values(status='pending', attempts=0, next_attempt_at=None, last_error=None))
    db.commit()
    db.refresh(connection)
    return connection


def require_runtime(db):
    from sqlalchemy import inspect
    from fastapi import HTTPException
    if not inspect(db.connection()).has_table('durable_jobs'):
        raise HTTPException(503, 'Notion setup is temporarily unavailable during migration')


def accessible_pages(connection: NotionConnection, cursor=None):
    payload = {'filter': {'property': 'object', 'value': 'page'}, 'page_size': 50}
    if cursor:
        payload['start_cursor'] = cursor
    response = requests.post('https://api.notion.com/v1/search',
        headers=headers(decrypt(connection.access_token_encrypted)), json=payload, timeout=20)
    response.raise_for_status()
    data = response.json()
    pages = []
    for page in data.get('results', []):
        parts = next((p.get('title') or [] for p in page.get('properties', {}).values()
                      if p.get('type') == 'title'), [])
        pages.append({'id': page['id'], 'title': ''.join(p.get('plain_text', '') for p in parts) or 'Untitled page'})
    return pages, data.get('next_cursor') if data.get('has_more') else None


def create_remote_database(connection, parent_page_id):
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
    database_id = result['id']
    sources = result.get('data_sources') or []
    try:
        if not sources:
            detail = requests.get(f'https://api.notion.com/v1/databases/{database_id}', headers=headers(token), timeout=20)
            detail.raise_for_status()
            sources = detail.json()['data_sources']
        return database_id, sources[0]['id']
    except Exception as exc:
        exc.database_id = database_id
        raise


def create_offer_database(db: Session, connection: NotionConnection, parent_page_id: str) -> None:
    database_id, source_id = create_remote_database(connection, parent_page_id)
    db.execute(delete(NotionSync).where(NotionSync.connection_id == connection.id))
    connection.database_id, connection.data_source_id = database_id, source_id
    connection.last_error, connection.setup_status, connection.parent_page_id = None, 'ready', parent_page_id
    db.flush()


def _rich(value):
    return {"rich_text": [{"text": {"content": str(value)[:1900]}}]} if value else {"rich_text": []}


def recover_database(db, connection, database_id):
    import uuid
    database_id = str(uuid.UUID(database_id))
    response = requests.get(f'https://api.notion.com/v1/databases/{database_id}', headers=headers(decrypt(connection.access_token_encrypted)), timeout=30)
    response.raise_for_status()
    data = response.json()
    if connection.parent_page_id and data.get('parent', {}).get('page_id', '').replace('-', '') != connection.parent_page_id.replace('-', ''):
        raise ValueError('Database belongs to another parent')
    source_id = data['data_sources'][0]['id']
    response = requests.get(f'https://api.notion.com/v1/data_sources/{source_id}', headers=headers(decrypt(connection.access_token_encrypted)), timeout=30)
    response.raise_for_status()
    schema = response.json()['properties']
    expected = {'Name': 'title', 'Company': 'rich_text', 'Offer URL': 'url', 'Region': 'select', 'Programme Type': 'select', 'Start Term': 'rich_text', 'Categories': 'multi_select', 'Opening Date': 'date', 'Closing Date': 'date', 'Stage': 'select', 'Rolling': 'checkbox', 'Needs CV': 'checkbox', 'Needs Cover Letter': 'checkbox', 'Notes': 'rich_text', 'Status': 'select'}
    if any(schema.get(k, {}).get('type') != v for k, v in expected.items()):
        raise ValueError('Database schema does not match Trackr')
    connection.database_id, connection.data_source_id = database_id, source_id
    connection.setup_status, connection.last_error = 'ready', None
    db.flush()


def offer_properties(offer: Offer) -> dict:
    categories = json.loads(offer.categories or "[]")
    def date_prop(value): return {"date": {"start": value.isoformat()}} if value else {"date": None}
    return {
        "Name": {"title": [{"text": {"content": offer.name[:1900]}}]}, "Company": _rich(offer.company),
        "Offer URL": {"url": offer.canonical_url}, "Region": {"select": {"name": offer.region_label}},
        "Programme Type": {"select": {"name": offer.programme_label}}, "Start Term": _rich(offer.source_label('start_term')),
        "Categories": {"multi_select": [{"name": str(item)[:100]} for item in categories]},
        "Opening Date": date_prop(offer.opening_date), "Closing Date": date_prop(offer.closing_date),
        "Stage": {"select": {"name": offer.stage[:100]}}, "Rolling": {"checkbox": offer.rolling},
        "Needs CV": {"checkbox": offer.needs_cv}, "Needs Cover Letter": {"checkbox": offer.needs_cover_letter},
        "Notes": _rich(offer.notes or offer.company_description), "Status": {"select": {"name": "Open" if offer.is_open else "Closed"}},
    }


def _notion_available():
    return (NotionSync.status.in_(['pending', 'processing']) &
        or_(NotionSync.next_attempt_at.is_(None), NotionSync.next_attempt_at <= utcnow()))


def _process_notion_job(db, job_id, user_id, deadline):
    from datetime import timedelta
    user = db.scalar(select(User).where(User.id == user_id).with_for_update(skip_locked=True)
        .execution_options(populate_existing=True))
    if not user:
        db.rollback()
        return False
    job = db.scalar(select(NotionSync).where(NotionSync.id == job_id, _notion_available())
        .with_for_update().execution_options(populate_existing=True))
    if not job:
        db.commit()
        return False
    if not user.is_active:
        job.status = 'cancelled'; db.commit()
        return False
    if job.attempts >= 5:
        job.status, job.last_error = 'failed', job.last_error or 'NotionLeaseExhausted'
        db.commit()
        return False
    connection = db.get(NotionConnection, job.connection_id)
    offer = db.get(Offer, job.offer_id)
    if not connection or not connection.data_source_id or not offer:
        db.commit()
        return False
    job.status, job.next_attempt_at = 'processing', utcnow() + timedelta(minutes=5)
    job.attempts += 1
    lease, connection_id = job.next_attempt_at, connection.id
    # Materialize every lazy attribute before releasing the transaction.
    failure, page_id = None, job.notion_page_id
    try:
        token, source_id = decrypt(connection.access_token_encrypted), connection.data_source_id
        properties, url = offer_properties(offer), offer.canonical_url
    except Exception as exc:
        failure = error_code(exc)
    db.commit()
    if not failure:
        try:
            def budget():
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    raise TimeoutError('Notion invocation budget exhausted')
                return min(30, remaining)
            if not page_id:
                lookup = requests.post(f'https://api.notion.com/v1/data_sources/{source_id}/query',
                    headers=headers(token), json={'filter': {'property': 'Offer URL',
                    'url': {'equals': url}}, 'page_size': 1}, timeout=budget())
                lookup.raise_for_status()
                matches = lookup.json().get('results') or []
                if matches:
                    page_id = matches[0]['id']
            if page_id:
                response = requests.patch(f'https://api.notion.com/v1/pages/{page_id}',
                    headers=headers(token), json={'properties': properties}, timeout=budget())
            else:
                response = requests.post('https://api.notion.com/v1/pages', headers=headers(token),
                    json={'parent': {'data_source_id': source_id}, 'properties': properties}, timeout=budget())
            response.raise_for_status()
            page_id = response.json()['id']
        except Exception as exc:
            failure = error_code(exc)
    # Preserve user -> job lock ordering and ignore a revoked/replaced claim.
    user = db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
    job = db.scalar(select(NotionSync).where(NotionSync.id == job_id,
        NotionSync.status == 'processing', NotionSync.next_attempt_at == lease)
        .with_for_update().execution_options(populate_existing=True))
    if not job:
        db.commit()
        return False
    connection = db.get(NotionConnection, connection_id, populate_existing=True)
    if not user or not user.is_active or not connection:
        job.status = 'cancelled'
        db.commit()
        return False
    if failure:
        job.last_error, job.next_attempt_at = failure, next_retry(job.attempts)
        job.status = 'failed' if job.attempts >= 5 or failure == 'InvalidToken' else 'pending'
        if connection:
            connection.last_error = failure
    else:
        job.notion_page_id = page_id
        job.status, job.synced_at, job.last_error, job.next_attempt_at = 'synced', utcnow(), None, None
        job.attempts = max(0, job.attempts - 1)
        if connection:
            connection.last_error = None
    db.commit()
    return failure is None


@guarded(auth=False)
def process_notion_queue(db: Session) -> int:
    deadline = time.monotonic() + 90
    jobs = db.execute(select(NotionSync.id, NotionConnection.user_id)
        .join(NotionConnection, NotionConnection.id == NotionSync.connection_id)
        .where(_notion_available()).order_by(NotionSync.id).limit(100)).all()
    db.commit()
    completed = 0
    for job_id, user_id in jobs:
        if time.monotonic() >= deadline:
            break
        try:
            completed += _process_notion_job(db, job_id, user_id, deadline)
        except Exception as exc:
            db.rollback()
            print(f'Notion job deferred: {error_code(exc)}')
    return completed

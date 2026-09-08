from datetime import timedelta
from sqlalchemy import select, func
from .models import AuthMail, Delivery, Invitation, NotionSync, WorkerState, utcnow
from .config import settings
from .scraper import TRACKERS


def operational_status(db):
    now = utcnow()
    states = {s.key: s for s in db.scalars(select(WorkerState)).all()}
    required = ['process-invitations', 'process-immediate-alerts', 'process-digests']
    required += ['source/' + '/'.join((p.get('season', settings.season), p['region'], p['type'])) for p in TRACKERS]
    if settings.notion_available:
        required.append('sync-notion')
    stale = []
    errors = []
    for key in required:
        state = states.get(key)
        if not state or not state.last_success_at or state.last_success_at.replace(tzinfo=state.last_success_at.tzinfo or now.tzinfo) < now - timedelta(minutes=30):
            stale.append(key)
        if state and state.last_error:
            errors.append(key)
    failures = db.scalar(select(func.count()).select_from(Delivery).where(Delivery.status == 'failed'))
    failures += db.scalar(select(func.count()).select_from(Invitation).where(Invitation.delivery_status == 'failed'))
    failures += db.scalar(select(func.count()).select_from(AuthMail).where(AuthMail.status == 'failed', AuthMail.invitation_id.is_(None)))
    auth_delayed = db.scalar(select(func.count()).select_from(AuthMail).where(AuthMail.status.in_(['pending', 'processing']), AuthMail.created_at < now-timedelta(minutes=15)))
    if settings.notion_available:
        failures += db.scalar(select(func.count()).select_from(NotionSync).where(NotionSync.status == 'failed'))
    # Daily digests intentionally wait until the subscriber's chosen local time.
    delayed = db.scalar(select(func.count()).select_from(Delivery).where(Delivery.mode == 'immediate', Delivery.status.in_(['pending', 'processing']), Delivery.created_at < now-timedelta(hours=1)))
    return {'status': 'degraded' if stale or errors or failures or delayed or auth_delayed else 'ok', 'stale_workers': stale, 'workers_with_errors': errors, 'failed_tasks': failures, 'delayed_immediate_alerts': delayed, 'delayed_auth_emails': auth_delayed}

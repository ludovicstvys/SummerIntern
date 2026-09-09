from datetime import timedelta
from sqlalchemy import select, func
from .models import AuthMail, Delivery, Invitation, NotionSync, WorkerState, LegacyTask, DurableJob, utcnow
from .config import settings
from .scraper import TRACKERS


def operational_status(db):
    now = utcnow()
    states = {s.key: s for s in db.scalars(select(WorkerState)).all()}
    required = ['process-auth-mail', 'process-invitations', 'process-immediate-alerts', 'process-digests']
    required += ['source/' + '/'.join((p.get('season', settings.season), p['region'], p['type'])) for p in TRACKERS]
    if settings.notion_available:
        required.extend(['sync-notion', 'process-notion-setup'])
    required.append('process-matches')
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
    from .legacy import legacy_notion_enabled
    legacy_filter = [] if legacy_notion_enabled() else [LegacyTask.channel != 'notion']
    failures += db.scalar(select(func.count()).select_from(LegacyTask).where(*legacy_filter, LegacyTask.status == 'failed'))
    legacy_delayed = db.scalar(select(func.count()).select_from(LegacyTask).where(*legacy_filter,
        LegacyTask.status.in_(['pending', 'processing']), LegacyTask.created_at < now-timedelta(hours=1)))
    failures += db.scalar(select(func.count()).select_from(DurableJob).where(DurableJob.status.in_(['failed', 'uncertain'])))
    queues = {}
    for name, model in [('auth', AuthMail), ('alerts', Delivery), ('legacy', LegacyTask), ('durable', DurableJob)]:
        rows = db.execute(select(model.status, func.count(), func.min(model.created_at))
            .group_by(model.status)).all()
        queues[name] = {status: {'count': count, 'oldest_created_at': oldest.isoformat() if oldest else None}
            for status, count, oldest in rows}
    executions = {key.removeprefix('execution/'): {'last_execution': state.last_success_at.isoformat() if state.last_success_at else None, 'outcome': state.last_error or 'ok'} for key, state in states.items() if key.startswith('execution/')}
    interrupted = db.scalar(select(func.count()).select_from(DurableJob).where(DurableJob.status == 'processing', DurableJob.lease_until < now))
    # Daily digests intentionally wait until the subscriber's chosen local time.
    delayed = db.scalar(select(func.count()).select_from(Delivery).where(Delivery.mode == 'immediate', Delivery.status.in_(['pending', 'processing']), Delivery.created_at < now-timedelta(hours=1)))
    return {'status': 'degraded' if stale or errors or failures or delayed or auth_delayed or legacy_delayed or interrupted else 'ok', 'stale_workers': stale, 'workers_with_errors': errors, 'failed_tasks': failures, 'delayed_immediate_alerts': delayed, 'delayed_auth_emails': auth_delayed, 'delayed_legacy_tasks': legacy_delayed, 'queues': queues, 'executions': executions, 'interrupted_jobs': interrupted}

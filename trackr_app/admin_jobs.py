"""Administrative inspection and single-job retries; never bulk resend."""
from sqlalchemy import select
from .models import AuthMail, Delivery, Invitation, LegacyTask, NotionSync, DurableJob

MODELS = {'auth': AuthMail, 'email': Delivery, 'invitation': Invitation,
          'legacy': LegacyTask, 'notion': NotionSync, 'durable': DurableJob}


def failed_jobs(db, kind, after=''):
    model = MODELS[kind]
    identity = model.key if kind in ('legacy', 'durable') else model.id
    status = model.delivery_status if kind == 'invitation' else model.status
    query = select(model).where(status.in_(['failed', 'uncertain'])).order_by(identity).limit(50)
    if after:
        query = query.where(identity > (after if kind in ('legacy', 'durable') else int(after)))
    return [{'id': str(getattr(row, 'key' if kind in ('legacy', 'durable') else 'id')),
        'status': getattr(row, 'delivery_status' if kind == 'invitation' else 'status'),
        'attempts': row.attempts, 'error': row.last_error} for row in db.scalars(query)]


def retry(db, kind, identity):
    model = MODELS[kind]
    identity = identity if kind in ('legacy', 'durable') else int(identity)
    task = db.get(model, identity, with_for_update=True, populate_existing=True)
    column = 'delivery_status' if kind == 'invitation' else 'status'
    if not task or getattr(task, column) != 'failed':
        raise ValueError('Only a failed job can be retried; uncertain creation requires recovery')
    setattr(task, column, 'pending')
    task.attempts, task.last_error, task.next_attempt_at = 0, None, None
    if kind == 'durable':
        task.lease_until, task.lease_token = None, None
    db.commit()

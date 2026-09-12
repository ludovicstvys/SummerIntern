from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tests.test_audit import db, offer, user
from trackr_app.models import AuthMail, Delivery, utcnow
from trackr_app.monitoring import operational_status
from trackr_app.workers import process_immediate_alerts


ROOT = Path(__file__).resolve().parents[1]


def _workflow():
    # BaseLoader keeps GitHub's `on` key as a string rather than YAML 1.1's bool.
    return yaml.load(
        (ROOT / '.github/workflows/email-delivery.yml').read_text(),
        Loader=yaml.BaseLoader,
    )


def test_email_delivery_workflow_is_the_single_scheduled_mail_sender():
    workflow = _workflow()
    assert workflow['name'] == 'Trackr email delivery'
    assert workflow['on']['schedule'] == [{'cron': '*/10 * * * *'}]
    assert 'workflow_dispatch' in workflow['on']

    job = workflow['jobs']['process']
    assert job['environment'] == 'production'
    steps = job['steps']
    expected = [
        ('auth', 'timeout 100s python -m trackr_app.cli process-auth-mail'),
        ('invitations', 'timeout 100s python -m trackr_app.cli process-invitations'),
        ('immediate', 'timeout 150s python -m trackr_app.cli process-immediate-alerts'),
        ('digests', 'timeout 150s python -m trackr_app.cli process-digests'),
    ]
    delivery_steps = [(step.get('id'), step.get('run')) for step in steps if step.get('id') in dict(expected)]
    assert delivery_steps == expected
    for step in steps:
        if step.get('id') in dict(expected):
            assert step['continue-on-error'] == 'true'

    report = next(step for step in steps if step.get('run') == 'exit 1')
    assert all(f"steps.{step_id}.outcome == 'failure'" in report['if'] for step_id, _ in expected)

    assert not (ROOT / '.github/workflows/authentication.yml').exists()
    platform = (ROOT / '.github/workflows/platform-jobs.yml').read_text()
    assert 'process-immediate-alerts' not in platform
    assert 'process-digests' not in platform

    schedules = (ROOT / 'scripts/worker_schedules.py').read_text()
    assert 'email-delivery.yml' in schedules
    assert 'authentication.yml' not in schedules


def test_operations_marks_auth_mail_delayed_only_after_thirty_minutes(db):
    now = utcnow()
    db.add(AuthMail(
        email_encrypted='encrypted-recent', email_hash='recent', kind='magic',
        status='pending', created_at=now - timedelta(minutes=29),
    ))
    db.commit()
    with patch('trackr_app.monitoring.utcnow', return_value=now):
        assert operational_status(db)['delayed_auth_emails'] == 0

    db.add(AuthMail(
        email_encrypted='encrypted-old', email_hash='old', kind='password',
        status='processing', created_at=now - timedelta(minutes=31),
    ))
    db.commit()
    with patch('trackr_app.monitoring.utcnow', return_value=now):
        assert operational_status(db)['delayed_auth_emails'] == 1


@pytest.mark.parametrize('eligibility_change', ['account', 'delivery_mode', 'preferences'])
def test_queued_immediate_alert_is_cancelled_when_worker_revalidates_eligibility(
    db, user, offer, eligibility_change,
):
    user.preference.delivery_mode = 'immediate'
    delivery = Delivery(user_id=user.id, offer_id=offer.id, mode='immediate')
    db.add(delivery)
    db.commit()

    if eligibility_change == 'account':
        user.is_active = False
    elif eligibility_change == 'delivery_mode':
        user.preference.delivery_mode = 'daily_digest'
    else:
        user.preference.regions = '["UK"]'
    db.commit()

    with patch('trackr_app.workers.send_email') as sender:
        assert process_immediate_alerts(db) == 0
    sender.assert_not_called()
    db.refresh(delivery)
    assert delivery.status == 'cancelled'

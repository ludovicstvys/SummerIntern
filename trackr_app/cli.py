import argparse
import json
import time
from datetime import datetime, timezone

from sqlalchemy import func, select, delete

from .database import SessionLocal
from .models import Delivery, NotionSync, Invitation, User, Preference, LegacyTask, UserSession, MagicLink, WorkerState, utcnow
from .config import settings
from .invitations import process_invitations
from .operations import lock_state, insert_for
from .preferences import activate_preference
from .scraper import scrape_all
from .workers import process_digests, process_immediate_alerts, sync_notion
from .legacy import reconcile_summer_snapshot, cancel_legacy_notion_window, sync_last_email_offers


def run_command(command):
    with SessionLocal() as db:
        if command == 'sync-notion' and not settings.notion_available:
            print(json.dumps({'command': command, 'status': 'disabled'}))
            return 0
        if command == "scrape-all":
            result = scrape_all(db)
            print(json.dumps(result), flush=True)
            return int(result["failed_trackers"] > 0)
        model = Invitation if command == 'process-invitations' else NotionSync if command == "sync-notion" else Delivery
        filters = [Delivery.mode == ("immediate" if command == "process-immediate-alerts" else "daily_digest")] if model is Delivery else []
        before = db.scalar(select(func.coalesce(func.sum(model.attempts), 0)).where(*filters))
        count = {"process-immediate-alerts": process_immediate_alerts, "process-digests": process_digests, "sync-notion": sync_notion, 'process-invitations': process_invitations}[command](db)
        after = db.scalar(select(func.coalesce(func.sum(model.attempts), 0)).where(*filters))
        status_column = Invitation.delivery_status if model is Invitation else model.status
        statuses = dict(db.execute(select(status_column, func.count()).where(*filters).group_by(status_column)).all())
        print(json.dumps({"command": command, "completed": count, "deferred": statuses.get("pending", 0) + statuses.get("processing", 0), "failed": statuses.get("failed", 0), "errors_this_run": max(0, after - before)}), flush=True)
        failed = int(after > before or statuses.get("failed", 0) > 0)
        state = lock_state(db, command)
        state.last_error = 'Processing errors' if failed else None
        if not failed:
            state.last_success_at = utcnow()
        db.commit()
        return failed


def maintenance(args):
    with SessionLocal() as db:
        if args.command == 'promote-admin':
            if not args.email:
                raise ValueError('--email is required')
            from email_validator import validate_email
            email = validate_email(args.email, check_deliverability=False).normalized.lower()
            db.execute(insert_for(db, User).values(email=email).on_conflict_do_nothing(index_elements=['email']))
            user = db.scalar(select(User).where(User.email == email).with_for_update())
            user.role, user.is_active = 'admin', True
            db.execute(insert_for(db, Preference).values(user_id=user.id).on_conflict_do_nothing(index_elements=['user_id']))
            db.execute(delete(UserSession).where(UserSession.user_id == user.id))
            db.execute(delete(MagicLink).where(MagicLink.user_id == user.id))
            db.commit()
            print(json.dumps({'promoted_user_id': user.id, 'sessions_revoked': True}))
        elif args.command == 'retry-failed':
            if not args.kind or not args.id:
                raise ValueError('--kind and --id are required for a targeted retry')
            model = {'email': Delivery, 'notion': NotionSync, 'invitation': Invitation, 'legacy': LegacyTask}[args.kind]
            identity = args.id if model is LegacyTask else int(args.id)
            task = db.get(model, identity, with_for_update=True)
            column = 'delivery_status' if model is Invitation else 'status'
            if not task or getattr(task, column) != 'failed':
                raise ValueError('Task is not failed')
            setattr(task, column, 'pending')
            task.attempts, task.last_error, task.next_attempt_at = 0, None, None
            db.commit()
            print(json.dumps({'retried_kind': args.kind, 'id': args.id}))
        elif args.command == 'import-legacy-subscribers':
            import test as legacy
            count = 0
            for email in legacy.read_email_recipients():
                from email_validator import validate_email
                email = validate_email(email, check_deliverability=False).normalized.lower()
                identity = db.scalar(insert_for(db, User).values(email=email).on_conflict_do_nothing(index_elements=['email']).returning(User.id))
                if not identity:
                    continue  # Never reactivate a disabled existing account.
                pref = Preference(user_id=identity)
                db.add(pref); db.flush()
                activate_preference(db, pref, commit=False)
                count += 1
            db.commit()
            print(json.dumps({'imported': count, 'email_sent': False}))
        elif args.command == 'check-operations':
            from .monitoring import operational_status
            result = operational_status(db)
            print(json.dumps(result))
            return int(result['status'] != 'ok')
        elif args.command == 'reconcile-legacy-summer':
            import test as legacy_adapter
            result = reconcile_summer_snapshot(legacy_adapter, args.snapshot_dir, apply=args.apply)
            print(json.dumps(result, default=str))
        elif args.command == 'sync-last-email-offers':
            import test as legacy_adapter
            print(json.dumps(sync_last_email_offers(legacy_adapter, args.snapshot_dir, apply=args.apply)))
        elif args.command == 'remediate-legacy-notion':
            start = datetime.fromisoformat(args.start.replace('Z', '+00:00')) if args.start else datetime(2026, 9, 7, 12, 21, 0, tzinfo=timezone.utc)
            end = datetime.fromisoformat(args.end.replace('Z', '+00:00')) if args.end else datetime(2026, 9, 7, 12, 30, 0, tzinfo=timezone.utc)
            if start.tzinfo is None or end.tzinfo is None:
                raise ValueError('--start and --end must include a timezone')
            print(json.dumps(cancel_legacy_notion_window(db, start, end, apply=args.apply)))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Trackr Alerts background commands")
    parser.add_argument("command", choices=("scrape-all", "process-immediate-alerts", "process-digests", "digest-worker", "sync-notion", 'process-invitations', 'retry-failed', 'promote-admin', 'import-legacy-subscribers', 'check-operations', 'reconcile-legacy-summer', 'sync-last-email-offers', 'remediate-legacy-notion'))
    parser.add_argument('--kind', choices=['email', 'notion', 'invitation', 'legacy'])
    parser.add_argument('--id')
    parser.add_argument('--email')
    parser.add_argument('--apply', action='store_true', help='perform the otherwise read-only remediation or reconciliation')
    parser.add_argument('--snapshot-dir', default='.')
    parser.add_argument('--start')
    parser.add_argument('--end')
    args = parser.parse_args()
    command = args.command
    if command in ('retry-failed', 'promote-admin', 'import-legacy-subscribers', 'check-operations', 'reconcile-legacy-summer', 'sync-last-email-offers', 'remediate-legacy-notion'):
        raise SystemExit(maintenance(args))
    if command == "digest-worker":
        while True:
            try:
                run_command("process-digests")
            except Exception as exc:
                print(json.dumps({"digest_worker_error": type(exc).__name__}), flush=True)
            time.sleep(60)
    try:
        status = run_command(command)
    except Exception as exc:
        print(json.dumps({"command": command, "error": type(exc).__name__}), flush=True)
        status = 1
    raise SystemExit(status)


if __name__ == "__main__":
    main()

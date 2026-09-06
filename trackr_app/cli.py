import argparse
import json
import time

from sqlalchemy import func, select

from .database import SessionLocal
from .models import Delivery, NotionSync
from .scraper import scrape_all
from .workers import process_digests, process_immediate_alerts, sync_notion


def run_command(command):
    with SessionLocal() as db:
        if command == "scrape-all":
            result = scrape_all(db)
            print(json.dumps(result), flush=True)
            return int(result["failed_trackers"] > 0)
        model = NotionSync if command == "sync-notion" else Delivery
        filters = [] if model is NotionSync else [Delivery.mode == ("immediate" if command == "process-immediate-alerts" else "daily_digest")]
        before = db.scalar(select(func.coalesce(func.sum(model.attempts), 0)).where(*filters))
        count = {"process-immediate-alerts": process_immediate_alerts, "process-digests": process_digests, "sync-notion": sync_notion}[command](db)
        after = db.scalar(select(func.coalesce(func.sum(model.attempts), 0)).where(*filters))
        statuses = dict(db.execute(select(model.status, func.count()).where(*filters).group_by(model.status)).all())
        print(json.dumps({"command": command, "completed": count, "deferred": statuses.get("pending", 0) + statuses.get("processing", 0), "failed": statuses.get("failed", 0), "errors_this_run": max(0, after - before)}), flush=True)
        return int(after > before or statuses.get("failed", 0) > 0)


def main():
    parser = argparse.ArgumentParser(description="Trackr Alerts background commands")
    parser.add_argument("command", choices=("scrape-all", "process-immediate-alerts", "process-digests", "digest-worker", "sync-notion"))
    command = parser.parse_args().command
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

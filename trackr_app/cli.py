import argparse
import time

from .database import SessionLocal
from .scraper import scrape_all
from .workers import process_digests, process_immediate_alerts, sync_notion


def main():
    parser = argparse.ArgumentParser(description="Trackr Alerts background commands")
    parser.add_argument("command", choices=("scrape-all", "process-immediate-alerts", "process-digests", "digest-worker", "sync-notion"))
    command = parser.parse_args().command
    if command == "digest-worker":
        while True:
            try:
                with SessionLocal() as db:
                    print({"digests_sent": process_digests(db)}, flush=True)
            except Exception as exc:
                print({"digest_worker_error": str(exc)}, flush=True)
            time.sleep(60)
        return
    with SessionLocal() as db:
        if command == "scrape-all":
            print(scrape_all(db))
        elif command == "process-immediate-alerts":
            print({"emails_sent": process_immediate_alerts(db)})
        elif command == "process-digests":
            print({"digests_sent": process_digests(db)})
        else:
            print({"notion_synced": sync_notion(db)})


if __name__ == "__main__":
    main()

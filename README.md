# Trackr Alerts

Private, invitation-only alerts for Trackr finance internships. Subscribers choose programme types, regions, off-cycle start terms, immediate or daily delivery, and can connect a personal Notion workspace.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn trackr_app.main:app --reload
```

Set `ADMIN_EMAIL`; the account is created on application startup. Request its first magic link from `/login`. In local development, a failed Resend delivery prints the link to the server log.

Generate a valid encryption key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

## Background commands

```bash
python -m trackr_app.cli scrape-all
python -m trackr_app.cli process-immediate-alerts
python -m trackr_app.cli process-digests
python -m trackr_app.cli digest-worker
python -m trackr_app.cli sync-notion
```

On Railway, deploy the web service using `railway.json`, add PostgreSQL, then create services from the same repository. Run scraping and immediate delivery every ten minutes (`python -m trackr_app.cli scrape-all && python -m trackr_app.cli process-immediate-alerts && python -m trackr_app.cli sync-notion`). Railway cron has a five-minute minimum, so use a small persistent worker (`python -m trackr_app.cli digest-worker`) to evaluate user-local digest times every minute. Keep the legacy GitHub Actions workflow enabled until staging output has been compared, then disable its schedule to avoid duplicate collection.

## Notion

Create a public Notion integration whose OAuth redirect URI is `${APP_URL}/notion/callback`. Each subscriber authorizes pages, selects an accessible parent page, and the platform creates an `Internship Opportunities` database there.

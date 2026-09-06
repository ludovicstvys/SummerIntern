# Trackr Alerts

Private, invitation-only alerts for Trackr finance internships. Subscribers choose programme types, regions, off-cycle start terms, immediate or daily SMTP delivery, and can connect a personal Notion workspace.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn trackr_app.main:app --reload
```

Set `ADMIN_EMAIL`; the account is created on application startup. Request its first magic link from `/login`. In local development, a failed SMTP delivery prints the link to the server log.

Generate a valid encryption key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

## Background commands

```bash
python -m trackr_app.cli scrape-all
python -m trackr_app.cli process-immediate-alerts
python -m trackr_app.cli process-digests
python -m trackr_app.cli digest-worker
python -m trackr_app.cli sync-notion
```

Production uses Vercel for FastAPI, Neon PostgreSQL for shared state, Gmail SMTP for delivery, and GitHub Actions for scheduled jobs. `platform-jobs.yml` runs collection, immediate alerts, delayed-safe daily digests, and Notion synchronization every five minutes. The legacy CSV/Notion workflow remains enabled independently.

## Production bootstrap

1. Create a Neon database through the Vercel integration and copy its pooled `DATABASE_URL`.
2. Create the Vercel project `trackr-alerts`, linked to this repository, and keep its stable URL `https://trackr-alerts.vercel.app`.
3. Add the web variables from `.env.example` to Vercel Production, setting `ENVIRONMENT=production` and `APP_URL=https://trackr-alerts.vercel.app`.
4. Add `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`, `DATABASE_URL`, application secrets, SMTP values, and Notion OAuth values as GitHub Actions secrets.
5. Configure the public Notion integration callback as `https://trackr-alerts.vercel.app/notion/callback`.
6. Push to `main`; the production workflow tests, migrates Neon, deploys Vercel, and verifies `/health`.

Rotate `SECRET_KEY`, `ENCRYPTION_KEY`, SMTP app passwords, Notion credentials, database credentials, and the Vercel token immediately if any value is exposed. Never commit them.

## Notion

Create a public Notion integration whose OAuth redirect URI is `${APP_URL}/notion/callback`. Each subscriber authorizes pages, selects an accessible parent page, and the platform creates an `Internship Opportunities` database there.

## Reliability and operations

Previewing preferences never changes active alerts. Activation reconciles unfinished deliveries and keeps already-sent history. Deactivation revokes sessions and cancels pending work. PostgreSQL user locks serialize delivery with preference changes and prevent overlapping digest sends. SMTP delivery is at-least-once: a crash after SMTP acceptance but before database commit can still cause a retry; a stable Message-ID is not a provider deduplication guarantee.

Login requests are limited in the shared database (one request per email per minute bucket, five per 15-minute bucket, twenty per IP per 15-minute bucket). Only hashed identifiers are stored. Vercel's proxy header is trusted only inside Vercel.

An absent offer closes only after two successful complete snapshots; failed or ambiguous empty responses do not count. Unchanged offers do not reset Notion retries. Changing Notion destinations removes local sync references without deleting any remote database. Public Notion OAuth is optional for web startup; configure both client credentials to enable it.

`/health` checks database access and the production schema revision, and reports `APP_COMMIT` (or Vercel's commit SHA). Worker commands report completed, deferred and failed tasks, and return nonzero on processing errors. Failed deliveries remain inspectable in the database.

The production workflow owns deployment; Vercel Git auto-deploy is disabled so migrations and tests finish first. Configure a durable project-scoped `VERCEL_TOKEN` in GitHub secrets. `VERCEL_BOOTSTRAP_TOKEN` is an optional temporary bootstrap credential and must be removed after bootstrap. The workflow synchronizes the web configuration from GitHub secrets; `ADMIN_EMAIL` and `SMTP_FROM` default to `SMTP_USER` when omitted. Keep `SECRET_KEY` and `ENCRYPTION_KEY` stable across web and workers.

Run `PYTHON_DOTENV_DISABLED=1 DATABASE_URL=sqlite:// ENVIRONMENT=development python -m pytest -q`. Set `TEST_DATABASE_URL` to run PostgreSQL migration and concurrency tests; these create and remove isolated `audit_test_*` schemas. Production verification checks health, commit, login, assets and unauthenticated redirects. On verification failure the workflow restores the previous deployment, retaining additive migrations and user data.

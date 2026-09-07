# Trackr Alerts

Private internship alerts with invitation-only accounts, programme/region/start-term preferences, immediate or daily email, and optional personal Notion synchronization.

## Local setup

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
alembic upgrade head
uvicorn trackr_app.main:app --reload
```

Set `ADMIN_EMAIL` to bootstrap a new administrator, then request a link from `/login`. SMTP is required to receive sign-in links; logs never contain these links. Gmail application-password separators are removed automatically. Other SMTP passwords are preserved.

SQLite startup migrates recognized old local databases through Alembic. Back up an existing database first. Production PostgreSQL migrations are run only by deployment, never by web startup. The current revision is `20260907_0004`.

## Accounts and delivery

Invitations persist before email delivery. Failed invitations retry with a fresh 15-minute link, exponential delay and a five-attempt limit. Their status is visible in `/admin`; inviting again retries a failed invitation. Repeated successful invitations within one minute are suppressed. Login responses remain generic.

Activation records existing matches as a baseline without bulk email. New matching offers and offers whose metadata becomes relevant are queued once per user. An offer can belong to several regions/programmes; source membership is tracked separately. Restoring filters or reactivating an account resumes matching unsent work. Already-sent offers and baseline offers are not emailed again on reopening. Draft accounts do not receive alerts.

Openings in the future and past closing dates are excluded, including rolling offers with an explicit past closing date. Missing dates do not imply closure. The last source must disappear from two complete snapshots before closing an offer. An ambiguous empty response remains an error; a structured response explicitly declaring zero results is accepted. A failed source never erases the previous snapshot.

The dashboard defaults to currently relevant open matches. History and pagination remain available.

## Workers and operations

```sh
python -m trackr_app.cli scrape-all
python -m trackr_app.cli process-invitations
python -m trackr_app.cli process-immediate-alerts
python -m trackr_app.cli process-digests
python -m trackr_app.cli sync-notion
python -m trackr_app.cli check-operations
```

Workers use database locks, delay retries and bound their processing time. SMTP remains **at-least-once**: a crash after provider acceptance but before the database commit can cause a duplicate. A stable Message-ID is not a provider deduplication guarantee. Daily digests are sent at most once per local date; a partial SMTP failure does not consume that date.

The scraper monitors the six regional Finance feeds plus these Trackr pages: UK Spring Weeks, UK Industrial Placements, UK Graduate Programmes, UK Events, and France Graduate Programmes. Spring Weeks are read from Trackr's dedicated `/spring-weeks` API and linked back to the page with a stable item anchor; the other pages use the `/programmes` API with their page type. These source types are available in preferences as `spring-weeks`, `industrial-placements`, `graduate-programmes`, and `events`.

`/health` checks database/schema and commit for deployment readiness. `/admin/operations` is administrator-only and reports stale workers (30 minutes), failed tasks and immediate notifications delayed over one hour. `check-operations` exposes the same result to the scheduler and returns nonzero when degraded. Sources are monitored independently. Daily digests waiting for their scheduled hour are not flagged as delayed immediate mail.

Failed jobs can be replayed individually, without editing SQL:

```sh
python -m trackr_app.cli retry-failed --kind email --id 123
python -m trackr_app.cli retry-failed --kind notion --id 456
python -m trackr_app.cli retry-failed --kind invitation --id 789
python -m trackr_app.cli retry-failed --kind legacy --id TASK_HASH
```

Only failed jobs can be reset. Workers still enforce account activity, preferences and open status. Logs retain exception type/provider status without credentials or email bodies.

For explicit administrator recovery:

```sh
python -m trackr_app.cli promote-admin --email admin@example.com
```

This activates/promotes the selected account and revokes its sessions and links. Merely changing `ADMIN_EMAIL` never silently promotes an existing subscriber.

## Personal Notion

Personal synchronization is **paused by default**. Set `NOTION_SYNC_ENABLED=true` together with both OAuth credentials in web and workers to enable it. The dashboard accurately shows availability; the scheduler calls the command, which exits without external requests when disabled.

Create a public integration with callback `${APP_URL}/notion/callback`. Connect an account, choose a shared parent page and create its opportunities database. Repeated setup submissions reuse the existing destination. An uncertain creation is not repeated automatically: the setup page lets the user attach the database already created, after access, parent and schema verification. Disconnecting keeps the remote database. Reconnecting the same workspace retries failed syncs; a changed workspace resets local destination references.

Provider revocation requires reconnecting. Automatic refresh-token exchange is not assumed without a verified provider contract.

## Historical CSV / shared Notion collectors

The six original entry points remain supported. They share validation and atomic CSV replacement, and persist independent email/Notion tasks in the same `DATABASE_URL` before writing CSV. A failed SMTP or Notion request is replayable even after the CSV has been updated. Upstream errors do not block retrying existing tasks.

The historical workflow now needs the shared database and its migrations. `LEGACY_EMAIL_ENABLED=true` preserves delivery to **unmigrated** recipients from `TO_ADDRS` (or a private local `email.csv`). Every address that has a platform account, active or disabled, is excluded from historical mail. This prevents duplicate channels and makes account deactivation effective. Recipients receive separate messages, never a shared `To` header. `email.csv` is no longer versioned; configure `TO_ADDRS` for GitHub Actions before deployment if recipients previously came only from that file.

To migrate the private recipient list to platform accounts:

```sh
python -m trackr_app.cli import-legacy-subscribers
```

This imports only missing accounts, activates baseline preferences and sends no messages. Review their preferences after import. It never reactivates an existing disabled account. Once all recipients are migrated, set `LEGACY_EMAIL_ENABLED=false` to retain CSV/shared Notion only.

`FORCE_EMAIL_ALL=1` creates an explicit resend batch independent of Notion creation status. Ordinary runs deduplicate existing deliveries. `OUTPUT_FILE` overrides the selected script's CSV destination. Off-cycle filters use `OFF_CYCLE_EMAIL_START_TERM`, with `HK_OFF_CYCLE_EMAIL_START_TERM` as the Hong Kong override. Set `LEGACY_TODO_ENABLED=true` with a valid `TODO_DATA_SOURCE_ID` to create/update associated tasks, due two days after opening. It stays off by default. Descriptions come from upstream metadata; collectors no longer fetch arbitrary employer URLs server-side.

## Deployment

Production uses Vercel, PostgreSQL, SMTP and GitHub Actions. Keep `SECRET_KEY` and `ENCRYPTION_KEY` identical between web and workers. Set `ENVIRONMENT=production`, an HTTPS `APP_URL`, and the secrets listed in `.env.example`. The configuration synchronizer also propagates `NOTION_SYNC_ENABLED`.

The production workflow tests PostgreSQL, migrates and deploys, then checks the release. Deployment, platform workers and historical collectors share a concurrency group so they do not execute migrations and workers simultaneously. Platform steps have separate timeouts and continue to the other workers after a failure. GitHub schedules are not an exact-time delivery guarantee. Git auto-deploy remains disabled. Rollback retains additive migrations; rolling back across a schema revision requires verifying compatibility of the target release's health check and models.

Before shipping this revision: provide the historical recipient list through GitHub secrets, verify optional feature flags, and rotate any exposed provider credentials. Changing `ENCRYPTION_KEY` on a populated database requires re-encrypting stored tokens or reconnecting affected accounts; do not blindly replace it.

## Tests

```sh
PYTHON_DOTENV_DISABLED=1 DATABASE_URL=sqlite:// ENVIRONMENT=development python -m pytest -q
```

Set `TEST_DATABASE_URL` for real PostgreSQL migration/concurrency tests. They use disposable `audit_test_*` schemas. Alternatively, with a local PostgreSQL installation:

```sh
POSTGRES_BIN=/path/to/postgresql/bin python scripts/test_postgres_local.py
```

This starts an isolated loopback-only temporary cluster, runs the suite, stops the server and removes the cluster. It does not enable a system service. The audit regressions in `audit/test_workflow_20260907.py` are now ordinary passing tests, without `xfail` markers.

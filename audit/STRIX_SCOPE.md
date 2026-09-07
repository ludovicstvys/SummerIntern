# Authorized local assessment: Trackr Alerts

Assess only the provided disposable source directory and services you start inside your sandbox. The entire local application is in scope, including password/magic-link authentication, invitation-only access, password recovery, sliding sessions, CSRF, authorization, preferences, administration, workers and integrations.

Do not scan or contact production applications, URLs found in the source, Notion, Vercel, SMTP servers, scraper upstreams, or any other third-party target. External access is only for installing public development dependencies. Mock integrations and email delivery. Do not send email or invoke production workflows. Do not look for credentials outside the supplied snapshot.

The snapshot includes current uncommitted changes. It deliberately excludes `.env*`, `.git`, real databases, CSV recipient lists, private audit exports, backups and installed dependencies. Never infer that these exclusions prove the original repository is free of secrets.

This is Python/FastAPI with SQLAlchemy and Alembic. Install `requirements-dev.txt` into a sandbox virtual environment. Use `PYTHON_DOTENV_DISABLED=1`, `ENVIRONMENT=development`, `APP_URL=http://localhost:8000`, `DATABASE_URL=sqlite:///./audit-test.db`, and `NOTION_SYNC_ENABLED=false`. Set a fresh disposable `SECRET_KEY`. Leave SMTP and external API credentials unset. Migrate the disposable database with `alembic upgrade head`; start `uvicorn trackr_app.main:app --host 0.0.0.0 --port 8000` if dynamic testing is useful. Seed synthetic users directly in the disposable database as needed.

Existing tests use `python -m pytest -q`. PostgreSQL tests require an isolated `TEST_DATABASE_URL`; otherwise report them as not covered. The latest authentication migration is `20260907_0005`.

Validate findings with reproducible local proofs of concept. Do not treat intended invitation-only access, generic login responses, or explicitly configured development defaults as production exploits without evidence. Record coverage limitations, failed setup steps, and budget truncation. Produce Markdown and structured findings. Do not remediate the source automatically; proposed fixes and test artifacts may be written only inside the disposable snapshot.

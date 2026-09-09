"""Run only after the serving web release explicitly accepts the target schema."""
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, func, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from trackr_app.models import WorkerState, utcnow
from trackr_app.operations import lock_state
from trackr_app.health import SCHEMA_REVISION


def migrate(engine, migrate_call=None, timeout=360):
    with Session(engine) as db:
        gate = lock_state(db, 'runtime/gate')
        if gate.last_error:
            raise RuntimeError('Migration already paused; inspect before recovery')
        gate.last_error = 'migration/' + os.getenv('GITHUB_RUN_ID', 'local')
        db.commit()
    try:
        deadline = time.monotonic() + timeout
        while True:
            with Session(engine) as db:
                active = db.scalar(select(func.count()).select_from(WorkerState).where(
                    WorkerState.key.startswith('runtime/active/'), WorkerState.last_success_at > utcnow()))
            if not active:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('Active workers did not drain')
            time.sleep(2)
        with engine.begin() as connection:
            if connection.dialect.name == 'postgresql':
                connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                connection.execute(text("SET LOCAL statement_timeout = '120s'"))
            if migrate_call:
                migrate_call(connection)
            else:
                config = Config('alembic.ini')
                config.attributes['connection'] = connection
                command.upgrade(config, SCHEMA_REVISION)
    finally:
        with Session(engine) as db:
            gate = lock_state(db, 'runtime/gate')
            gate.last_error = None
            db.commit()


if __name__ == '__main__':
    import urllib.request
    with urllib.request.urlopen(os.environ['APP_URL'].rstrip('/') + '/health', timeout=20) as response:
        import json
        health = json.load(response)
    if SCHEMA_REVISION not in health.get('compatible_schemas', []):
        raise SystemExit('Deploy and verify the compatibility bridge before migrating')
    url = os.environ['MIGRATION_DATABASE_URL'].replace('postgres://', 'postgresql+psycopg://', 1).replace('postgresql://', 'postgresql+psycopg://', 1)
    migrate(create_engine(url, poolclass=NullPool, connect_args={'connect_timeout': 5}))

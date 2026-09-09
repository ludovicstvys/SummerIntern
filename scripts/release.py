"""Deploy the compatible web bridge, migrate, then verify production."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deployment_status import verify, request
from scripts.worker_schedules import pause, resume
from scripts.migrate_coordinated import migrate
from trackr_app.config import settings
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool


def vercel(*args):
    # Tokens are passed as arguments, never printed by this wrapper.
    result = subprocess.run(['vercel', *args, '--token=' + os.environ['VERCEL_TOKEN']],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().replace(os.environ['VERCEL_TOKEN'], '***')
        raise RuntimeError('VercelCommandFailed:' + args[0] + (':' + detail if detail else ''))
    return result.stdout.strip()


def previous():
    project, team = os.environ['VERCEL_PROJECT_ID'], os.environ['VERCEL_ORG_ID']
    req = urllib.request.Request(f'https://api.vercel.com/v9/projects/{project}?teamId={team}',
        headers={'Authorization': 'Bearer ' + os.environ['VERCEL_TOKEN']})
    with urllib.request.urlopen(req, timeout=20) as response:
        target = json.load(response)['targets']['production']
    with urllib.request.urlopen(request(settings.app_url + '/health'), timeout=20) as response:
        health = json.load(response)
    return 'https://' + target['url'].removeprefix('https://'), health


def release():
    previous()
    vercel('deploy', '--prod', '--yes',
        '--env', 'APP_COMMIT=' + os.environ['GITHUB_SHA'], '--env', 'ALLOW_DEPLOYMENT_HOST=true').splitlines()[-1]
    try:
        verify(settings.app_url)
        pause()  # Also drains old worker versions that predate the database gate.
        if not settings.migration_database_url:
            raise RuntimeError('MIGRATION_DATABASE_URL is required')
        engine = create_engine(settings.migration_database_url, poolclass=NullPool,
            connect_args={'connect_timeout': 5})
        try:
            migrate(engine)
        finally:
            engine.dispose()
        verify(settings.app_url)
    finally:
        resume()


if __name__ == '__main__':
    try:
        release()
    except Exception as exc:
        print('Release failed: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)

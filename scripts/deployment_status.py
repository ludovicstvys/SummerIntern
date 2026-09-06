"""Public deployment smoke checks and rollback metadata."""
import json
import os
import sys
import time
import urllib.request


def previous():
    project, team = os.environ['VERCEL_PROJECT_ID'], os.environ['VERCEL_ORG_ID']
    request = urllib.request.Request(f'https://api.vercel.com/v9/projects/{project}?teamId={team}', headers={'Authorization': 'Bearer ' + os.environ['VERCEL_TOKEN']})
    with urllib.request.urlopen(request, timeout=30) as response:
        production = json.load(response).get('targets', {}).get('production', {})
    print('url=' + production.get('url', ''))


def verify():
    base = 'https://trackr-alerts.vercel.app'
    for attempt in range(12):
        try:
            with urllib.request.urlopen(base + '/health', timeout=30) as response:
                health = json.load(response)
            if health.get('status') != 'ok' or health.get('commit') != os.environ['GITHUB_SHA']:
                raise RuntimeError('Unexpected production version')
            for path, content in [('/login', 'Sign'), ('/static/app.css', '{'), ('/dashboard', 'Sign')]:
                with urllib.request.urlopen(base + path, timeout=30) as response:
                    if content not in response.read().decode():
                        raise RuntimeError('Unexpected public response')
            print('Production health, version, login, assets and authentication verified')
            return
        except Exception as exc:
            print(f'Smoke check attempt {attempt + 1}: {type(exc).__name__}')
            if attempt == 11:
                raise SystemExit(1)
            time.sleep(10)


if __name__ == '__main__':
    {'previous': previous, 'verify': verify}[sys.argv[1]]()

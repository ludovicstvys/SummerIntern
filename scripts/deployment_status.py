"""Public deployment smoke checks and rollback metadata."""
import json
import os
import subprocess
import sys
import time
import urllib.request


def request(url):
    headers = {}
    if os.getenv('VERCEL_AUTOMATION_BYPASS_SECRET'):
        headers['x-vercel-protection-bypass'] = os.environ['VERCEL_AUTOMATION_BYPASS_SECRET']
    return urllib.request.Request(url, headers=headers)


def read(url, candidate=False):
    if candidate:
        result = subprocess.run(['vercel', 'curl', url, '--token=' + os.environ['VERCEL_TOKEN']],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip().replace(os.environ['VERCEL_TOKEN'], '***')
            raise RuntimeError('CandidateCurlFailed' + (':' + detail if detail else ''))
        return result.stdout.encode()
    with urllib.request.urlopen(request(url), timeout=20) as response:
        return response.read()


def previous():
    project, team = os.environ['VERCEL_PROJECT_ID'], os.environ['VERCEL_ORG_ID']
    request = urllib.request.Request(f'https://api.vercel.com/v9/projects/{project}?teamId={team}', headers={'Authorization': 'Bearer ' + os.environ['VERCEL_TOKEN']})
    with urllib.request.urlopen(request, timeout=30) as response:
        production = json.load(response).get('targets', {}).get('production', {})
    print('url=' + production.get('url', ''))


def verify(base=None, expected_commit=None, candidate=False):
    base = (base or os.getenv('VERIFY_URL') or os.getenv('APP_URL', 'https://trackr-alerts.vercel.app')).rstrip('/')
    expected_commit = expected_commit or os.getenv('EXPECTED_COMMIT') or os.environ['GITHUB_SHA']
    for attempt in range(12):
        try:
            health = json.loads(read(base + '/health', candidate))
            if health.get('status') != 'ok' or health.get('commit') != expected_commit:
                raise RuntimeError('Unexpected production version')
            for path, content in [('/login', 'Sign'), ('/static/app.css', '{'), ('/dashboard', 'Sign')]:
                if content not in read(base + path, candidate).decode():
                    raise RuntimeError('Unexpected public response')
            print('Health, version, login, assets and protected-page redirect verified')
            return health
        except Exception as exc:
            print(f'Smoke check attempt {attempt + 1}: {exc}')
            if attempt == 11:
                raise RuntimeError('DeploymentVerificationFailed') from None
            time.sleep(10)


if __name__ == '__main__':
    {'previous': previous, 'verify': verify}[sys.argv[1]]()

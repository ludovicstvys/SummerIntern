"""Temporarily pause scheduled producers, including pre-bridge executions."""
import json
import subprocess
import time
from pathlib import Path

NAMES = ['platform-jobs.yml', 'scrape.yml', 'email-delivery.yml', 'collection.yml', 'maintenance.yml']
STATE = Path('.worker-schedules.json')


def gh(*args):
    return subprocess.check_output(['gh', *args], text=True)


def pause():
    workflows = json.loads(gh('api', 'repos/{owner}/{repo}/actions/workflows', '--paginate'))['workflows']
    ids = [w['id'] for w in workflows if w['path'].rsplit('/', 1)[-1] in NAMES and w['state'] == 'active']
    STATE.write_text(json.dumps(ids))
    for identity in ids:
        gh('workflow', 'disable', str(identity))
    deadline = time.monotonic() + 1200
    while True:
        running = []
        for identity in ids:
            data = json.loads(gh('api', f'repos/{{owner}}/{{repo}}/actions/workflows/{identity}/runs?per_page=100'))
            for run in data['workflow_runs']:
                if run['status'] in ('queued', 'waiting', 'pending', 'requested'):
                    gh('run', 'cancel', str(run['id']))
                elif run['status'] == 'in_progress':
                    running.append(run['id'])
        if not running:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError('Old scheduled workers did not finish')
        time.sleep(10)


def resume():
    if STATE.exists():
        for identity in json.loads(STATE.read_text()):
            gh('workflow', 'enable', str(identity))


if __name__ == '__main__':
    import sys
    {'pause': pause, 'resume': resume}[sys.argv[1]]()

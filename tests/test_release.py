from types import SimpleNamespace
from unittest.mock import patch
import pytest
from scripts.release import release
from scripts.deployment_status import read


def test_compatible_bridge_is_verified_before_migration(monkeypatch):
    monkeypatch.setenv('GITHUB_SHA', 'new-commit')
    events = []
    def vercel(*args):
        events.append(('vercel', args))
        return 'https://deployed.vercel.app' if args[0] == 'deploy' else ''
    def verify(url, expected_commit=None, candidate=False):
        events.append(('verify', url, expected_commit, candidate))
        return {'status': 'ok'}
    def migration(engine):
        events.append(('migrate',))
        return None
    with (patch('scripts.release.settings', SimpleNamespace(app_url='https://canonical.example', migration_database_url='postgresql://test')),
        patch('scripts.release.previous', return_value=('https://old.vercel.app', {'commit': 'old-commit'})),
        patch('scripts.release.vercel', side_effect=vercel), patch('scripts.release.verify', side_effect=verify),
        patch('scripts.release.pause', side_effect=lambda: events.append(('pause',))),
        patch('scripts.release.resume', side_effect=lambda: events.append(('resume',))),
        patch('scripts.release.create_engine'), patch('scripts.release.migrate', side_effect=migration)):
        release()
    assert events.index(('verify', 'https://canonical.example', None, False)) < events.index(('migrate',))
    deploy = next(args for kind, args in events if kind == 'vercel' and args[0] == 'deploy')
    assert '--prebuilt' not in deploy
    assert '--prod' in deploy and '--skip-domain' not in deploy
    assert events.count(('verify', 'https://canonical.example', None, False)) == 2
    assert events[-1] == ('resume',)


def test_candidate_smoke_request_uses_vercel_curl(monkeypatch):
    monkeypatch.setenv('VERCEL_TOKEN', 'test-token')
    result = SimpleNamespace(returncode=0, stdout='{"status":"ok"}', stderr='')
    with patch('scripts.deployment_status.subprocess.run', return_value=result) as run:
        assert read('https://candidate.vercel.app/health', candidate=True) == b'{"status":"ok"}'
    assert run.call_args.args[0] == ['vercel', 'curl', 'https://candidate.vercel.app/health', '--', '--location']


def test_candidate_smoke_request_redacts_cli_token_on_failure(monkeypatch):
    monkeypatch.setenv('VERCEL_TOKEN', 'test-token')
    result = SimpleNamespace(returncode=1, stdout='', stderr='invalid token: test-token')
    with patch('scripts.deployment_status.subprocess.run', return_value=result):
        with pytest.raises(RuntimeError, match=r'CandidateCurlFailed:invalid token: \*\*\*'):
            read('https://candidate.vercel.app/health', candidate=True)

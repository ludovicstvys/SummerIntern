from types import SimpleNamespace
from unittest.mock import patch
import pytest
from scripts.release import release


def test_bridge_is_verified_before_migration_and_is_safe_rollback(monkeypatch):
    monkeypatch.setenv('GITHUB_SHA', 'new-commit')
    events = []
    candidate = 'https://candidate.vercel.app'
    def vercel(*args):
        events.append(('vercel', args[0], args[1] if len(args)>1 else ''))
        return candidate if args[0] == 'deploy' else ''
    def verify(url, expected_commit=None):
        events.append(('verify', url, expected_commit))
        return {'status': 'ok'}
    def migration(engine):
        events.append(('migrate',))
        raise RuntimeError('migration failed')
    with (patch('scripts.release.settings', SimpleNamespace(app_url='https://canonical.example', migration_database_url='postgresql://test')),
        patch('scripts.release.previous', return_value=('https://old.vercel.app', {'commit': 'old-commit'})),
        patch('scripts.release.vercel', side_effect=vercel), patch('scripts.release.verify', side_effect=verify),
        patch('scripts.release.pause', side_effect=lambda: events.append(('pause',))),
        patch('scripts.release.resume', side_effect=lambda: events.append(('resume',))),
        patch('scripts.release.create_engine'), patch('scripts.release.migrate', side_effect=migration)):
        with pytest.raises(RuntimeError):
            release()
    assert events.index(('verify', 'https://canonical.example', None)) < events.index(('migrate',))
    assert ('vercel', 'rollback', candidate) in events
    assert ('verify', 'https://canonical.example', 'new-commit') in events
    assert events[-1] == ('resume',)

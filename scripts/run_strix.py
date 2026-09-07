"""Audit a disposable source snapshot; never mount the working repository."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {'trackr_app', 'api', 'alembic', 'tests', 'scripts', '.github'}
SOURCE_FILES = {'README.md', 'requirements.txt', 'requirements-dev.txt', 'alembic.ini', 'pytest.ini'}
EXTENSIONS = {'.py', '.html', '.css', '.ini', '.toml', '.yaml', '.yml', '.md', '.txt'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--mode', choices=('quick', 'standard', 'deep'), default='quick')
    parser.add_argument('--max-budget', type=float, default=5.0, help='Maximum model cost in USD')
    args = parser.parse_args()
    if args.max_budget <= 0:
        parser.error('--max-budget must be positive')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    workspace = ROOT / 'audit' / 'strix-work' / stamp
    snapshot = workspace / 'source'
    snapshot.mkdir(parents=True)
    files = subprocess.check_output(
        ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT
    ).decode().split('\0')
    copied = []
    for name in sorted(set(files)):
        path = Path(name)
        if not name or path.suffix not in EXTENSIONS:
            continue
        if not (path.parts[0] in SOURCE_DIRS or name in SOURCE_FILES or (len(path.parts) == 1 and path.suffix == '.py')):
            continue
        source = ROOT / path
        if not source.is_file() or source.is_symlink() or any(p.is_symlink() for p in source.parents if p != ROOT):
            continue
        destination = snapshot / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(name)
    command = [shutil.which('strix') or str(Path.home() / '.strix/bin/strix'), '-n', '-t', str(snapshot),
               '--scan-mode', args.mode, '--scope-mode', 'full', '--max-budget', str(args.max_budget),
               '--instruction-file', str(ROOT / 'audit' / 'STRIX_SCOPE.md')]
    metadata = {'status': 'prepared', 'files': copied, 'command': command,
                'upstream': 'https://github.com/usestrix/strix',
                'upstream_commit': subprocess.check_output(['git', '-C', str(ROOT / 'tools/strix'), 'rev-parse', 'HEAD']).decode().strip()}
    report = workspace / 'preflight.json'
    report.write_text(json.dumps(metadata, indent=2) + '\n')
    print(f'Snapshot: {snapshot}\nSource files: {len(copied)}\nMetadata: {report}', flush=True)
    if args.prepare_only:
        return 0
    # Do not inherit application/production provider secrets into Strix.
    allowed = {'PATH', 'HOME', 'USER', 'LANG', 'TMPDIR', 'TERM', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE',
               'LLM_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENROUTER_API_KEY', 'DOCKER_HOST', 'DOCKER_CONTEXT'}
    env = {key: value for key, value in os.environ.items() if key in allowed or key.startswith('STRIX_')}
    env.update(STRIX_TELEMETRY='false', PYTHON_DOTENV_DISABLED='1')
    # Docker's Python SDK does not automatically honor the CLI's active context.
    if not env.get('DOCKER_HOST') and shutil.which('docker'):
        endpoint = subprocess.check_output(
            ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
            text=True, env=env,
        ).strip()
        if endpoint:
            env['DOCKER_HOST'] = endpoint
    # Public sandbox image: avoid stale Docker Desktop credential helpers.
    docker_config = workspace / 'docker-config'
    docker_config.mkdir(mode=0o700)
    (docker_config / 'config.json').write_text('{"auths": {}}\n')
    env['DOCKER_CONFIG'] = str(docker_config)
    result = subprocess.run(command, cwd=workspace, env=env)
    metadata.update(status='process_exited', exit_code=result.returncode)
    report.write_text(json.dumps(metadata, indent=2) + '\n')
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())

"""Run tests against a disposable loopback-only PostgreSQL cluster."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile


def main():
    binary = Path(os.getenv('POSTGRES_BIN', '/opt/homebrew/opt/postgresql@17/bin'))
    if not (binary / 'initdb').exists():
        found = shutil.which('initdb')
        if not found:
            raise SystemExit('Set POSTGRES_BIN to a PostgreSQL installation bin directory')
        binary = Path(found).parent
    with tempfile.TemporaryDirectory(prefix='trackr-pg-') as directory:
        cluster = Path(directory) / 'data'
        subprocess.run([str(binary/'initdb'), '-D', str(cluster), '-U', 'trackr', '--auth=trust', '--encoding=UTF8', '--no-locale'], check=True, capture_output=True)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        subprocess.run([str(binary/'pg_ctl'), '-D', str(cluster), '-l', str(Path(directory)/'server.log'), '-o', f'-h 127.0.0.1 -p {port} -k {directory}', '-w', 'start'], check=True, capture_output=True)
        try:
            env = {**os.environ, 'PYTHON_DOTENV_DISABLED': '1', 'DATABASE_URL': 'sqlite://', 'ENVIRONMENT': 'development', 'TEST_DATABASE_URL': f'postgresql+psycopg://trackr@127.0.0.1:{port}/postgres'}
            result = subprocess.run([sys.executable, '-m', 'pytest', '-q', *sys.argv[1:]], env=env)
        finally:
            subprocess.run([str(binary/'pg_ctl'), '-D', str(cluster), '-m', 'fast', '-w', 'stop'], check=True, capture_output=True)
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())

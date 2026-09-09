"""Reject private and development files in a built Vercel function artifact."""
import argparse
from pathlib import Path


def check(root):
    root = Path(root)
    if not root.is_dir():
        raise ValueError('Build output directory is missing')
    violations = []
    forbidden_dirs = {'audit', 'tools', '.trackr-backups', '.git', '.github', 'tests'}
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if (forbidden_dirs.intersection(relative.parts) or path.name == '.env' or
            path.name.startswith('.env.') or path.suffix in ('.db', '.sqlite', '.csv')):
            violations.append(str(relative))
    if violations:
        raise ValueError('Forbidden artifact paths: ' + ', '.join(violations[:20]))
    print('Artifact excludes private data and development files')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root')
    check(parser.parse_args().root)

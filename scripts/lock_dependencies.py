"""Capture the installed runtime dependency closure, excluding unrelated tools."""
from importlib.metadata import distribution
from pathlib import Path
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def main():
    pending = []
    for line in Path('requirements.txt').read_text().splitlines():
        if line and not line.startswith(('#', '-')):
            pending.append(Requirement(line))
    found = {}
    expanded = set()
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        extras = tuple(sorted(requirement.extras))
        if (name, extras) in expanded:
            continue
        expanded.add((name, extras))
        item = distribution(requirement.name)
        found[name] = item.version
        for raw in item.requires or []:
            child = Requirement(raw)
            if child.marker is None or any(child.marker.evaluate({**target, 'extra': extra}) for target in ({}, {'python_version': '3.12', 'python_full_version': '3.12.0', 'sys_platform': 'linux', 'platform_system': 'Linux', 'platform_machine': 'x86_64'}) for extra in ('', *extras)):
                pending.append(child)
    Path('requirements.lock').write_text('# Resolved runtime constraints; regenerate after dependency upgrades.\n' +
        ''.join(f'{name}=={version}\n' for name, version in sorted(found.items())))


if __name__ == '__main__':
    main()

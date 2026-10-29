#!/usr/bin/env python3
"""Check local documentation, JSON examples and safe CLI help without live actions."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {'.git', '.venv', '__pycache__', '.pytest_cache', 'runtime', 'backups'}
LINK = re.compile(r'\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))(?:\s+[\"\'][^)]*)?\)')


def files(root: Path):
    for base, directories, names in os.walk(root):
        directories[:] = [name for name in directories if name not in SKIP_PARTS]
        for name in names:
            yield Path(base) / name


def slug(value: str) -> str:
    value = re.sub(r'[`*_~]', '', value.strip()).lower()
    value = re.sub(r'[^\w\-\s]', '', value, flags=re.UNICODE)
    return re.sub(r'\s', '-', value)


def anchors(path: Path) -> set[str]:
    result, repeated = set(), {}
    for line in path.read_text(encoding='utf-8').splitlines():
        match = re.match(r'^#{1,6}\s+(.+?)(?:\s+#+)?$', line)
        if match:
            name = slug(match.group(1)); number = repeated.get(name, 0)
            result.add(name if not number else f'{name}-{number}')
            repeated[name] = number + 1
    return result


def documentation(root: Path) -> list[str]:
    problems = []
    for path in files(root):
        if path.suffix.lower() != '.md':
            continue
        text = path.read_text(encoding='utf-8')
        fenced = False
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith('```'):
                fenced = not fenced
                continue
            if fenced:
                continue
            for match in LINK.finditer(line):
                target = match.group(1) or match.group(2)
                split = urllib.parse.urlsplit(target)
                if split.scheme or split.netloc:
                    continue
                relative = urllib.parse.unquote(split.path)
                resolved = (path.parent / relative).resolve() if relative else path.resolve()
                if not resolved.is_relative_to(root.resolve()):
                    problems.append(f'{path.relative_to(root)}:{number}: link escapes release tree')
                elif not resolved.exists():
                    problems.append(f'{path.relative_to(root)}:{number}: missing local link {relative}')
                elif split.fragment and resolved.suffix.lower() == '.md':
                    fragment = urllib.parse.unquote(split.fragment)
                    if fragment not in anchors(resolved):
                        problems.append(f'{path.relative_to(root)}:{number}: missing heading #{fragment}')
        if fenced:
            problems.append(f'{path.relative_to(root)}: unclosed fenced code block')
    return problems


def json_examples(root: Path) -> list[str]:
    problems = []
    for path in files(root / 'examples'):
        if path.suffix != '.json':
            continue
        try:
            json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError) as error:
            problems.append(f'{path.relative_to(root)}: invalid JSON ({type(error).__name__})')
    return problems


def safe_cli_help(root: Path) -> tuple[list[str], int]:
    problems, count = [], 0
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith(('WECHAT_DIGEST_', 'WECHATAGENT_')):
            environment.pop(key)
    with tempfile.TemporaryDirectory(prefix='wechat-doc-check-') as private:
        environment.update(WECHAT_DIGEST_ROOT=str(root), WECHAT_DIGEST_CONFIG=str(Path(private) / 'not-configured.json'))
        environment['PYTHONPATH'] = os.pathsep.join(str(root / part) for part in ('', 'deployment', 'memory', 'agent_console', 'deployment/vendor'))
        candidates = [*sorted((root / 'deployment').glob('*.py')),
                      root / 'memory/decrypt_sync.py', root / 'memory/memory_ingest.py', root / 'memory/media_sync.py',
                      root / 'agent_console/daily_report.py', root / 'scripts/generate-demo.py']
        for path in candidates:
            if not path.exists() or path.name.startswith('test_'):
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
            if not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr == 'ArgumentParser' for node in ast.walk(tree)):
                continue
            try:
                result = subprocess.run([sys.executable, str(path), '--help'], cwd=root, env=environment,
                                        capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired:
                problems.append(f'{path.relative_to(root)}: --help timed out'); continue
            count += 1
            if result.returncode or 'usage:' not in (result.stdout + result.stderr).lower():
                # Avoid printing arbitrary stderr/configuration content.
                problems.append(f'{path.relative_to(root)}: --help failed (exit {result.returncode})')
    return problems, count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--cli', action='store_true', help='also run only safe --help entry points with no live config')
    args = parser.parse_args()
    root = args.root.resolve()
    problems = documentation(root) + json_examples(root)
    count = 0
    if args.cli:
        extra, count = safe_cli_help(root); problems += extra
    for problem in problems:
        print(problem)
    print(json.dumps({'documentation_errors': len(problems), 'cli_help_checked': count}, ensure_ascii=False))
    return bool(problems)


if __name__ == '__main__':
    raise SystemExit(main())

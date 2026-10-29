#!/usr/bin/env python3
"""Audit a source release without printing matching credentials or private values."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.venv', '__pycache__', '.pytest_cache', 'runtime', 'config', 'backups', 'exports'}
TEXT = {'.py', '.sh', '.md', '.json', '.yaml', '.yml', '.toml', '.ini', '.conf', '.caddy', '.initd', '.txt', '.example'}
FORBIDDEN = {'all_keys.json', 'passphrase.json', '.env', 'id_rsa', 'id_ed25519'}
SYNTHETIC_GROUP = '10000000000@chatroom'


def enumerate_files(root: Path):
    for base, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in SKIP]
        for name in names:
            yield Path(base) / name


def audit(root: Path, patterns: dict[str, str]) -> list[dict]:
    findings = []
    if (root / '.git').exists():
        command = subprocess.run(['git', 'ls-files', '-z'], cwd=root, capture_output=True, text=True, check=True)
        for name in filter(None, command.stdout.split('\0')):
            if name.split('/')[0] in SKIP - {'.git', '__pycache__', '.pytest_cache'}:
                findings.append({'file': name, 'line': 0, 'category': 'private-directory-in-index'})
    for path in enumerate_files(root):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            findings.append({'file': relative, 'line': 0, 'category': 'symlink-needs-explicit-review'})
            continue
        if path.name in FORBIDDEN or path.suffix in {'.pem', '.key', '.p12', '.pcap'}:
            findings.append({'file': relative, 'line': 0, 'category': 'secret-or-capture-file'})
        if path.suffix in {'.db', '.sqlite'} and relative != 'examples/synthetic/synthetic-sqlcipher4.db':
            findings.append({'file': relative, 'line': 0, 'category': 'database-outside-synthetic-fixture'})
        if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf'} and not relative.startswith('examples/synthetic/'):
            findings.append({'file': relative, 'line': 0, 'category': 'media-outside-reviewed-synthetic-examples'})
        if path.suffix not in TEXT and path.name not in {'Dockerfile', 'LICENSE', '.gitignore', '.dockerignore', '.env.example'}:
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', line):
                findings.append({'file': relative, 'line': number, 'category': 'private-key-material'})
            ids = re.findall(r'\b[0-9]{10,}@chatroom\b', line)
            for identity in ids:
                if identity != SYNTHETIC_GROUP:
                    findings.append({'file': relative, 'line': number, 'category': 'non-synthetic-hardcoded-chat-id'})
            for category, pattern in patterns.items():
                if re.search(pattern, line):
                    findings.append({'file': relative, 'line': number, 'category': category})
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--private-patterns', type=Path, help='private JSON category-to-regex map kept OUTSIDE the repository')
    parser.add_argument('--report', type=Path, help='optional redacted JSON report (contains no matching values)')
    args = parser.parse_args()
    patterns = json.loads(args.private_patterns.read_text()) if args.private_patterns else {}
    if not isinstance(patterns, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in patterns.items()):
        parser.error('private patterns must map category strings to regular expressions')
    findings = audit(args.root.resolve(), patterns)
    result = {'findings': findings, 'count': len(findings)}
    if args.report:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    for item in findings:
        print(f"{item['file']}:{item['line']}: {item['category']}")
    print(json.dumps({'release_audit_findings': len(findings)}))
    return bool(findings)


if __name__ == '__main__':
    raise SystemExit(main())

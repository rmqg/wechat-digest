#!/usr/bin/env python3
"""Explicitly install the verified Linux x86-64/Python 3.12 OCR wheel lock."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import urllib.request
import zipfile


def install(lock_path: Path, target: Path, wheel_dir: Path, *, offline=False):
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64') or sys.version_info[:2] != (3, 12):
        raise RuntimeError('this OCR lock requires Linux x86-64 and Python 3.12; other platforms need their own verified lock')
    lock = json.loads(lock_path.read_text())
    target.mkdir(parents=True, exist_ok=True)
    wheel_dir.mkdir(parents=True, exist_ok=True)
    for row in lock:
        name = row['file']
        if Path(name).name != name or not name.endswith('.whl'):
            raise ValueError('invalid locked wheel name')
        wheel = wheel_dir / name
        if not wheel.exists():
            if offline:
                raise RuntimeError('missing offline wheel: '+name)
            package, version, _ = name.split('-', 2)
            with urllib.request.urlopen('https://pypi.org/pypi/'+package.replace('_','-')+'/'+version+'/json', timeout=30) as response:
                metadata = json.load(response)
            artifact = next((item for item in metadata['urls'] if item['filename'] == name), None)
            if artifact is None or artifact['digests']['sha256'] != row['sha256']:
                raise RuntimeError('official metadata does not match locked wheel: '+name)
            with urllib.request.urlopen(artifact['url'], timeout=90) as response:
                data = response.read()
            if hashlib.sha256(data).hexdigest() != row['sha256']:
                raise RuntimeError('download checksum mismatch: '+name)
            temporary = wheel.with_suffix('.tmp');temporary.write_bytes(data);os.replace(temporary,wheel)
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != row['sha256']:
            raise RuntimeError('cached wheel checksum mismatch: '+name)
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                path = Path(member.filename)
                if path.is_absolute() or '..' in path.parts or '\\' in member.filename:
                    raise ValueError('unsafe wheel member')
                # Preserve import packages, shared libraries, model data and license metadata.
                if member.is_dir():
                    (target/path).mkdir(parents=True,exist_ok=True)
                else:
                    output=target/path;output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(archive.read(member))
    return {'locked_wheels':len(lock),'installed':True,'platform':'linux-x86_64-python3.12'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock',type=Path,default=Path(__file__).resolve().parents[1]/'deployment/ocr-wheel-lock.json')
    parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--wheel-dir',type=Path,required=True)
    parser.add_argument('--offline',action='store_true')
    args=parser.parse_args()
    try:
        print(json.dumps(install(args.lock,args.target,args.wheel_dir,offline=args.offline)));return 0
    except Exception as exc:
        print(json.dumps({'installed':False,'error':str(exc)}));return 1

if __name__=='__main__':raise SystemExit(main())

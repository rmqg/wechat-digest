#!/usr/bin/env python3
"""Prepare or explicitly start login-time key capture for the isolated Linux client."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import settings
ROOT = settings.ROOT
HOME = settings.RUNTIME / "gui"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="Exact account directory when multiple accounts exist")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--capture", action="store_true", help="Wait for explicit phone-approved manual re-login in the running desktop")
    mode.add_argument("--check", action="store_true", help="Read-only preflight; never attach a debugger (default)")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    project = settings.load_project()
    image = settings.runtime_image("keyscan")
    desktop = settings.verify_desktop_container(project)
    candidates = []
    for pattern in ("xwechat_files/*/db_storage", "Documents/xwechat_files/*/db_storage", "文档/xwechat_files/*/db_storage"):
        candidates.extend(p for p in HOME.glob(pattern) if p.is_dir())
    candidates = sorted(set(candidates))
    if args.account:
        candidates = [p for p in candidates if p.parent.name == args.account]
    if len(candidates) != 1:
        raise SystemExit(f"Need one explicitly selected, phone-approved account; found {len(candidates)}.")
    database = candidates[0]
    if not any(database.rglob("*.db")):
        raise SystemExit("The client has not created local databases yet.")
    private = settings.RUNTIME / "keyscan"
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    container_database = "/config/" + database.relative_to(HOME).as_posix()
    command = [
        "docker", "run", "--rm", "--pull", "never", "--network", "none",
        "--name", "wechat-digest-keyscan", "--pid", f"container:{desktop}", "--user", "0:0",
        "--read-only", "--cap-drop", "ALL", "--cap-add", "SYS_PTRACE", "--cap-add", "DAC_READ_SEARCH",
        "--security-opt", "no-new-privileges:true", "--memory", "1536m", "--cpus", "1",
        "--mount", f"type=bind,src={HOME},dst=/config,readonly",
        "--mount", f"type=bind,src={private},dst=/scan",
        "--mount", f"type=bind,src={ROOT / 'deployment'},dst=/tool,readonly",
        "--mount", f"type=bind,src={ROOT / 'tools/wcdb-key-tool'},dst=/opt/wcdb-key-tool,readonly",
        "--volumes-from", f"{desktop}:ro",
        "--tmpfs", "/tmp:rw,nosuid,size=64m", "--workdir", "/tool",
        image, "/tool/capture_41.py", "--db-dir", container_database, "--timeout", str(args.timeout),
    ]
    if not args.capture:
        command.append("--check")
    try:
        # The capture entry point emits only status/counts, never encryption material.
        result = subprocess.run(command, timeout=args.timeout + 90)
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        subprocess.run(["docker", "stop", "--time", "20", "wechat-digest-keyscan"], capture_output=True, timeout=30)
        raise
    if result.returncode:
        return result.returncode
    if not args.capture:
        return 0
    keys = private / "all_keys.json"
    os.chmod(keys, 0o600)
    try:
        os.chown(keys, project["worker_uid"], project["worker_gid"])
    except PermissionError as exc:
        raise SystemExit(f"Cannot grant worker {project['worker_uid']}:{project['worker_gid']} access to key file; run this command as root or align worker_uid/gid to the current owner") from exc
    data = json.loads(keys.read_text(encoding="utf-8"))
    entries = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("enc_key")}
    if not entries:
        raise SystemExit("No HMAC-verified database keys were returned.")
    selection = settings.RUNTIME / "selection.json"
    selection_data = {"source_db_dir": container_database, "wechat_base_dir": str(Path(container_database).parent)}
    selection.write_text(json.dumps(selection_data), encoding="utf-8")
    os.chmod(selection, 0o600)
    os.chown(selection, project["worker_uid"], project["worker_gid"])
    print(json.dumps({"keyed_databases": len(entries), "messages_read": False, "source_read_only": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

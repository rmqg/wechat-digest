#!/usr/bin/env python3
"""Linux 4.1 login-time capture with hardware breakpoints and local HMAC checks."""
import argparse
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
_TOOL_DIR = Path(__file__).resolve().parents[1] / 'tools' / 'wcdb-key-tool'
if not (_TOOL_DIR / 'wcdb_key_tool.py').is_file():
    _TOOL_DIR = Path('/opt/wcdb-key-tool')
sys.path.insert(0, str(_TOOL_DIR))
import wcdb_key_tool as wcdb


def process_state(pid):
    fields = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    return fields


def capture_address(pid, binary, mapped_path=None):
    with binary.open("rb") as f:
        kind = struct.unpack_from("<H", f.read(64), 16)[0]
    mappings = []
    for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[5] == (mapped_path or str(binary)):
            start, end = (int(x, 16) for x in fields[0].split("-"))
            mappings.append((start, end, fields[1], int(fields[2], 16)))
    # ELF virtual addresses are relative to the load bias, not the RX segment start.
    bias = min(start - offset for start, end, perms, offset in mappings) if kind == 3 else 0
    address = bias + wcdb.find_hook_offset(binary)
    segment = next((m for m in mappings if "x" in m[2] and m[0] <= address < m[1]), None)
    if segment is None:
        raise RuntimeError("Capture point is outside executable mappings")
    with binary.open("rb") as f:
        f.seek(segment[3] + address - segment[0])
        expected = f.read(16)
    with open(f"/proc/{pid}/mem", "rb", buffering=0) as memory:
        memory.seek(address)
        if memory.read(16) != expected:
            raise RuntimeError("Runtime capture point does not match the official binary")
    return address


def release_debugger(proc, pid):
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Hardware breakpoints never replace client instructions with INT3.
                proc.kill()
                proc.wait(timeout=5)
    state = process_state(pid)
    if int(state.get("TracerPid", "0")) != 0:
        raise RuntimeError("Debugger did not detach; client state requires inspection")
    if state.get("State", "").startswith(("T", "t")):
        os.kill(pid, signal.SIGCONT)


def _gdb_script(pid, address, result_file):
    return f'''set pagination off
set confirm off
set debuginfod enabled off
set auto-load safe-path /nonexistent
set print thread-events off
handle SIGPIPE nostop noprint pass
handle SIGUSR1 nostop noprint pass
handle SIGUSR2 nostop noprint pass
handle SIGALRM nostop noprint pass
attach {pid}
python
import gdb, json, os
class Capture(gdb.Breakpoint):
    def stop(self):
        candidates = []
        try:
            pointer = int(gdb.parse_and_eval("$rsi"))
            length = int(gdb.parse_and_eval("$rdx"))
            inferior = gdb.selected_inferior()
            if pointer and length == 32:
                candidates.append(inferior.read_memory(pointer, 32).tobytes().hex())
            if pointer:
                try:
                    size = int(gdb.parse_and_eval("*(unsigned long long*)($rsi+16)"))
                    value = int(gdb.parse_and_eval("*(unsigned long long*)($rsi+8)"))
                    if size == 32 and value:
                        candidates.append(inferior.read_memory(value, 32).tobytes().hex())
                except gdb.error:
                    pass
        except gdb.error:
            return False
        if not candidates:
            return False
        fd = os.open({str(result_file)!r}, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(list(dict.fromkeys(candidates)), output)
        return True
Capture("*{address:#x}", type=gdb.BP_HARDWARE_BREAKPOINT)
print("CAPTURE_ARMED", flush=True)
end
continue
detach
quit
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-dir", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    pid = wcdb._find_wechat_pid()
    mapped_path = os.readlink(f'/proc/{pid}/exe')
    if mapped_path.endswith(' (deleted)'):
        raise RuntimeError('Running client executable was replaced; restart the client explicitly before capture')
    binary = Path(f'/proc/{pid}/root') / mapped_path.lstrip('/')
    state = process_state(pid)
    if int(state.get("TracerPid", "0")) or state.get("State", "").startswith(("T", "t")):
        raise RuntimeError("Client is already traced or stopped; refusing to attach")
    address = capture_address(pid, binary, mapped_path)
    if args.check:
        print(json.dumps({"capture_point_located": True, "debugger_attached": False, "binary_matches": True}))
        return
    with tempfile.TemporaryDirectory(prefix="capture-", dir="/scan") as temporary:
        folder = Path(temporary)
        result_file = folder / "candidates.json"
        script = folder / "capture.gdb"
        script.write_text(_gdb_script(pid, address, result_file), encoding="utf-8")
        print("Waiting for phone-approved re-login in this client's window; no messages will be sent.", flush=True)
        with (folder / "gdb.log").open("w+") as log:
            proc = subprocess.Popen(["gdb", "-q", "--nx", "--batch", "-x", str(script)], stdout=log, stderr=subprocess.STDOUT)
            try:
                proc.wait(timeout=args.timeout)
            finally:
                release_debugger(proc, pid)
            if not result_file.exists():
                log.seek(0)
                detail = log.read()
                if "hardware break" in detail.lower() or "Cannot insert" in detail:
                    raise RuntimeError("Hardware capture point was not supported; no software breakpoint fallback used")
                raise RuntimeError("Login-time capture did not complete; private debugger output was not logged externally")
        candidates = json.loads(result_file.read_text())
        db_files, salts = wcdb.collect_db_files(args.db_dir)
        key_map = {}
        valid_passphrase = None
        for candidate in candidates:
            derived = wcdb._derive_keys_from_passphrase(bytes.fromhex(candidate), db_files, salts)
            if derived:
                key_map = derived
                valid_passphrase = candidate
                break
        if not key_map:
            raise RuntimeError("Captured candidates did not pass database HMAC verification")
        keys = {rel: {"enc_key": key_map[salt], "salt": salt} for rel, path, size, salt, page in db_files if salt in key_map}
        if not any(rel.startswith("message/message_") and not rel.endswith("_fts.db") for rel in keys):
            raise RuntimeError("No verified message database key; refusing to publish partial setup")
        keys["_db_dir"] = args.db_dir
        for name, payload in (("all_keys.json", keys), ("passphrase.json", {"passphrase": valid_passphrase})):
            destination = Path("/scan") / name
            staging = destination.with_suffix(".tmp")
            staging.write_text(json.dumps(payload), encoding="utf-8")
            os.chmod(staging, 0o600)
            staging.replace(destination)
        print(json.dumps({"verified_databases": len(keys) - 1, "database_files": len(db_files), "debugger_detached": True, "message_bodies_read": False}), flush=True)


if __name__ == "__main__":
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    main()

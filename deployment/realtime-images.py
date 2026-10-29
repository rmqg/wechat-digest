#!/usr/bin/env python3
"""Continuously process newly synchronized WeChat images without generating a digest."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from datetime import datetime, time as datetime_time, timedelta, timezone

import hashlib
from zoneinfo import ZoneInfo

import settings
ROOT = settings.ROOT
RUNTIME = settings.RUNTIME
DIGEST = RUNTIME / "digest"
STATE = DIGEST / "realtime-images-state.json"
LOCK = DIGEST / "realtime-images.lock"
POLL_SECONDS = 15
RECHECK_SECONDS = 600
TZ = ZoneInfo(settings.load_project().get("timezone", "Asia/Shanghai"))
STOP = False


def _stop(_signum, _frame):
    global STOP
    STOP = True
    raise SystemExit(0)


def _load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
def _save_state(state):
    if not _storage_ready():
        raise RuntimeError("data volume is not mounted")
    DIGEST.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DIGEST, 0o700)
    fd, name = tempfile.mkstemp(prefix=".realtime-state-", dir=DIGEST)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, STATE)
        os.chmod(STATE, 0o600)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _storage_ready():
    return RUNTIME.is_dir()


def _source_dir():
    selection = json.loads((RUNTIME / "selection.json").read_text(encoding="utf-8"))
    selected = selection.get("source_db_dir")
    if not isinstance(selected, str) or not selected.startswith("/config/"):
        raise ValueError("invalid selected account path")
    relative = Path(selected[len("/config/"):])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid selected account path")
    gui = (RUNTIME / "gui").resolve(strict=True)
    source = (gui / relative).resolve(strict=True)
    if not source.is_dir() or not source.is_relative_to(gui):
        raise ValueError("selected account path is outside GUI data")
    return source


def _fingerprint():
    source = _source_dir()
    paths = []
    message = source / "message"
    if message.is_dir():
        for path in message.iterdir():
            if path.name.startswith("message_") and (path.name.endswith(".db") or path.name.endswith(".db-wal")):
                paths.append(path)
    resource = message / "message_resource.db"
    paths.extend((resource, Path(str(resource) + "-wal")))
    paths.extend((RUNTIME / "gemini-model.json", RUNTIME / "vision-qwen.json"))
    details = []
    for path in sorted(set(paths), key=lambda value: value.name):
        try:
            stat = path.stat()
            details.append((path.name, stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            details.append((path.name, None, None))
    encoded = json.dumps(details, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _window(now):
    local = datetime.fromtimestamp(now, TZ)
    previous_day = local.date() - timedelta(days=1)
    daily_hour = int(settings.load_project().get('daily_hour', 5))
    baseline = datetime.combine(previous_day, datetime_time(daily_hour, 0), tzinfo=TZ)
    return int(baseline.timestamp()), int(now) + 1


def process_once(state=None, force=False):
    state = _load_state() if state is None else state
    now = int(time.time())
    fingerprint = _fingerprint()
    changed = fingerprint != state.get("source_fingerprint")
    pending = state.get("pending_start")
    due = not state.get("next_attempt_at") or now >= int(state["next_attempt_at"])
    recheck_due = now - int(state.get("last_scan", 0)) >= RECHECK_SECONDS
    if not force and not changed and not recheck_due and (pending is None or not due):
        return state, False
    start, end = _window(now)
    if pending is not None:
        start = min(start, int(pending))
    try:
        from image_pipeline import process_window
        state.update(last_status="processing", pending_start=start, source_fingerprint=fingerprint, last_scan=now)
        _save_state(state)
        result = process_window(start, end, max_images=4, wait_for_lock=False, scope="realtime")
        status = result.get("status")
        state.update(last_scan=int(time.time()), source_fingerprint=fingerprint,
                     counts=result.get("counts", {}), pending_start=result.get("pending_start"),
                     next_attempt_at=result.get("next_attempt_at"), last_error=None, last_status=status,
                     timings=result.get("timings", {}))
        if status == "busy":
            state["pending_start"] = min(start, int(pending)) if pending is not None else start
            state["next_attempt_at"] = now + 15
        elif status == "complete":
            state["last_success"] = int(time.time())
        elif status == "failed":
            state["pending_start"] = start
            state["last_error"] = "image processing failed"
            state["next_attempt_at"] = result.get("next_attempt_at") or now + 60
        elif status == "pending":
            if state.get("pending_start") is None:
                state["pending_start"] = start
                state["next_attempt_at"] = now + 60
        else:
            state["last_error"] = "unexpected image processing status"
            state["last_status"] = "failed"
            state["pending_start"] = min(start, int(pending)) if pending is not None else start
            state["next_attempt_at"] = now + 60
    except Exception as exc:
        # Deliberately omit exception text: it may contain private paths or URLs.
        state.update(last_scan=now, source_fingerprint=fingerprint,
                     pending_start=min(start, int(pending)) if pending is not None else start,
                     last_error=type(exc).__name__, last_status="failed", next_attempt_at=now + 60)
    _save_state(state)
    return state, True


def serve():
    if not _storage_ready():
        print("data volume is not mounted", file=sys.stderr)
        return 1
    DIGEST.mkdir(parents=True, exist_ok=True, mode=0o700)
    with LOCK.open("a+") as lock:
        os.chmod(LOCK, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("realtime image worker already running", file=sys.stderr)
            return 1
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        state = _load_state()
        while not STOP:
            if not _storage_ready():
                print("data volume is not mounted", file=sys.stderr)
                return 1
            now = int(time.time())
            try:
                state, _ = process_once(state)
            except Exception:
                if not _storage_ready():
                    print("data volume is not mounted", file=sys.stderr)
                    return 1
                state.update(last_scan=now, last_error="source probe failed", next_attempt_at=now + 60)
                _save_state(state)
            for _ in range(POLL_SECONDS):
                if STOP:
                    break
                time.sleep(1)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true", help="run continuously (default)")
    mode.add_argument("--once", action="store_true", help="perform one eligible scan")
    mode.add_argument("--status", action="store_true", help="print persisted state")
    args = parser.parse_args(argv)
    if not _storage_ready():
        print("data volume is not mounted", file=sys.stderr)
        return 1
    if args.status:
        print(json.dumps(_load_state(), sort_keys=True))
        return 0
    if args.once:
        DIGEST.mkdir(parents=True, exist_ok=True, mode=0o700)
        with LOCK.open("a+") as lock:
            os.chmod(LOCK, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({"status": "busy", "reason": "realtime worker already owns the service lock"}))
                return 0
            try:
                state, _ = process_once(_load_state(), force=True)
            except Exception:
                return 1
            print(json.dumps(state, sort_keys=True))
            return 1 if state.get("last_status") == "failed" else 0
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())

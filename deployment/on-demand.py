#!/usr/bin/env python3
"""Scan and deliver explicit on-demand summaries for the selected WeChat group."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo
import time

import settings
ROOT = settings.ROOT
RUNTIME = settings.RUNTIME
DIGEST = RUNTIME / "digest"
CONFIG = RUNTIME / "summary-trigger.json"
WORK = DIGEST / "summary-commands"
JOBS = DIGEST / "on-demand-jobs"
RECEIPTS = DIGEST / "on-demand-receipts"
LOCK = DIGEST / "on-demand.lock"
DELIVERY_LOCK = DIGEST / "delivery.lock"
SCANNER = ROOT / "deployment" / "summary_commands.py"
GENERATOR = ROOT / "deployment" / "run-digest.py"
SENDER = ROOT / "deployment" / "send-report.py"
CHAT_ID = settings.load_project().get("group", {}).get("id")
REPORT_PREFIX = settings.load_project()["report_prefix"]
POLL_SECONDS = 10
STOP = False


def _project() -> dict:
    return settings.load_project(required=True)


def _group_id() -> str:
    value = _project().get("group", {}).get("id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("explicit group.id is required for on-demand processing")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    project = settings.load_project()
    uid, gid = int(project["worker_uid"]), int(project["worker_gid"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if path.is_relative_to(WORK):
        parent = path.parent
        while parent != WORK and parent.is_relative_to(WORK):
            os.chmod(parent, 0o700)
            try:
                os.chown(parent, uid, gid)
            except PermissionError:
                pass
            parent = parent.parent
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        if path.is_relative_to(WORK):
            try:
                os.chown(name, uid, gid)
            except PermissionError:
                pass
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _load(path: Path, default=None):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else ({} if default is None else default)
    except FileNotFoundError:
        return {} if default is None else default


def _storage_ready() -> bool:
    mount = os.environ.get("WECHAT_DIGEST_STORAGE_MOUNT")
    return not mount or Path(mount).is_mount()


def _config() -> dict:
    project = _project()
    bot = settings.bot_settings(project)
    value = _load(CONFIG)
    if value.get("chat_id") != project["group"]["id"] or value.get("bot_username") != bot["username"]:
        raise ValueError("trigger target and bot must match explicit project configuration")
    names = value.get("bot_names")
    if not isinstance(names, list) or not names or any(not isinstance(name, str) or not name for name in names):
        raise ValueError("invalid trigger configuration")
    if not set(names).issubset(set(bot["names"])):
        raise ValueError("trigger names must be configured in project bot.names")
    enabled = value.get("enabled_from")
    if isinstance(enabled, bool) or not isinstance(enabled, int) or enabled < 0:
        raise ValueError("invalid trigger configuration")
    return value


def _runtime_mounts():
    selection = _load(RUNTIME / "selection.json")
    source = selection.get("source_db_dir")
    if not isinstance(source, str):
        raise ValueError("selected source database unavailable")
    settings.load_project(required=True)
    image = settings.runtime_image("runtime")
    return selection, image


def _container_base(selection: dict, image: str, network: str = "none") -> list[str]:
    project = settings.load_project(required=True)
    args = ["docker", "run", "--rm", "--pull", "never", "--read-only", "--user",
            f"{project['worker_uid']}:{project['worker_gid']}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--memory", "1g", "--cpus", "1", "--tmpfs", "/tmp:rw,nosuid,size=64m,mode=1777",
            "--env", "PYTHONPATH=/source:/source/memory:/source/agent_console:/tool:/tool/vendor",
            "--env", "WECHAT_DIGEST_CONFIG=/runtime/project.json", "--env", "TZ=" + project["timezone"], "--network", network]
    mounts = [(ROOT / "memory", "/source/memory", True), (ROOT / "agent_console", "/source/agent_console", True),
              (ROOT / "assets", "/source/assets", True), (ROOT / "deployment", "/tool", True),
              (settings.CONFIG_PATH.resolve(), "/runtime/project.json", True),
              (RUNTIME / "gui", "/config", True), (RUNTIME / "keyscan" / "all_keys.json", "/keys/all_keys.json", True),
              (DIGEST, "/work", False), (RUNTIME / "selection.json", "/runtime/selection.json", True),
              (RUNTIME / "gemini-model.json", "/models/gemini.json", True)]
    for src, dst, readonly in mounts:
        args += ["--mount", f"type=bind,src={src},dst={dst}" + (",readonly" if readonly else "")]
    return args + [image]


def _container_call(argv: list[str], *, network="none", timeout=3600):
    selection, image = _runtime_mounts()
    return subprocess.run(_container_base(selection, image, network) + argv, cwd=ROOT,
                          capture_output=True, text=True, timeout=timeout)


def _save_cursor_and_jobs(scan: dict) -> None:
    # Durable job records precede cursor advancement: a crash can at worst rediscover
    # an already-persisted ID, never lose an observed command.
    commands = scan.get("commands")
    cursor = scan.get("cursor")
    if not isinstance(commands, list) or not isinstance(cursor, dict):
        raise ValueError("invalid scanner output")
    for item in commands:
        request_id = item.get("request_id") if isinstance(item, dict) else None
        if not isinstance(request_id, str) or len(request_id) != 64 or any(c not in "0123456789abcdef" for c in request_id):
            raise ValueError("invalid scanner command")
        path = JOBS / f"{request_id}.json"
        if not path.exists():
            _atomic_json(path, {"status": "queued", "request_id": request_id, "candidate": item,
                                "created_at": int(time.time()), "attempts": 0})
    _atomic_json(WORK / "cursor.json", cursor)


def _source_fingerprint() -> str:
    selected = _load(RUNTIME / 'selection.json')['source_db_dir']
    if not isinstance(selected, str) or not selected.startswith('/config/'):
        raise ValueError('invalid selected account path')
    gui = (RUNTIME / 'gui').resolve(strict=True)
    source = (gui / selected[len('/config/'):]).resolve(strict=True)
    if not source.is_relative_to(gui):
        raise ValueError('selected account path escapes GUI storage')
    paths = list((source / 'message').glob('message_*.db')) + list((source / 'message').glob('message_*.db-wal'))
    values = [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(paths)]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def scan_once(config: dict) -> dict:
    WORK.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(WORK, 0o700)
    fingerprint = _source_fingerprint()
    scan_state = _load(WORK / 'scan-state.json')
    if scan_state.get('fingerprint') == fingerprint and time.time() - scan_state.get('scanned_at', 0) < 600:
        return {'status': 'idle', 'new_commands': 0}
    candidate_path = WORK / "scan.json"
    args = ["/tool/summary_commands.py", "--scan", "--source-db-dir", _load(RUNTIME / "selection.json")["source_db_dir"],
            "--bot-username", config["bot_username"], "--enabled-from", str(config["enabled_from"]),
            "--cursor-file", "/work/summary-commands/cursor.json", "--output", "/work/summary-commands/scan.json"]
    for name in config["bot_names"]:
        args += ["--bot-name", name]
    result = _container_call(args, timeout=300)
    if result.returncode:
        raise RuntimeError("command scan failed")
    scan = json.loads(candidate_path.read_text(encoding="utf-8"))
    _save_cursor_and_jobs(scan)
    _atomic_json(WORK / 'scan-state.json', {'fingerprint': fingerprint, 'scanned_at': int(time.time())})
    return {"status": "scanned", "new_commands": len(scan["commands"])}


def _local_report_path(value: str) -> Path:
    candidate = Path(value)
    if str(candidate).startswith("/reports/"):
        candidate = RUNTIME / "reports" / candidate.relative_to("/reports")
    elif not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve(strict=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _report_day(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, ZoneInfo(_project()["timezone"])).date().isoformat()


def _failure(job: dict, stage: str, *, retry=True) -> dict:
    job.update(status="failed", stage=stage, updated_at=int(time.time()), next_attempt_at=int(time.time()) + 60 if retry else None)
    job["attempts"] = int(job.get("attempts", 0)) + 1
    _atomic_json(JOBS / f"{job['request_id']}.json", job)
    return {"status": "failed", "stage": stage, "request_id": job["request_id"]}


def _interpret(job: dict) -> dict | None:
    if job.get("intent", {}).get("status") == "accepted":
        return job["intent"]
    candidate = WORK / "jobs" / job["request_id"] / "candidate.json"
    output = candidate.with_name("intent.json")
    _atomic_json(candidate, job["candidate"])
    result = _container_call(["/tool/summary_commands.py", "--interpret", "--input", str(Path("/work") / candidate.relative_to(DIGEST)),
                             "--output", str(Path("/work") / output.relative_to(DIGEST))], network="wechat-digest_default", timeout=300)
    if result.returncode:
        _failure(job, "interpret")
        return None
    intent = json.loads(output.read_text(encoding="utf-8"))
    if intent.get("status") == "ignored":
        intent["request_id"] = job["request_id"]
    if intent.get("request_id") != job["request_id"] or intent.get("status") not in {"accepted", "ignored"}:
        _failure(job, "invalid-intent")
        return None
    job.update(intent=intent, updated_at=int(time.time()))
    if intent["status"] == "ignored":
        job["status"] = "ignored"
    else:
        job["status"] = "accepted"
    _atomic_json(JOBS / f"{job['request_id']}.json", job)
    return intent


def _edition_receipt_path(request_id: str, edition: str) -> Path:
    suffix = "-review" if edition == "review" else ""
    return RECEIPTS / f"{request_id}{suffix}.json"

def _recover_on_demand_bundle(request_id: str, start: int, end: int) -> dict:
    reports = (RUNTIME / "reports").resolve(strict=True)
    base = reports / f"{REPORT_PREFIX}-request-{request_id}"
    expected_request = {"kind": "on_demand", "id": request_id, "trigger_timestamp": end}
    expected_window = {"start_unix": start, "end_unix_exclusive": end}
    receipt_paths = {edition: _edition_receipt_path(request_id, edition) for edition in ("brief", "review")}
    if receipt_paths["review"].exists() and not receipt_paths["brief"].exists():
        raise ValueError("review receipt exists without a confirmed brief receipt")
    recovered = {}
    for edition in ("brief", "review"):
        stem = Path(str(base) + "-" + edition)
        html_path = _local_report_path(str(stem.with_suffix(".html")))
        png = _local_report_path(str(stem.with_suffix(".png")))
        metadata_path = _local_report_path(str(stem.with_suffix(".json")))
        if (not html_path.is_relative_to(reports) or html_path.suffix.lower() != ".html"
                or not png.is_relative_to(reports) or png.suffix.lower() != ".png"
                or not metadata_path.is_relative_to(reports)):
            raise ValueError("recovered edition paths must stay within runtime/reports")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (metadata.get("edition") != edition or metadata.get("request") != expected_request
                or metadata.get("chat_username") != CHAT_ID or metadata.get("window") != expected_window):
            raise ValueError("recovered edition metadata does not match request and time window")
        digest = _sha256(png)
        item = {"status": "prepared", "html": str(html_path), "png": str(png),
                "json": str(metadata_path), "png_sha256": digest}
        receipt_path = receipt_paths[edition]
        if receipt_path.exists():
            receipt = _load(receipt_path)
            if (receipt.get("status") != "sent" or receipt.get("edition") != edition
                    or receipt.get("chat_id") != CHAT_ID or receipt.get("request_id") != request_id
                    or receipt.get("report_day") != _report_day(end)
                    or not receipt.get("server_id") or receipt.get("png_sha256") != digest):
                raise ValueError(f"recovered {edition} receipt does not match its report artifact")
            item.update(status="sent", server_id=str(receipt["server_id"]), sent_at=receipt["sent_at"])
        recovered[edition] = item
    return recovered


def _retry_complete_notice(job: dict) -> None:
    receipt = _load(_edition_receipt_path(job["request_id"], "brief"))
    review = _load(_edition_receipt_path(job["request_id"], "review"))
    if (receipt.get("status") != "sent" or receipt.get("edition") != "brief"
            or receipt.get("chat_id") != CHAT_ID or receipt.get("request_id") != job["request_id"]
            or review.get("status") != "sent" or review.get("edition") != "review"
            or review.get("chat_id") != CHAT_ID or review.get("request_id") != job["request_id"]
            or review.get("report_day") != receipt.get("report_day")):
        return
    try:
        _retry_notice(receipt, job["request_id"])
    except Exception:
        pass


def process_job(path: Path) -> dict:
    global CHAT_ID
    project = _project()
    bot = settings.bot_settings(project)
    if not bot["names"]:
        raise ValueError("project.bot.names is required for on-demand delivery")
    CHAT_ID = project["group"]["id"]
    job = _load(path)
    request_id = job["request_id"]
    status = job.get("status")
    now = int(time.time())
    root_receipt_path = _edition_receipt_path(request_id, "brief")
    legacy_receipt = _load(root_receipt_path)
    if (legacy_receipt.get("status") == "sent" and "edition" not in legacy_receipt
            and legacy_receipt.get("chat_id") == CHAT_ID and legacy_receipt.get("request_id") == request_id
            and legacy_receipt.get("server_id")):
        if status != "sent":
            job.update(status="sent", server_id=legacy_receipt["server_id"],
                       sent_at=legacy_receipt.get("sent_at", now), updated_at=now)
            _atomic_json(path, job)
        return {"status": "sent", "request_id": request_id, "already_terminal": True}
    has_new_brief = (legacy_receipt.get("status") == "sent" and legacy_receipt.get("edition") == "brief"
                     and legacy_receipt.get("chat_id") == CHAT_ID and legacy_receipt.get("request_id") == request_id
                     and legacy_receipt.get("server_id"))
    if status in {"sent", "unknown", "ignored"} and not isinstance(job.get("editions"), dict):
        if not (status == "sent" and has_new_brief):
            return {"status": status, "request_id": request_id}
    if status == "sending" and not isinstance(job.get("editions"), dict) and not has_new_brief:
        job.update(status="unknown", error="interrupted during send; automatic retry prohibited", updated_at=now)
        _atomic_json(path, job)
        return {"status": "unknown", "request_id": request_id}
    if status in {"failed", "prepared"} and job.get("next_attempt_at") and now < job["next_attempt_at"]:
        return {"status": "backoff", "request_id": request_id}
    intent = job.get("intent") if job.get("intent", {}).get("status") == "accepted" else _interpret(job)
    if not intent or intent.get("status") == "ignored":
        return {"status": "ignored" if intent else "failed", "request_id": request_id}
    start, end = intent.get("start"), intent.get("end")
    if any(isinstance(x, bool) or not isinstance(x, int) for x in (start, end)) or end != job["candidate"].get("trigger_timestamp") or start >= end or intent.get("duration_seconds") != end - start:
        return _failure(job, "invalid-window")
    expected_window = {"start_unix": start, "end_unix_exclusive": end}
    expected_request = {"kind": "on_demand", "id": request_id, "trigger_timestamp": end}
    editions = job.get("editions")
    if isinstance(editions, dict):
        for edition in ("brief", "review"):
            item = editions.get(edition)
            receipt_path = _edition_receipt_path(request_id, edition)
            if receipt_path.exists():
                continue
            if isinstance(item, dict) and item.get("status") in {"unknown", "sending"}:
                if item.get("status") == "sending":
                    item.update(status="unknown", error="interrupted during send; automatic retry prohibited")
                    job.update(status="unknown", updated_at=int(time.time()))
                    _atomic_json(path, job)
                return {"status": "unknown", "request_id": request_id, "edition": edition}
            if isinstance(item, dict) and item.get("status") == "sent":
                item.update(status="unknown", error="sent edition receipt is missing; automatic retry prohibited")
                job.update(status="unknown", updated_at=int(time.time()))
                _atomic_json(path, job)
                return {"status": "unknown", "request_id": request_id, "edition": edition}
    prepared_ok = isinstance(editions, dict) and all(
        isinstance(editions.get(edition), dict)
        and all(editions[edition].get(key) for key in ("html", "png", "json", "png_sha256"))
        for edition in ("brief", "review"))
    if not prepared_ok and any(_edition_receipt_path(request_id, edition).exists() for edition in ("brief", "review")):
        try:
            recovered = _recover_on_demand_bundle(request_id, start, end)
            job.update(status="prepared", editions=recovered, report_day=_report_day(end),
                       window={"start": start, "end": end}, updated_at=int(time.time()))
            job.pop("png", None)
            job.pop("json", None)
            job.pop("png_sha256", None)
            job.pop("error", None)
            _atomic_json(path, job)
            editions = job["editions"]
            prepared_ok = True
        except Exception as exc:
            job.update(status="failed", stage="integrity", error=f"could not recover confirmed edition bundle: {exc}",
                       next_attempt_at=None, updated_at=int(time.time()))
            _atomic_json(path, job)
            return {"status": "failed", "stage": "integrity", "request_id": request_id}
    if prepared_ok:
        try:
            reports = (RUNTIME / "reports").resolve(strict=True)
            for edition in ("brief", "review"):
                item = editions[edition]
                receipt_path = _edition_receipt_path(request_id, edition)
                if receipt_path.exists():
                    receipt = _load(receipt_path)
                    if (receipt.get("status") != "sent" or receipt.get("edition") != edition
                            or receipt.get("chat_id") != CHAT_ID or receipt.get("request_id") != request_id
                            or not receipt.get("server_id") or not isinstance(receipt.get("png_sha256"), str)):
                        raise ValueError
                    item.update(status="sent", png_sha256=receipt["png_sha256"],
                                server_id=str(receipt["server_id"]), sent_at=receipt["sent_at"])
                    _atomic_json(path, job)
                elif item.get("status") == "sent":
                    item.update(status="unknown", error="sent edition receipt is missing; automatic retry prohibited")
                    job.update(status="unknown", updated_at=int(time.time()))
                    _atomic_json(path, job)
                    return {"status": "unknown", "request_id": request_id, "edition": edition}
                elif item.get("status") in {"unknown", "sending"}:
                    if item.get("status") == "sending":
                        item.update(status="unknown", error="interrupted during send; automatic retry prohibited")
                        job.update(status="unknown", updated_at=int(time.time()))
                        _atomic_json(path, job)
                    return {"status": "unknown", "request_id": request_id, "edition": edition}
                if item.get("status") == "sent":
                    continue
                html_path = _local_report_path(item["html"])
                png = _local_report_path(item["png"])
                metadata_path = _local_report_path(item["json"])
                if (not html_path.is_relative_to(reports) or html_path.suffix.lower() != ".html"
                        or not png.is_relative_to(reports) or png.suffix.lower() != ".png"
                        or not metadata_path.is_relative_to(reports)):
                    raise ValueError
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if (metadata.get("edition") != edition or metadata.get("request") != expected_request
                        or metadata.get("chat_username") != CHAT_ID or metadata.get("window") != expected_window
                        or _sha256(png) != item["png_sha256"]):
                    raise ValueError
        except Exception:
            return _failure(job, "integrity")
    else:
        job.update(status="generating", window={"start": start, "end": end}, updated_at=now)
        _atomic_json(path, job)
        result = subprocess.run([sys.executable, str(GENERATOR), "--start", str(start), "--end", str(end), "--request-id", request_id],
                                cwd=ROOT, capture_output=True, text=True, timeout=3600)
        if result.returncode:
            return _failure(job, "generate")
        try:
            generated = json.loads(result.stdout.strip().splitlines()[-1])
            generated_editions = generated["editions"]
            reports = (RUNTIME / "reports").resolve(strict=True)
            prepared = {}
            for edition in ("brief", "review"):
                files = generated_editions[edition]
                html_path = _local_report_path(files["html"])
                png = _local_report_path(files["png"])
                metadata_path = _local_report_path(files["json"])
                if (not html_path.is_relative_to(reports) or html_path.suffix.lower() != ".html"
                        or not png.is_relative_to(reports) or png.suffix.lower() != ".png"
                        or not metadata_path.is_relative_to(reports)):
                    raise ValueError
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if (metadata.get("edition") != edition or metadata.get("request") != expected_request
                        or metadata.get("window") != expected_window or metadata.get("chat_username") != CHAT_ID):
                    raise ValueError
                prepared[edition] = {"status": "prepared", "html": str(html_path), "png": str(png),
                                     "json": str(metadata_path), "png_sha256": _sha256(png)}
        except Exception:
            return _failure(job, "generation-output")
        job.update(status="prepared", editions=prepared, report_day=_report_day(end),
                   window={"start": start, "end": end}, updated_at=int(time.time()))
        job.pop("png", None)
        job.pop("json", None)
        job.pop("png_sha256", None)
        job.pop("error", None)
        _atomic_json(path, job)
        editions = job["editions"]

    if all(editions[edition].get("status") == "sent" for edition in ("brief", "review")):
        job.update(status="sent", updated_at=int(time.time()))
        job.pop("next_attempt_at", None)
        _atomic_json(path, job)
        _retry_complete_notice(job)
        return {"status": "sent", "request_id": request_id}

    lock = DELIVERY_LOCK.open("a+")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            job.update(status="prepared", next_attempt_at=now + 10, updated_at=int(time.time()))
            _atomic_json(path, job)
            return {"status": "prepared", "reason": "daily delivery lock busy"}
        for edition in ("brief", "review"):
            item = editions[edition]
            receipt_path = _edition_receipt_path(request_id, edition)
            if receipt_path.exists():
                receipt = _load(receipt_path)
                if (receipt.get("status") != "sent" or receipt.get("edition") != edition
                        or receipt.get("chat_id") != CHAT_ID or receipt.get("request_id") != request_id
                        or not receipt.get("png_sha256") or not receipt.get("server_id")):
                    return _failure(job, "receipt-integrity")
                item.update(status="sent", png_sha256=receipt["png_sha256"],
                            server_id=str(receipt["server_id"]), sent_at=receipt["sent_at"])
                _atomic_json(path, job)
                continue
            if item.get("status") in {"unknown", "sending"}:
                if item.get("status") == "sending":
                    item.update(status="unknown", error="interrupted during send; automatic retry prohibited")
                    job.update(status="unknown", updated_at=int(time.time()))
                    _atomic_json(path, job)
                return {"status": "unknown", "request_id": request_id, "edition": edition}
            if edition == "review" and editions["brief"].get("status") != "sent":
                return {"status": editions["brief"].get("status", "prepared"), "request_id": request_id, "edition": "brief"}
            if _sha256(Path(item["png"])) != item["png_sha256"]:
                return _failure(job, "integrity")
            item.update(status="sending", send_started_at=int(time.time()))
            job.update(status="sending", sending_edition=edition, updated_at=int(time.time()))
            _atomic_json(path, job)
            try:
                sent = subprocess.run([sys.executable, str(SENDER), item["png"], "--send", "--chat-id", CHAT_ID],
                                      cwd=ROOT, capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired:
                item.update(status="unknown", error="sender timed out; do not retry")
                job.update(status="unknown", updated_at=int(time.time()))
                _atomic_json(path, job)
                return {"status": "unknown", "request_id": request_id, "edition": edition}
            except OSError:
                item.update(status="prepared", stage="sender-start", next_attempt_at=now + 60)
                job.update(status="prepared", next_attempt_at=now + 60, updated_at=int(time.time()))
                _atomic_json(path, job)
                return {"status": "prepared", "request_id": request_id}
            try:
                confirmation = json.loads(sent.stdout.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                confirmation = {}
            if (sent.returncode == 0 and confirmation.get("status") == "sent"
                    and confirmation.get("confirmed") is True and confirmation.get("server_id")
                    and confirmation.get("sha256") == item["png_sha256"]):
                receipt = {"status": "sent", "edition": edition, "chat_id": CHAT_ID,
                           "server_id": str(confirmation["server_id"]), "png_sha256": item["png_sha256"],
                           "sent_at": int(time.time()), "report_day": job["report_day"], "request_id": request_id}
                _atomic_json(receipt_path, receipt)
                item.update(status="sent", server_id=receipt["server_id"], sent_at=receipt["sent_at"])
                job.update(status="prepared", updated_at=int(time.time()))
                job.pop("next_attempt_at", None)
                _atomic_json(path, job)
                continue
            if confirmation.get("status") == "failed" and confirmation.get("sent") is False and confirmation.get("before_submit") is True:
                item.update(status="prepared", stage="send-before-submit", next_attempt_at=int(time.time()) + 60)
                job.update(status="prepared", next_attempt_at=int(time.time()) + 60, updated_at=int(time.time()))
                _atomic_json(path, job)
                return {"status": "prepared", "request_id": request_id, "edition": edition}
            item.update(status="unknown", confirmation=confirmation)
            job.update(status="unknown", updated_at=int(time.time()))
            _atomic_json(path, job)
            return {"status": "unknown", "request_id": request_id, "edition": edition}
        job.update(status="sent", updated_at=int(time.time()))
        job.pop("next_attempt_at", None)
        _atomic_json(path, job)
        fcntl.flock(lock, fcntl.LOCK_UN)
        _retry_complete_notice(job)
        return {"status": "sent", "request_id": request_id,
                "server_id": editions["brief"].get("server_id")}
    finally:
        lock.close()

def _retry_notice(receipt: dict, request_id: str) -> None:
    import site_access_notice
    with DELIVERY_LOCK.open("a+") as delivery_lock:
        try:
            fcntl.flock(delivery_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        site_access_notice.process_notice(receipt, request_id=request_id)


def process_pending_notices() -> None:
    import site_access_notice
    now = int(time.time())
    for receipt_path in sorted(RECEIPTS.glob("*.json")):
        # Review receipts are never notice identities; only the root brief receipt is.
        if receipt_path.stem.endswith("-review"):
            continue
        receipt = _load(receipt_path)
        request_id = receipt.get("request_id")
        if receipt.get("status") != "sent" or not isinstance(request_id, str):
            continue
        if receipt.get("edition") == "review":
            continue
        if receipt.get("edition") == "brief":
            review = _load(_edition_receipt_path(request_id, "review"))
            if (review.get("status") != "sent" or review.get("edition") != "review"
                    or review.get("request_id") != request_id or review.get("chat_id") != CHAT_ID
                    or review.get("report_day") != receipt.get("report_day")):
                continue
        state_path = site_access_notice.RECEIPTS / f"{request_id}.json"
        state = _load(state_path)
        if state.get("status") in {"sent", "unknown"}:
            continue
        if state.get("status") == "failed" and (state.get("before_submit") is not True or now - int(state.get("updated_at", now)) < 60):
            continue
        try:
            _retry_notice(receipt, request_id)
        except Exception:
            # Notice errors are retried from their private persisted state.
            continue

def _tick(config: dict) -> dict:
    process_pending_notices()
    scan_result = scan_once(config)
    JOBS.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = [(path, _load(path)) for path in JOBS.glob('*.json')]
    for path, job in sorted(pending, key=lambda pair: (pair[1].get('candidate', {}).get('trigger_timestamp', 0), pair[0].name)):
        if job.get("status") in {"sent", "unknown", "ignored"}:
            root = _load(_edition_receipt_path(job.get("request_id", ""), "brief"))
            partial_brief = (job.get("status") == "sent" and not isinstance(job.get("editions"), dict)
                             and root.get("status") == "sent" and root.get("edition") == "brief")
            if not partial_brief:
                continue
        if job.get("next_attempt_at", 0) and time.time() < job["next_attempt_at"]:
            continue
        return {**scan_result, 'job': process_job(path)}
    return scan_result


def _stop(_signum, _frame):
    global STOP
    STOP = True


def serve(once=False, status=False) -> int:
    if not _storage_ready():
        print(json.dumps({"status": "failed", "reason": "data volume is not mounted"}))
        return 1
    if status:
        public_fields = ('request_id', 'status', 'stage', 'attempts', 'created_at', 'updated_at', 'next_attempt_at', 'window')
        print(json.dumps({'jobs': [{k: v for k, v in _load(p).items() if k in public_fields} for p in sorted(JOBS.glob('*.json'))]}, ensure_ascii=False))
        return 0
    config = _config()
    WORK.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(WORK, 0o700)
    project = _project()
    try:
        os.chown(WORK, int(project["worker_uid"]), int(project["worker_gid"]))
    except PermissionError:
        pass
    DIGEST.mkdir(parents=True, exist_ok=True, mode=0o700)
    with LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "busy", "reason": "service already running"}))
            return 0
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        failures = 0
        while True:
            try:
                outcome = _tick(config)
                failures = 0
                if once:
                    print(json.dumps(outcome, ensure_ascii=False))
                    return 1 if outcome.get("job", {}).get("status") == "failed" else 0
            except Exception:
                # Avoid disclosing database locations, commands, URLs, or credentials.
                failures += 1
                if once:
                    print(json.dumps({"status": "failed", "reason": "service operation failed"}))
                    return 1
            if STOP:
                break
            delay = min(60, POLL_SECONDS * (2 ** min(failures, 3))) if failures else POLL_SECONDS
            for _ in range(delay):
                if STOP:
                    break
                time.sleep(1)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--serve", action="store_true")
    modes.add_argument("--once", action="store_true", help="scan once and process at most one queued request")
    modes.add_argument("--status", action="store_true", help="print persisted queue state")
    args = parser.parse_args(argv)
    return serve(once=args.once, status=args.status)


if __name__ == "__main__":
    raise SystemExit(main())

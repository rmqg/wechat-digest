#!/usr/bin/env python3
"""Scheduled report delivery using the configured project's timezone and target."""
from __future__ import annotations

import argparse
from datetime import datetime, time as day_time, timedelta
from zoneinfo import ZoneInfo
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import site_access_notice
import settings

ROOT = settings.ROOT
RUNTIME = settings.RUNTIME
PROJECT = settings.load_project()
GROUP_ID = PROJECT.get("group", {}).get("id")
REPORT_PREFIX = PROJECT["report_prefix"]
TZ = ZoneInfo(PROJECT["timezone"])
PREPARE_AT = day_time(int(PROJECT["daily_hour"]), 0)
STATE = RUNTIME / "digest" / "delivery-state.json"
LOCK = RUNTIME / "digest" / "delivery.lock"
GENERATOR = ROOT / "deployment" / "run-digest.py"
SENDER = ROOT / "deployment" / "send-report.py"
RECEIPTS = RUNTIME / "digest" / "sent-receipts"

def _runtime_root() -> Path:
    return settings.RUNTIME if ROOT == settings.ROOT else ROOT / "runtime"


def _apply_project_timing(project: dict) -> None:
    global TZ, PREPARE_AT, REPORT_PREFIX
    TZ = ZoneInfo(project["timezone"])
    PREPARE_AT = day_time(int(project["daily_hour"]), 0)
    REPORT_PREFIX = project["report_prefix"]


def _configured_group_id() -> str:
    project = settings.load_project(required=True)
    group_id = project.get("group", {}).get("id")
    if not isinstance(group_id, str) or not group_id.strip():
        raise ValueError("explicit group.id is required for delivery")
    _apply_project_timing(project)
    return group_id


def day_window(now: datetime) -> tuple[str, int, int]:
    _apply_project_timing(settings.load_project())
    local = now.astimezone(TZ)
    end_local = datetime.combine(local.date(), PREPARE_AT, tzinfo=TZ)
    start_local = end_local - timedelta(days=1)
    return local.date().isoformat(), int(start_local.timestamp()), int(end_local.timestamp())


def read_state() -> dict:
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid delivery state")
        return value
    except FileNotFoundError:
        return {}


def write_state(value: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)




def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def local_report_path(value: str) -> Path:
    candidate = Path(value)
    if str(candidate).startswith("/reports/"):
        candidate = _runtime_root() / "reports" / candidate.relative_to("/reports")
    elif not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve(strict=True)

def _process_site_notice(receipt: dict) -> dict:
    try:
        return site_access_notice.process_notice(receipt)
    except Exception:
        # Configuration and subprocess details may contain private credentials.
        return {"status": "failed", "reason": "site access notice could not be processed"}


def _recover_daily_bundle(report_day: str, start_epoch: int, end_epoch: int,
                          receipt_paths: dict[str, Path]) -> dict:
    reports = (_runtime_root() / "reports").resolve(strict=True)
    stamp = datetime.fromtimestamp(start_epoch, TZ).strftime("%Y%m%d-%H%M")
    base = reports / f"{REPORT_PREFIX}-{stamp}"
    expected_window = {"start_unix": start_epoch, "end_unix_exclusive": end_epoch}
    if receipt_paths["review"].exists() and not receipt_paths["brief"].exists():
        raise ValueError("review receipt exists without a confirmed brief receipt")
    recovered = {}
    for edition in ("brief", "review"):
        stem = Path(str(base) + "-" + edition)
        html_path = local_report_path(str(stem.with_suffix(".html")))
        png = local_report_path(str(stem.with_suffix(".png")))
        json_path = local_report_path(str(stem.with_suffix(".json")))
        if (not html_path.is_relative_to(reports) or html_path.suffix.lower() != ".html"
                or not png.is_relative_to(reports) or png.suffix.lower() != ".png"
                or not json_path.is_relative_to(reports)):
            raise ValueError("recovered edition paths must stay within runtime/reports")
        metadata = json.loads(json_path.read_text(encoding="utf-8"))
        if (metadata.get("edition") != edition or metadata.get("chat_username") != GROUP_ID
                or metadata.get("window") != expected_window or metadata.get("request") is not None):
            raise ValueError("recovered edition metadata does not match target and time window")
        digest = sha256(png)
        item = {"status": "prepared", "html": str(html_path), "png": str(png),
                "json": str(json_path), "png_sha256": digest}
        receipt_path = receipt_paths[edition]
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if (receipt.get("status") != "sent" or receipt.get("edition") != edition
                    or receipt.get("report_day") != report_day or receipt.get("chat_id") != GROUP_ID
                    or not receipt.get("server_id") or receipt.get("png_sha256") != digest):
                raise ValueError(f"recovered {edition} receipt does not match its report artifact")
            item.update(status="sent", server_id=str(receipt["server_id"]), sent_at=receipt["sent_at"],
                        confirmation=receipt.get("confirmation"))
        recovered[edition] = item
    return recovered


def run_tick(now: datetime, *, force: bool = False, prepare_only: bool = False) -> dict:
    global GROUP_ID
    GROUP_ID = _configured_group_id()
    now = now.astimezone(TZ)
    if not force and now.time().replace(tzinfo=None) < PREPARE_AT:
        return {"status": "waiting", "reason": f"before {PREPARE_AT:%H:%M} {TZ}"}
    report_day, start_epoch, end_epoch = day_window(now)
    expected_window = {"start_unix": start_epoch, "end_unix_exclusive": end_epoch}
    editions_dir = {edition: (RECEIPTS / (report_day + ("-review" if edition == "review" else "") + ".json"))
                    for edition in ("brief", "review")}

    # A pre-contract root receipt is a completed historical single-edition send.
    brief_path = editions_dir["brief"]
    if brief_path.exists():
        receipt = json.loads(brief_path.read_text(encoding="utf-8"))
        if receipt.get("status") != "sent" or receipt.get("chat_id") != GROUP_ID or not receipt.get("server_id"):
            raise ValueError("invalid persisted delivery receipt")
        if "edition" not in receipt:
            return {"status": "sent", "report_day": report_day, "already_terminal": True,
                    "server_id": receipt["server_id"]}
        if receipt.get("edition") != "brief":
            raise ValueError("invalid persisted brief receipt")

    state = read_state()
    if state.get("report_day") == report_day and not isinstance(state.get("editions"), dict):
        if state.get("status") == "sent" and not (brief_path.exists() and json.loads(brief_path.read_text(encoding="utf-8")).get("edition") == "brief"):
            return {"status": "sent", "report_day": report_day, "already_terminal": True}
        if state.get("status") == "unknown":
            return {"status": "unknown", "report_day": report_day, "already_terminal": True}
        if state.get("status") == "sending" and not (brief_path.exists() and json.loads(brief_path.read_text(encoding="utf-8")).get("edition") == "brief"):
            state.update(status="unknown", error="interrupted while sending; automatic retry prohibited", updated_at=int(time.time()))
            write_state(state)
            return {"status": "unknown", "report_day": report_day, "already_terminal": True}
    if (state.get("report_day") == report_day and not isinstance(state.get("editions"), dict)
            and state.get("status") in {"prepared", "failed"} and state.get("window") != expected_window):
        state = {"report_day": report_day}
    current = state if state.get("report_day") == report_day else {"report_day": report_day}
    current["report_day"] = report_day
    current["window"] = expected_window
    editions = current.get("editions")
    if isinstance(editions, dict):
        for edition in ("brief", "review"):
            item = editions.get(edition)
            receipt_path = editions_dir[edition]
            if receipt_path.exists() and isinstance(item, dict) and item.get("png_sha256"):
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if (receipt.get("status") != "sent" or receipt.get("edition") != edition
                        or receipt.get("report_day") != report_day or receipt.get("chat_id") != GROUP_ID
                        or not receipt.get("server_id") or not isinstance(receipt.get("png_sha256"), str)):
                    raise ValueError(f"invalid persisted {edition} receipt")
                item.update(status="sent", png_sha256=receipt["png_sha256"], server_id=str(receipt["server_id"]),
                            sent_at=receipt["sent_at"], confirmation=receipt.get("confirmation"))
        for edition in ("brief", "review"):
            item = editions.get(edition)
            if isinstance(item, dict) and item.get("status") in {"unknown", "sending"} and not editions_dir[edition].exists():
                if item.get("status") == "sending":
                    item.update(status="unknown", error="interrupted while sending; automatic retry prohibited")
                    current.update(status="unknown", updated_at=int(time.time()))
                    write_state(current)
                return {"status": "unknown", "report_day": report_day, "edition": edition}
            if isinstance(item, dict) and item.get("status") == "sent" and not editions_dir[edition].exists():
                item.update(status="unknown", error="sent edition receipt is missing; automatic retry prohibited")
                current.update(status="unknown", updated_at=int(time.time()))
                write_state(current)
                return {"status": "unknown", "report_day": report_day, "edition": edition}
    prepared_ok = isinstance(editions, dict) and all(
        isinstance(editions.get(edition), dict)
        and all(editions[edition].get(key) for key in ("html", "png", "json", "png_sha256"))
        for edition in ("brief", "review"))

    if not prepared_ok and any(path.exists() for path in editions_dir.values()):
        try:
            recovered = _recover_daily_bundle(report_day, start_epoch, end_epoch, editions_dir)
            current.update(status="prepared", editions=recovered, window=expected_window, updated_at=int(time.time()))
            current.pop("error", None)
            write_state(current)
            editions = current["editions"]
            prepared_ok = True
        except Exception as exc:
            current.update(status="failed", error=f"could not recover confirmed edition bundle: {exc}",
                           updated_at=int(time.time()))
            write_state(current)
            return {"status": "failed", "stage": "integrity", "report_day": report_day, "error": current["error"]}
    if prepared_ok:
        try:
            reports = (_runtime_root() / "reports").resolve(strict=True)
            for edition in ("brief", "review"):
                item = editions[edition]
                if item.get("status") == "sent":
                    continue
                html_path = local_report_path(item["html"])
                png = local_report_path(item["png"])
                json_path = local_report_path(item["json"])
                if (not html_path.is_relative_to(reports) or html_path.suffix.lower() != ".html"
                        or not png.is_relative_to(reports) or png.suffix.lower() != ".png"
                        or not json_path.is_relative_to(reports)):
                    raise ValueError("generated edition paths must stay within runtime/reports")
                metadata = json.loads(json_path.read_text(encoding="utf-8"))
                if (metadata.get("edition") != edition or metadata.get("chat_username") != GROUP_ID
                        or metadata.get("window") != expected_window or metadata.get("request") is not None):
                    raise ValueError("generated edition does not match target, edition, and time window")
                if sha256(png) != item["png_sha256"]:
                    raise ValueError("prepared edition fingerprint changed")
        except Exception as exc:
            current.update(status="failed", error=f"invalid prepared editions: {exc}", updated_at=int(time.time()))
            write_state(current)
            return {"status": "failed", "stage": "integrity", "report_day": report_day, "error": current["error"]}
    else:
        # Preserve ambiguous legacy in-flight state; never replace it with a fresh dual send.
        if current.get("status") == "sending":
            current.update(status="unknown", error="interrupted while sending; automatic retry prohibited", updated_at=int(time.time()))
            write_state(current)
            return {"status": "unknown", "report_day": report_day}
        write_state(current)
        result = subprocess.run([sys.executable, str(GENERATOR), "--hours", str((end_epoch - start_epoch) / 3600), "--end", str(end_epoch)],
                                cwd=ROOT, capture_output=True, text=True, timeout=3600)
        if result.returncode:
            current.update(status="failed", error=(result.stderr or result.stdout)[-4000:], updated_at=int(time.time()))
            write_state(current)
            return {"status": "failed", "stage": "generate", "error": current["error"]}
        try:
            generated = json.loads(result.stdout.strip().splitlines()[-1])
            generated_editions = generated["editions"]
            reports = (_runtime_root() / "reports").resolve(strict=True)
            prepared = {}
            for edition in ("brief", "review"):
                files = generated_editions[edition]
                html_path = local_report_path(files["html"])
                png = local_report_path(files["png"])
                json_path = local_report_path(files["json"])
                if (not html_path.is_relative_to(reports) or html_path.suffix.lower() != ".html"
                        or not png.is_relative_to(reports) or png.suffix.lower() != ".png"
                        or not json_path.is_relative_to(reports)):
                    raise ValueError("generator returned edition paths outside runtime/reports")
                metadata = json.loads(json_path.read_text(encoding="utf-8"))
                if (metadata.get("edition") != edition or metadata.get("chat_username") != GROUP_ID
                        or metadata.get("window") != expected_window or metadata.get("request") is not None):
                    raise ValueError("generated edition does not match target, edition, and time window")
                prepared[edition] = {"status": "prepared", "html": str(html_path), "png": str(png),
                                     "json": str(json_path), "png_sha256": sha256(png)}
            current.update(status="prepared", editions=prepared, window=expected_window, updated_at=int(time.time()))
            current.pop("error", None)
            current.pop("png", None)
            current.pop("png_sha256", None)
            current.pop("json", None)
            write_state(current)
        except Exception as exc:
            current.update(status="failed", error=f"invalid generation output: {exc}", updated_at=int(time.time()))
            write_state(current)
            return {"status": "failed", "stage": "generation-output", "error": current["error"]}
    editions = current["editions"]

    if prepare_only:
        return {"status": "prepared", "report_day": report_day,
                "editions": {edition: {"html": editions[edition]["html"], "json": editions[edition]["json"],
                                       "png": editions[edition]["png"], "sha256": editions[edition]["png_sha256"]}
                             for edition in ("brief", "review")}}

    for edition in ("brief", "review"):
        item = editions[edition]
        receipt_path = editions_dir[edition]
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if (receipt.get("status") != "sent" or receipt.get("edition") != edition
                    or receipt.get("report_day") != report_day or receipt.get("chat_id") != GROUP_ID
                    or not receipt.get("server_id") or not isinstance(receipt.get("png_sha256"), str)):
                raise ValueError(f"invalid persisted {edition} receipt")
            item.update(status="sent", png_sha256=receipt["png_sha256"], server_id=str(receipt["server_id"]),
                        sent_at=receipt["sent_at"], confirmation=receipt.get("confirmation"))
            current.update(status="prepared", updated_at=int(time.time()))
            write_state(current)
        elif item.get("status") == "sent":
            item.update(status="unknown", error="sent edition receipt is missing; automatic retry prohibited")
            current.update(status="unknown", updated_at=int(time.time()))
            write_state(current)
            return {"status": "unknown", "report_day": report_day}
        if item.get("status") == "sent":
            continue
        if item.get("status") in {"unknown", "sending"}:
            if item.get("status") == "sending":
                item.update(status="unknown", error="interrupted while sending; automatic retry prohibited")
                current.update(status="unknown", updated_at=int(time.time()))
                write_state(current)
            return {"status": "unknown", "report_day": report_day, "edition": edition}
        if edition == "review" and editions["brief"].get("status") != "sent":
            return {"status": editions["brief"].get("status", "prepared"), "report_day": report_day, "edition": "brief"}
        if sha256(Path(item["png"])) != item["png_sha256"]:
            item.update(status="failed", error="prepared report fingerprint changed")
            current.update(status="failed", updated_at=int(time.time()))
            write_state(current)
            return {"status": "failed", "stage": "integrity", "report_day": report_day, "edition": edition}
        item.update(status="sending", send_started_at=int(time.time()))
        current.update(status="sending", updated_at=int(time.time()))
        write_state(current)
        try:
            result = subprocess.run([sys.executable, str(SENDER), item["png"], "--send", "--chat-id", GROUP_ID],
                                    cwd=ROOT, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired as exc:
            item.update(status="unknown", error=f"sender timed out; do not retry: {exc}")
            current.update(status="unknown", updated_at=int(time.time()))
            write_state(current)
            return {"status": "unknown", "report_day": report_day, "edition": edition}
        try:
            delivered = json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            delivered = {"status": "unknown", "reason": "sender gave no parseable result"}
        if (result.returncode == 0 and delivered.get("status") == "sent" and delivered.get("confirmed") is True
                and delivered.get("server_id") and delivered.get("sha256") == item["png_sha256"]):
            sent_at = int(time.time())
            receipt = {"status": "sent", "edition": edition, "report_day": report_day,
                       "chat_id": GROUP_ID, "server_id": str(delivered["server_id"]),
                       "png_sha256": item["png_sha256"], "sent_at": sent_at, "confirmation": delivered}
            receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = receipt_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(receipt_path)
            item.update(status="sent", server_id=receipt["server_id"], sent_at=sent_at, confirmation=delivered)
            current.update(status="prepared", updated_at=int(time.time()))
            current.pop("error", None)
            write_state(current)
        elif delivered.get("status") == "failed" and delivered.get("sent") is False and delivered.get("before_submit") is True:
            item.update(status="failed", confirmation=delivered, error=delivered.get("error", "pre-submit failure"))
            current.update(status="failed", updated_at=int(time.time()))
            write_state(current)
            return {"status": "failed", "report_day": report_day, "edition": edition, "error": item["error"]}
        else:
            item.update(status="unknown", confirmation=delivered,
                        error=(result.stderr or delivered.get("error") or delivered.get("reason") or
                               "delivery cannot be independently confirmed")[-4000:])
            current.update(status="unknown", updated_at=int(time.time()))
            write_state(current)
            return {"status": "unknown", "report_day": report_day, "edition": edition}

    current.update(status="sent", updated_at=int(time.time()))
    notice = None
    if not prepare_only and current.get("notice_status") not in {"sent", "unknown"}:
        brief_receipt = json.loads(editions_dir["brief"].read_text(encoding="utf-8"))
        notice = _process_site_notice(brief_receipt)
        current["notice_status"] = notice.get("status")
    write_state(current)
    outcome = {"status": "sent", "report_day": report_day, "editions": {
        edition: {"server_id": editions[edition].get("server_id"), "png": editions[edition]["png"]}
        for edition in ("brief", "review")}}
    if notice is not None:
        outcome["notice"] = notice
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tick", action="store_true", help="run one cron tick (default)")
    parser.add_argument("--force", action="store_true", help="bypass schedule time for controlled validation")
    parser.add_argument("--prepare-only", action="store_true", help="generate and verify report without sending")
    args = parser.parse_args()
    mount = os.environ.get("WECHAT_DIGEST_STORAGE_MOUNT")
    if mount and not Path(mount).is_mount():
        print(json.dumps({"status": "failed", "stage": "storage", "error": "configured data volume is not mounted"}))
        return 1
    LOCK.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(LOCK.parent, 0o700)
    except OSError:
        pass
    with LOCK.open("a+") as lock:
        os.chmod(LOCK, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "running", "reason": "another tick owns the delivery lock"}))
            return 0
        outcome = run_tick(datetime.now(TZ), force=args.force, prepare_only=args.prepare_only)
    print(json.dumps(outcome, ensure_ascii=False))
    return 1 if outcome["status"] in {"failed", "unknown"} else 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import datetime as dt

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Callable, Any
from urllib.parse import quote, urlsplit, urlunsplit

from site_access_message import build_access_message

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TERMINAL = {"sent", "unknown", "cancelled"}


def _day(value: Any) -> str | None:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return None
    try:
        from datetime import date
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _report_day(task: dict) -> str | None:
    if task["kind"] == "daily":
        return _day(task.get("daily_key"))
    end = task.get("end")
    if type(end) is not int:
        return None
    try:
        return dt.datetime.fromtimestamp(end, dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _safe_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    if not stat.S_ISDIR(path.stat().st_mode):
        raise OSError("receipt path is not a directory")


def _atomic_json(path: Path, value: dict) -> None:
    value["source"] = "deployment/qq_site_access_notice.py:process_notice"
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("invalid notice receipt")
    return value


def _valid_config(path: Path, group_id: int, bot_names: list[str]) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("invalid notice configuration")
    if type(config.get("group_id")) is not int or config["group_id"] != group_id:
        raise ValueError("invalid notice configuration")
    for key in ("username", "password"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError("invalid notice configuration")
    url = config.get("url")
    if not isinstance(url, str):
        raise ValueError("invalid notice configuration")
    parsed = urlsplit(url)
    if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path != f"/qq/{group_id}/"):
        raise ValueError("invalid notice configuration")
    bot_name = config.get("bot_name")
    if (not isinstance(bot_name, str) or "\n" in bot_name or "\r" in bot_name
            or bot_name not in bot_names):
        raise ValueError("invalid notice configuration")
    enabled_from = config.get("enabled_from")
    if isinstance(enabled_from, bool) or not isinstance(enabled_from, int):
        raise ValueError("invalid notice configuration")
    return config


def _access_url(config: dict) -> str:
    parts = urlsplit(config["url"])
    return urlunsplit(("https", f"{quote(config['username'], safe='')}:{quote(config['password'], safe='')}@{parts.netloc}",
                       parts.path, "", ""))


def _metadata(state: dict) -> dict:
    keys = ("task_id", "report_day", "group_id", "status", "attempts", "message_id",
            "error_type", "next_attempt_at", "retry_count", "before_submit", "location",
            "text_sha256", "updated_at", "source")
    return {key: state[key] for key in keys if key in state}


def read_receipts(state_dir: Path) -> list[dict]:
    """Read safe notice metadata without changing receipts or recovery state."""
    result = []
    try:
        paths = sorted(Path(state_dir).glob("*.json"))
    except OSError:
        return result
    for path in paths:
        try:
            data = _read_json(path)
            if data:
                result.append(_metadata(data))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return result


def process_notice(task: dict, *, config_path: Path, group_id: int, bot_names: list[str],
                   state_dir: Path, open_verified: Callable, close_client: Callable,
                   now: int) -> dict:
    """Send one standalone access notice for a completed daily or requested summary."""
    if (not isinstance(task, dict) or task.get("kind") not in ("daily", "request")
            or task.get("status") != "complete" or not isinstance(task.get("id"), str)
            or not task["id"] or type(group_id) is not int or group_id <= 0
            or isinstance(now, bool) or not isinstance(now, int)):
        return {"status": "ineligible"}
    report_day = _report_day(task)
    if report_day is None:
        return {"status": "ineligible"}
    task_id = task["id"]
    ident = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    receipt_path = Path(state_dir) / f"{ident}.json"
    lock_path = Path(state_dir) / f"{ident}.lock"
    _safe_dir(Path(state_dir))
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy"}
        state = _read_json(receipt_path)
        if state.get("status") in _TERMINAL:
            return {"status": state["status"], "already_terminal": True}
        if state.get("status") == "sending":
            state.update(status="unknown", error_type="InterruptedWhileSending", location="recovery", updated_at=now)
            state.pop("next_attempt_at", None)
            _atomic_json(receipt_path, state)
            return {"status": "unknown"}
        if state and not (state.get("status") == "failed" and state.get("before_submit") is True):
            state.update(status="unknown", error_type="InterruptedBeforeSubmit", location="recovery", updated_at=now)
            _atomic_json(receipt_path, state)
            return {"status": "unknown"}
        if state.get("next_attempt_at", 0) > now:
            return {"status": "failed", "retry_count": state.get("retry_count", 0),
                    "next_attempt_at": state["next_attempt_at"]}
        try:
            config = _valid_config(Path(config_path), group_id, bot_names)
        except Exception as exc:
            return _failed_before_send(receipt_path, state, task_id, report_day, group_id,
                                       now, exc, "configuration")
        end = task.get("end")
        if isinstance(end, bool) or not isinstance(end, int):
            return _failed_before_send(receipt_path, state, task_id, report_day, group_id,
                                       now, ValueError(), "task")
        if end < config["enabled_from"]:
            return {"status": "disabled"}
        body = build_access_message(report_day, _access_url(config), config["bot_name"])
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if state.get("text_sha256") not in (None, digest):
            return {"status": "failed"}
        attempts = int(state.get("attempts", 0))
        retry_count = int(state.get("retry_count", 0))
        try:
            client = open_verified()
        except Exception as exc:
            return _failed_before_send(receipt_path, state, task_id, report_day, group_id,
                                       now, exc, "connection", attempts, retry_count)
        try:
            try:
                sending = {"task_id": task_id, "report_day": report_day, "group_id": group_id,
                           "status": "sending", "attempts": attempts + 1, "retry_count": retry_count,
                           "before_submit": False, "location": "send_text", "text_sha256": digest, "updated_at": now}
                _atomic_json(receipt_path, sending)
            except Exception as exc:
                return _failed_before_send(receipt_path, state, task_id, report_day, group_id,
                                           now, exc, "receipt", attempts, retry_count)
            try:
                message_id = client.send_text(group_id, body)
            except Exception as exc:
                unknown = {**sending, "status": "unknown", "error_type": type(exc).__name__,
                           "location": "send_text"}
                try:
                    _atomic_json(receipt_path, unknown)
                except Exception:
                    pass
                return {"status": "unknown"}
            sent = {**sending, "status": "sent", "message_id": str(message_id),
                    "before_submit": False, "location": "send_text"}
            try:
                _atomic_json(receipt_path, sent)
            except Exception as exc:
                unknown = {**sending, "status": "unknown", "error_type": type(exc).__name__,
                           "location": "receipt_after_submit"}
                try:
                    _atomic_json(receipt_path, unknown)
                except Exception:
                    pass
                return {"status": "unknown"}
            return {"status": "sent", "message_id": sent["message_id"]}
        finally:
            try:
                close_client(client)
            except Exception:
                pass
    finally:
        os.close(lock_fd)


def _failed_before_send(path: Path, previous: dict, task_id: str, day: str, group_id: int,
                        now: int, exc: Exception, location: str, attempts: int = 0,
                        retry_count: int = 0) -> dict:
    retry_count += 1
    delay = min(3600, 30 * (2 ** min(retry_count - 1, 20)))
    state = {"task_id": task_id, "report_day": day, "group_id": group_id, "status": "failed",
             "attempts": attempts, "retry_count": retry_count, "before_submit": True,
             "location": location, "error_type": type(exc).__name__,
             "next_attempt_at": now + delay, "updated_at": now}
    if previous.get("text_sha256"):
        state["text_sha256"] = previous["text_sha256"]
    _atomic_json(path, state)
    return {"status": "failed", "retry_count": retry_count, "next_attempt_at": now + delay}

#!/usr/bin/env python3
"""Independently notify Bark about daily delivery; never generates or sends reports."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit
import urllib.request
from zoneinfo import ZoneInfo

import settings

ROOT = settings.ROOT
RUNTIME = settings.RUNTIME
CONFIG = RUNTIME / "daily-watchdog.json"
STATE = RUNTIME / "daily-watchdog" / "state.json"
DELIVERY = RUNTIME / "digest" / "delivery-state.json"


def _defaults() -> dict:
    project = settings.load_project()
    return {"group": "WeChat-Daily", "fail_level": "active", "recovery_level": "passive",
            "daily_hour": int(project["daily_hour"]), "daily_minute": 0,
            "timezone": project["timezone"], "overdue_minutes": 60}


def read_json(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def load_config(path: Path) -> dict:
    config = {**_defaults(), **read_json(path)}
    for key in ("bark_url", "group", "fail_level", "recovery_level", "timezone"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError("invalid notification configuration")
    url = urlsplit(config["bark_url"])
    if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password:
        raise ValueError("invalid notification URL")
    ZoneInfo(config["timezone"])
    for key, low, high in (("daily_hour", 0, 23), ("daily_minute", 0, 59), ("overdue_minutes", 1, 1440)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError("invalid schedule")
    return config


def init_config(source: Path, destination: Path) -> None:
    original = read_json(source)
    for key in ("bark_url", "fail_level", "recovery_level"):
        if not isinstance(original.get(key), str) or not original[key].strip():
            raise ValueError("source Bark configuration is incomplete")
    config = {**_defaults(), **{key: original[key] for key in ("bark_url", "fail_level", "recovery_level")}}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def observe(delivery: dict, config: dict, now: float) -> tuple[str, str]:
    tz = ZoneInfo(config["timezone"])
    local = datetime.fromtimestamp(now, tz)
    day = local.date().isoformat()
    cutoff = local.replace(hour=config["daily_hour"], minute=config["daily_minute"], second=0, microsecond=0)
    if local < cutoff:
        return day, "waiting"
    if delivery.get("report_day") == day:
        status = delivery.get("status")
        if status in ("failed", "unknown", "unreadable"):
            return day, status
        editions = delivery.get("editions")
        if isinstance(editions, dict) and all(
                isinstance(editions.get(edition), dict) and editions[edition].get("status") == "sent"
                for edition in ("brief", "review")):
            return day, "sent"
    if local >= cutoff + timedelta(minutes=config["overdue_minutes"]):
        return day, "overdue"
    return day, "pending"


def notify(config: dict, title: str, body: str, level: str) -> bool:
    payload = json.dumps({"title": title, "body": body, "group": config["group"], "level": level},
                         ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(config["bark_url"], data=payload, method="POST",
                                    headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.loads(response.read())
            return response.status < 300 and isinstance(result, dict) and result.get("code") == 200
    except Exception:
        return False


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def tick(config_path: Path = CONFIG, state_path: Path = STATE, delivery_path: Path = DELIVERY,
         *, now: float | None = None, notifier=notify) -> dict:
    config = load_config(config_path)
    stamp = time.time() if now is None else now
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_path.parent, 0o700)
    lock_path = state_path.parent / "tick.lock"
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "notified": False}
        try:
            try:
                delivery = read_json(delivery_path)
            except FileNotFoundError:
                delivery = {}
            except (ValueError, OSError):
                tz = ZoneInfo(config["timezone"])
                delivery = {"report_day": datetime.fromtimestamp(stamp, tz).date().isoformat(),
                            "status": "unreadable"}
            day, status = observe(delivery, config, stamp)
            try:
                state = read_json(state_path)
            except FileNotFoundError:
                state = {}
            if state.get("report_day") != day:
                state = {"report_day": day, "failure_notified": False, "recovery_notified": False}
            notified = False
            if status in {"failed", "unknown", "overdue", "unreadable"} and not state.get("failure_notified"):
                reasons = {"failed": "生成、校验或发送失败，尚未确认两版日报均已送达。",
                           "unknown": "发送结果无法确认，不能盲目重发，请检查投递回执。",
                           "overdue": f"已超过计划开始时间 {config['overdue_minutes']} 分钟，尚未确认两版日报均已送达；任务可能仍在运行。",
                           "unreadable": "无法读取日报投递状态，请检查调度及状态文件。"}
                body = f"{day} 日报：{reasons[status]} 此监控不修改回执、不补发报告。"
                notified = notifier(config, "微信日报未确认送达", body, config["fail_level"])
                if notified:
                    state.update(failure_notified=True, failure_sent_at=stamp)
            elif status == "sent" and state.get("failure_notified") and not state.get("recovery_notified"):
                body = f"{day} 简报与回顾均已记录为发送成功。"
                notified = notifier(config, "微信日报投递已恢复", body, config["recovery_level"])
                if notified:
                    state.update(recovery_notified=True, recovery_sent_at=stamp)
            state.update(status=status, checked_at=stamp)
            save_state(state_path, state)
            return {"report_day": day, "status": status, "notified": notified,
                    "failure_notified": bool(state.get("failure_notified")), "checked_at": stamp}
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tick", action="store_true")
    mode.add_argument("--init-config", action="store_true", help="create separate private config from existing Bark settings")
    mode.add_argument("--check-config", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--source-config", type=Path, default=RUNTIME / "wechat-watchdog.json")
    args = parser.parse_args(argv)
    try:
        if args.init_config:
            init_config(args.source_config, args.config)
            result = {"status": "initialized"}
        elif args.check_config:
            config = load_config(args.config)
            result = {"status": "configured", "group": config["group"]}
        else:
            result = tick(config_path=args.config)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except FileExistsError:
        print(json.dumps({"status": "error", "reason": "configuration_already_exists"}))
        return 1
    except Exception:
        print(json.dumps({"status": "error", "reason": "configuration_or_state_invalid"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

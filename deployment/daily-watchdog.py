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


STAGES = {"generate": "生成", "generation-output": "完整性校验", "integrity": "完整性校验",
          "send": "发送", "sending": "发送", "receipt": "回执确认", "unknown": "回执确认",
          "overdue": "超期", "state-read": "状态读取", "delivery-state-read": "状态读取", "unreadable": "状态读取",
          "window_lookup": "窗口定位", "window_validation": "窗口验证", "ui_input": "界面操作",
          "payload_prepare": "内容准备", "draft_verification": "草稿核验", "visual_verification": "视觉核验",
          "receipt_confirmation": "回执确认", "worker_start": "worker启动", "worker_receipt": "worker回执",
          "worker_timeout": "worker超时", "host_preparation": "宿主准备", "artifact_validation": "产物校验",
          "image_delivery": "图片发送", "text_delivery": "文本发送", "source_validation": "来源校验",
          "submit": "提交"}

def _machine_label() -> str:
    value = os.environ.get("WECHAT_DIGEST_MACHINE") or os.uname().nodename.split(".")[0]
    if not value or len(value) > 64 or not all(char.isalnum() or char in "._-" for char in value):
        return "machine-unknown"
    return value


def _diagnostic(delivery: dict, status: str, day: str) -> str:
    editions = delivery.get("editions") if isinstance(delivery.get("editions"), dict) else {}
    edition = delivery.get("edition")
    item = editions.get(edition) if edition in ("brief", "review") else None
    if (not isinstance(item, dict) or (status in ("failed", "unknown")
            and item.get("status") not in ("failed", "unknown"))):
        edition, item = next(((key, value) for key, value in editions.items()
                              if key in ("brief", "review") and isinstance(value, dict)
                              and value.get("status") in ("failed", "unknown")), (None, {}))
    confirmation = item.get("confirmation") if isinstance(item.get("confirmation"), dict) else {}
    location = confirmation.get("failure_location") or delivery.get("failure_location")
    stage = confirmation.get("stage") or delivery.get("stage")
    failure_text = delivery.get("error") or item.get("error")
    if not isinstance(stage, str) and status == "failed" and isinstance(failure_text, str):
        if failure_text.startswith("invalid generation output:"):
            stage = "generation-output"
        elif failure_text.startswith(("invalid prepared editions:", "could not recover confirmed edition bundle:", "prepared report fingerprint changed")):
            stage = "integrity"
        elif not editions:
            stage = "generate"
    if not isinstance(stage, str):
        stage = {"unknown": "receipt", "overdue": "overdue", "unreadable": "state-read"}.get(status, "unknown")
    if not location and stage in ("generate", "generation-output", "integrity"):
        location = {"file": "deployment/daily-job.py", "function": "run_tick"}
    elif not location and stage == "state-read":
        location = {"file": "deployment/daily-watchdog.py", "function": "tick"}
    elif not location and stage == "overdue":
        location = {"file": "deployment/daily-watchdog.py", "function": "observe"}
    if not isinstance(location, dict):
        location = {}
    file, function, line = location.get("file"), location.get("function"), location.get("line")
    file = (file if isinstance(file, str) and file and not file.startswith("/") and "://" not in file
            and all(part not in ("", ".", "..") for part in file.split("/"))
            and len(file) <= 180 and all(char.isalnum() or char in "._/-" for char in file) else None)
    function = (function if isinstance(function, str) and len(function) <= 80 and function
                and all(char.isalnum() or char in "._<>" for char in function) else None)
    line = line if type(line) is int and line > 0 else None
    edition = edition if edition in ("brief", "review") else "版别未知"
    where = file or "来源未知"
    if function:
        where += ":" + function
    if line and file:
        where += ":" + str(line) if function else ":line=" + str(line)
    stage_label = STAGES.get(stage)
    if stage_label is None:
        stage_label = stage if (isinstance(stage, str) and 0 < len(stage) <= 48
                                and stage.isascii() and stage.replace("_", "").isalnum()) else "阶段未知"
    error_type = confirmation.get("error_type") or delivery.get("error_type") or item.get("error_type")
    if not isinstance(error_type, str) or len(error_type) > 80 or not error_type.replace("_", "").isalnum():
        error_type = "未知"
    evidence = "runtime/daily-watchdog/state.json" if stage == "state-read" or status == "overdue" else "runtime/digest/delivery-state.json"
    edition_label = "brief+review" if status == "overdue" else edition
    return (f"机器{_machine_label()}/日报投递监控服务；{day} 日报/{edition_label}：阶段{stage_label}，"
            f"位置 {where}，原因类型 {error_type}；证据 {evidence}。")[:400]


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
                delivery = {"report_day": datetime.fromtimestamp(stamp, tz).date().isoformat(), "status": "unreadable", "stage": "delivery-state-read"}
            day, status = observe(delivery, config, stamp)
            try:
                state = read_json(state_path)
            except FileNotFoundError:
                state = {}
            except (ValueError, OSError):
                tz = ZoneInfo(config["timezone"])
                day = datetime.fromtimestamp(stamp, tz).date().isoformat()
                status = "unreadable"
                delivery = {"report_day": day, "status": "unreadable", "stage": "state-read"}
                state = {}
            if state.get("report_day") != day:
                state = {"report_day": day, "failure_notified": False, "recovery_notified": False}
            notified = False
            if status in {"failed", "unknown", "overdue", "unreadable"}:
                context = {"text": _diagnostic(delivery, status, day), "status": status, "report_day": day}
                if state.get("failure_notified"):
                    if not isinstance(state.get("failure_context"), dict):
                        state["failure_context"] = context
                else:
                    body = context["text"] + " 此监控不修改回执、不补发报告。"
                    notified = notifier(config, "微信日报未确认送达", body, config["fail_level"])
                    if notified:
                        state.update(failure_notified=True, failure_sent_at=stamp, failure_context=context)
            elif status == "sent" and state.get("failure_notified") and not state.get("recovery_notified"):
                context = state.get("failure_context")
                if not isinstance(context, dict):
                    context = {"text": _diagnostic({}, "unknown", day), "status": "unknown", "report_day": day}
                    state["failure_context"] = context
                body = f"{day} 日报已恢复，两版发送状态已确认；原故障：{context['text']}"
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

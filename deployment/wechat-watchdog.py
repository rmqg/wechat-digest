#!/usr/bin/env python3
"""Read-only WeChat GUI watchdog; intended to be called once per minute with --tick."""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
import settings

ROOT = settings.ROOT
RUNTIME = settings.RUNTIME
CONFIG_PATH = RUNTIME / "wechat-watchdog.json"
STATE_DIR = RUNTIME / "wechat-watchdog"
STATE_PATH = STATE_DIR / "state.json"
LOCK_PATH = STATE_DIR / "tick.lock"
CALIBRATION_PATH = RUNTIME / "ui-calibration.json"
SENDER_PATH = ROOT / "deployment" / "send-report.py"


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError
    return value


def _load_config(path: Path = CONFIG_PATH) -> dict:
    settings.load_project(required=True)
    value = _read_json(path)
    required = ("bark_url", "group", "fail_level", "recovery_level")
    if any(not isinstance(value.get(k), str) or not value[k] for k in required):
        raise ValueError("invalid watchdog configuration")
    return value


def classify_windows(windows: list[dict], main_size: tuple[int, int], login_size: tuple[int, int]) -> str:
    """Classify visible Weixin client windows; coordinates are intentionally ignored."""
    weixin = [w for w in windows if w.get("viewable") is True and w.get("title") == "Weixin"]
    if any((w.get("width"), w.get("height")) == login_size for w in weixin):
        return "offline"
    if any((w.get("width"), w.get("height")) == main_size for w in weixin):
        return "online"
    return "unavailable"


def _probe(main_size: tuple[int, int], login_size: tuple[int, int]) -> str:
    module = sys.modules.get("_wechat_watchdog_sender")
    if module is None:
        spec = importlib.util.spec_from_file_location("_wechat_watchdog_sender", SENDER_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    x11 = module.X11(":99")
    try:
        windows = []
        for ident in x11._client_windows():
            attrs = module.XWindowAttributes()
            if x11.x.XGetWindowAttributes(x11.display, ident, module.ctypes.byref(attrs)):
                windows.append({"title": x11._window_title(ident),
                                "viewable": attrs.map_state == x11.IS_VIEWABLE,
                                "width": attrs.width, "height": attrs.height})
        return classify_windows(windows, main_size, login_size)
    finally:
        x11.x.XCloseDisplay(x11.display)


def observe() -> str:
    calibration = _read_json(CALIBRATION_PATH)
    project = settings.load_project(required=True)
    container = settings.verify_desktop_container(project)
    main = calibration["window"]
    login = calibration["login_window"]
    args = ["docker", "exec", "--user", f"{project['worker_uid']}:{project['worker_gid']}", container,
            "python3", "/deployment/wechat-watchdog.py", "--probe",
            "--main-width", str(int(main["width"])), "--main-height", str(int(main["height"])),
            "--login-width", str(int(login["width"])), "--login-height", str(int(login["height"]))]
    result = subprocess.run(args, capture_output=True, text=True, timeout=15, check=True)
    value = json.loads(result.stdout)
    status = value.get("status")
    if status not in ("online", "offline", "unavailable"):
        raise RuntimeError
    return status


def _notify(config: dict, title: str, body: str, level: str) -> bool:
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

def _write_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def transition(state: dict, observed: str, now: float) -> dict:
    previous = state.get("status")
    if observed == "online":
        if previous != "online":
            was_notified = bool(state.get("failure_notified"))
            state.update(status="online", changed_at=now,
                         pending="recovery" if was_notified else None,
                         failure_notified=False)
    else:
        if previous != observed:
            state.update(status=observed, changed_at=now)
        if state.get("failure_notified"):
            state["pending"] = None
        elif state.get("pending") != "failure":
            state["pending"] = "failure"
    return state


def _send_pending(state: dict, config: dict, notifier=_notify) -> bool:
    kind = state.get("pending")
    if kind == "failure":
        if state["status"] == "unavailable":
            title, body = "微信状态无法确认", "目前无法确认微信是否在线，请检查桌面窗口。"
        else:
            title, body = "微信可能已掉线", "检测到微信登录窗口，请检查微信登录状态。"
        level = config["fail_level"]
    elif kind == "recovery":
        title, body, level = "微信聊天界面已恢复", "已检测到微信主聊天界面；这不代表网络健康状态。", config["recovery_level"]
    else:
        return False
    if notifier(config, title, body, level):
        state["pending"] = None
        state["failure_notified"] = kind == "failure"
        return True
    return False


def tick(config_path: Path = CONFIG_PATH, state_path: Path = STATE_PATH, observer=observe,
         notifier=_notify, now: float | None = None) -> dict:
    config = _load_config(config_path)
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_path.parent, 0o700)
    lock_path = state_path.parent / "tick.lock"
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "state": None, "notified": False, "pending": None}
        try:
            stamp = time.time() if now is None else now
            try:
                status = observer()
                if status not in ("online", "offline", "unavailable"):
                    status = "unavailable"
            except Exception:
                status = "unavailable"
            try:
                state = _read_json(state_path)
            except FileNotFoundError:
                state = {}
            state["checked_at"] = stamp
            transition(state, status, stamp)
            notified = _send_pending(state, config, notifier)
            _write_state(state, state_path)
            return {"status": status, "state": status, "notified": notified,
                    "pending": state.get("pending"), "checked_at": stamp}
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def test_notify(config_path: Path = CONFIG_PATH) -> bool:
    config = _load_config(config_path)
    return _notify(config, "微信掉线提醒测试", "这是一条测试通知，监测状态未被修改。", config["fail_level"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tick", action="store_true", help="perform one monitoring tick")
    mode.add_argument("--test-notify", action="store_true", help="send one test notification without changing state")
    mode.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--main-width", type=int)
    parser.add_argument("--main-height", type=int)
    parser.add_argument("--login-width", type=int)
    parser.add_argument("--login-height", type=int)
    args = parser.parse_args(argv)
    if args.probe:
        try:
            if not all((args.main_width, args.main_height, args.login_width, args.login_height)):
                raise ValueError
            print(json.dumps({"status": _probe((args.main_width, args.main_height),
                                                (args.login_width, args.login_height))}))
            return 0
        except Exception:
            print(json.dumps({"status": "unavailable"}))
            return 1
    try:
        if args.test_notify:
            sent = test_notify()
            print(json.dumps({"status": "test", "state": None, "notified": sent, "pending": None}))
            return 0 if sent else 1
        result = tick()
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except FileNotFoundError as exc:
        if Path(exc.filename or "") == CONFIG_PATH:
            print(json.dumps({"status": "error", "error": "configuration_missing"}))
        else:
            print(json.dumps({"status": "error", "error": "watchdog_unavailable"}))
        return 1
    except Exception:
        print(json.dumps({"status": "error", "error": "configuration_or_watchdog_invalid"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

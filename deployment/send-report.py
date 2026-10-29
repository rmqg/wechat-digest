#!/usr/bin/env python3
"""Send a generated report only to an explicitly configured WeChat group.

Host mode launches the pinned runtime image in the desktop GUI network namespace.
Worker mode uses ctypes X11 clipboard support and confirms delivery in a private
read-only database snapshot. No Docker socket is mounted in the worker.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

import settings
ROOT = settings.ROOT
RUNTIME = settings.RUNTIME
PROJECT = settings.load_project()
CHAT_NAME = PROJECT.get("group", {}).get("name")
CHAT_ID = PROJECT.get("group", {}).get("id")
REPORT_PREFIX = PROJECT["report_prefix"]
CALIBRATION_PATH = Path("/calibration/config.json")
REPORTS_ROOT = Path("/reports")


def _delivery_config() -> dict:
    project = settings.load_project(required=True)
    group = project.get("group", {})
    name, chat_id = group.get("name"), group.get("id")
    if not isinstance(name, str) or not name.strip() or not isinstance(chat_id, str) or not chat_id.strip():
        raise ValueError("explicit group.id and group.name are required for delivery")
    global CHAT_NAME, CHAT_ID, REPORT_PREFIX
    CHAT_NAME, CHAT_ID = name, chat_id
    REPORT_PREFIX = project["report_prefix"]
    return project


def _verify_target(expected_chat_id: str | None, project: dict, do_send: bool) -> tuple[str | None, str | None]:
    group = project.get("group", {})
    chat_name, chat_id = group.get("name"), group.get("id")
    if do_send and (not chat_name or not chat_id or not expected_chat_id):
        raise ValueError("explicit group.id, group.name, and target chat ID are required for sending")
    if expected_chat_id is not None and expected_chat_id != chat_id:
        raise ValueError("target does not match the explicitly configured group.id")
    return chat_name, chat_id


def _worker_identity(project: dict) -> tuple[int, int]:
    return int(project["worker_uid"]), int(project["worker_gid"])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_png(path: Path, reports_root: Path) -> tuple[Path, str]:
    path = path.resolve(strict=True)
    reports_root = reports_root.resolve(strict=True)
    if (not path.is_file() or path.suffix.lower() != ".png" or not path.is_relative_to(reports_root)
            or not path.name.startswith(REPORT_PREFIX + "-")):
        raise ValueError("input must be a report PNG inside the private reports directory")
    if not path.with_suffix(".json").is_file() or not path.with_suffix(".html").is_file():
        raise ValueError("PNG is not accompanied by the generated JSON and HTML report")
    with path.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("input is not a PNG file")
    return path, sha256_file(path)


TEXT_ROOT = RUNTIME / "digest" / "site-access-messages"


def checked_text(path: Path, text_root: Path) -> tuple[Path, str, str]:
    path = path.resolve(strict=True)
    text_root = text_root.resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(text_root):
        raise ValueError("text input must be inside the private site-access message directory")
    raw = path.read_bytes()
    text = raw.decode("utf-8", "strict")
    if not text or "\x00" in text:
        raise ValueError("text input must be nonempty UTF-8 text without NUL")
    return path, text, hashlib.sha256(raw).hexdigest()


def host_send_text(text_file: Path, do_send: bool, expected_chat_id: str, expected_sha256: str = '') -> dict:
    project = _delivery_config() if do_send else settings.load_project()
    chat_name, chat_id = _verify_target(expected_chat_id, project, do_send)
    path, _text, text_hash = checked_text(text_file, TEXT_ROOT)
    if expected_sha256 and expected_sha256 != text_hash:
        raise ValueError('notice content changed after scheduling; send refused')
    if not do_send:
        return {"status": "prepared", "sent": False, "chat_name": chat_name,
                "chat_id": chat_id, "sha256": text_hash,
                "note": "No GUI action or message send; pass --send explicitly."}
    image = settings.runtime_image("runtime", ROOT)
    uid, gid = _worker_identity(project)
    gui_data = RUNTIME / "gui"
    keys = RUNTIME / "keyscan" / "all_keys.json"
    selection = RUNTIME / "selection.json"
    calibration = RUNTIME / "ui-calibration.json"
    assets = RUNTIME / "ui-calibration"
    deps = RUNTIME / "python-deps"
    source_memory = ROOT / "memory"
    if (not gui_data.is_dir() or not keys.is_file() or not selection.is_file() or
            not calibration.is_file() or not assets.is_dir() or not deps.is_dir() or not source_memory.is_dir()):
        raise RuntimeError("required private GUI, selection, key, calibration, Python deps, or source memory mount is missing")
    evidence = RUNTIME / "digest" / "send-evidence"
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(evidence, uid, gid)
    os.chmod(evidence, 0o700)
    directory = path.parent
    while directory.is_relative_to(TEXT_ROOT.resolve(strict=True)):
        os.chown(directory, uid, gid)
        os.chmod(directory, 0o700)
        if directory == TEXT_ROOT.resolve(strict=True):
            break
        directory = directory.parent
    os.chown(path, uid, gid)
    os.chmod(path, 0o600)
    command = ["docker", "run", "--rm", "--pull", "never", "--network", f"container:{project['desktop_container']}",
               "--read-only", "--user", f"{uid}:{gid}", "--cap-drop", "ALL", "--security-opt",
               "no-new-privileges:true", "--memory", "768m", "--cpus", "1",
               "--tmpfs", f"/tmp:rw,nosuid,nodev,size=384m,mode=0700,uid={uid},gid={gid}",
               "--env", "PYTHONPATH=/source:/source/memory:/source/agent_console:/tool:/tool/vendor:/deps", "--env", "DISPLAY=:99",
               "--env", "TZ=" + project["timezone"], "--env", "WECHAT_DIGEST_CONFIG=/runtime/project.json"]
    mounts = [(RUNTIME / "digest" / "site-access-messages", "/messages", True),
              (settings.CONFIG_PATH.resolve(), "/runtime/project.json", True),
              (keys, "/keys/all_keys.json", True), (selection, "/runtime/selection.json", True),
              (gui_data, "/config", True), (source_memory, "/source/memory", True),
              (ROOT / "agent_console", "/source/agent_console", True),
              (ROOT / "assets", "/source/assets", True),
              (deps, "/deps", True), (ROOT / "deployment", "/tool", True),
              (calibration, "/calibration/config.json", True), (assets, "/calibration/assets", True),
              (evidence, "/evidence", False)]
    for source, target, readonly in mounts:
        command += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]
    command += [image, "/tool/send-report.py", "--worker", "--send", "--text-file",
                f"/messages/{path.name}", "--chat-id", expected_chat_id, "--expected-sha256", text_hash]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "sent": None, "before_submit": False, "chat_name": chat_name,
                "chat_id": chat_id, "sha256": text_hash,
                "error": "delivery runtime timed out; do not resend"}
    try:
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"status": "unknown" if result.returncode else "failed", "sent": None,
                "before_submit": False if result.returncode else True,
                "chat_name": chat_name, "chat_id": chat_id, "sha256": text_hash,
                "error": "worker returned no receipt"}
    ui_evidence = outcome.get("ui_evidence")
    if isinstance(ui_evidence, str) and ui_evidence.startswith("/evidence/"):
        outcome["ui_evidence"] = str(evidence / Path(ui_evidence).name)
    if result.returncode and outcome.get("status") not in {"failed", "unknown"}:
        outcome.update(status="unknown", sent=None, reason="worker exited after reporting; do not resend")
    return outcome


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def host_send(png: Path, do_send: bool, expected_chat_id: str) -> dict:
    project = _delivery_config() if do_send else settings.load_project()
    chat_name, chat_id = _verify_target(expected_chat_id, project, do_send)
    reports = RUNTIME / "reports"
    png, png_hash = checked_png(png, reports)
    if not do_send:
        return {"status": "prepared", "sent": False, "chat_name": chat_name,
                "chat_id": chat_id, "png": str(png), "sha256": png_hash,
                "note": "No GUI action or message send; pass --send explicitly."}
    image = settings.runtime_image("runtime", ROOT)
    uid, gid = _worker_identity(project)
    gui_data = RUNTIME / "gui"
    keys = RUNTIME / "keyscan" / "all_keys.json"
    selection = RUNTIME / "selection.json"
    calibration = RUNTIME / "ui-calibration.json"
    assets = RUNTIME / "ui-calibration"
    deps = RUNTIME / "python-deps"
    source_memory = ROOT / "memory"
    if (not gui_data.is_dir() or not keys.is_file() or not selection.is_file() or
            not calibration.is_file() or not assets.is_dir() or not deps.is_dir() or not source_memory.is_dir()):
        raise RuntimeError("required private GUI, selection, key, calibration, Python deps, or source memory mount is missing")
    evidence = RUNTIME / "digest" / "send-evidence"
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(evidence, uid, gid)
    os.chmod(evidence, 0o700)
    command = [
        "docker", "run", "--rm", "--pull", "never",
        "--network", f"container:{project['desktop_container']}",
        "--read-only", "--user", f"{uid}:{gid}", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--memory", "768m", "--cpus", "1",
        "--tmpfs", f"/tmp:rw,nosuid,nodev,size=384m,mode=0700,uid={uid},gid={gid}",
        "--env", "PYTHONPATH=/source:/source/memory:/source/agent_console:/tool:/tool/vendor:/deps",
        "--env", "DISPLAY=:99", "--env", "TZ=" + project["timezone"],
        "--env", "WECHAT_DIGEST_CONFIG=/runtime/project.json",
    ]
    mounts = [
        (reports, "/reports", True), (keys, "/keys/all_keys.json", True),
        (selection, "/runtime/selection.json", True), (settings.CONFIG_PATH.resolve(), "/runtime/project.json", True),
        (gui_data, "/config", True), (source_memory, "/source/memory", True),
        (ROOT / "agent_console", "/source/agent_console", True),
        (ROOT / "assets", "/source/assets", True), (deps, "/deps", True),
        (ROOT / "deployment", "/tool", True), (calibration, "/calibration/config.json", True),
        (assets, "/calibration/assets", True), (evidence, "/evidence", False),
    ]
    for source, target, readonly in mounts:
        command += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]
    command += [image, "/tool/send-report.py", "--worker", "--send", f"/reports/{png.name}",
                "--chat-id", expected_chat_id, "--expected-sha256", png_hash]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "sent": None, "chat_name": chat_name, "chat_id": chat_id,
                "png": str(png), "sha256": png_hash, "error": "delivery runtime timed out; do not resend"}
    try:
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"status": "unknown" if result.returncode else "failed", "sent": None,
                "chat_name": chat_name, "chat_id": chat_id, "png": str(png), "sha256": png_hash,
                "error": (result.stderr or result.stdout or "worker returned no receipt")[-4000:]}
    outcome["host_png"] = str(png)
    outcome["host_png_sha256"] = png_hash
    ui_evidence = outcome.get("ui_evidence")
    if isinstance(ui_evidence, str) and ui_evidence.startswith("/evidence/"):
        outcome["ui_evidence"] = str(evidence / Path(ui_evidence).name)
    if result.returncode and outcome.get("status") not in {"failed", "unknown"}:
        outcome.update(status="unknown", sent=None,
                       reason="worker exited after reporting without a successful process exit; do not resend")
    return outcome


# Xlib ABI structures used by the ctypes-only worker.
class XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int),
        ("height", ctypes.c_int), ("border_width", ctypes.c_int), ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p), ("root", ctypes.c_ulong), ("class", ctypes.c_int),
        ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int), ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong), ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int), ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p),
    ]



class XImageData(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int),
                ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
                ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
                ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int),
                ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int),
                ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
                ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong),
                ("blue_mask", ctypes.c_ulong)]


class XSelectionRequestEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("owner", ctypes.c_ulong),
                ("requestor", ctypes.c_ulong), ("selection", ctypes.c_ulong),
                ("target", ctypes.c_ulong), ("property", ctypes.c_ulong), ("time", ctypes.c_ulong)]


class XSelectionEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("requestor", ctypes.c_ulong),
                ("selection", ctypes.c_ulong), ("target", ctypes.c_ulong),
                ("property", ctypes.c_ulong), ("time", ctypes.c_ulong)]


class XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("xselectionrequest", XSelectionRequestEvent),
                ("xselection", XSelectionEvent), ("pad", ctypes.c_long * 24)]


class X11:
    ZPIXMAP = 2
    IS_VIEWABLE = 2
    CURRENT_TIME = 0
    PROP_REPLACE = 0
    SELECTION_REQUEST = 30
    SELECTION_NOTIFY = 31
    XA_ATOM = 4

    def __init__(self, display_name: str):
        x11_path = ctypes.util.find_library("X11")
        xtst_path = ctypes.util.find_library("Xtst")
        if not x11_path or not xtst_path:
            raise RuntimeError("pinned runtime must provide libX11 and libXtst")
        self.x = ctypes.CDLL(x11_path)
        self.xtst = ctypes.CDLL(xtst_path)
        self._bind()
        self.x.XInitThreads()
        self.display = self.x.XOpenDisplay(display_name.encode())
        if not self.display:
            raise RuntimeError(f"cannot connect to X11 DISPLAY={display_name}")
        self.root = self.x.XDefaultRootWindow(self.display)
        self._selection_thread: threading.Thread | None = None
        self._selection_stop = threading.Event()
        self._selection_error: list[str] = []
        self._image_requested = threading.Event()
        self._png_buffer = None
        self._owner_window = 0

    def _bind(self) -> None:
        x = self.x
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]; x.XOpenDisplay.restype = ctypes.c_void_p
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]; x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                                 ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                                 ctypes.POINTER(ctypes.c_uint)]; x.XQueryTree.restype = ctypes.c_int
        x.XFetchName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]; x.XFetchName.restype = ctypes.c_int
        x.XFree.argtypes = [ctypes.c_void_p]; x.XFree.restype = ctypes.c_int
        x.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XWindowAttributes)]; x.XGetWindowAttributes.restype = ctypes.c_int
        x.XTranslateCoordinates.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int,
                                            ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                            ctypes.POINTER(ctypes.c_ulong)]; x.XTranslateCoordinates.restype = ctypes.c_int
        x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]; x.XInternAtom.restype = ctypes.c_ulong
        x.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
                                         ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                                         ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                                         ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                                         ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
        x.XGetWindowProperty.restype = ctypes.c_int
        x.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]; x.XRaiseWindow.restype = ctypes.c_int
        x.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        x.XCloseDisplay.argtypes = [ctypes.c_void_p]; x.XCloseDisplay.restype = ctypes.c_int
        x.XExtendedMaxRequestSize.argtypes = [ctypes.c_void_p]; x.XExtendedMaxRequestSize.restype = ctypes.c_long
        x.XMaxRequestSize.argtypes = [ctypes.c_void_p]; x.XMaxRequestSize.restype = ctypes.c_long
        x.XPending.argtypes = [ctypes.c_void_p]; x.XPending.restype = ctypes.c_int
        x.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong]
        x.XCreateSimpleWindow.restype = ctypes.c_ulong
        x.XSetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        x.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]; x.XGetSelectionOwner.restype = ctypes.c_ulong
        x.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                      ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
        x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.POINTER(XEvent)]
        x.XDeleteProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        x.XConvertSelection.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        x.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(XEvent)]
        x.XGetInputFocus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int)]
        x.XFlush.argtypes = [ctypes.c_void_p]; x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]; x.XGetImage.restype = ctypes.c_void_p
        x.XDestroyImage.argtypes = [ctypes.c_void_p]
        x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]; x.XKeysymToKeycode.restype = ctypes.c_uint
        x.XStringToKeysym.argtypes = [ctypes.c_char_p]; x.XStringToKeysym.restype = ctypes.c_ulong
        self.xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]

    def atom(self, name: str) -> int:
        return int(self.x.XInternAtom(self.display, name.encode(), 0))

    def _window_title(self, window: int) -> str:
        atom = self.atom("_NET_WM_NAME")
        actual_type, actual_format = ctypes.c_ulong(), ctypes.c_int()
        item_count, bytes_after = ctypes.c_ulong(), ctypes.c_ulong()
        property_data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.x.XGetWindowProperty(self.display, window, atom, 0, 1024, 0, 0,
                                           ctypes.byref(actual_type), ctypes.byref(actual_format),
                                           ctypes.byref(item_count), ctypes.byref(bytes_after),
                                           ctypes.byref(property_data))
        if status == 0 and property_data and actual_format.value == 8 and item_count.value:
            try:
                return ctypes.string_at(property_data, item_count.value).decode("utf-8", "replace")
            finally:
                self.x.XFree(property_data)
        if property_data:
            self.x.XFree(property_data)
        result = ctypes.c_void_p()
        if not self.x.XFetchName(self.display, window, ctypes.byref(result)) or not result.value:
            return ""
        try:
            return ctypes.string_at(result.value).decode("utf-8", "replace")
        finally:
            self.x.XFree(result)

    def _children(self, parent: int) -> list[int]:
        root_ret, parent_ret = ctypes.c_ulong(), ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        if not self.x.XQueryTree(self.display, parent, ctypes.byref(root_ret), ctypes.byref(parent_ret),
                                 ctypes.byref(children), ctypes.byref(count)):
            raise RuntimeError(f"XQueryTree failed for window {parent:#x}")
        try:
            return [int(children[i]) for i in range(count.value)]
        finally:
            if children:
                self.x.XFree(children)

    def _client_windows(self) -> list[int]:
        # The window manager lists application windows, not short-lived clipboard owners.
        actual_type, actual_format = ctypes.c_ulong(), ctypes.c_int()
        count, remaining = ctypes.c_ulong(), ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.x.XGetWindowProperty(
            self.display, self.root, self.atom('_NET_CLIENT_LIST'), 0, 4096, 0, 33,
            ctypes.byref(actual_type), ctypes.byref(actual_format), ctypes.byref(count),
            ctypes.byref(remaining), ctypes.byref(data))
        try:
            if status != 0 or actual_format.value != 32 or remaining.value or not data:
                raise RuntimeError('window manager did not provide its application window list')
            return list(ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[:count.value])
        finally:
            if data:
                self.x.XFree(data)

    def find_window(self, config: dict) -> dict:
        expected = config["window"]
        pattern = str(config.get("window_title_substring", "Weixin"))
        matches = []
        visible_titles = []
        for candidate in self._client_windows():
            attrs = XWindowAttributes()
            if not self.x.XGetWindowAttributes(self.display, candidate, ctypes.byref(attrs)) or attrs.map_state != self.IS_VIEWABLE:
                continue
            title = self._window_title(candidate)
            if title:
                visible_titles.append((candidate, title))
            if pattern.casefold() not in title.casefold():
                continue
            rx, ry, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
            self.x.XTranslateCoordinates(self.display, candidate, self.root, 0, 0,
                                         ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(child))
            info = {'window_id': candidate, 'title': title, 'x': rx.value, 'y': ry.value,
                    'width': attrs.width, 'height': attrs.height, 'visual': attrs.visual}
            if all(info[k] == int(expected[k]) for k in ('x', 'y', 'width', 'height')):
                matches.append(info)
        if len(matches) != 1:
            raise RuntimeError(f"expected one calibrated Weixin client window; found {len(matches)}")
        main = matches[0]
        allowed = set(config.get("allowed_visible_window_titles", []))
        unexpected = [(wid, title) for wid, title in visible_titles
                      if title != main["title"] and title not in allowed]
        if unexpected:
            raise RuntimeError(f"unexpected visible window/dialog: {unexpected!r}")
        return main
    def activate(self, win: dict) -> None:
        self.x.XRaiseWindow(self.display, win["window_id"])
        self.x.XSetInputFocus(self.display, win["window_id"], 2, self.CURRENT_TIME)
        self.x.XSync(self.display, 0)
        time.sleep(0.15)
    def focus_within(self, window: dict) -> bool:
        focused, revert = ctypes.c_ulong(), ctypes.c_int()
        self.x.XGetInputFocus(self.display, ctypes.byref(focused), ctypes.byref(revert))
        allowed = {int(window["window_id"])}
        stack = [int(window["window_id"])]
        while stack:
            parent = stack.pop()
            children = self._children(parent)
            allowed.update(children)
            stack.extend(children)
        return int(focused.value) in allowed

    @staticmethod
    def _mask_component(pixel: int, mask: int) -> int:
        if not mask:
            return 0
        shift = (mask & -mask).bit_length() - 1
        maximum = mask >> shift
        return ((pixel & mask) >> shift) * 255 // maximum

    def screenshot(self, win: dict):
        from PIL import Image
        attrs = XWindowAttributes()
        if not self.x.XGetWindowAttributes(self.display, win["window_id"], ctypes.byref(attrs)):
            raise RuntimeError("cannot read calibrated WeChat window attributes")
        rx, ry, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
        self.x.XTranslateCoordinates(self.display, win["window_id"], self.root, 0, 0,
                                     ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(child))
        pointer = self.x.XGetImage(self.display, self.root, rx.value, ry.value, attrs.width, attrs.height,
                                   ctypes.c_ulong(-1).value, self.ZPIXMAP)
        if not pointer:
            raise RuntimeError("XGetImage failed")
        data = ctypes.cast(pointer, ctypes.POINTER(XImageData)).contents
        try:
            stride = int(data.bytes_per_line)
            bits = int(data.bits_per_pixel)
            if stride <= 0 or bits not in (16, 24, 32):
                raise RuntimeError(f"unsupported XImage layout: stride={stride}, bits_per_pixel={bits}")
            raw = ctypes.string_at(data.data, stride * attrs.height)
            masks = (int(data.red_mask), int(data.green_mask), int(data.blue_mask))
            order = int(data.byte_order)
            mode = None
            if bits == 32 and masks == (0x00FF0000, 0x0000FF00, 0x000000FF):
                mode = "BGRX" if order == 0 else "XRGB"
            elif bits == 32 and masks == (0x000000FF, 0x0000FF00, 0x00FF0000):
                mode = "RGBX" if order == 0 else "XBGR"
            elif bits == 24 and masks == (0x00FF0000, 0x0000FF00, 0x000000FF):
                mode = "BGR" if order == 0 else "RGB"
            elif bits == 24 and masks == (0x000000FF, 0x0000FF00, 0x00FF0000):
                mode = "RGB" if order == 0 else "BGR"
            if mode:
                return Image.frombuffer("RGB", (attrs.width, attrs.height), raw,
                                        "raw", mode, stride, 1).copy()
            bytes_per_pixel = (bits + 7) // 8
            pixels = bytearray(attrs.width * attrs.height * 3)
            offset = 0
            endian = "little" if order == 0 else "big"
            for y in range(attrs.height):
                row_offset = y * stride
                for x in range(attrs.width):
                    start = row_offset + x * bytes_per_pixel
                    value = int.from_bytes(raw[start:start + bytes_per_pixel], endian)
                    pixels[offset] = self._mask_component(value, masks[0])
                    pixels[offset + 1] = self._mask_component(value, masks[1])
                    pixels[offset + 2] = self._mask_component(value, masks[2])
                    offset += 3
            return Image.frombytes("RGB", (attrs.width, attrs.height), bytes(pixels))
        finally:
            self.x.XDestroyImage(pointer)

    def key(self, name: str, down: bool) -> None:
        sym = self.x.XStringToKeysym(name.encode("ascii"))
        if not sym:
            raise RuntimeError(f"unknown X11 keysym {name}")
        code = self.x.XKeysymToKeycode(self.display, sym)
        if not code or not self.xtst.XTestFakeKeyEvent(self.display, code, int(down), self.CURRENT_TIME):
            raise RuntimeError(f"XTest could not inject key {name}")

    def press(self, name: str) -> None:
        self.key(name, True); self.x.XFlush(self.display); time.sleep(0.035); self.key(name, False); self.x.XFlush(self.display)

    def chord(self, modifier: str, key: str) -> None:
        self.key(modifier, True); self.key(key, True); self.x.XFlush(self.display)
        time.sleep(0.04); self.key(key, False); self.key(modifier, False); self.x.XFlush(self.display)

    def type_ascii(self, text: str) -> None:
        for char in text:
            if not 32 <= ord(char) <= 126:
                raise ValueError("only printable ASCII text is permitted in the native search field")
            # Printable Latin-1 keysyms equal their character code. XStringToKeysym
            # expects names such as 'minus', not the literal '-' character.
            code = self.x.XKeysymToKeycode(self.display, ord(char))
            if not code:
                raise RuntimeError(f"unsupported ASCII key {char!r}")
            shifted = char.isupper() or char in "~!@#$%^&*()_+{}|:\"<>?"
            if shifted:
                self.key("Shift_L", True)
            try:
                self.xtst.XTestFakeKeyEvent(self.display, code, 1, self.CURRENT_TIME)
                self.xtst.XTestFakeKeyEvent(self.display, code, 0, self.CURRENT_TIME)
            finally:
                if shifted:
                    self.key("Shift_L", False)
            self.x.XFlush(self.display)
            time.sleep(0.018)

    def click(self, window: dict, point: list[int]) -> None:
        if len(point) != 2:
            raise ValueError("calibration coordinates must be [x,y]")
        x, y = map(int, point)
        if not (0 <= x < window["width"] and 0 <= y < window["height"]):
            raise ValueError("calibration click is outside WeChat window")
        if not self.xtst.XTestFakeMotionEvent(self.display, -1, window["x"] + x, window["y"] + y, self.CURRENT_TIME):
            raise RuntimeError("XTest mouse motion failed")
        if not self.xtst.XTestFakeButtonEvent(self.display, 1, 1, self.CURRENT_TIME):
            raise RuntimeError("XTest mouse press failed")
        self.x.XFlush(self.display)
        time.sleep(0.04)
        if not self.xtst.XTestFakeButtonEvent(self.display, 1, 0, self.CURRENT_TIME):
            raise RuntimeError("XTest mouse release failed")
        self.x.XFlush(self.display)

    def close(self) -> None:
        if self.display:
            self.x.XCloseDisplay(self.display)
            self.display = None

    def composer_empty(self, window: dict, point: list[int]) -> bool:
        self.activate(window)
        # Copy is non-destructive. An empty Qt editor leaves clipboard ownership
        # unchanged; a real text/image draft transfers ownership to Qt.
        probe = self.x.XCreateSimpleWindow(self.display, self.root, -10, -10, 1, 1, 0, 0, 0)
        if not probe:
            raise RuntimeError("cannot create private draft probe")
        clipboard = self.atom("CLIPBOARD")
        self.x.XSetSelectionOwner(self.display, clipboard, probe, self.CURRENT_TIME)
        self.x.XSync(self.display, 0)
        self.click(window, point)
        self.chord("Control_L", "a")
        self.chord("Control_L", "c")
        time.sleep(0.2)
        self.x.XSync(self.display, 0)
        return int(self.x.XGetSelectionOwner(self.display, clipboard)) == int(probe)

    def start_png_clipboard(self, png: bytes) -> None:
        extended_words = int(self.x.XExtendedMaxRequestSize(self.display))
        if extended_words <= 0:
            extended_words = int(self.x.XMaxRequestSize(self.display))
        if len(png) + 128 > extended_words * 4:
            raise RuntimeError(f"PNG ({len(png)} bytes) exceeds direct XChangeProperty limit; refusing INCR/file fallback")
        root = self.root
        owner = self.x.XCreateSimpleWindow(self.display, root, -10, -10, 1, 1, 0, 0, 0)
        if not owner:
            raise RuntimeError("cannot create X11 clipboard owner")
        self._owner_window = int(owner)
        clip = self.atom("CLIPBOARD")
        image_atom = self.atom("image/png")
        targets = self.atom("TARGETS")
        self._png_buffer = (ctypes.c_ubyte * len(png)).from_buffer_copy(png)
        self._selection_stop.clear(); self._image_requested.clear(); self._selection_error.clear()
        self.x.XSetSelectionOwner(self.display, clip, owner, self.CURRENT_TIME)
        self.x.XSync(self.display, 0)
        if int(self.x.XGetSelectionOwner(self.display, clip)) != owner:
            raise RuntimeError("could not own X11 CLIPBOARD selection")

        def serve() -> None:
            try:
                while not self._selection_stop.is_set():
                    if self.x.XPending(self.display) == 0:
                        time.sleep(0.025)
                        continue
                    event = XEvent()
                    self.x.XNextEvent(self.display, ctypes.byref(event))
                    if event.type != self.SELECTION_REQUEST:
                        continue
                    req = event.xselectionrequest
                    prop = int(req.property or req.target)
                    response = XSelectionEvent(self.SELECTION_NOTIFY, req.serial, 1, req.display,
                                               req.requestor, req.selection, req.target, prop, req.time)
                    if req.target == targets:
                        atom_values = (ctypes.c_ulong * 2)(targets, image_atom)
                        self.x.XChangeProperty(self.display, req.requestor, prop, self.XA_ATOM, 32,
                                               self.PROP_REPLACE, ctypes.cast(atom_values, ctypes.POINTER(ctypes.c_ubyte)), 2)
                    elif req.target == image_atom:
                        self.x.XChangeProperty(self.display, req.requestor, prop, image_atom, 8,
                                               self.PROP_REPLACE, self._png_buffer, len(png))
                        self._image_requested.set()
                    else:
                        response.property = 0
                    self.x.XSendEvent(self.display, req.requestor, 0, 0,
                                      ctypes.byref(XEvent(xselection=response)))
                    self.x.XFlush(self.display)
            except Exception as exc:
                self._selection_error.append(f"{type(exc).__name__}: {exc}")
        self._selection_thread = threading.Thread(target=serve, name="x11-png-selection", daemon=True)
        self._selection_thread.start()

    def start_text_clipboard(self, text: str) -> None:
        raw = text.encode("utf-8", "strict")
        if not raw or b"\x00" in raw:
            raise ValueError("text clipboard content must be nonempty UTF-8 without NUL")
        owner = self.x.XCreateSimpleWindow(self.display, self.root, -10, -10, 1, 1, 0, 0, 0)
        if not owner:
            raise RuntimeError("cannot create X11 clipboard owner")
        self._owner_window = int(owner)
        clip, utf8, targets = self.atom("CLIPBOARD"), self.atom("UTF8_STRING"), self.atom("TARGETS")
        self._text_buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        self._selection_stop.clear(); self._selection_error.clear()
        self.x.XSetSelectionOwner(self.display, clip, owner, self.CURRENT_TIME)
        self.x.XSync(self.display, 0)
        if int(self.x.XGetSelectionOwner(self.display, clip)) != owner:
            raise RuntimeError("could not own X11 CLIPBOARD selection")

        def serve() -> None:
            try:
                while not self._selection_stop.is_set():
                    if self.x.XPending(self.display) == 0:
                        time.sleep(0.025); continue
                    event = XEvent(); self.x.XNextEvent(self.display, ctypes.byref(event))
                    if event.type != self.SELECTION_REQUEST:
                        continue
                    req = event.xselectionrequest
                    prop = int(req.property or req.target)
                    response = XSelectionEvent(self.SELECTION_NOTIFY, req.serial, 1, req.display,
                                               req.requestor, req.selection, req.target, prop, req.time)
                    if req.target == targets:
                        atoms = (ctypes.c_ulong * 2)(targets, utf8)
                        self.x.XChangeProperty(self.display, req.requestor, prop, self.XA_ATOM, 32,
                                               self.PROP_REPLACE, ctypes.cast(atoms, ctypes.POINTER(ctypes.c_ubyte)), 2)
                    elif req.target in (utf8, self.atom("TEXT"), self.atom("STRING")):
                        self.x.XChangeProperty(self.display, req.requestor, prop, utf8, 8,
                                               self.PROP_REPLACE, self._text_buffer, len(raw))
                    else:
                        response.property = 0
                    self.x.XSendEvent(self.display, req.requestor, 0, 0, ctypes.byref(XEvent(xselection=response)))
                    self.x.XFlush(self.display)
            except Exception:
                self._selection_error.append("text clipboard selection owner failed")
        self._selection_thread = threading.Thread(target=serve, name="x11-text-selection", daemon=True)
        self._selection_thread.start()

    def read_clipboard_bytes(self, mime: str, timeout: float = 3.0) -> bytes:
        clipboard, target = self.atom('CLIPBOARD'), self.atom(mime)
        prop = self.atom('_WECHAT_DIGEST_TEXT_READBACK')
        window = self.x.XCreateSimpleWindow(self.display, self.root, -10, -10, 1, 1, 0, 0, 0)
        if not window:
            raise RuntimeError('cannot create private clipboard requestor')
        data = ctypes.POINTER(ctypes.c_ubyte)()
        try:
            self.x.XConvertSelection(self.display, clipboard, target, prop, window, self.CURRENT_TIME)
            self.x.XFlush(self.display)
            deadline = time.monotonic() + timeout
            notified = False
            while time.monotonic() < deadline:
                if not self.x.XPending(self.display):
                    time.sleep(0.01)
                    continue
                event = XEvent()
                self.x.XNextEvent(self.display, ctypes.byref(event))
                if event.type == self.SELECTION_NOTIFY and event.xselection.requestor == window:
                    notified = event.xselection.property == prop
                    break
            if not notified:
                raise RuntimeError('editor text could not be read back from clipboard')
            actual_type, actual_format = ctypes.c_ulong(), ctypes.c_int()
            count, after = ctypes.c_ulong(), ctypes.c_ulong()
            status = self.x.XGetWindowProperty(self.display, window, prop, 0, 16384, 1, 0,
                                               ctypes.byref(actual_type), ctypes.byref(actual_format),
                                               ctypes.byref(count), ctypes.byref(after), ctypes.byref(data))
            if status != 0 or actual_format.value != 8 or after.value or count.value > 65536:
                raise RuntimeError('editor returned unsupported or incomplete clipboard data')
            return ctypes.string_at(data, count.value)
        finally:
            if data:
                self.x.XFree(data)
            self.x.XDestroyWindow(self.display, window)
            self.x.XFlush(self.display)

    def copy_composer(self, window: dict, point: list[int], targets: tuple[str, ...]) -> dict[str, bytes]:
        self.activate(window)
        self.click(window, point)
        probe = self.x.XCreateSimpleWindow(self.display, self.root, -10, -10, 1, 1, 0, 0, 0)
        if not probe:
            raise RuntimeError('cannot create fresh clipboard probe')
        clipboard = self.atom('CLIPBOARD')
        try:
            self.x.XSetSelectionOwner(self.display, clipboard, probe, self.CURRENT_TIME)
            self.x.XSync(self.display, 0)
            self.chord('Control_L', 'a')
            self.chord('Control_L', 'c')
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                owner = int(self.x.XGetSelectionOwner(self.display, clipboard))
                if owner and owner != probe:
                    return {target: self.read_clipboard_bytes(target) for target in targets}
                time.sleep(0.01)
            return {}
        finally:
            self.x.XDestroyWindow(self.display, probe)
            self.x.XFlush(self.display)
            if self.focus_within(window):
                self.press('Right')

    def read_clipboard_text(self, timeout: float = 3.0) -> str:
        return self.read_clipboard_bytes('UTF8_STRING', timeout).decode('utf-8', 'strict')

    def verify_text_draft(self, window: dict, point: list[int], expected: str) -> bool:
        copied = self.copy_composer(window, point, ('UTF8_STRING',))
        return copied.get('UTF8_STRING', b'').decode('utf-8', 'strict') == expected

    def stop_clipboard(self) -> None:
        if not self._owner_window:
            return
        self._selection_stop.set()
        clip = self.atom("CLIPBOARD")
        if int(self.x.XGetSelectionOwner(self.display, clip)) == self._owner_window:
            self.x.XSetSelectionOwner(self.display, clip, 0, self.CURRENT_TIME)
        self.x.XFlush(self.display)
        if self._selection_thread:
            self._selection_thread.join(timeout=2)
            self._selection_thread = None
        self._owner_window = 0

    XA_ATOM = 4
    PROP_REPLACE = 0
    CURRENT_TIME = 0


def image_match(screen, rect: list[int], template_path: Path, threshold: int, max_bad_fraction: float) -> tuple[bool, float]:
    from PIL import Image, ImageChops
    if len(rect) != 4:
        raise ValueError("template region must be [x,y,width,height]")
    x, y, width, height = map(int, rect)
    if width <= 0 or height <= 0 or x < 0 or y < 0 or x + width > screen.width or y + height > screen.height:
        raise ValueError(f"calibration region outside window: {rect}")
    with Image.open(template_path) as source:
        template = source.convert("RGB")
    if template.size != (width, height):
        raise ValueError(f"template {template_path} dimensions {template.size} != {(width, height)}")
    crop = screen.crop((x, y, x + width, y + height))
    diff = ImageChops.difference(crop, template).convert("L")
    bad = sum(1 for value in diff.getdata() if value > threshold) / (width * height)
    return bad <= max_bad_fraction, bad


def send_button_ready(screen, rect: list[int]) -> bool:
    x, y, width, height = map(int, rect)
    region = screen.crop((x, y, x + width, y + height)).convert('RGB')
    return sum(g > r + 40 and g > b + 20 for r, g, b in region.getdata()) >= 16


def capture_private(x11: X11, win: dict, target: Path, redact_rect: list[int] | None = None):
    image = x11.screenshot(win)
    if redact_rect:
        from PIL import ImageDraw
        x, y, width, height = map(int, redact_rect)
        ImageDraw.Draw(image).rectangle((x, y, x + width - 1, y + height - 1), fill=(0, 0, 0))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    image.save(target, format="PNG")
    os.chmod(target, 0o600)
    return image


def source_context() -> tuple[Path, dict]:
    selection = load_json(Path("/runtime/selection.json"))
    source_raw = str(selection.get("source_db_dir") or "")
    if not source_raw:
        raise RuntimeError("selection.json has no source_db_dir")
    source_root = Path(source_raw).resolve(strict=True)
    if not source_root.is_relative_to(Path("/config")):
        raise RuntimeError("selected source database must resolve inside the read-only /config mount")
    if not source_root.is_dir():
        raise RuntimeError("selected source_db_dir is not a directory")
    keys = load_json(Path("/keys/all_keys.json"))
    return source_root, {key: value for key, value in keys.items() if not str(key).startswith("_")}


def decrypted_snapshot(source: Path, key_info: dict, target: Path) -> Path:
    # Source originals remain read-only. Copy the DB and its WAL, then use the
    # project's existing SQLCipher page/WAL decoder on private mode-0700 files.
    import decrypt_sync
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encrypted_copy = target.with_suffix(target.suffix + ".encrypted")
    wal_copy = Path(str(encrypted_copy) + "-wal")
    src_wal = Path(str(source) + "-wal")

    def signature(path: Path) -> tuple[int, int, int] | None:
        try:
            stat = path.stat()
            return stat.st_size, stat.st_mtime_ns, stat.st_ino
        except FileNotFoundError:
            return None

    stable = False
    for _ in range(3):
        before = (signature(source), signature(src_wal))
        if before[0] is None:
            raise RuntimeError(f"source database disappeared during snapshot: {source.name}")
        shutil.copyfile(source, encrypted_copy); encrypted_copy.chmod(0o600)
        if before[1] is not None:
            shutil.copyfile(src_wal, wal_copy); wal_copy.chmod(0o600)
        else:
            wal_copy.unlink(missing_ok=True)
        if before == (signature(source), signature(src_wal)):
            stable = True
            break
    if not stable:
        encrypted_copy.unlink(missing_ok=True); wal_copy.unlink(missing_ok=True)
        raise RuntimeError(f"source database changed during all snapshot attempts: {source.name}")
    try:
        enc_key = bytes.fromhex(key_info["enc_key"])
        decrypt_sync.full_decrypt(encrypted_copy, target, enc_key)
        decrypt_sync.patch_wal(wal_copy, target, enc_key)
        uri = target.resolve().as_uri() + "?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        encrypted_copy.unlink(missing_ok=True); wal_copy.unlink(missing_ok=True)
    return target


def unique_target(source_root: Path, keys: dict, temp: Path) -> None:
    import decrypt_sync
    from memory_ingest import load_contact_names
    names: dict[str, str] = {}
    for source in sorted(source_root.rglob("*.db")):
        if "contact" not in source.name.lower():
            continue
        rel = source.relative_to(source_root).as_posix()
        info = decrypt_sync.key_for_rel(keys, rel)
        if not info:
            continue
        dest = temp / "contact" / (hashlib.sha256(rel.encode()).hexdigest() + ".db")
        plain = decrypted_snapshot(source, info, dest)
        names.update({uid: item.get("display_name", "") for uid, item in load_contact_names(plain).items()})
    exact = [uid for uid, name in names.items() if name == CHAT_NAME]
    if exact != [CHAT_ID]:
        raise RuntimeError(f"exact contact name must uniquely identify fixed id; matches={exact!r}")
    search_collisions = [(uid, name) for uid, name in names.items()
                         if CHAT_NAME.casefold() in name.casefold() and (uid != CHAT_ID or name != CHAT_NAME)]
    if search_collisions:
        raise RuntimeError(f"full-name search is ambiguous; configured group name also appears in {search_collisions!r}")


def message_databases(source_root: Path, keys: dict, temp: Path) -> list[tuple[Path, Path]]:
    import decrypt_sync
    found = []
    for source in sorted(source_root.rglob("message_*.db")):
        if not source.stem.removeprefix('message_').isdigit():
            continue
        rel = source.relative_to(source_root).as_posix()
        info = decrypt_sync.key_for_rel(keys, rel)
        if not info:
            continue
        target = temp / "message" / (hashlib.sha256(rel.encode()).hexdigest() + ".db")
        found.append((source, target))
    if not found:
        raise RuntimeError("no keyed message_*.db shards found in selected source directory")
    return found


def query_group_rows(database: Path, table: str, local_id_floor: int | None = None,
                     start_epoch: int | None = None, include_content: bool = False,
                     message_type: int = 3) -> list[dict]:
    uri = database.resolve().as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in tables:
            return []
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info([{table}])")}
        needed = {"local_id", "server_id", "local_type", "create_time", "origin_source"}
        if not needed <= columns:
            raise RuntimeError(f"message schema missing columns: {sorted(needed - columns)}")
        if local_id_floor is None:
            maximum = conn.execute(f"SELECT MAX(local_id) FROM [{table}]").fetchone()[0]
            return [] if maximum is None else [{"local_id": int(maximum)}]
        if start_epoch is None:
            raise ValueError("outgoing message query requires a start timestamp")
        has_status = "status" in columns
        select = "local_id, server_id, local_type, create_time, origin_source" + (", status" if has_status else "")
        if include_content:
            content_cols = {"message_content", "compress_content", "WCDB_CT_message_content"}
            if not content_cols <= columns:
                raise RuntimeError("message schema missing content decoding columns")
            select += ", message_content, compress_content, WCDB_CT_message_content"
        sql = (f"SELECT {select} FROM [{table}] WHERE local_id>? AND COALESCE(server_id,0)<>0 "
               "AND COALESCE(create_time,0)>=? AND COALESCE(origin_source,0)=1 "
               f"AND (local_type & 4294967295)={int(message_type)} ORDER BY create_time DESC, local_id DESC")
        names = ["local_id", "server_id", "local_type", "create_time", "origin_source"]
        if has_status: names.append("status")
        if include_content: names.extend(("message_content", "compress_content", "WCDB_CT_message_content"))
        return [dict(zip(names, row)) for row in conn.execute(sql, (local_id_floor, start_epoch))]


def locate_new_self_text(rows_by_shard: dict[str, list[dict]], baseline: dict[str, int],
                         start_epoch: int, expected_hash: str) -> dict | None:
    import memory_ingest
    import message_parse
    candidates = []
    for shard, rows in rows_by_shard.items():
        for row in rows:
            if int(row.get("local_id") or 0) <= baseline.get(shard, 0):
                continue
            if int(row.get("server_id") or 0) == 0 or int(row.get("create_time") or 0) < start_epoch:
                continue
            if int(row.get("origin_source") or 0) != 1 or (int(row.get("local_type") or 0) & 0xFFFFFFFF) != 1:
                continue
            body = memory_ingest.decompress_content(row.get("message_content"), row.get("WCDB_CT_message_content"))
            packed = memory_ingest.decompress_content(row.get("compress_content"), None)
            decoded = body if body is not None else packed
            _sender, text = message_parse.split_group_sender(decoded)
            text = text if text else (decoded or "")
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected_hash:
                continue
            candidates.append({**row, "source_db": shard})
    return candidates[0] if len(candidates) == 1 else None

def scan_all(databases: list[tuple[Path, Path]], source_root: Path, keys: dict, table: str,
             local_floors: dict[str, int] | None = None, start_epoch: int | None = None,
             include_content: bool = False, message_type: int = 3) -> dict[str, list[dict]]:
    import decrypt_sync
    result = {}
    for source, target in databases:
        rel = source.relative_to(source_root).as_posix()
        info = decrypt_sync.key_for_rel(keys, rel)
        snap = decrypted_snapshot(source, info, target)
        result[rel] = query_group_rows(snap, table,
                                       None if local_floors is None else local_floors.get(rel, 0),
                                       start_epoch, include_content, message_type)
        snap.unlink(missing_ok=True)
    return result


def highwaters(rows_by_shard: dict[str, list[dict]]) -> dict[str, int]:
    return {shard: max((int(row.get("local_id") or 0) for row in rows), default=0)
            for shard, rows in rows_by_shard.items()}


def locate_new_self_image(rows_by_shard: dict[str, list[dict]], baseline: dict[str, int], start_epoch: int) -> dict | None:
    candidates = []
    for shard, rows in rows_by_shard.items():
        floor = baseline.get(shard, 0)
        for row in rows:
            local_id = int(row.get("local_id") or 0)
            server_id = int(row.get("server_id") or 0)
            local_type = int(row.get("local_type") or 0)
            create_time = int(row.get("create_time") or 0)
            origin = int(row.get("origin_source") or 0)
            if local_id > floor and server_id != 0 and create_time >= start_epoch and \
                    (local_type & 0xFFFFFFFF) == 3 and origin == 1:
                candidates.append({**row, "local_id": local_id, "server_id": server_id,
                                   "create_time": create_time, "source_db": shard,
                                   "status": row.get("status", "confirmed-in-decrypted-db")})
    return candidates[0] if len(candidates) == 1 else None


def worker_send(png_path: Path, expected_chat_id: str, expected_sha256: str) -> dict:
    project = _delivery_config()
    _verify_target(expected_chat_id, project, True)
    png_path, png_hash = checked_png(png_path, REPORTS_ROOT)
    if expected_sha256 != png_hash:
        raise ValueError("report PNG hash changed between host validation and worker snapshot")
    calibration = load_json(CALIBRATION_PATH)
    if calibration.get("version") != 1:
        raise ValueError("ui-calibration.json must use version 1")
    display = str(calibration.get("display", ":99"))
    if display != ":99":
        raise ValueError("calibrated DISPLAY must be exactly :99")
    fixed_window = {"x": 181, "y": 117, "width": 917, "height": 667}
    if calibration.get("window") != fixed_window:
        raise ValueError(f"UI calibration window must be the fixed geometry {fixed_window}")
    source_root, keys = source_context()
    os.umask(0o077)
    temp_context = tempfile.TemporaryDirectory(prefix="wechat-send-", dir="/tmp")
    temp = Path(temp_context.name); os.chmod(temp, 0o700)
    x11 = None
    submitted = False
    baseline = {}
    chat_table = "Msg_" + hashlib.md5(CHAT_ID.encode("utf-8")).hexdigest()
    evidence_dir = Path("/evidence")
    run_stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    before_evidence = evidence_dir / f"{run_stamp}-before.png"
    after_evidence = evidence_dir / f"{run_stamp}-after.png"
    try:
        unique_target(source_root, keys, temp)
        shards = message_databases(source_root, keys, temp)
        x11 = X11(display)
        window = x11.find_window(calibration)
        header_template = (CALIBRATION_PATH.parent / str(calibration["header_template"])).resolve(strict=True)
        header_rect = calibration["header_region"]
        header_threshold = int(calibration.get("header_pixel_threshold", 18))
        header_bad_fraction = float(calibration.get("header_max_bad_fraction", 0.001))

        # Search by full ASCII group name. The group metadata uniqueness assertion
        # above binds this visual selection to the one permitted chat id.
        x11.activate(window)
        x11.click(window, calibration["search_box"])
        time.sleep(0.2)
        x11.chord("Control_L", "a"); x11.press("BackSpace")
        x11.type_ascii(CHAT_NAME)
        time.sleep(float(calibration.get("search_delay_seconds", 0.8)))
        x11.click(window, calibration["search_result"])
        time.sleep(float(calibration.get("switch_delay_seconds", 1.2)))
        screen = capture_private(x11, window, before_evidence)
        header_ok, header_bad = image_match(screen, header_rect, header_template, header_threshold, header_bad_fraction)
        if not header_ok:
            raise RuntimeError("selected conversation does not match the configured group header")
        if not x11.composer_empty(window, calibration['compose']):
            raise RuntimeError('composer contains user content; preserving it and refusing to send')

        # Snapshot the selected conversation's high-water marks before submitting.
        before_rows = scan_all(shards, source_root, keys, chat_table)
        baseline = highwaters(before_rows)
        png_bytes = png_path.read_bytes()
        x11.start_png_clipboard(png_bytes)
        x11.click(window, calibration['compose'])
        x11.chord('Control_L', 'v')
        if not x11._image_requested.wait(timeout=float(calibration.get('clipboard_wait_seconds', 5.0))):
            raise RuntimeError('Qt did not request image/png from clipboard; no submit performed')
        ready_deadline = time.monotonic() + float(calibration.get('image_ready_timeout_seconds', 45))
        while True:
            pasted = x11.screenshot(window)
            if send_button_ready(pasted, calibration['send_button_region']):
                break
            if time.monotonic() >= ready_deadline:
                raise RuntimeError('image paste is not ready to send; no submit performed')
            time.sleep(0.25)
        header_ok, _ = image_match(pasted, header_rect, header_template, header_threshold, header_bad_fraction)
        if not header_ok:
            raise RuntimeError('target header changed before image submit')
        capture_private(x11, window, after_evidence)
        if x11._selection_error:
            raise RuntimeError("clipboard selection owner failed: " + "; ".join(x11._selection_error))
        x11.click(window, calibration['compose'])
        if not x11.focus_within(window):
            raise RuntimeError('compose field lost focus before submit')
        # Submit once, then use the target conversation's server receipt as the result.
        start_epoch = int(time.time())
        submitted = True
        x11.activate(window)
        x11.click(window, calibration['send_button'])
        time.sleep(float(calibration.get("send_settle_seconds", 1.5)))

        deadline = time.monotonic() + float(calibration.get("confirmation_timeout_seconds", 25))
        receipt = None
        while time.monotonic() < deadline:
            rows = scan_all(shards, source_root, keys, chat_table, baseline, start_epoch)
            receipt = locate_new_self_image(rows, baseline, start_epoch)
            if receipt:
                break
            time.sleep(2.5)
        if not receipt:
            return {"status": "unknown", "sent": None, "chat_name": CHAT_NAME, "chat_id": CHAT_ID,
                    "png": str(png_path), "sha256": png_hash,
                    "reason": "no new self-authored image with nonzero server_id confirmed in target DB; do not resend",
                    "ui_evidence": str(after_evidence), "baseline_local_ids": baseline}
        return {"status": "sent", "sent": True, "confirmed": True,
                "chat_name": CHAT_NAME, "chat_id": CHAT_ID, "png": str(png_path), "sha256": png_hash,
                "local_id": receipt["local_id"], "server_id": receipt["server_id"],
                "baseline_local_id": baseline.get(receipt["source_db"], 0),
                "message_status": receipt["status"], "create_time": receipt["create_time"],
                "source_db": receipt["source_db"], "ui_evidence": str(after_evidence),
                "ui_header_verified": True, "confirmation_method": "new self image, local_type&0xffffffff=3, origin_source=1, nonzero server_id"}
    except Exception as exc:
        return {"status": "unknown" if submitted else "failed", "sent": None if submitted else False,
                "before_submit": not submitted, "chat_name": CHAT_NAME, "chat_id": CHAT_ID,
                "png": str(png_path), "sha256": png_hash,
                "error": f"{type(exc).__name__}: {exc}",
                "ui_evidence": str(after_evidence) if after_evidence.exists() else None,
                "baseline_local_ids": baseline or None}
    finally:
        if x11:
            try:
                x11.stop_clipboard()
                x11.close()
            except Exception:
                pass
        temp_context.cleanup()


def worker_send_text(text_path: Path, expected_chat_id: str, expected_sha256: str) -> dict:
    project = _delivery_config()
    _verify_target(expected_chat_id, project, True)
    text_path, expected_text, text_hash = checked_text(text_path, Path("/messages"))
    if expected_sha256 != text_hash:
        raise ValueError("text message hash changed between host validation and worker snapshot")
    calibration = load_json(CALIBRATION_PATH)
    if calibration.get("version") != 1 or str(calibration.get("display", ":99")) != ":99":
        raise ValueError("calibrated UI version or DISPLAY mismatch")
    fixed_window = {"x": 181, "y": 117, "width": 917, "height": 667}
    if calibration.get("window") != fixed_window:
        raise ValueError("UI calibration window geometry mismatch")
    source_root, keys = source_context()
    os.umask(0o077)
    temp_context = tempfile.TemporaryDirectory(prefix="wechat-send-text-", dir="/tmp")
    temp = Path(temp_context.name); os.chmod(temp, 0o700)
    x11 = None
    submitted = False
    baseline = {}
    table = "Msg_" + hashlib.md5(CHAT_ID.encode("utf-8")).hexdigest()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    evidence_dir = Path("/evidence")
    before_evidence = evidence_dir / f"{stamp}-text-before.png"
    after_evidence = evidence_dir / f"{stamp}-text-after.png"
    try:
        unique_target(source_root, keys, temp)
        shards = message_databases(source_root, keys, temp)
        x11 = X11(":99")
        window = x11.find_window(calibration)
        header_rect = calibration["header_region"]
        redact_y = int(header_rect[1]) + int(header_rect[3])
        text_redact = [0, redact_y, int(calibration["window"]["width"]),
                       int(calibration["window"]["height"]) - redact_y]
        header_template = (CALIBRATION_PATH.parent / str(calibration["header_template"])).resolve(strict=True)
        header_rect = calibration["header_region"]
        header_threshold = int(calibration.get("header_pixel_threshold", 18))
        header_bad_fraction = float(calibration.get("header_max_bad_fraction", 0.001))
        x11.activate(window)
        capture_private(x11, window, before_evidence, text_redact)
        x11.click(window, calibration["search_box"])
        time.sleep(0.2)
        x11.chord("Control_L", "a"); x11.press("BackSpace")
        x11.type_ascii(CHAT_NAME)
        time.sleep(float(calibration.get("search_delay_seconds", 0.8)))
        x11.click(window, calibration["search_result"])
        time.sleep(float(calibration.get("switch_delay_seconds", 1.2)))
        screen = x11.screenshot(window)
        header_ok, _ = image_match(screen, header_rect, header_template, header_threshold, header_bad_fraction)
        if not header_ok:
            raise RuntimeError("selected conversation does not match the configured group header")
        if not x11.composer_empty(window, calibration["compose"]):
            raise RuntimeError("composer contains user content; preserving it and refusing to send")
        baseline = highwaters(scan_all(shards, source_root, keys, table))

        x11.start_text_clipboard(expected_text)
        x11.click(window, calibration["compose"])
        x11.chord("Control_L", "v")
        time.sleep(float(calibration.get("paste_settle_seconds", 1.0)))
        x11.stop_clipboard()
        if not x11.verify_text_draft(window, calibration["compose"], expected_text):
            raise RuntimeError("pasted editor text did not exactly match private UTF-8 message")
        if x11._selection_error:
            raise RuntimeError("clipboard selection owner failed")
        draft = capture_private(x11, window, after_evidence, text_redact)
        header_ok, _ = image_match(draft, header_rect, header_template, header_threshold, header_bad_fraction)
        if not header_ok:
            raise RuntimeError("target header changed while verifying text draft")

        x11.click(window, calibration["compose"])
        if not x11.focus_within(window):
            raise RuntimeError("WeChat window did not retain input focus before submit")
        start_epoch = int(time.time())
        submitted = True
        x11.click(window, calibration['send_button'])
        time.sleep(float(calibration.get("send_settle_seconds", 1.5)))
        deadline = time.monotonic() + float(calibration.get("confirmation_timeout_seconds", 25))
        receipt = None
        while time.monotonic() < deadline:
            rows = scan_all(shards, source_root, keys, table, baseline, start_epoch,
                            include_content=True, message_type=1)
            receipt = locate_new_self_text(rows, baseline, start_epoch, text_hash)
            if receipt:
                break
            time.sleep(2.5)
        if not receipt:
            return {"status": "unknown", "sent": None, "before_submit": False, "chat_id": CHAT_ID,
                    "sha256": text_hash, "reason": "no unique matching self-authored text in target DB; do not resend",
                    "ui_evidence": str(after_evidence)}
        return {"status": "sent", "sent": True, "confirmed": True, "before_submit": False,
                "chat_name": CHAT_NAME, "chat_id": CHAT_ID, "sha256": text_hash,
                "local_id": int(receipt["local_id"]), "server_id": int(receipt["server_id"]),
                "create_time": int(receipt["create_time"]), "source_db": receipt["source_db"],
                "ui_evidence": str(after_evidence), "ui_header_verified": True,
                "confirmation_method": "unique new self text, local_type&0xffffffff=1, origin_source=1, nonzero server_id, exact decoded UTF-8 SHA-256"}
    except Exception as exc:
        # Never serialize exception text: clipboard and DB providers may include private content.
        return {"status": "unknown" if submitted else "failed", "sent": None if submitted else False,
                "before_submit": not submitted, "chat_id": CHAT_ID, "sha256": text_hash,
                "error": type(exc).__name__,
                "ui_evidence": str(after_evidence) if after_evidence.exists() else None,
                "baseline_local_ids": baseline or None}
    finally:
        if x11:
            try:
                x11.stop_clipboard(); x11.close()
            except Exception:
                pass
        temp_context.cleanup()

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("png", nargs="?", type=Path)
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--send", action="store_true", help="explicitly send; default is local validation only")
    parser.add_argument("--chat-id", default=CHAT_ID)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--expected-sha256", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if (args.png is None) == (args.text_file is None):
            raise ValueError("supply exactly one PNG positional path or --text-file")
        if args.text_file is not None:
            if args.worker:
                if not args.send:
                    raise ValueError("worker mode requires explicit --send authorization")
                result = worker_send_text(args.text_file, args.chat_id, args.expected_sha256)
            else:
                result = host_send_text(args.text_file, args.send, args.chat_id, args.expected_sha256)
        elif args.worker:
            if not args.send:
                raise ValueError("worker mode requires explicit --send authorization")
            result = worker_send(args.png, args.chat_id, args.expected_sha256)
        else:
            result = host_send(args.png, args.send, args.chat_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("status") in {"prepared", "sent"} else 3 if result.get("status") == "unknown" else 1
    except subprocess.TimeoutExpired:
        result = {"status": "unknown" if args.send else "failed", "sent": None if args.send else False,
                  "before_submit": not args.send, "error": "delivery runtime timed out; do not resend unless before_submit is true"}
        print(json.dumps(result, ensure_ascii=False))
        return 3 if args.send else 1
    except Exception as exc:
        result = {"status": "failed", "sent": False, "before_submit": True,
                  "error": type(exc).__name__}
        print(json.dumps(result, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

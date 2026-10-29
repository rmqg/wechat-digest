#!/usr/bin/env python3
"""Publish the generated static report site to its configured rsync target."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Any

from report_site import build_site, REPORT_RENDERER_PATH

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "runtime" / "report-site.json"
STATE_DIR = ROOT / "runtime" / "report-site-state"
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:.]+\])$")
_REMOTE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_ASSET_EXTENSIONS = {".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".woff", ".woff2", ".ttf", ".otf", ".ico"}
_ORIGIN_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_ORIGIN_IMAGE_RE = re.compile(r"assets/[0-9a-f]{64}\.(?:png|jpg|jpeg|gif|webp)\Z")


class PublishError(RuntimeError):
    """A configuration, file-safety, or remote-publication error."""


def _load_config(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"cannot read publish config {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise PublishError("publish config must be a JSON object")
    required = ("host", "user", "port", "identity_file", "known_hosts", "remote_path")
    missing = [key for key in required if key not in config]
    if missing:
        raise PublishError("publish config is missing required fields: " + ", ".join(missing))
    host, user, port = config["host"], config["user"], config["port"]
    if not isinstance(host, str) or not _HOST_RE.fullmatch(host):
        raise PublishError("publish config host is invalid")
    if not isinstance(user, str) or not _USER_RE.fullmatch(user):
        raise PublishError("publish config user is invalid")
    if type(port) is not int or not 1 <= port <= 65535:
        raise PublishError("publish config port must be an integer from 1 to 65535")
    remote_path = config["remote_path"]
    if (not isinstance(remote_path, str) or not _REMOTE_PATH_RE.fullmatch(remote_path)
            or remote_path.startswith("/") or ".." in remote_path.split("/") or "//" in remote_path):
        raise PublishError("publish config remote_path must be a safe relative path")
    resolved = dict(config)
    for key in ("identity_file", "known_hosts"):
        raw = config[key]
        if not isinstance(raw, str) or not raw:
            raise PublishError(f"publish config {key} must be a non-empty path")
        file_path = Path(raw).expanduser()
        if not file_path.is_absolute():
            file_path = path.parent / file_path
        file_path = file_path.resolve()
        if not file_path.is_file():
            raise PublishError(f"publish config {key} is not a regular file")
        resolved[key] = str(file_path)
    return resolved


def _safe_files(output_dir: Path, files: Any) -> list[str]:
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise PublishError("report site output directory must be a real directory")
    if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
        raise PublishError("report_site.build_site must return a files list of paths")
    if len(files) != len(set(files)):
        raise PublishError("report_site.build_site returned duplicate file paths")
    safe: list[str] = []
    for item in files:
        path = PurePosixPath(item)
        parts = item.split("/")
        if (not item or path.is_absolute() or path.as_posix() != item
                or any(part in ("", ".", "..") or not _COMPONENT_RE.fullmatch(part) for part in parts)
                or "\\" in item):
            raise PublishError(f"unsafe site file path: {item!r}")
        if item != "index.html":
            if len(parts) < 2 or parts[0] not in ("reports", "assets"):
                raise PublishError(f"site file is outside publishable static paths: {item!r}")
            suffix = Path(parts[-1]).suffix.lower()
            if parts[0] == "reports" and suffix != ".html":
                raise PublishError(f"report file is not HTML: {item!r}")
            if parts[0] == "assets" and suffix not in _ASSET_EXTENSIONS:
                raise PublishError(f"asset type is not publishable: {item!r}")
            if parts[0] == "assets" and suffix in _ORIGIN_IMAGE_EXTENSIONS and not _ORIGIN_IMAGE_RE.fullmatch(item):
                raise PublishError(f"image asset is not addressable by the origin: {item!r}")
        candidate = output_dir
        for index, part in enumerate(parts):
            candidate = candidate / part
            try:
                candidate.lstat()
            except OSError as exc:
                raise PublishError(f"site file is missing: {item!r}") from exc
            if candidate.is_symlink():
                raise PublishError(f"site file path contains a symlink: {item!r}")
            if index < len(parts) - 1 and not candidate.is_dir():
                raise PublishError(f"site path component is not a directory: {item!r}")
        if not candidate.is_file():
            raise PublishError(f"site path is not a regular file: {item!r}")
        safe.append(item)
    if "index.html" not in safe:
        raise PublishError("report_site.build_site did not return index.html")
    return sorted(safe)


def _content_hash(output_dir: Path, files: list[str]) -> str:
    digest = hashlib.sha256()
    for name in files:
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        with (output_dir / name).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _load_success(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (not isinstance(value, dict) or not isinstance(value.get("sha256"), str)
            or not isinstance(value.get("files"), list)):
        return None
    return value


def _input_stamp(root: Path, config: dict, source_media_files: list[str] | None = None) -> str:
    paths = [p for p in (root / 'runtime/reports').iterdir()
             if p.suffix in ('.json', '.html', '.png')]
    paths.extend([Path(__file__), Path(__file__).with_name('report_site.py'),
                  Path(__file__).with_name('source_media.py'), REPORT_RENDERER_PATH,
                  REPORT_RENDERER_PATH.with_name('report_metrics.py')])
    paths.extend((root / 'runtime/digest/sent-receipts').glob('*.json'))
    paths.extend((root / 'runtime/digest/on-demand-receipts').glob('*.json'))
    paths.extend(Path(path) for path in (source_media_files or []))
    signatures = []
    for path in sorted(set(paths), key=str):
        try:
            stat = path.stat()
            signatures.append((str(path), stat.st_size, stat.st_mtime_ns))
        except OSError:
            signatures.append((str(path), None, None))
    target = [config[k] for k in ('host', 'port', 'user', 'remote_path')]
    return hashlib.sha256(json.dumps([target, signatures], sort_keys=True).encode()).hexdigest()


def _remote_shell(config: dict[str, Any]) -> str:
    return shlex.join(["ssh", "-p", str(config["port"]), "-i", config["identity_file"],
                       "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                       '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                       "-o", "GlobalKnownHostsFile=/dev/null",
                       "-o", "UserKnownHostsFile=" + config["known_hosts"], "-o", "IdentitiesOnly=yes"])


def _rsync_stage(config: dict[str, Any], output_dir: Path, files: list[str]) -> None:
    if not files:
        return
    host = config["host"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    destination = f"{config['user']}@{host}:{config['remote_path']}"
    command = ["rsync", "-rtc", "--whole-file", "--timeout=90", "--from0", "--files-from=-", "-e", _remote_shell(config),
               "--", str(output_dir) + "/", destination]
    try:
        subprocess.run(command, input=b"\0".join(name.encode("utf-8") for name in files) + b"\0",
                       check=True, capture_output=True, text=False, timeout=(1200 if all(name.startswith("assets/") for name in files) else 180))
    except (OSError, subprocess.CalledProcessError) as exc:
        details = getattr(exc, "stderr", b"")
        if isinstance(details, bytes):
            details = details.decode("utf-8", errors="replace")
        details = details.strip()
        raise PublishError("rsync failed" + (f": {details}" if details else "")) from exc


def _write_success(path: Path, files: list[str], digest: str, input_stamp: str = '', source_media_files: list[str] | None = None) -> None:
    payload = json.dumps({"sha256": digest, "files": files, 'input_stamp': input_stamp,
                          'source_media_files': sorted(set(source_media_files or []))}, sort_keys=True, separators=(",", ":"))
    fd, temporary = tempfile.mkstemp(prefix="success.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def publish_reports(config_path: Path = CONFIG_PATH, *, root: Path = ROOT) -> dict[str, Any]:
    """Build and publish a safe static-file manifest; absent config disables publishing."""
    config_path = Path(config_path)
    config = _load_config(config_path)
    if config is None:
        return {"status": "disabled"}
    root = Path(root)
    state_dir = root / "runtime" / "report-site-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "publish.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy"}
        success_path = state_dir / 'success.json'
        prior = _load_success(success_path)
        prior_media_files = prior.get('source_media_files', []) if prior else []
        if not isinstance(prior_media_files, list) or any(not isinstance(path, str) for path in prior_media_files):
            prior_media_files = []
        input_stamp = _input_stamp(root, config, prior_media_files)
        if prior and prior.get('input_stamp') == input_stamp:
            return {'status': 'unchanged', 'reports': sum(p.startswith('reports/') for p in prior['files']), 'files': prior['files']}
        output_dir = root / "runtime" / "report-site"
        if output_dir.is_symlink():
            raise PublishError("report site output directory must not be a symlink")
        result = build_site(root / "runtime" / "reports", output_dir)
        if not isinstance(result, dict):
            raise PublishError("report_site.build_site must return an object")
        report_count = result.get("reports")
        if type(report_count) is not int or report_count < 0:
            raise PublishError("report_site.build_site must return a non-negative reports count")
        source_media_files = result.get('source_media_files', [])
        if not isinstance(source_media_files, list) or any(not isinstance(path, str) for path in source_media_files):
            raise PublishError("report_site.build_site source_media_files must be a list of paths")
        source_media_files = sorted(set(source_media_files))
        input_stamp = _input_stamp(root, config, source_media_files)
        files = _safe_files(output_dir, result.get("files"))
        target = json.dumps([config[k] for k in ('host', 'port', 'user', 'remote_path')])
        digest = hashlib.sha256((_content_hash(output_dir, files) + target).encode()).hexdigest()
        if prior and prior.get("sha256") == digest and prior.get("files") == files:
            _write_success(success_path, files, digest, input_stamp, source_media_files)
            return {"status": "unchanged", "reports": report_count, "files": files}
        # A failed partial rsync invalidates any previous receipt of remote contents.
        success_path.unlink(missing_ok=True)
        try:
            _rsync_stage(config, output_dir, [name for name in files if name.startswith("assets/")
                         and PurePosixPath(name).suffix.lower() not in _ORIGIN_IMAGE_EXTENSIONS])
            _rsync_stage(config, output_dir, [name for name in files if name.startswith("reports/")])
            _rsync_stage(config, output_dir, [name for name in files if name == "index.html"])
        except Exception:
            success_path.unlink(missing_ok=True)
            raise
        _write_success(success_path, files, digest, input_stamp, source_media_files)
        return {"status": "published", "reports": report_count, "files": files}


def main() -> int:
    try:
        result = publish_reports()
    except Exception as exc:
        print(f"report publish: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({**{k: v for k, v in result.items() if k != 'files'}, 'file_count': len(result.get('files', []))}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

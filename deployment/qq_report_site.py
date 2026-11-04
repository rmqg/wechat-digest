#!/usr/bin/env python3
"""Private, per-group HTTP access to QQ digest reports."""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import html
import hmac
import json
import os
from pathlib import Path
import re
import stat
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn, UnixStreamServer
from urllib.parse import unquote, urlsplit

_GROUP_ID = re.compile(r"[0-9]{1,32}\Z")
_SAFE_STEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}\Z")
_ALLOWED_ASSET_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
}
_MIME_TYPES = {
    ".html": "text/html; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".avif": "image/avif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
    ".otf": "font/otf", ".eot": "application/vnd.ms-fontobject",
}


def hash_password(password: str) -> str:
    """Return a salted scrypt verifier; the cleartext is never persisted."""
    if not isinstance(password, str):
        raise TypeError("password must be text")
    salt = secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n_text, r_text, p_text, salt_hex, digest_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_text), int(r_text), int(p_text)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        if n < 2 or n > 2**20 or n & (n - 1) or not 1 <= r <= 32 or not 1 <= p <= 16:
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (AttributeError, TypeError, ValueError, MemoryError):
        return False


def load_config(path: str | os.PathLike[str]) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("bind") != "127.0.0.1":
        raise ValueError("bind must be exactly 127.0.0.1")
    port = config.get("port")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be an integer from 0 to 65535")
    socket_path = config.get("socket_path")
    if socket_path is not None and (not isinstance(socket_path, str) or not Path(socket_path).is_absolute()):
        raise ValueError("socket_path must be an absolute path")
    groups = config.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise ValueError("groups must be a non-empty mapping")
    usernames = set()
    report_roots = []
    validated = {}
    for group_id, group in groups.items():
        if not isinstance(group_id, str) or not _GROUP_ID.fullmatch(group_id):
            raise ValueError(f"invalid group id: {group_id!r}")
        if not isinstance(group, dict):
            raise ValueError(f"group {group_id} must be an object")
        name, username = group.get("name"), group.get("username")
        encoded = group.get("password_scrypt")
        reports_dir = group.get("reports_dir")
        if not isinstance(name, str) or not name or not isinstance(username, str) or not username:
            raise ValueError(f"group {group_id} requires name and username")
        if username in usernames:
            raise ValueError("each group must have an independent username")
        usernames.add(username)
        if not isinstance(encoded, str):
            raise ValueError(f"group {group_id} requires a valid password_scrypt")
        try:
            scheme, n_text, r_text, p_text, salt_hex, digest_hex = encoded.split("$")
            n, r, p = int(n_text), int(r_text), int(p_text)
            salt, digest = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
            if (scheme != "scrypt" or n != 2**14 or r != 8 or p != 1
                    or len(salt) != 16 or len(digest) != 32):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError(f"group {group_id} requires a valid password_scrypt") from exc
        if not isinstance(reports_dir, str) or not reports_dir:
            raise ValueError(f"group {group_id} requires reports_dir")
        root = Path(reports_dir).expanduser().resolve(strict=True)
        if any(root == existing or root in existing.parents or existing in root.parents for existing in report_roots):
            raise ValueError("groups must use independent, non-overlapping reports_dir paths")
        report_roots.append(root)
        if not root.is_dir():
            raise ValueError(f"reports_dir for group {group_id} must be a directory")
        validated[group_id] = {"name": name, "username": username, "password_scrypt": encoded,
                               "reports_dir": root}
    return {"bind": "127.0.0.1", "port": port, "socket_path": socket_path, "groups": validated}



def _has_symlink_component(root: Path, target: Path) -> bool:
    try:
        relative = target.relative_to(root)
    except ValueError:
        return True
    current = root
    if current.is_symlink():
        return True
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _safe_file(root: Path, rel: str) -> Path | None:
    try:
        root = root.resolve(strict=True)
    except OSError:
        return None
    if not rel or "\\" in rel or "\x00" in rel:
        return None
    relpath = Path(rel)
    if relpath.is_absolute() or any(part in ("", ".", "..") for part in relpath.parts):
        return None
    target = root / relpath
    try:
        if _has_symlink_component(root, target):
            return None
        resolved = target.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            return None
        return resolved
    except (OSError, ValueError):
        return None


def _report_entries(group_id: str, group: dict):
    try:
        root = group["reports_dir"].resolve(strict=True)
        names = sorted(path.name for path in root.iterdir() if path.suffix.lower() == ".json")
    except OSError:
        return []
    editions = {}
    expected_chat = f"qq:{group_id}"
    for name in names:
        if name == "sources.json" or name.endswith(".sources.json") or not _SAFE_STEM.fullmatch(name[:-5]):
            continue
        stem = name[:-5]
        if stem.endswith("-brief"):
            edition = "brief"
            base = stem[:-len("-brief")]
        elif stem.endswith("-review"):
            edition = "review"
            base = stem[:-len("-review")]
        else:
            continue
        if not base or not _SAFE_STEM.fullmatch(base):
            continue
        trio = [_safe_file(root, stem + suffix) for suffix in (".html", ".png", ".json")]
        try:
            if any(path is None or path.stat().st_size == 0 for path in trio):
                continue
            metadata = json.loads(trio[2].read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict) or metadata.get("edition") != edition:
            continue
        stats = metadata.get("stats")
        messages = stats.get("messages") if isinstance(stats, dict) else None
        if metadata.get("chat_username") != expected_chat or type(messages) is not int or messages <= 0:
            continue
        editions.setdefault(base, {})[edition] = {
            "stem": stem, "metadata": metadata, "html": trio[0], "png": trio[1],
        }

    result = []
    for pair in editions.values():
        if set(pair) != {"brief", "review"}:
            continue
        brief, review = pair["brief"], pair["review"]
        brief_meta, review_meta = brief["metadata"], review["metadata"]
        brief_stats, review_stats = brief_meta["stats"], review_meta["stats"]
        if (brief_meta.get("chat_username") != review_meta.get("chat_username")
                or brief_stats["messages"] != review_stats["messages"]):
            continue
        comparable = []
        for key in ("window", "range_text"):
            left, right = brief_meta.get(key), review_meta.get(key)
            if left is not None and right is not None and left != "" and right != "":
                comparable.append(left == right)
        if not comparable or not all(comparable):
            continue
        result.extend((brief, review))
    return result


def _index_html(group_id: str, group: dict) -> bytes:
    entries = _report_entries(group_id, group)
    rows = []
    for entry in entries:
        meta = entry["metadata"]
        label = "简报" if meta.get("edition") == "brief" else "回顾"
        window = meta.get("range_text") or meta.get("day")
        if not window:
            value = meta.get("window")
            if isinstance(value, dict):
                start = value.get("start") or value.get("start_time") or value.get("start_unix")
                end = value.get("end") or value.get("end_time") or value.get("end_unix_exclusive")
                window = f"{start} – {end}" if start is not None and end is not None else "统计窗口"
            else:
                window = "统计窗口"
        stats = meta.get("stats") or {}
        rows.append('<li><a href="' + html.escape(entry["stem"] + ".html", quote=True) + '">' + label
                    + '</a> · ' + html.escape(str(window)) + ' · 消息 '
                    + html.escape(str(stats.get("messages", 0))) + ' 条</li>')
    body = "\n".join(rows) if rows else "<li>暂无报告</li>"
    page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow"><title>' + html.escape(group["name"]) + '</title><h1>'
            + html.escape(group["name"]) + '</h1><ul>' + body + '</ul></html>')
    return page.encode("utf-8")


def make_handler(config: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "QQReportSite/1.0"
        sys_version = ""

        def end_headers(self):
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _unauthorized(self, realm="QQ reports"):
            body = (
                '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                '<title>需要登录</title></head><body><h1>需要登录</h1>'
                '<p>请在浏览器的登录窗口中使用当前群的专用账号。</p></body></html>'
            ).encode('utf-8')
            self.send_response(401)
            self.send_header("WWW-Authenticate", f'Basic realm="{realm}", charset="UTF-8"')
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _credentials(self):
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return None
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
                return decoded.split(":", 1)
            except (binascii.Error, UnicodeError, ValueError):
                return None

        def _authenticated(self, group):
            credentials = self._credentials()
            return bool(credentials and hmac.compare_digest(credentials[0], group["username"])
                        and verify_password(credentials[1], group["password_scrypt"]))

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            raw_path = urlsplit(self.path).path
            try:
                decoded = unquote(raw_path, errors="strict")
            except (UnicodeError, ValueError):
                return self._send(404)
            if decoded == "/":
                credentials = self._credentials()
                if credentials:
                    username, password = credentials
                    for group_id, candidate in config["groups"].items():
                        if (hmac.compare_digest(username, candidate["username"])
                                and verify_password(password, candidate["password_scrypt"])):
                            self.send_response(302)
                            self.send_header("Location", f"/qq/{group_id}/")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                return self._unauthorized()
            match = re.fullmatch(r"/qq/([0-9]{1,32})(?:/(.*))?", decoded)
            if not match:
                return self._send(404)
            group_id, tail = match.group(1), match.group(2) or ""
            group = config["groups"].get(group_id)
            if group is None:
                return self._send(404)
            if not self._authenticated(group):
                return self._unauthorized(group_id)
            if not tail or tail == "index.html":
                return self._send(200, _index_html(group_id, group), "text/html; charset=utf-8")
            if "%" in tail or "\\" in tail or "\x00" in tail:
                return self._send(404)
            root = group["reports_dir"]
            if "/" not in tail and _SAFE_STEM.fullmatch(Path(tail).stem):
                suffix = Path(tail).suffix.lower()
                if suffix not in (".html", ".png"):
                    return self._send(404)
                stem = Path(tail).stem
                entry = next((item for item in _report_entries(group_id, group) if item["stem"] == stem), None)
                if entry is None:
                    return self._send(404)
                target = entry["html"] if suffix == ".html" else entry["png"]
            else:
                parts = tail.split("/")
                if len(parts) < 2 or parts[0] != "assets" or any(p in ("", ".", "..") for p in parts):
                    return self._send(404)
                rel_asset = "/".join(parts[1:])
                ext = Path(rel_asset).suffix.lower()
                if ext not in _ALLOWED_ASSET_SUFFIXES:
                    return self._send(404)
                target = _safe_file(root, tail)
                if target is None:
                    return self._send(404)
            try:
                body = target.read_bytes()
            except OSError:
                return self._send(404)
            mime = _MIME_TYPES.get(target.suffix.lower(), "application/octet-stream")
            self._send(200, body, mime)

        def log_message(self, fmt, *args):
            return

    return Handler


class ThreadingUnixHTTPServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True


def create_server(config: dict):
    handler = make_handler(config)
    socket_path = config.get("socket_path")
    if not socket_path:
        return ThreadingHTTPServer((config["bind"], config["port"]), handler)
    path = Path(socket_path)
    if not path.is_absolute():
        raise ValueError("socket_path must be an absolute path")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(existing.st_mode):
            raise FileExistsError(f"refusing to remove non-socket at {path}")
        path.unlink()
    server = ThreadingUnixHTTPServer(str(path), handler)
    try:
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError("Unix HTTP server did not create a socket")
        server._qq_socket_path = path
        server._qq_socket_identity = (info.st_dev, info.st_ino)
        os.chmod(path, 0o660)
        return server
    except Exception:
        close_server(server)
        raise


def close_server(server):
    socket_path = getattr(server, "_qq_socket_path", None)
    identity = getattr(server, "_qq_socket_identity", None)
    try:
        server.server_close()
    finally:
        if socket_path is not None and identity is not None:
            try:
                info = socket_path.lstat()
                if (stat.S_ISSOCK(info.st_mode)
                        and (info.st_dev, info.st_ino) == identity):
                    socket_path.unlink()
            except FileNotFoundError:
                pass


def serve(config: dict):
    server = create_server(config)
    try:
        server.serve_forever()
    finally:
        close_server(server)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="JSON site configuration")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--hash-password", action="store_true", help="read one password line from stdin and print its scrypt verifier")
    args = parser.parse_args(argv)
    if args.hash_password:
        if args.check_config or args.serve or args.config:
            parser.error("--hash-password is used alone")
        password = sys.stdin.readline().rstrip("\r\n")
        print(hash_password(password))
        return 0
    if not args.config or args.check_config == args.serve:
        parser.error("provide --config and exactly one of --check-config or --serve")
    try:
        config = load_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    if args.check_config:
        print("configuration OK")
        return 0
    serve(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

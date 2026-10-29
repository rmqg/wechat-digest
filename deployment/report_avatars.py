from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import ipaddress
import json
import os
import socket
import sqlite3
import ssl
import urllib.error
import urllib.parse
import http.client
import urllib.request
import tempfile
from pathlib import Path

import digest

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_URL_BYTES = 4096
_ALLOWED_HOSTS = {"wx.qlogo.cn", "thirdwx.qlogo.cn", "mmhead.hk.wechat.com", "mmhead.c2c.wechat.com"}


def _image_bytes(data: bytes) -> bytes | None:
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data)) as image:
            if image.width <= 0 or image.height <= 0 or image.width * image.height > 40_000_000:
                return None
            image.load()
            image = ImageOps.exif_transpose(image)
            image.thumbnail((256, 256), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.convert("RGBA").save(output, format="PNG", optimize=True)
            return output.getvalue()
    except Exception:
        return None


def _cached_image_bytes(data: bytes) -> bytes | None:
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or not 0 < image.width <= 256 or not 0 < image.height <= 256:
                return None
            image.verify()
        return data
    except (OSError, ValueError):
        return None


def _public_addresses(host: str, port: int) -> list[str]:
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    if not addresses:
        raise OSError("avatar host has no address")
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise OSError("avatar host resolved to a non-public address")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, addresses: list[str], **kwargs):
        super().__init__(host, **kwargs)
        self._addresses = addresses

    def connect(self):
        last_error = None
        for address in self._addresses:
            raw = None
            try:
                raw = socket.create_connection((address, self.port), self.timeout, self.source_address)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except OSError as error:
                if raw is not None:
                    raw.close()
                self.sock = None
                last_error = error
        raise last_error or OSError("avatar connection failed")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        parsed = urllib.parse.urlsplit(request.full_url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or host not in _ALLOWED_HOSTS:
            raise urllib.error.URLError("avatar host is not allowed")
        addresses = _public_addresses(host, parsed.port or 443)
        return self.do_open(
            lambda h, **kw: _PinnedHTTPSConnection(host, addresses, **kw), request,
            context=self._context,
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        return None


def _download(url: str) -> bytes | None:
    try:
        if not url or len(url.encode("utf-8")) > MAX_URL_BYTES:
            return None
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment):
            return None
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*", "Cache-Control": "no-cache"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _PinnedHTTPSHandler(), _NoRedirect())
        with opener.open(request, timeout=5) as response:
            if response.geturl() != url or response.status != 200:
                return None
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_IMAGE_BYTES:
                return None
            data = response.read(MAX_IMAGE_BYTES + 1)
            return data if len(data) <= MAX_IMAGE_BYTES else None
    except (OSError, ValueError, urllib.error.URLError, TimeoutError, ssl.SSLError):
        return None


def _db_files(root: Path, name: str):
    return sorted((path for path in root.rglob("*.db") if path.name.lower() == name), key=lambda p: p.as_posix())


def _database_path(encrypted: Path, root: Path, keys: dict, out_dir: Path, decrypted_dir: Path | None) -> Path | None:
    if decrypted_dir is not None:
        plain = decrypted_dir / encrypted.relative_to(root)
        if plain.is_file():
            return plain
    return digest._decrypt(encrypted, root, keys, out_dir)


def _contact_records(source: Path, keys: dict, out_dir: Path, usernames: set[str], decrypted_dir: Path | None) -> dict:
    found = {}
    root = source.resolve()
    for encrypted in _db_files(root, "contact.db"):
        try:
            plain = _database_path(encrypted, root, keys, out_dir, decrypted_dir)
            if plain is None:
                continue
            with digest._open_ro(plain) as conn:
                if "contact" not in digest._table_names(conn):
                    continue
                columns = {row[1] for row in conn.execute("PRAGMA table_info(contact)")}
                if "username" not in columns:
                    continue
                selects = [name for name in ("username", "big_head_url", "small_head_url", "head_img_md5") if name in columns]
                query = "SELECT " + ",".join(f'"{name}"' for name in selects) + " FROM contact WHERE username IN (" + ",".join("?" for _ in usernames) + ")"
                for row in conn.execute(query, tuple(usernames)):
                    values = dict(zip(selects, row))
                    found[str(values["username"])] = values
            if usernames <= found.keys():
                break
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            continue
    return found


def _head_images(source: Path, keys: dict, out_dir: Path, usernames: set[str], decrypted_dir: Path | None) -> dict[str, bytes]:
    found = {}
    root = source.resolve()
    for encrypted in _db_files(root, "head_image.db"):
        try:
            plain = _database_path(encrypted, root, keys, out_dir, decrypted_dir)
            if plain is None:
                continue
            with digest._open_ro(plain) as conn:
                if "head_image" not in digest._table_names(conn):
                    continue
                cols = {row[1] for row in conn.execute("PRAGMA table_info(head_image)")}
                if not {"username", "image_buffer"} <= cols:
                    continue
                for username in usernames - found.keys():
                    row = conn.execute("SELECT image_buffer FROM head_image WHERE username=? AND length(image_buffer)>0", (username,)).fetchone()
                    if row and row[0]:
                        found[username] = bytes(row[0])
            if usernames <= found.keys():
                break
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            continue
    return found

def _private_replace(path: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".avatar-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collect_avatars(source: Path, keys: dict, usernames: list[str], runtime_dir: Path,
                    decrypted_dir: Path | None = None, *, now: datetime | None = None) -> dict[str, dict]:
    """Refresh requested real avatars once per Beijing day; retain stale images on failure."""
    ids = list(dict.fromkeys(str(name).strip() for name in usernames if str(name).strip()))
    cache_dir = Path(runtime_dir) / "avatars"
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(cache_dir, 0o700)
    source = Path(source)
    if decrypted_dir is None:
        with tempfile.TemporaryDirectory(prefix=".avatar-decrypted-", dir=Path(runtime_dir)) as temp:
            temp_dir = Path(temp)
            contacts = _contact_records(source, keys, temp_dir, set(ids), None)
            local = _head_images(source, keys, temp_dir, set(ids), None)
    else:
        temp_dir = Path(decrypted_dir)
        temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        contacts = _contact_records(source, keys, temp_dir, set(ids), temp_dir)
        local = _head_images(source, keys, temp_dir, set(ids), temp_dir)

    current = (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=8)))
    day, epoch = current.date().isoformat(), current.timestamp()

    def refresh(username):
        contact = contacts.get(username, {})
        urls = [(str(contact.get(key) or ""), key) for key in ("big_head_url", "small_head_url")]
        fingerprint = hashlib.sha256(json.dumps({"md5": str(contact.get("head_img_md5") or ""),
            "urls": [hashlib.sha256(url.encode()).hexdigest() for url, _ in urls]}, sort_keys=True).encode()).hexdigest()
        uid_hash = hashlib.sha256(username.encode()).hexdigest()
        cache_image, cache_meta = cache_dir / f"{uid_hash}.png", cache_dir / f"{uid_hash}.json"
        cached, normalized = {}, None
        try:
            cached = json.loads(cache_meta.read_text(encoding="utf-8"))
            normalized = _cached_image_bytes(cache_image.read_bytes())
        except (OSError, ValueError, TypeError):
            pass
        fresh = normalized is not None and cached.get("contact_hash") == fingerprint and cached.get("refreshed_day") == day
        origin = str(cached.get("source") or "")
        due = (not fresh and (cached.get("last_attempt_day") != day or cached.get("attempt_contact_hash") != fingerprint
                             or epoch - float(cached.get("last_attempt_at") or 0) >= 300))
        if due:
            seen = set()
            for url, source_name in urls:
                if not url or url in seen:
                    continue
                seen.add(url)
                downloaded = _image_bytes(_download(url) or b"")
                if downloaded:
                    normalized, origin, fresh = downloaded, source_name, True
                    cached = {"contact_hash": fingerprint, "source": origin, "refreshed_day": day,
                              "refreshed_at": epoch}
                    _private_replace(cache_image, normalized)
                    break
            cached.update(last_attempt_day=day, last_attempt_at=epoch, attempt_contact_hash=fingerprint)
            if not fresh:
                cached["refresh_error"] = "avatar_download_failed" if seen else "avatar_url_unavailable"
        local_fallback = False
        if normalized is None:
            normalized = _image_bytes(local.get(username, b""))
            if normalized:
                local_fallback = True
                origin = "head_image"
                cached.pop("refreshed_day", None)
                cached.pop("refreshed_at", None)
                cached.update(contact_hash=fingerprint, source=origin)
                _private_replace(cache_image, normalized)
        if due or local_fallback or (normalized and not cache_meta.exists()):
            _private_replace(cache_meta, json.dumps(cached, ensure_ascii=False).encode())
        if normalized is None:
            return username, {"available": False, "data_uri": "", "source": "", "refreshed_today": False,
                              "reason": "avatar_unavailable", "refresh_error": cached.get("refresh_error")}
        return username, {"available": True, "data_uri": "data:image/png;base64," + base64.b64encode(normalized).decode("ascii"),
                          "source": origin, "refreshed_today": fresh, "refreshed_day": cached.get("refreshed_day"),
                          "refresh_error": None if fresh else cached.get("refresh_error", "avatar_not_refreshed")}

    with (cache_dir / "refresh.lock").open("a+") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(pool.map(refresh, ids))

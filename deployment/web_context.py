#!/usr/bin/env python3
"""Fetch bounded public web context owned by messages in the configured group."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import tempfile
import time
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit

import settings

HERE = Path(__file__).resolve().parent
ROOT = settings.ROOT
sys.path.insert(0, str(ROOT / "memory"))
if Path('/source/memory').is_dir():
    sys.path.insert(0, '/source/memory')
import message_parse
MAX_BYTES = 1024 * 1024
MAX_REDIRECTS = 2
TIMEOUT = 10
CACHE_TTL = 24 * 60 * 60
URL_RE = re.compile(r"https?://[^\s<>\"'\u3000]+", re.IGNORECASE)
CREDENTIAL_RE = re.compile(r"(?:token|api[_-]?key|access[_-]?token|password|passwd|secret|authorization|auth)=", re.IGNORECASE)


class UnsafeURL(ValueError):
    pass


def _canonical_url(value: str) -> str:
    value = html.unescape(str(value or "")).strip().rstrip(".,;:!?)]}，。；：！？、》」』")
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise UnsafeURL("unsupported_url")
    credential_material = unquote_plus(parts.query) + "&" + unquote_plus(parts.path)
    if parts.username is not None or parts.password is not None or CREDENTIAL_RE.search(credential_material):
        raise UnsafeURL("credential_url")
    host = parts.hostname.rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise UnsafeURL("non_public_host")
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        if not ip.is_global:
            raise UnsafeURL("non_public_host")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL("invalid_port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeURL("invalid_port")
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((parts.scheme.lower() == "http" and port == 80) or (parts.scheme.lower() == "https" and port == 443)):
        netloc += f":{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def _public_addresses(host: str, port: int) -> list[tuple]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeURL("dns_failure") from exc
    records = []
    seen = set()
    if not infos:
        raise UnsafeURL("dns_failure")
    for family, socktype, proto, _, sockaddr in infos:
        address = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        if not address.is_global:
            raise UnsafeURL("non_public_host")
        key = (family, sockaddr)
        if key not in seen:
            records.append((family, socktype, proto, sockaddr))
            seen.add(key)
    return records


def _request_once(url: str, *, accept: str = "text/html, text/plain;q=0.9") -> tuple[int, dict, bytes, str | None]:
    canonical = _canonical_url(url)
    parts = urlsplit(canonical)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    host = parts.hostname
    addresses = _public_addresses(host, port)
    last_error = None
    for family, socktype, proto, sockaddr in addresses:
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(TIMEOUT)
        try:
            sock.connect(sockaddr)
            if parts.scheme == "https":
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=host)
                sock.settimeout(TIMEOUT)
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
            if parts.scheme == "https":
                conn = http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
            conn.sock = sock
            conn.request("GET", path, headers={"Host": parts.netloc, "User-Agent": "WeChatDigestWebContext/1.0", "Accept": accept, "Accept-Encoding": "identity", "Connection": "close"})
            response = conn.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            location = headers.get("location")
            if response.status in {301, 302, 303, 307, 308}:
                response.close()
                conn.close()
                return response.status, headers, b"", location
            body = response.read(MAX_BYTES + 1)
            response.close()
            conn.close()
            if len(body) > MAX_BYTES:
                raise ValueError("response_too_large")
            return response.status, headers, body, None
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
            try:
                sock.close()
            except OSError:
                pass
        break
    if last_error:
        raise TimeoutError("network_error") from last_error
    raise TimeoutError("network_error")


def _fetch(url: str, *, check_robots: bool = False) -> tuple[int, dict, bytes, str]:
    current = _canonical_url(url)
    for redirect in range(MAX_REDIRECTS + 1):
        if check_robots and not _robots_allowed(current):
            raise ValueError("robots_disallowed_or_unavailable")
        status, headers, body, location = _request_once(current)
        if location is None:
            encoding = headers.get("content-encoding", "identity").strip().lower()
            if encoding not in {"", "identity"}:
                raise ValueError("unsupported_content_encoding")
            return status, headers, body, current
        if redirect >= MAX_REDIRECTS:
            raise ValueError("too_many_redirects")
        current = _canonical_url(urljoin(current, location))
    raise ValueError("too_many_redirects")


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.skip = 0
        self.in_title = False
        self.main_depth = 0
        self.selected: list[str] = []
        self.in_main = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag in {"script", "style", "nav", "noscript", "svg", "iframe", "form"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag in {"main", "article"}:
            self.in_main = True
            self.main_depth += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "section", "article", "main"}:
            self.parts.append("\n")
            if self.in_main:
                self.selected.append("\n")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if tag in {"main", "article"} and self.main_depth:
            self.main_depth -= 1
            self.in_main = self.main_depth > 0
        if tag in {"script", "style", "nav", "noscript", "svg", "iframe", "form"} and self.skip:
            self.skip -= 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "section", "article", "main"}:
            self.parts.append("\n")
            if self.in_main:
                self.selected.append("\n")
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if not self.skip and not self.in_title:
            self.parts.append(data)
            if self.in_main:
                self.selected.append(data)

    def values(self) -> tuple[str, str]:
        text = " ".join(("".join(self.selected) if self.selected else "".join(self.parts)).split())
        return " ".join("".join(self.title_parts).split())[:500], text


def _extract(headers: dict, body: bytes) -> tuple[str, str]:
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    charset = "utf-8"
    match = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", headers.get("content-type", ""), re.I)
    if match:
        try:
            "".encode(match.group(1))
            charset = match.group(1)
        except LookupError:
            pass
    decoded = body.decode(charset, errors="replace")
    if content_type in {"text/plain", "text/markdown"}:
        return "", " ".join(decoded.split())
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise ValueError("unsupported_content_type")
    parser = _TextExtractor()
    parser.feed(decoded)
    return parser.values()


def _cache_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json")


def _cache_read(path: Path, now: float | None = None) -> dict | None:
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get('extraction_version') != 2:
            return None
        stamp = dt.datetime.fromisoformat(item["fetched_at"].replace("Z", "+00:00")).timestamp()
        if isinstance(item, dict) and item.get("status") == "success" and item.get("text_complete") is not True:
            return None
        if isinstance(item, dict) and time.time() - stamp < CACHE_TTL and time.time() >= stamp:
            return item
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _cache_write(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".web-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(item, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _robots_allowed(url: str) -> bool:
    parts = urlsplit(url)
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    try:
        status, headers, body, _ = _fetch(robots_url)
        if status == 404:
            return True
        if status != 200 or headers.get("content-type", "").split(";", 1)[0].strip().lower() not in {"text/plain", ""}:
            return False
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        agent_groups: list[tuple[list[str], list[tuple[str, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[str, str]] = []
        saw_rule = False
        for line in body.decode("utf-8", errors="replace").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (bit.strip() for bit in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if saw_rule and agents:
                    agent_groups.append((agents, rules))
                    agents, rules, saw_rule = [], [], False
                agents.append(value.lower())
            elif key in {"allow", "disallow"} and agents:
                rules.append((key, value))
                saw_rule = True
        if agents:
            agent_groups.append((agents, rules))
        exact = [group_rules for group_agents, group_rules in agent_groups if "wechatdigestwebcontext" in group_agents]
        selected = exact or [group_rules for group_agents, group_rules in agent_groups if "*" in group_agents]
        decisions = []
        for group_rules in selected:
            for directive, pattern in group_rules:
                if not pattern:
                    continue
                end_anchored = pattern.endswith("$")
                literal = pattern[:-1] if end_anchored else pattern
                matcher = "^" + re.escape(literal).replace(r"\*", ".*") + ("$" if end_anchored else "")
                if re.match(matcher, path):
                    specificity = len(literal.replace("*", ""))
                    decisions.append((specificity, directive == "allow"))
        return not decisions or max(decisions)[1]
    except Exception:
        return False


def _looks_like_access_gate(url: str, title: str, text: str, body: bytes) -> bool:
    path = urlsplit(url).path.lower()
    if re.search(r"(?:^|/)(?:login|log-in|signin|sign-in|auth|captcha|verify|verification)(?:/|$)", path):
        return True
    content = (title + " " + text + " " + body.decode("utf-8", errors="ignore")).lower()
    if re.search(r"captcha|verify\s+you\s+are\s+human|checking\s+your\s+browser|sign\s+in\s+to\s+continue|log\s+in\s+to\s+continue|authentication\s+required|security\s+check", content):
        return True
    if re.search(r'unable to view details|requires wechat [0-9]|请在微信客户端打开|当前环境不支持|请启用javascript', content):
        return True
    return bool(re.search(r"<form\b[^>]*>.*?<input\b[^>]*type=[\"']password", content, re.S)
                and re.search(r"log\s*in|sign\s*in|password", content))


def _fetch_page(url: str) -> dict:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    result = {"url": url, "title": "", "text": "", "text_complete": False, "fetched_at": now, "content_sha256": "", "status": "unavailable", "extraction_version": 2}
    try:
        canonical = _canonical_url(url)
        status, headers, body, final_url = _fetch(canonical, check_robots=True)
        result["url"] = final_url
        if status != 200:
            result["reason"] = f"http_{status}"
            return result
        title, text = _extract(headers, body)
        if _looks_like_access_gate(final_url, title, text, body):
            result["reason"] = "login_or_verification_page"
            return result
        if not text:
            result["reason"] = "empty_content"
            return result
        if len(text.encode("utf-8")) > MAX_BYTES:
            result["reason"] = "too_large"
            return result
        result.update({"status": "success", "title": title, "text": text, "text_complete": True,
                       "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    except (UnsafeURL, TimeoutError, ValueError, OSError, ssl.SSLError, http.client.HTTPException) as exc:
        result["reason"] = "too_large" if str(exc) == "response_too_large" else (str(exc)[:120] or "fetch_failed")
    except Exception:
        result["reason"] = "fetch_failed"
    return result


def _urls(message: dict) -> list[str]:
    text = message.get("text") or ""
    raw = message.get("raw_content") or message.get("_source", {}).get("decoded_content") or ""
    found = URL_RE.findall(str(text))
    try:
        app_url = message_parse.parse_app_message(raw).get("app_url")
        if app_url:
            found.append(app_url)
    except Exception:
        pass
    output = []
    seen = set()
    for candidate in found:
        candidate = html.unescape(str(candidate)).rstrip(".,;:!?)]}，。；：！？、》」』")
        try:
            url = _canonical_url(candidate)
        except UnsafeURL as exc:
            url = ("blocked:credential-url:" + hashlib.sha256(candidate.encode("utf-8")).hexdigest()
                   if str(exc) == "credential_url" else candidate)
        if url not in seen:
            seen.add(url)
            output.append(url)
    return output


def _snapshot_messages(path: Path, start: int, end: int) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("messages", []) if isinstance(data, dict) else data
    return [row for row in rows if isinstance(row, dict) and start <= int(row.get("timestamp", row.get("create_time", 0)) or 0) < end]


def _database_messages(source_dir: Path, keys_path: Path, start: int, end: int, temp: Path) -> list[dict]:
    import digest
    keys = digest._load_keyfile(keys_path)
    groups, contacts, _, _ = digest._groups_for_source(source_dir, keys, temp)
    project = settings.load_project(required=True)
    group = digest.select_group(groups, project['group']['name'], project['group']['id'])
    result = []
    for db in sorted((source_dir / "message").glob("message_[0-9]*.db")):
        if not digest.decrypt_sync.key_for_rel(keys, db.relative_to(source_dir).as_posix()):
            continue
        decrypted = digest._decrypt(db, source_dir, keys, temp)
        if decrypted:
            result.extend(digest._message_rows(decrypted, group["group_id"], contacts, start, end))
    return result


def collect(messages: list[dict], cache_dir: Path, max_pages: int = 0) -> dict:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(cache_dir, 0o700)
    refs: list[tuple[str, str]] = []
    unique: list[str] = []
    seen = set()
    for msg in messages:
        uid = str(msg.get("message_uid", ""))
        if not uid:
            continue
        for url in _urls(msg):
            refs.append((uid, url))
            if url not in seen:
                seen.add(url)
                unique.append(url)
    cached_pages = {url: _cache_read(_cache_path(cache_dir, url)) for url in unique}
    uncached = [url for url in unique if cached_pages[url] is None]
    limit = len(uncached) if max_pages <= 0 else max_pages
    selected = set(uncached[:limit])
    by_url = {url: item for url, item in cached_pages.items() if item is not None}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_fetch_page, url): url for url in selected}
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            item = future.result()
            _cache_write(_cache_path(cache_dir, url), item)
            by_url[url] = item
    for url in unique:
        if url not in by_url:
            by_url[url] = {"url": url, "title": "", "text": "", "text_complete": False, "fetched_at": "", "content_sha256": "", "status": "pending"}
    output = {}
    for uid, url in refs:
        page = dict(by_url[url])
        output.setdefault(uid, {"status": "success", "source_kind": "web", "pages": []})
        output[uid]["pages"].append(page)
    for item in output.values():
        statuses = {page["status"] for page in item["pages"]}
        item["status"] = "pending" if "pending" in statuses else ("unavailable" if "unavailable" in statuses else "success")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db-dir", type=Path)
    parser.add_argument("--keys", type=Path, default=Path("/keys/all_keys.json"))
    parser.add_argument("--start", required=True, type=int)
    parser.add_argument("--end", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("/work/web-cache"))
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--sources-json", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.start >= args.end or args.max_pages < 0:
        raise SystemExit("invalid window or max-pages")
    if bool(args.sources_json) == bool(args.source_db_dir):
        raise SystemExit("provide exactly one of --sources-json or --source-db-dir")
    args.cache_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.cache_dir, 0o700)
    with tempfile.TemporaryDirectory(prefix=".web-context-", dir=args.cache_dir.parent) as temp_name:
        temp = Path(temp_name)
        os.chmod(temp, 0o700)
        if args.sources_json:
            messages = _snapshot_messages(args.sources_json, args.start, args.end)
        else:
            messages = _database_messages(args.source_db_dir, args.keys, args.start, args.end, temp)
        result = collect(messages, args.cache_dir, args.max_pages)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(args.output, 0o600)
    pages = [page for item in result.values() for page in item["pages"]]
    print(json.dumps({"messages": len(result), "pages": len(pages), "fetched": sum(p["status"] == "success" for p in pages),
                      "pending": sum(p["status"] == "pending" for p in pages), "unavailable": sum(p["status"] == "unavailable" for p in pages)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

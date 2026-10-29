#!/usr/bin/env python3
"""Retrieve message-bound full images using the official client's cached CDN route.

Wire-format reference: thisnick/agent-wechat, wechat-protocol/media.rs and
large_cdn_config.rs. This independent implementation does not log in, load the
client, change its files, or use UI coordinates. CDN credentials stay in memory.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

import settings
ROOT = settings.ROOT
CLIENT_VERSION = (-227271398) & 0xffffffff  # Pinned Linux 4.1.13.26 protocol version.
MAX_IMAGE_BYTES = 26_214_383
_PREPARER = None


def preparer():
    global _PREPARER
    if _PREPARER is None:
        spec = importlib.util.spec_from_file_location("original_image_preparer", Path(__file__).with_name("prepare-images.py"))
        _PREPARER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_PREPARER)
    return _PREPARER


def protobuf(data):
    if len(data) > 512 * 1024:
        raise ValueError("CDN configuration exceeds the supported bound")
    p = preparer()
    result, at = [], 0
    while at < len(data):
        tag, at = p.varint(data, at)
        number, wire = tag >> 3, tag & 7
        if not number or number > 10000:
            raise ValueError("Invalid CDN configuration tag")
        if wire == 0:
            value, at = p.varint(data, at)
        elif wire == 2:
            value, at = p.span(data, at)
        else:
            raise ValueError("Unsupported CDN configuration layout")
        result.append((number, wire, value))
        if len(result) > 10000:
            raise ValueError("Too many CDN configuration fields")
    return result


def one(fields, number, wire, default=None):
    values = [v for n, w, v in fields if n == number and w == wire]
    if not values and default is not None:
        return default
    if len(values) != 1:
        raise ValueError("Missing or ambiguous CDN configuration field")
    return values[0]


def image_route(source, filetype):
    p = preparer()
    image_key, _ = p.image_key(source)
    usernames = {source.parent.name, source.parent.name.rsplit("_", 1)[0]}
    cache = Path("/config/.xwechat/net/cdncomm/cdninfo_new.cache").read_bytes()
    bound = []
    for number, wire, raw in protobuf(cache):
        if number != 1 or wire != 2:
            continue
        record = protobuf(raw)
        table = protobuf(one(record, 7, 2))
        uin = one(table, 2, 0)
        if any(hashlib.md5((str(uin) + user).encode()).hexdigest()[:16] == image_key for user in usernames):
            bound.append(table)
    if len(bound) != 1:
        raise ValueError("CDN cache is not uniquely bound to the selected account")
    table = bound[0]
    rules = []
    for number, wire, raw in table:
        if number != 8 or wire != 2:
            continue
        rule = protobuf(raw)
        if any(n not in range(1, 7) or w != 0 for n, w, _ in rule):
            raise ValueError("Unsupported CDN routing predicate")
        if all(one(rule, n, 0, 0) in (0, actual) for n, actual in ((2, 1), (3, 1), (4, filetype), (5, 2))):
            sequence = one(rule, 1, 0) & 0xffffffff
            if sequence & (1 << 31):
                sequence -= 1 << 32
            rules.append((sequence, one(rule, 6, 0)))
    rules.sort()
    if not rules or sum(rank == rules[0][0] for rank, _ in rules) != 1:
        raise ValueError("Missing or ambiguous original-image CDN route")
    configs = [protobuf(raw) for n, w, raw in table if n == 7 and w == 2]
    configs = [c for c in configs if one(c, 1, 0) == rules[0][1]]
    if len(configs) != 1:
        raise ValueError("CDN route target is ambiguous")
    config = configs[0]
    auth = one(config, 8, 2)
    ips = []
    for n, w, value in config:
        if n != 3 or w != 0:
            continue
        low = value & 0xffffffff
        signed = low if low < (1 << 31) else low + 0xffffffff00000000
        if value not in (low, signed):
            raise ValueError("Invalid CDN endpoint integer")
        ip = ipaddress.IPv4Address(struct.pack("<I", low))
        if not ip.is_global:
            raise ValueError("CDN endpoint is not public IPv4")
        ips.append(str(ip))
    if not ips or len(ips) > 32 or not 0 < len(auth) <= 65536 or 443 not in [v for n, w, v in config if n == 7 and w == 0]:
        raise ValueError("Invalid CDN route credentials or endpoint")
    return {"version": one(table, 1, 0), "uin": one(table, 2, 0), "auth": auth, "server_ips": ips}


def pack_fields(fields):
    return b"".join(struct.pack(">I", len(part)) + part
                    for key, value in sorted(fields.items()) for part in (key.encode("ascii"), value))


def unpack_fields(raw):
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("CDN range response exceeds the supported bound")
    fields, at = {}, 0
    while at < len(raw):
        parts = []
        for _ in range(2):
            if at + 4 > len(raw):
                raise ValueError("Truncated CDN field length")
            size = struct.unpack_from(">I", raw, at)[0]
            at += 4
            if size > len(raw) - at:
                raise ValueError("Truncated CDN field")
            parts.append(raw[at:at + size])
            at += size
        key = parts[0].decode("ascii")
        if not key or len(key) > 128 or not all(c.isalnum() or c in "_-" for c in key) or key in fields:
            raise ValueError("Invalid or duplicate CDN field")
        fields[key] = parts[1]
        if len(fields) > 256:
            raise ValueError("Too many CDN response fields")
    return fields


def number(fields, key):
    value = fields.get(key, b"")
    if not value or len(value) > 20 or not value.lstrip(b"-").isdigit():
        raise ValueError("Missing or invalid CDN response number")
    return int(value)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def download(route, descriptor, deadline):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    size = descriptor["expected_length"]
    expected_ciphertext = (size // 16 + 1) * 16
    original = descriptor['filetype'] == 1
    total = expected_ciphertext if original else None
    representation = None
    server_size = None
    server_md5 = None
    fields = {k: str(v).encode() for k, v in {
        "ver": route["version"], "weixinnum": route["uin"], "clientversion": CLIENT_VERSION,
        "nettype": 1, "acceptdupack": 1, "safeproto": 1, "filetype": descriptor["filetype"],
        "wxchattype": 0, "lastretcode": 0, "ipseq": 0, "largesvideo": 0,
        # Standard images may be a different representation from the original.
        "sourceflag": 0, "wxautostart": 0, "downpicformat": 4,
    }.items()}
    fields.update(clientostype=b"UnifiedPCLinux", authkey=route["auth"], wxmsgflag=b"", fileid=descriptor["fileid"].encode())
    encrypted = bytearray()
    opener = urllib.request.build_opener(NoRedirect())
    sequence = 0
    while total is None or len(encrypted) < total:
        sequence += 1
        if sequence > ((MAX_IMAGE_BYTES // 16 + 1) * 16 + 65535) // 65536:
            raise ValueError('CDN transfer exceeds the supported bound')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Original-image download deadline exceeded")
        start = len(encrypted)
        end = start + 65535 if total is None else min(start + 65535, total - 1)
        fields.update(seq=str(sequence).encode(), rangestart=str(start).encode(), rangeend=str(end).encode())
        body = pack_fields(fields)
        failure = None
        for endpoint in tuple(route["server_ips"]):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Original-image download deadline exceeded")
            request = urllib.request.Request("http://" + endpoint + ":443/download", data=body,
                headers={"Accept": "*/*", "Content-Type": "application/octet-stream", "User-Agent": "MicroMessenger Client",
                         "Connection": "close", "C-Seq": str(sequence)}, method="POST")
            try:
                with opener.open(request, timeout=min(15, remaining)) as response:
                    if response.status != 200:
                        raise RuntimeError(f"CDN HTTP status {response.status}")
                    reply = unpack_fields(response.read(2 * 1024 * 1024 + 1))
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"CDN HTTP status {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                failure = exc
                continue
            route["server_ips"].remove(endpoint)
            route["server_ips"].insert(0, endpoint)
            break
        else:
            reason = failure.reason if isinstance(failure, urllib.error.URLError) else failure
            raise RuntimeError(f"All configured CDN endpoints failed ({type(reason).__name__})") from None
        code = number(reply, "retcode")
        if code:
            raise RuntimeError(f"CDN original-image download returned {code}")
        block = reply.get('filedata', b'')
        returned_total = number(reply, 'totalsize')
        returned_end = number(reply, 'rangeend')
        if (number(reply, 'seq') != sequence or number(reply, 'rangestart') != start
                or not block or not 0 < returned_total <= (MAX_IMAGE_BYTES // 16 + 1) * 16
                or returned_total % 16 or returned_end != start + len(block) - 1
                or returned_end > end or returned_end >= returned_total
                or total is not None and total != returned_total):
            raise ValueError('CDN range correlation or size changed')
        total = returned_total
        current_representation = {key: number(reply, key) if key in reply else None
                                  for key in ('substituteftype', 'rsppicformat')}
        if representation is not None and representation != current_representation:
            raise ValueError('CDN representation changed during transfer')
        representation = current_representation
        if not original and representation['substituteftype'] not in (2, 12):
            raise ValueError('CDN did not return a complete standard image')
        if 'rawfilesize' in reply:
            value = number(reply, 'rawfilesize')
            if server_size is not None and value != server_size:
                raise ValueError('CDN source size changed during transfer')
            server_size = value
        if reply.get('rawfilemd5'):
            value = reply['rawfilemd5'].decode('ascii').lower()
            if len(value) != 32 or any(c not in '0123456789abcdef' for c in value) or server_md5 is not None and value != server_md5:
                raise ValueError('CDN source checksum changed during transfer')
            server_md5 = value
        encrypted.extend(block)
    if total is None or len(encrypted) != total:
        raise ValueError('CDN transfer is incomplete')
    plain = unpad(AES.new(descriptor['key'], AES.MODE_ECB).decrypt(encrypted), 16)
    digest = hashlib.md5(plain).hexdigest()
    original_matches = len(plain) == size and digest == descriptor['expected_md5']
    if not 0 < len(plain) <= MAX_IMAGE_BYTES or original and not original_matches:
        raise ValueError('Downloaded original failed message length or MD5 verification')
    if original and ((server_size is not None and len(plain) != server_size) or (server_md5 is not None and digest != server_md5)):
        raise ValueError('Downloaded image failed server integrity verification')
    return plain, {'ciphertext_bytes': total, 'requested_filetype': descriptor['filetype'],
                   **representation, 'original_md5_matches': original_matches,
                   'reported_source_bytes': server_size, 'reported_source_md5': server_md5}


def private_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def safe_request(path, project=None):
    project = project or settings.load_project(required=True)
    request = json.loads(path.read_text(encoding="utf-8"))
    if request.get("version") != 1 or request.get("chat_id") != project["group"]["id"] or request.get("chat_name") != project["group"]["name"]:
        raise ValueError("Original-image request has the wrong version or target")
    targets = request.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("Original-image request has no targets")
    if len({t["message_uid"] for t in targets}) != len(targets):
        raise ValueError("Duplicate original-image request identity")
    return request


def worker(path):
    import digest
    import media_sync

    os.umask(0o077)
    request = safe_request(path)
    selection = json.loads(Path("/runtime/selection.json").read_text())
    if request["source_db_dir"] != selection["source_db_dir"]:
        raise ValueError("Original-image request does not match the selected profile")
    source = Path(selection['source_db_dir'])
    targets = {t["message_uid"]: t for t in request["targets"]}
    keys = digest._load_keyfile(Path("/keys/all_keys.json"))
    descriptors = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        groups, contacts, _, _ = digest._groups_for_source(source, keys, tmp)
        project = settings.load_project(required=True)
        group = digest.select_group(groups, project["group"]["name"], project["group"]["id"])
        earliest = min(t["timestamp"] for t in targets.values())
        latest = max(t["timestamp"] for t in targets.values()) + 1
        for db in (source / "message").glob("message_[0-9]*.db"):
            plain = digest._decrypt(db, source, keys, tmp)
            if not plain:
                raise ValueError("An active message shard cannot be read")
            for message in digest._message_rows(plain, group["group_id"], contacts, earliest, latest):
                uid = message["message_uid"]
                if uid not in targets:
                    continue
                if message["type"] != "image":
                    raise ValueError("Requested message is not an image")
                xml = media_sync.parse_xml_payload(message["raw_content"])
                image = xml.find(".//img") if xml is not None else None
                if image is None:
                    raise ValueError("Image message has no download descriptor")
                attrs = image.attrib
                original = bool(attrs.get("cdnbigimgurl"))
                fileid = attrs.get("cdnbigimgurl") or attrs.get("cdnmidimgurl")
                size = int(attrs.get("hdlength") or 0) if original else int(attrs.get("length") or 0)
                md5 = attrs.get("md5", "").lower()
                key = bytes.fromhex(attrs.get("aeskey", ""))
                if not fileid or len(fileid) > 8192 or len(key) != 16 or not 0 < size <= MAX_IMAGE_BYTES:
                    raise ValueError("Unsupported or incomplete full-image descriptor")
                if len(md5) != 32 or any(c not in "0123456789abcdef" for c in md5):
                    raise ValueError("Image message has no valid source checksum")
                if targets[uid].get("expected_md5") != md5 or targets[uid].get("expected_length") != size:
                    raise ValueError("Original-image request became stale")
                descriptors[uid] = {"fileid": fileid, "key": key, "expected_length": size, "expected_md5": md5,
                                    'descriptor_sha256': preparer().descriptor_fingerprint(attrs),
                                    "filetype": 1 if original else 2, "quality": "original" if original else "client_full"}
    if set(descriptors) != set(targets):
        raise ValueError("Not every requested image is present in the selected group")
    routes = {kind: image_route(source, kind) for kind in {d["filetype"] for d in descriptors.values()}}
    directory = Path("/work/downloaded-originals")
    directory.mkdir(mode=0o700, exist_ok=True)
    index_path = Path("/work/downloaded-originals.json")
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    completed = []
    deadline = time.monotonic() + 600
    for uid, descriptor in descriptors.items():
        raw, transfer = download(routes[descriptor['filetype']], descriptor, deadline)
        fmt = preparer().source_image_format(raw)
        if fmt not in {"png", "jpg", "gif", "webp", "bmp", "tif", "hevc", "heic"}:
            raise ValueError("Downloaded image format is not supported by the pinned decoder")
        _, _, dimensions = preparer().model_image(raw, fmt)
        filename = hashlib.sha256(raw).hexdigest() + ('.wxgf' if fmt == 'hevc' else '.' + fmt)
        destination = directory / filename
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(raw)
        temporary.chmod(0o600)
        temporary.replace(destination)
        index[uid] = {"message_uid": uid, "path": str(destination), "source_format": fmt,
                      "source_bytes": len(raw), "source_sha256": hashlib.sha256(raw).hexdigest(),
                      "message_md5": descriptor["expected_md5"], "source_quality": descriptor["quality"],
                      'descriptor_sha256': descriptor['descriptor_sha256'], 'transfer': transfer,
                      "dimensions": list(dimensions), "transport": "official-cdn", "fetched_at": int(time.time())}
        private_json(index_path, index)
        completed.append({"message_uid": uid, "source_bytes": len(raw), "dimensions": list(dimensions),
                          'source_quality': descriptor['quality'], 'message_md5_verified': transfer['original_md5_matches'],
                          'complete_transfer_verified': True})
    return {"status": "fetched", "requested": len(targets), "fetched": len(completed), "images": completed}


def host(path):
    path = path.resolve(strict=True)
    root = settings.RUNTIME / "digest"
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Original-image request must be inside the private digest directory")
    project = settings.load_project(required=True)
    request = safe_request(path, project)
    selection_path = settings.RUNTIME / "selection.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    selected = selection.get("source_db_dir")
    if not isinstance(selected, str) or not selected.startswith("/config/"):
        raise ValueError("Selected source database is not a configured profile path")
    relative = Path(selected.removeprefix("/config/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Selected source database path is unsafe")
    source_db = (settings.RUNTIME / "gui" / relative).resolve(strict=True)
    gui_root = (settings.RUNTIME / "gui").resolve(strict=True)
    if not source_db.is_relative_to(gui_root) or not source_db.is_dir():
        raise ValueError("Selected source database is outside the configured profile")
    if request["source_db_dir"] != selected:
        raise ValueError("Original-image request does not match the selected profile")
    image = settings.runtime_image("runtime")
    name = "wechat-original-" + uuid.uuid4().hex[:12]
    command = ["docker", "run", "--rm", "--pull", "never", "--name", name, "--network", project["report_prefix"] + "_default",
               "--read-only", "--user", f"{project['worker_uid']}:{project['worker_gid']}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
               "--memory", "1g", "--cpus", "1", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
               "--env", "PYTHONPATH=/source/memory:/tool:/tool/vendor", "--env", "WECHAT_DIGEST_CONFIG=/runtime/project.json"]
    mounts = [(ROOT / "deployment", "/tool", True), (ROOT / "memory", "/source/memory", True),
              (settings.RUNTIME / "project.json", "/runtime/project.json", True),
              (selection_path, "/runtime/selection.json", True),
              (settings.RUNTIME / "keyscan/all_keys.json", "/keys/all_keys.json", True),
              (gui_root, "/config", True), (root, "/work", False)]
    for source, target, readonly in mounts:
        command += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]
    command += [image, "/tool/fetch-originals.py", "--worker", str(Path("/work") / path.relative_to(root))]
    lock_path = root / "original-download.lock"
    with lock_path.open("a+") as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=660)
        except BaseException:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
            raise
    if result.returncode:
        raise RuntimeError((result.stdout or result.stderr or "Original-image worker failed")[-3000:])
    answer = json.loads(result.stdout.strip().splitlines()[-1])
    if answer.get("status") != "fetched" or answer.get("fetched") != answer.get("requested"):
        raise ValueError("Original-image worker did not verify all requested files")
    return answer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--request", type=Path)
    mode.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        answer = worker(args.worker) if args.worker else host(args.request)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps(answer, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

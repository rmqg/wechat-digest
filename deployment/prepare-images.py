#!/usr/bin/env python3
"""Decode only the selected group's local pictures using its offline MMKV identity."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import zlib
from Crypto.Cipher import AES
from PIL import Image
import media_sync
import settings

# Application format constant for the pinned Linux 4.1.13.26 MMKV layout;
# not an account password. Reference: thisnick/agent-wechat wechat-core/format.rs.
FORMAT_KEY = b"xwechat_crypt_ke"


def varint(data, offset):
    value = 0
    for index in range(10):
        if offset >= len(data):
            raise ValueError("Truncated MMKV value")
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << (index * 7)
        if not byte & 128:
            return value, offset
    raise ValueError("Invalid MMKV varint")


def span(data, offset):
    size, offset = varint(data, offset)
    end = offset + size
    if end > len(data):
        raise ValueError("Truncated MMKV span")
    return data[offset:end], end


def image_key(source):
    config = source.parent.parent / "all_users/config"
    data = (config / "global_config").read_bytes()
    metadata = (config / "global_config.crc").read_bytes()
    if len(metadata) < 40 or struct.unpack_from("<I", metadata, 4)[0] != 4:
        raise ValueError("Unsupported MMKV identity metadata")
    size = struct.unpack_from("<I", metadata, 28)[0]
    encrypted = data[4:4 + size]
    if len(encrypted) != size or zlib.crc32(encrypted) & 0xffffffff != struct.unpack_from("<I", metadata)[0]:
        raise ValueError("MMKV identity snapshot CRC mismatch")
    plain = AES.new(FORMAT_KEY, AES.MODE_CFB, iv=metadata[12:28], segment_size=128).decrypt(encrypted)
    _, at = varint(plain, 0)
    entries = {}
    while at < len(plain):
        key, at = span(plain, at)
        value, at = span(plain, at)
        if value:
            entries[key.decode()] = value
        else:
            entries.pop(key.decode(), None)
    username_raw, _ = span(entries["mmkv_key_user_name"], 0)
    username = username_raw.decode()
    uin, end = varint(entries["mmkv_key_latest_login_uin"], 0)
    if end != len(entries["mmkv_key_latest_login_uin"]) or not uin or uin > 0xffffffff:
        raise ValueError("Invalid image identity")
    if not (source.parent.name == username or source.parent.name.startswith(username + "_")):
        raise ValueError("Image identity does not match the selected account")
    aes = hashlib.md5((str(uin) + username).encode()).hexdigest()[:16]
    xor = next(((uin >> shift) & 255 for shift in range(4) if (uin >> shift) & 255), 0)
    return aes, xor


def image_aliases(md5, attributes):
    aliases = {md5} if md5 else set()
    original = attributes.get("md5")
    if isinstance(original, str) and len(original) == 32 and all(c in "0123456789abcdefABCDEF" for c in original):
        aliases.add(original.lower())
    return aliases


def descriptor_fingerprint(attributes):
    fields = {key: str(attributes.get(key) or '') for key in
              ('md5', 'aeskey', 'cdnbigimgurl', 'cdnmidimgurl', 'hdlength', 'length')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_downloaded_image(cached, payload, attributes):
    expected_length = int(attributes.get('hdlength') or 0) or int(attributes.get('length') or 0)
    original_matches = (len(payload) == expected_length
                        and hashlib.md5(payload).hexdigest() == str(attributes.get('md5') or '').lower())
    if len(payload) != cached.get('source_bytes') or hashlib.sha256(payload).hexdigest() != cached.get('source_sha256'):
        raise ValueError('downloaded image bytes changed after verification')
    quality = cached.get('source_quality')
    if quality == 'original':
        if not original_matches:
            raise ValueError('downloaded original no longer matches its message')
    elif quality == 'client_full':
        if not original_matches:
            transfer = cached.get('transfer') or {}
            if (attributes.get('cdnbigimgurl') or cached.get('descriptor_sha256') != descriptor_fingerprint(attributes)
                    or transfer.get('requested_filetype') != 2 or transfer.get('substituteftype') not in (2, 12)
                    or transfer.get('ciphertext_bytes') != (len(payload) // 16 + 1) * 16):
                raise ValueError('client-full download is not bound to this complete message asset')
    else:
        raise ValueError('downloaded image has an unsupported quality')
    return original_matches


def full_quality(path, md5, data, attributes):
    if path.name not in {f"{alias}{suffix}.dat" for alias in image_aliases(md5, attributes) for suffix in ("", "_h", "_W")}:
        return None
    expected_length = int(attributes.get("hdlength") or 0) or int(attributes.get("length") or 0)
    if (expected_length == len(data)
            and str(attributes.get("md5") or "").lower() == hashlib.md5(data).hexdigest()):
        return "original"
    return "client_full"


def requires_download(item, original_advertised):
    return not item["available"] or (original_advertised and item.get("source_quality") != "original")


def source_image_format(data):
    known = media_sync.detect_image_format(data)
    if known:
        return known
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"heic", b"heix", b"hevc", b"hevx"}:
        return "heic"
    return None


def model_image(data, fmt):
    if fmt == "hevc":
        offset = data.find(b"\x00\x00\x00\x01\x40\x01")
        if offset < 0:
            raise ValueError("WXGF image has no HEVC video parameter set")
        decoded = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "hevc", "-i", "pipe:0",
             "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1"],
            input=data[offset:], capture_output=True, timeout=30)
        if decoded.returncode:
            raise ValueError("WXGF image could not be decoded by the pinned runtime")
        data, fmt = decoded.stdout, "png"
    elif fmt == "heic":
        from PIL import ImageOps
        from pillow_heif import register_heif_opener
        register_heif_opener()
        output = io.BytesIO()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            ImageOps.exif_transpose(image, in_place=True)
            image.save(output, format="PNG")
        data, fmt = output.getvalue(), "png"
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        dimensions = image.size
    return data, fmt, dimensions


def cached_model_image(data, fmt, cache_dir):
    """Reuse only byte-verified conversions of the exact complete source image."""
    source_sha = hashlib.sha256(data).hexdigest()
    metadata_path = cache_dir / f"{source_sha}.json"
    try:
        cached = json.loads(metadata_path.read_text())
        model_format = cached["format"]
        if cached.get("version") != 1 or cached.get("source_format") != fmt or model_format not in {"png", "jpg", "gif", "webp", "bmp", "tif"}:
            raise ValueError("cached conversion metadata differs")
        path = cache_dir / f"{source_sha}.{model_format}"
        converted = path.read_bytes()
        if hashlib.sha256(converted).hexdigest() != cached["sha256"]:
            raise ValueError("cached conversion fingerprint changed")
        dimensions = tuple(cached["dimensions"])
        if len(dimensions) != 2 or any(type(value) is not int or value <= 0 for value in dimensions):
            raise ValueError("invalid cached image dimensions")
        return converted, model_format, dimensions, path
    except (OSError, ValueError, KeyError, TypeError):
        pass
    converted, model_format, dimensions = model_image(data, fmt)
    path = cache_dir / f"{source_sha}.{model_format}"
    for target, payload in ((path, converted), (metadata_path, json.dumps({
            "version": 1, "source_format": fmt, "format": model_format, "dimensions": list(dimensions),
            "sha256": hashlib.sha256(converted).hexdigest()}, ensure_ascii=False).encode())):
        fd, temporary = tempfile.mkstemp(prefix=".convert-", dir=cache_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return converted, model_format, dimensions, path


def file_signature(path):
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    return [str(resolved), info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def prepared_cache_hit(entry, signature, output_dir):
    if not isinstance(entry, dict) or entry.get("inputs") != signature:
        return None
    item = entry.get("item")
    if not isinstance(item, dict) or not item.get("available") or item.get("download_required"):
        return None
    try:
        path = Path(item["image_path"]).resolve(strict=True)
        if path.is_relative_to(output_dir.resolve()) and file_signature(path) == entry.get("output"):
            return dict(item)
    except (OSError, ValueError, KeyError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db-dir", required=True, type=Path)
    parser.add_argument("--keys", default="/keys/all_keys.json", type=Path)
    parser.add_argument("--runtime-dir", default="/work", type=Path)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--end")
    parser.add_argument("--start")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--download-request", type=Path)
    parser.add_argument("--exclude-server-ids", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    source = args.source_db_dir
    keys = digest._load_keyfile(args.keys)
    output = args.runtime_dir / "images" / "decoded"
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    downloaded_index = args.runtime_dir / "downloaded-originals.json"
    downloaded = json.loads(downloaded_index.read_text()) if downloaded_index.exists() else {}
    prepared_cache_path = args.runtime_dir / "prepared-image-cache.json"
    prepared_cache = json.loads(prepared_cache_path.read_text()) if prepared_cache_path.exists() else {}
    manifest = []
    download_targets = []
    excluded = {str(value) for value in json.loads(args.exclude_server_ids.read_text())} if args.exclude_server_ids else set()
    with tempfile.TemporaryDirectory(prefix=".images-", dir=args.runtime_dir) as temporary:
        temp = Path(temporary)
        groups, contacts, _, _ = digest._groups_for_source(source, keys, temp)
        project = settings.load_project(required=True)
        group = digest.select_group(groups, project["group"]["name"], project["group"]["id"])
        end = digest._parse_time(args.end, int(time.time()))
        start = digest._parse_time(args.start, end - int(args.hours * 3600)) if args.start else end - int(args.hours * 3600)
        rows = []
        for path in sorted((source / "message").glob("message_[0-9]*.db")):
            if not digest.decrypt_sync.key_for_rel(keys, path.relative_to(source).as_posix()):
                raise RuntimeError("a current message shard has no validated key")
            database = digest._decrypt(path, source, keys, temp)
            if database:
                rows.extend(row for row in digest._message_rows(database, group["group_id"], contacts, start, end) if row["type"] == "image")
        rows = [row for row in rows if str(row.get("server_id")) not in excluded]
        if rows:
            resource = digest._decrypt(source / "message/message_resource.db", source, keys, temp)
            resources = media_sync.load_resource_map(resource)
            aes, xor = image_key(source)
            for row in rows:
                md5 = resources.get((group["group_id"], int(row["local_id"])))
                xml = media_sync.parse_xml_payload(row["raw_content"])
                image_node = xml.find(".//img") if xml is not None else None
                attributes = image_node.attrib if image_node is not None else {}
                aliases = image_aliases(md5, attributes)
                if not md5 and aliases:
                    md5 = sorted(aliases)[0]
                candidates = [path for alias in aliases for path in media_sync.find_dat_files(source.parent, group["group_id"], alias)]
                choices = []
                failures = []
                cached = downloaded.get(row["message_uid"])
                inputs = {"version": 1, "account": str(source), "message_sha256": hashlib.sha256(row["raw_content"].encode()).hexdigest(),
                          "resource_md5": md5, "files": [file_signature(path) for path in sorted(candidates)],
                          "download_receipt": cached}
                if cached and isinstance(cached.get("path"), str):
                    try:
                        inputs["downloaded_file"] = file_signature(Path(cached["path"]))
                    except OSError:
                        inputs["downloaded_file"] = None
                ready = prepared_cache_hit(prepared_cache.get(row["message_uid"]), inputs, output)
                if ready:
                    manifest.append(ready)
                    continue
                if cached:
                    try:
                        original_path = Path(cached["path"]).resolve(strict=True)
                        if not original_path.is_relative_to((args.runtime_dir / "downloaded-originals").resolve()):
                            raise ValueError("downloaded original is outside its private directory")
                        payload = original_path.read_bytes()
                        original_matches = validate_downloaded_image(cached, payload, attributes)
                        quality = cached['source_quality']
                        source_format = source_image_format(payload)
                        data, fmt, dimensions, model_path = cached_model_image(payload, source_format, output)
                        evidence = {"cache_file": original_path.name, "source_format": source_format,
                                    "source_bytes": len(payload), "source_sha256": cached["source_sha256"],
                                    'message_md5_matches': original_matches, 'transport': 'official-cdn',
                                    "converted_without_resizing": source_format in {"hevc", "heic"}}
                        choices.append((quality == "original", True, dimensions[0] * dimensions[1],
                                        dimensions, data, fmt, quality, evidence, model_path))
                    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                        failures.append(f"owned original: {exc}")
                for original in candidates:
                    if original.name not in {f"{alias}{suffix}.dat" for alias in aliases for suffix in ("", "_h", "_W")}:
                        continue
                    try:
                        payload, source_format = media_sync.decrypt_dat(original, aes, xor)
                        if not payload:
                            raise ValueError("full image cache could not be decoded")
                        quality = full_quality(original, md5, payload, attributes)
                        data, fmt, dimensions, model_path = cached_model_image(payload, source_format, output)
                        evidence = {"cache_file": original.name, "source_format": source_format,
                                    "source_bytes": len(payload), "source_sha256": hashlib.sha256(payload).hexdigest(),
                                    "message_md5_matches": hashlib.md5(payload).hexdigest() == str(attributes.get("md5") or "").lower(),
                                    "converted_without_resizing": source_format == "hevc"}
                        choices.append((quality == "original", original.name.endswith("_h.dat"),
                                        dimensions[0] * dimensions[1], dimensions, data, fmt, quality, evidence, model_path))
                    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                        failures.append(f"{original.name}: {exc}")
                item = {"message_uid": row["message_uid"], "md5": md5, "timestamp": row["timestamp"], "available": bool(choices)}
                if choices:
                    _, _, _, dimensions, data, fmt, quality, evidence, path = max(choices, key=lambda value: value[:3])
                    item.update(image_path=str(path), dimensions=list(dimensions), source_quality=quality,
                                sha256=hashlib.sha256(data).hexdigest(), provenance=evidence)
                else:
                    item.update(source_quality="unavailable", reason="Full image is not cached; thumbnails are not used",
                                decode_errors=failures)
                original_advertised = bool(attributes.get("cdnbigimgurl"))
                item["original_md5"] = attributes.get("md5")
                item["download_required"] = requires_download(item, original_advertised)
                item["original_advertised"] = original_advertised
                if item["download_required"] and md5:
                    download_targets.append({"message_uid": row["message_uid"], "md5": md5,
                                             "timestamp": row["timestamp"], "local_id": row["local_id"],
                                             "server_id": str(row["server_id"]),
                                             "original_advertised": original_advertised,
                                             "expected_md5": str(attributes.get("md5") or "").lower(),
                                             'descriptor_sha256': descriptor_fingerprint(attributes),
                                             "expected_length": int(attributes.get("hdlength") or 0) or int(attributes.get("length") or 0)})
                if item["available"] and not item["download_required"]:
                    prepared_cache[row["message_uid"]] = {"inputs": inputs, "output": file_signature(Path(item["image_path"])), "item": item}
                manifest.append(item)
    fd, temporary = tempfile.mkstemp(prefix=".prepared-images-", dir=args.runtime_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(prepared_cache, stream, ensure_ascii=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, prepared_cache_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    path = args.manifest or args.runtime_dir / "image-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o600)
    request_path = args.download_request or args.runtime_dir / "original-download-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    request_path.write_text(json.dumps({"version": 1, "chat_id": project["group"]["id"], "chat_name": project["group"]["name"],
                                        "source_db_dir": str(source), "start": start, "end": end,
                                        "targets": download_targets}, ensure_ascii=False), encoding="utf-8")
    os.chmod(request_path, 0o600)
    print(json.dumps({"image_messages": len(manifest), "decoded_images": sum(item["available"] for item in manifest), "manifest": str(path), "keys_printed": False}), flush=True)


if __name__ == "__main__":
    main()

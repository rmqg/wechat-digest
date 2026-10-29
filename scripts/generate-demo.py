#!/usr/bin/env python3
"""Generate deterministic, wholly fictional WeChat-digest demonstration artifacts."""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "examples" / "synthetic"
TEST_KEY = b"PUBLIC-TEST-ONLY-not-a-real-WeChat-key"
TEST_HMAC = "PUBLIC TEST-ONLY — synthetic fixture integrity material; never valid for real data"
FIXTURE_SALT = bytes.fromhex("00112233445566778899aabbccddeeff")
sys.path.insert(0, str(ROOT))
from agent_console import daily_report


def _save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def _exec_sqlite(lib, db, sql: str) -> None:
    error = ctypes.c_char_p()
    result = lib.sqlite3_exec(db, sql.encode("utf-8"), None, None, ctypes.byref(error))
    if result:
        detail = error.value.decode("utf-8", "replace") if error.value else f"SQLite error {result}"
        if error.value:
            lib.sqlite3_free(error)
        raise RuntimeError(detail)


def _create_reserved_sqlite(path: Path) -> None:
    library = ctypes.util.find_library("sqlite3")
    if not library:
        raise RuntimeError("SQLite shared library is required to create reserve-80 fixture pages")
    lib = ctypes.CDLL(library)
    lib.sqlite3_open.argtypes = (ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p))
    lib.sqlite3_open.restype = ctypes.c_int
    lib.sqlite3_close.argtypes = (ctypes.c_void_p,)
    lib.sqlite3_file_control.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
    lib.sqlite3_exec.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p))
    lib.sqlite3_exec.restype = ctypes.c_int
    lib.sqlite3_free.argtypes = (ctypes.c_void_p,)
    database = ctypes.c_void_p()
    result = lib.sqlite3_open(str(path).encode("utf-8"), ctypes.byref(database))
    if result:
        raise RuntimeError(f"sqlite3_open failed: {result}")
    try:
        _exec_sqlite(lib, database, "PRAGMA page_size=4096")
        reserve = ctypes.c_int(80)
        result = lib.sqlite3_file_control(database, b"main", 38, ctypes.byref(reserve))
        if result:
            raise RuntimeError(f"SQLite SQLITE_FCNTL_RESERVE_BYTES failed: {result}")
        _exec_sqlite(lib, database, "CREATE TABLE synthetic_messages (message_id TEXT PRIMARY KEY, participant TEXT NOT NULL, body TEXT NOT NULL)")
        _exec_sqlite(lib, database, "INSERT INTO synthetic_messages VALUES ('SYN-M001', '林小满', '合成测试消息：周六上午见。'), ('SYN-M002', '周禾', 'PUBLIC TEST-ONLY key fixture。')")
    finally:
        lib.sqlite3_close(database)
    header = path.read_bytes()[:100]
    if len(header) != 100 or header[:16] != b"SQLite format 3\x00" or header[16:18] != b"\x10\x00" or header[20] != 80:
        raise RuntimeError("SQLite fixture must have a 4096-byte page size and exactly 80 reserved bytes")


def _image(path: Path) -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError("Pillow is required to generate the synthetic PNG illustration") from exc
    image = Image.new("RGB", (720, 360), "#f5f8fc")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((22, 22, 698, 338), radius=24, fill="#ffffff", outline="#b7c8df", width=3)
    draw.ellipse((68, 80, 208, 220), fill="#d8e9ff", outline="#6288b8", width=4)
    draw.ellipse((100, 110, 176, 186), fill="#81addb")
    draw.polygon([(112, 190), (164, 190), (138, 236)], fill="#81addb")
    draw.rounded_rectangle((248, 82, 620, 144), radius=18, fill="#e7f2ff")
    draw.rounded_rectangle((248, 166, 560, 228), radius=18, fill="#eaf5eb")
    draw.rounded_rectangle((248, 250, 624, 296), radius=18, fill="#fff1d9")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=False)


def _records() -> tuple[dict, dict, dict]:
    start = 1793145600  # 2026-10-28T00:00:00Z
    end = start + 86400
    participants = [
        {"account_id": "synthetic-alex", "name": "林小满", "count": 2},
        {"account_id": "synthetic-bo", "name": "周禾", "count": 2},
        {"account_id": "synthetic-cen", "name": "陈星", "count": 1},
    ]
    messages = [
        {"message_uid": "SYN-M001", "reference": "M001", "author_id": "synthetic-alex", "author": "林小满", "timestamp": start + 3600, "type": "text", "text": "周末的社区花园活动改到周六上午，入口旁边集合。"},
        {"message_uid": "SYN-M002", "reference": "M002", "author_id": "synthetic-bo", "author": "周禾", "timestamp": start + 3900, "type": "text", "text": "收到，我会带上工具箱。"},
        {"message_uid": "SYN-M003", "reference": "M003", "author_id": "synthetic-cen", "author": "陈星", "timestamp": start + 4200, "type": "reply", "text": "我可以负责准备标签。\n引用 林小满：周末的社区花园活动改到周六上午，入口旁边集合。", "reply_to": "SYN-M001"},
        {"message_uid": "SYN-M004", "reference": "M004", "author_id": "synthetic-alex", "author": "林小满", "timestamp": start + 7200, "type": "forward", "text": "转发：社区花园分区安排\nA区：香草\nB区：花苗", "forward_xml": "forward.xml"},
        {"message_uid": "SYN-M005", "reference": "M005", "author_id": "synthetic-bo", "author": "周禾", "timestamp": start + 10800, "type": "image", "text": "示意图已附（合成图，不是聊天原图）。", "image": "garden-plan.png", "missing_image_reference": "synthetic-missing-image.png"},
    ]
    refs = [{"message_uid": m["message_uid"], "reference": m["reference"], "author": m["author"], "author_id": m["author_id"], "timestamp": m["timestamp"], "type": m["type"], "text": m["text"]} for m in messages]
    refs[-1]['image_src'] = 'garden-plan.png'
    buckets = daily_report.build_rhythm_buckets((message['timestamp'] for message in messages), start, end)
    for bucket in buckets:
        bucket['label'] = datetime.fromtimestamp(bucket['start_unix'], timezone.utc).strftime('%Y-%m-%d %H:%M')
    common = {
        "chat_username": "synthetic-demo-chat", "chat_display": "合成示例群（虚构）", "day": "2026-10-28",
        "generated_at": "2026-10-29T00:00:00+00:00", "range_text": "2026-10-28 00:00–2026-10-29 00:00（UTC）",
        "window": {"start_unix": start, "end_unix_exclusive": end},
        "timezone": "UTC",
        "stats": {"messages": 5, "members": 3, "groups": 1, "images": 1},
        "one_line": "合成示例：成员协商周六的社区花园活动安排，并分配了准备工作。",
        "topics": [{"topic": "社区花园活动安排", "summary": "活动定在周六上午，成员讨论集合地点与物资分工。", "details": ["林小满提出在入口旁集合。", "周禾准备工具箱，陈星负责标签。", "转发内容展示了虚构的分区安排。"], "message_ids": [m['message_uid'] for m in messages], "reference_ids": [m['reference'] for m in messages], "references": refs, "start_unix": start + 3600, "end_unix": start + 10800, "participant_ids": ["synthetic-alex", "synthetic-bo", "synthetic-cen"]}],
        "participants": participants, "active_rank": participants,
        "leaders": {"peak": {"name": "2026-10-28 01:00", "count": 2}},
        "rhythm_buckets": buckets,
        "avatars": {}, "synthetic_example": True,
    }
    brief = {**common, "edition": "brief"}
    review = {**common, "edition": "review", "quotes": [
        {"speaker": "周禾", "speaker_id": "synthetic-bo", "text": refs[1]['text'], "time": "01:05", "source": refs[1]},
    ]}
    snapshot = {"synthetic_example": True, "group_id": "synthetic-demo-chat", "window": common["window"], "messages": messages}
    return brief, review, snapshot


def _xml(path: Path) -> None:
    forward = ET.Element("msg")
    app = ET.SubElement(forward, "appmsg")
    ET.SubElement(app, "title").text = "社区花园分区安排（虚构转发）"
    ET.SubElement(app, "des").text = "合成的转发消息，用于展示安全解析。"
    record = ET.SubElement(app, "recorditem")
    info = ET.SubElement(record, "recordinfo")
    for title, text in (("A区", "香草"), ("B区", "花苗")):
        item = ET.SubElement(info, "dataitem", datatype="1")
        ET.SubElement(item, "datadesc").text = title + "：" + text
    ET.indent(forward, space="  ")
    ET.ElementTree(forward).write(path / "forward.xml", encoding="utf-8", xml_declaration=True)
    reply = ET.Element("msg")
    ET.SubElement(reply, "refermsg", type="1").text = "周末的社区花园活动改到周六上午，入口旁边集合。"
    ET.indent(reply, space="  ")
    ET.ElementTree(reply).write(path / "reply.xml", encoding="utf-8", xml_declaration=True)


def _encrypted_fixture(path: Path) -> None:
    from memory import decrypt_sync
    plain = path / "synthetic-plain.sqlite"
    encrypted = path / "synthetic-sqlcipher4.db"
    plain.unlink(missing_ok=True)
    _create_reserved_sqlite(plain)
    decrypt_sync.encrypt_sqlite(plain, encrypted, TEST_KEY, salt=FIXTURE_SALT)
    plain.unlink()
    enc_key = decrypt_sync._derive_enc_key(TEST_KEY, FIXTURE_SALT)
    _, mac_key = decrypt_sync._keys(enc_key, FIXTURE_SALT)
    _save_json(path / "fixture-key.json", {"warning": TEST_HMAC, "key_utf8": TEST_KEY.decode("ascii"), "salt_hex": FIXTURE_SALT.hex(), "derived_enc_key_hex": enc_key.hex(), "derived_hmac_key_hex": mac_key.hex(), "cipher": "SQLCipher v4 compatible; page_size=4096; reserve=80; HMAC=SHA-512"})
    _save_json(path / "fixture-integrity.json", {"warning": TEST_HMAC, "hmac_sha256_file": hashlib.sha256(encrypted.read_bytes()).hexdigest(), "tamper_test_byte_offset": 128})


def generate(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    brief, review, snapshot = _records()
    _save_json(output / "brief.json", brief)
    _save_json(output / "review.json", review)
    _save_json(output / "brief.sources.json", snapshot)
    _save_json(output / "review.sources.json", snapshot)
    _xml(output)
    _image(output / "garden-plan.png")
    _encrypted_fixture(output)

    for name, report in (("brief", brief), ("review", review)):
        html = daily_report.render_html(report)
        (output / f"{name}.html").write_text(html, encoding="utf-8")
        daily_report.render_png_pillow(report, output / f"{name}.png")
    index = """<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\"><title>合成示例日报</title><body><main><h1>合成示例日报</h1><p>以下内容及图片均为虚构；这是渲染/解密示例，不是真实 AI 生成证明。</p><ul><li><a href=\"brief.html\">简报 HTML</a> · <a href=\"brief.png\">简报 PNG</a></li><li><a href=\"review.html\">回顾 HTML</a> · <a href=\"review.png\">回顾 PNG</a></li></ul><img src=\"garden-plan.png\" alt=\"合成社区花园示意图\" width=\"360\"></main></body></html>\n"""
    (output / "index.html").write_text(index, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="destination directory (default: examples/synthetic)")
    args = parser.parse_args()
    generate(args.output.resolve())
    print(f"Generated fictional demo artifacts in {args.output.resolve()}")


if __name__ == "__main__":
    main()

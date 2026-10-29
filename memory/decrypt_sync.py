from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import struct
import tempfile
from pathlib import Path

PAGE_SIZE = 4096
RESERVE = 80
KDF_ITERATIONS = 256000


def _derive_enc_key(passphrase: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac('sha512', passphrase, salt, KDF_ITERATIONS, 32)


def _keys(enc_key: bytes, salt: bytes) -> tuple[bytes, bytes]:
    if len(enc_key) != 32:
        raise ValueError('enc_key must be the 32-byte SQLCipher-derived key')
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac = hashlib.pbkdf2_hmac('sha512', enc_key, mac_salt, 2, 32)
    return enc_key, mac


def _decrypt_page(page: bytes, number: int, enc: bytes, mac: bytes, salt: bytes) -> bytes:
    from Crypto.Cipher import AES
    if len(page) != PAGE_SIZE:
        raise ValueError('short encrypted page')
    reserve = page[-RESERVE:]
    ciphertext = page[16:-RESERVE] if number == 1 else page[:-RESERVE]
    iv, tag = reserve[:16], reserve[16:]
    expected = hmac.new(mac, ciphertext + iv + struct.pack('<I', number), hashlib.sha512).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError(f'SQLCipher page {number} authentication failed')
    plain = AES.new(enc, AES.MODE_CBC, iv).decrypt(ciphertext)
    return (b'SQLite format 3\x00' + plain if number == 1 else plain) + bytes(RESERVE)


def full_decrypt(source: Path, target: Path, key: bytes) -> int:
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise ValueError('source and target must differ')
    with source.open('rb') as inp:
        salt = inp.read(16)
        if len(salt) != 16:
            raise ValueError('encrypted database is too short')
        enc, mac = _keys(bytes(key), salt)
        first = inp.read(PAGE_SIZE - 16)
        if len(first) != PAGE_SIZE - 16:
            raise ValueError('encrypted database has a partial page')
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix='.decrypt-', dir=target.parent)
        pages = 0
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as out:
                out.write(_decrypt_page(salt + first, 1, enc, mac, salt))
                pages = 1
                while True:
                    page = inp.read(PAGE_SIZE)
                    if not page:
                        break
                    if len(page) != PAGE_SIZE:
                        raise ValueError('encrypted database has a partial page')
                    pages += 1
                    out.write(_decrypt_page(page, pages, enc, mac, salt))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
            return pages
        except BaseException:
            try: os.unlink(temporary)
            except FileNotFoundError: pass
            raise


def patch_wal(wal: Path, target: Path, key: bytes) -> int:
    wal = Path(wal)
    if not wal.exists() or wal.stat().st_size < 32:
        return 0
    name = wal.name[:-4] if wal.name.endswith('-wal') else ''
    source = wal.with_name(name) if name else None
    if source is None or not source.is_file():
        raise ValueError('encrypted database source is required beside WAL')
    return patch_wal_for_source(source, wal, Path(target), key)


def patch_wal_for_source(source: Path, wal: Path, target: Path, key: bytes) -> int:
    wal = Path(wal)
    if not wal.exists() or wal.stat().st_size < 32:
        return 0
    with Path(source).open('rb') as f:
        salt = f.read(16)
    data = wal.read_bytes()
    magic, _, page_size, _, salt1, salt2, _, _ = struct.unpack('>8I', data[:32])
    if magic not in (0x377f0682, 0x377f0683) or page_size != PAGE_SIZE:
        raise ValueError('unsupported SQLite WAL header')
    byteorder = 'little' if magic == 0x377f0682 else 'big'
    def checksum(block, state=(0, 0)):
        s1, s2 = state
        for off in range(0, len(block), 8):
            x = int.from_bytes(block[off:off+4], byteorder)
            y = int.from_bytes(block[off+4:off+8], byteorder)
            s1 = (s1 + x + s2) & 0xffffffff
            s2 = (s2 + y + s1) & 0xffffffff
        return s1, s2
    state = checksum(data[:24])
    if state != struct.unpack('>2I', data[24:32]):
        raise ValueError('WAL header checksum mismatch')
    enc, mac = _keys(bytes(key), salt)
    frames, pos, committed = [], 32, 0
    while pos + 24 + PAGE_SIZE <= len(data):
        pg, dbsize, fs1, fs2, fc1, fc2 = struct.unpack('>6I', data[pos:pos+24])
        page = data[pos+24:pos+24+PAGE_SIZE]
        if (fs1, fs2) != (salt1, salt2) or pg == 0:
            break
        state = checksum(data[pos:pos+8] + page, state)
        if state != (fc1, fc2):
            break
        frames.append((pg, dbsize, page))
        if dbsize:
            committed = len(frames)
        pos += 24 + PAGE_SIZE
    if not committed:
        return 0
    updates = {}
    for pg, _, frame in frames[:committed]:
        updates[pg] = _decrypt_page(frame, pg, enc, mac, salt)
    db_size = frames[committed - 1][1] * PAGE_SIZE
    target = Path(target)
    fd, temporary = tempfile.mkstemp(prefix='.wal-patch-', dir=target.parent)
    os.close(fd)
    try:
        shutil.copyfile(target, temporary)
        os.chmod(temporary, 0o600)
        with open(temporary, 'r+b') as out:
            for pg, plain in updates.items():
                out.seek((pg-1)*PAGE_SIZE)
                out.write(plain)
            out.truncate(db_size)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise
    return len(updates)


def encrypt_sqlite(plain_path: Path, encrypted_path: Path, passphrase: bytes, *, salt: bytes) -> int:
    """Encrypt a 4096-byte SQLite fixture prepared with exactly 80 reserved bytes."""
    from Crypto.Cipher import AES
    plain_path, encrypted_path = Path(plain_path), Path(encrypted_path)
    if len(salt) != 16:
        raise ValueError('salt must be exactly 16 bytes')
    data = plain_path.read_bytes()
    if not data.startswith(b'SQLite format 3\x00') or len(data) % PAGE_SIZE:
        raise ValueError('fixture input must be a complete SQLite database with 4096-byte pages')
    enc_key = _derive_enc_key(bytes(passphrase), salt)
    enc, mac = _keys(enc_key, salt)
    encrypted = bytearray()
    for number, off in enumerate(range(0, len(data), PAGE_SIZE), 1):
        page = data[off:off+PAGE_SIZE]
        body = page[16:-RESERVE] if number == 1 else page[:-RESERVE]
        if len(body) % 16:
            raise ValueError('plain database must have an 80-byte reserved page tail')
        iv = hashlib.sha256(salt + struct.pack('<I', number)).digest()[:16]
        ciphertext = AES.new(enc, AES.MODE_CBC, iv).encrypt(body)
        tag = hmac.new(mac, ciphertext + iv + struct.pack('<I', number), hashlib.sha512).digest()
        output = (salt + ciphertext + iv + tag) if number == 1 else (ciphertext + iv + tag)
        if len(output) != PAGE_SIZE:
            raise ValueError('database pages must reserve exactly 80 bytes')
        encrypted.extend(output)
    encrypted_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.encrypt-', dir=encrypted_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(encrypted)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, encrypted_path)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise
    return len(data) // PAGE_SIZE


def key_for_rel(keys: dict, rel: str) -> dict | None:
    rel = Path(rel).as_posix().lstrip('./')
    candidates = keys.get('databases', keys)
    if not isinstance(candidates, dict):
        return None
    found = candidates.get(rel)
    if found is None:
        found = next((v for k, v in candidates.items() if Path(k).as_posix().lstrip('./') == rel), None)
    if isinstance(found, str):
        return {'enc_key': found}
    return found if isinstance(found, dict) and found.get('enc_key') else None


def should_skip_source_db(path: Path | str) -> bool:
    name = Path(path).name.lower()
    return name.endswith(('-wal', '-shm', '_fts.db')) or name in {'message_resource.db', 'media.db', 'fts.db'}


def verify_sqlite(path: Path) -> bool:
    try:
        uri = Path(path).resolve().as_uri() + '?mode=ro&immutable=1'
        with sqlite3.connect(uri, uri=True) as db:
            return db.execute('PRAGMA quick_check').fetchone() == ('ok',)
    except (sqlite3.Error, OSError):
        return False


def load_keys(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Authenticate and decrypt a SQLCipher4 database copy')
    parser.add_argument('source', type=Path)
    parser.add_argument('target', type=Path)
    key_choice = parser.add_mutually_exclusive_group(required=True)
    key_choice.add_argument('--key', help='hex derived SQLCipher enc_key')
    key_choice.add_argument('--keys', type=Path, help='JSON keyfile with enc_key entries')
    parser.add_argument('--rel', help='database-relative keyfile entry; defaults to source filename')
    args = parser.parse_args(argv)
    if args.key:
        key = bytes.fromhex(args.key)
    else:
        rel = args.rel or args.source.name
        info = key_for_rel(load_keys(args.keys), rel)
        if not info:
            raise SystemExit(f'no key entry for {rel}')
        key = bytes.fromhex(info['enc_key'])
    count = full_decrypt(args.source, args.target, key)
    wal = Path(str(args.source) + '-wal')
    if wal.exists():
        patch_wal(wal, args.target, key)
    if not verify_sqlite(args.target):
        raise SystemExit('decrypted database failed SQLite quick_check')
    print(f'decrypted {count} authenticated pages; sqlite_valid=True')


if __name__ == '__main__':
    main()

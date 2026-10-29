from __future__ import annotations

# V1/V2 .dat layout adapted independently from wxextract.images.py format
# documentation (Juan Boullosa, MIT); see the repository's wxextract notice.
import argparse
import os
import sqlite3
import struct
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

V2_MAGIC_FULL = b'\x07\x08V2\x08\x07'
V2_MAGIC_THUMB = V2_MAGIC_FULL
V1_MAGIC = b'\x07\x08V1\x08\x07'
V1_AES_KEY = b'cfcd208495d565ef'
_XOR_MAGICS = ((b'\x89PNG', 'png'), (b'GIF8', 'gif'), (b'II*\x00', 'tif'), (b'RIFF', 'webp'), (b'\xff\xd8\xff', 'jpg'))


def detect_image_format(data):
    if not data: return None
    checks = ((b'\xff\xd8\xff', 'jpg'), (b'\x89PNG\r\n\x1a\n', 'png'), (b'GIF87a', 'gif'), (b'GIF89a', 'gif'),
              (b'RIFF', 'webp'), (b'BM', 'bmp'), (b'II*\x00', 'tif'), (b'MM\x00*', 'tif'), (b'wxgf', 'hevc'))
    for magic, name in checks:
        if data.startswith(magic):
            if name == 'webp' and data[8:12] != b'WEBP': return None
            return name
    if len(data) >= 12 and data[4:8] == b'ftyp' and data[8:12] in (b'heic', b'heix', b'hevc', b'hevx', b'mif1'):
        return 'heic'
    return None


def _aes_key_bytes(key):
    if isinstance(key, bytes): return key
    if not isinstance(key, str): return b''
    if len(key) == 16: return key.encode('ascii', 'strict')
    try: return bytes.fromhex(key)
    except ValueError: return key.encode('ascii', 'strict')


def _complete_image(data, fmt):
    if fmt == 'hevc':
        # wxgf is WeChat's proprietary HEVC stream container; Pillow has no decoder.
        return data.startswith(b'wxgf')
    if fmt == 'jpg' and b'\xff\xd9' not in data: return False
    try:
        from PIL import Image, ImageFile, ImageSequence
        old_truncated = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        try:
            if fmt == 'heic':
                try:
                    import pillow_heif
                    pillow_heif.register_heif_opener()
                except ImportError:
                    return False
            import io
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                for frame in ImageSequence.Iterator(image):
                    frame.load()
            return True
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = old_truncated
    except Exception:
        return False


def _valid_result(data):
    fmt = detect_image_format(data)
    if fmt is None or not _complete_image(data, fmt): return None, None
    return data, fmt


def _aligned_padded_size(size):
    # PKCS#7 adds a full block when the prefix size is already aligned.
    return (size // 16 + 1) * 16


def decrypt_v2(data, key, xor_key=0x88):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    if isinstance(data, (str, Path)):
        data = Path(data).read_bytes()
    if len(data) < 15: return None, None
    signature = data[:6]
    if signature not in (V2_MAGIC_FULL, V1_MAGIC): return None, None
    aes_size, xor_size = struct.unpack_from('<II', data, 6)
    aligned = _aligned_padded_size(aes_size)
    offset = 15
    if not aes_size or offset + aligned > len(data) or xor_size > len(data) - offset - aligned:
        return None, None
    key_bytes = V1_AES_KEY if signature == V1_MAGIC else _aes_key_bytes(key)
    if len(key_bytes) < 16: return None, None
    try:
        decrypted_prefix = unpad(AES.new(key_bytes[:16], AES.MODE_ECB).decrypt(data[offset:offset+aligned]), 16)
    except (ValueError, TypeError):
        return None, None
    if len(decrypted_prefix) != aes_size: return None, None
    offset += aligned
    raw_end = len(data) - xor_size
    if raw_end < offset: return None, None
    xor = int(xor_key) & 255
    decoded = decrypted_prefix + data[offset:raw_end] + bytes(value ^ xor for value in data[raw_end:])
    return _valid_result(decoded)


def decrypt_dat(path, aes_key=None, xor_key=None):
    data = Path(path).read_bytes()
    if data[:6] in (V2_MAGIC_FULL, V1_MAGIC):
        return decrypt_v2(data, aes_key, 0x88 if xor_key is None else xor_key)
    if len(data) < 4: return None, None
    key = None
    for magic, _ in _XOR_MAGICS:
        if len(data) >= len(magic):
            candidate = data[0] ^ magic[0]
            if all((data[i] ^ candidate) == magic[i] for i in range(1, len(magic))):
                key = candidate
    if key is None and len(data) >= 14:
        candidate = data[0] ^ ord('B')
        header = bytes(value ^ candidate for value in data[:14])
        if header[:2] == b'BM':
            try:
                file_size, _, _, image_offset = struct.unpack_from('<IHHI', header, 2)
                if abs(file_size - len(data)) < 1024 and 14 <= image_offset <= 1078:
                    key = candidate
            except struct.error:
                pass
    if key is None: return None, None
    decoded = bytes(value ^ key for value in data)
    return _valid_result(decoded)


def find_dat_files(root, group_id, alias):
    token = str(alias).lower()
    if not token or any(c not in '0123456789abcdef_' for c in token): return []
    root = Path(root)
    base = root / str(group_id)
    if not base.is_dir(): base = root
    try: return sorted(path for path in base.rglob('*.dat') if token in path.stem.lower())
    except OSError: return []


def load_resource_map(database):
    database = Path(database)
    uri = database.resolve().as_uri() + '?mode=ro&immutable=1'
    try:
        with sqlite3.connect(uri, uri=True) as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                cols = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
                chat = next((c for c in cols if c.lower() in ('username', 'chatid', 'conversationid', 'talker')), None)
                local = next((c for c in cols if c.lower() in ('localid', 'msgid', 'messageid')), None)
                md5 = next((c for c in cols if c.lower() in ('md5', 'filemd5', 'resourceid')), None)
                if chat and local and md5:
                    return {(str(a), int(b)): str(c).lower() for a, b, c in db.execute(f'SELECT "{chat}","{local}","{md5}" FROM "{table}"') if a is not None and b is not None and c}
    except (sqlite3.Error, OSError, ValueError): pass
    return {}


def parse_xml_payload(payload):
    if payload is None: return None
    if isinstance(payload, bytes): payload = payload.decode('utf-8', 'replace')
    text = str(payload).strip()
    start = text.find('<')
    if start < 0: return None
    try: return ET.fromstring(text[start:])
    except ET.ParseError: return None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Decode local WeChat image cache files')
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--aes-key', help='16-byte key or hex-encoded AES key (required for V2)')
    parser.add_argument('--xor-key', type=lambda value: int(value, 0), default=0x88)
    args = parser.parse_args(argv)
    payload, fmt = decrypt_dat(args.source, args.aes_key, args.xor_key)
    if payload is None: raise SystemExit('unrecognized, incomplete, or invalid local image data')
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.media-', dir=args.output.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, args.output)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise
    print(f'wrote {len(payload)} bytes ({fmt})')


if __name__ == '__main__': main()

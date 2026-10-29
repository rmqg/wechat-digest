from __future__ import annotations
import argparse, ctypes, ctypes.util, hashlib, json, os, sqlite3, tempfile, zlib
from datetime import datetime, timezone
from pathlib import Path


def content_hash(*parts) -> str:
    h = hashlib.sha256()
    for part in parts:
        value = str(part if part is not None else '').encode('utf-8')
        h.update(len(value).to_bytes(8, 'big'))
        h.update(value)
    return h.hexdigest()


def _zstd_decompress(raw: bytes) -> bytes:
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(raw)
    except ImportError:
        library = ctypes.util.find_library('zstd')
        if not library:
            raise RuntimeError('zstd-compressed message requires python-zstandard or libzstd')
        zstd = ctypes.CDLL(library)
        zstd.ZSTD_decompressBound.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
        zstd.ZSTD_decompressBound.restype = ctypes.c_ulonglong
        zstd.ZSTD_decompress.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t)
        zstd.ZSTD_decompress.restype = ctypes.c_size_t
        zstd.ZSTD_isError.argtypes = (ctypes.c_size_t,)
        zstd.ZSTD_isError.restype = ctypes.c_uint
        source = ctypes.create_string_buffer(raw)
        capacity = zstd.ZSTD_decompressBound(source, len(raw))
        if not capacity or capacity > 1024 * 1024 * 1024:
            raise ValueError('invalid or unreasonably large zstd message')
        output = ctypes.create_string_buffer(capacity)
        size = zstd.ZSTD_decompress(output, capacity, source, len(raw))
        if zstd.ZSTD_isError(size):
            raise ValueError('zstd message decompression failed')
        return output.raw[:size]


def decompress_content(value, compression_type=None):
    if value is None: return None
    if isinstance(value, memoryview): value = value.tobytes()
    text_input = isinstance(value, str)
    if text_input: raw = value.encode('utf-8')
    elif isinstance(value, bytes): raw = value
    elif isinstance(value, bytearray): raw = bytes(value)
    else: return value
    if not raw: return ''
    mode = str(compression_type or '').lower()
    is_zstd = mode in {'1', 'zstd', 'zstandard'} or raw.startswith(b'\x28\xb5\x2f\xfd')
    if is_zstd:
        try:
            out = _zstd_decompress(raw)
        except Exception as exc:
            raise ValueError('zstd-compressed message could not be decoded') from exc
        return out.decode('utf-8', 'replace')
    if mode in {'0', 'none', 'uncompressed'}:
        return raw.decode('utf-8', 'replace')
    for wbits in (zlib.MAX_WBITS, zlib.MAX_WBITS | 16, -zlib.MAX_WBITS):
        try:
            return zlib.decompress(raw, wbits).decode('utf-8', 'replace')
        except zlib.error:
            pass
    return raw.decode('utf-8', 'replace')


def _rows(path, sql):
    if path is None or not Path(path).is_file(): return []
    uri = Path(path).resolve().as_uri() + '?mode=ro&immutable=1'
    try:
        with sqlite3.connect(uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(sql)]
    except sqlite3.Error: return []


def load_contact_names(path: Path | None) -> dict[str, dict]:
    tables = {r['name'] for r in _rows(path, "SELECT name FROM sqlite_master WHERE type='table'")}
    result = {}
    for table in ('contact', 'Contact', 'contact_info'):
        if table not in tables: continue
        columns = [r['name'] for r in _rows(path, f'PRAGMA table_info("{table}")')]
        by_lower = {column.lower(): column for column in columns}
        idcol = next((by_lower[name.lower()] for name in ('user_name', 'username', 'wxid') if name.lower() in by_lower), None)
        nickcol = next((by_lower[name.lower()] for name in ('nick_name', 'nickname') if name.lower() in by_lower), None)
        remarkcol = by_lower.get('remark')
        if not idcol: continue
        for row in _rows(path, f'SELECT * FROM "{table}"'):
            username = row.get(idcol)
            if not username: continue
            nickname = row.get(nickcol) if nickcol else ''
            remark = row.get(remarkcol) if remarkcol else ''
            display = remark or nickname or ''
            result[str(username)] = {'user_name': str(username), 'username': str(username),
                                     'nick_name': str(nickname or ''), 'nickname': str(nickname or ''),
                                     'remark': str(remark or ''), 'display_name': str(display)}
    return result


def load_sessions(path: Path | None) -> dict[str, dict]:
    tables = {r['name'] for r in _rows(path, "SELECT name FROM sqlite_master WHERE type='table'")}
    table = next((name for name in ('SessionTable', 'session', 'sessions') if name in tables), None)
    if not table: return {}
    rows = _rows(path, f'SELECT * FROM "{table}"')
    result = {}
    for row in rows:
        columns = {key.lower(): key for key in row}
        idcol = next((columns[name] for name in ('user_name', 'username', 'strusrname', 'usrname', 'talker') if name in columns), None)
        if not idcol or not row.get(idcol): continue
        username = str(row[idcol])
        item = dict(row)
        nickcol = next((columns[name] for name in ('nick_name', 'nickname', 'strnickname') if name in columns), None)
        remarkcol = columns.get('remark')
        nickname = str(row.get(nickcol) or '') if nickcol else ''
        remark = str(row.get(remarkcol) or '') if remarkcol else ''
        item.setdefault('user_name', username)
        item.setdefault('username', username)
        item.setdefault('nick_name', nickname)
        item.setdefault('nickname', nickname)
        item.setdefault('remark', remark)
        item.setdefault('display_name', remark or nickname)
        result[username] = item
    return result

def msg_type_label(value):
    try: n = int(value)
    except (TypeError, ValueError): return 'unknown'
    base_type = n & 0xffffffff
    return {1:'text', 3:'image', 34:'voice', 43:'video', 47:'emoji', 49:'appmsg', 10000:'system', 10002:'recall'}.get(base_type, 'system' if base_type >= 10000 else 'unknown')


def split_msg_type(value):
    try: n = int(value)
    except (TypeError, ValueError): return 0, 0
    n &= 0xffffffffffffffff
    return n & 0xffffffff, (n >> 32) & 0xffffffff


def utc_now_iso(): return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_name2id(path: Path) -> dict[str, str]:
    path = Path(path)
    try: data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError): data = None
    if isinstance(data, dict): return data
    tables = {r['name'] for r in _rows(path, "SELECT name FROM sqlite_master WHERE type='table'")}
    if 'Name2Id' not in tables: return {}
    rows = _rows(path, 'SELECT rowid, user_name FROM Name2Id')
    return {str(r['rowid']): str(r['user_name']) for r in rows if r.get('user_name')}


def _message_table(db, requested='MSG'):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return requested if requested in tables else next((t for t in sorted(tables) if 'message' in t.lower() or t.startswith('Msg_')), None)


def export_messages(database: Path, output: Path, table='MSG') -> int:
    uri = Path(database).resolve().as_uri() + '?mode=ro&immutable=1'
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        table = _message_table(db, table)
        if table is None: raise ValueError('no message table found')
        rows = [dict(r) for r in db.execute(f'SELECT * FROM "{table}"')]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.export-', dir=output.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, default=lambda x: bytes(x).hex()) + '\\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise
    return len(rows)


def index_database(database: Path, output: Path, table='MSG') -> int:
    import message_parse
    source_uri = Path(database).resolve().as_uri() + '?mode=ro&immutable=1'
    with sqlite3.connect(source_uri, uri=True) as source:
        source.row_factory = sqlite3.Row
        table = _message_table(source, table)
        if table is None: raise ValueError('no message table found')
        rows = [dict(r) for r in source.execute(f'SELECT * FROM "{table}"')]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix='.index-', dir=output.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        with sqlite3.connect(temp) as index:
            index.execute('PRAGMA journal_mode=DELETE')
            index.execute('CREATE TABLE messages (message_uid TEXT PRIMARY KEY, timestamp INTEGER, sender_id TEXT, type TEXT, content TEXT NOT NULL, source_row TEXT NOT NULL)')
            for i, row in enumerate(rows):
                raw = row.get('message_content')
                packed = row.get('compress_content')
                type_value = split_msg_type(row.get('local_type'))[0]
                type_label = msg_type_label(type_value)
                text = message_parse.message_index_text({'message_content': decompress_content(raw, row.get('WCDB_CT_message_content')), 'compress_content': decompress_content(packed), 'type_label': type_label})[1]
                uid = content_hash(database, table, row.get('local_id', i), row.get('server_id'), row.get('create_time'))
                index.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)', (uid, int(row.get('create_time') or 0), str(row.get('real_sender_id') or ''), type_label, text, json.dumps(row, ensure_ascii=False, default=lambda x: bytes(x).hex())))
            index.commit()
        os.chmod(temp, 0o600)
        os.replace(temp, output)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Export or build a local index from a decrypted message SQLite database')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('export', 'index'):
        item = sub.add_parser(command)
        item.add_argument('database', type=Path)
        item.add_argument('output', type=Path)
        item.add_argument('--table', default='MSG')
    args = parser.parse_args(argv)
    count = export_messages(args.database, args.output, args.table) if args.command == 'export' else index_database(args.database, args.output, args.table)
    print(f'{args.command} complete: {count} rows')


if __name__ == '__main__': main()

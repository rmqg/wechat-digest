#!/usr/bin/env python3
"""Generate an isolated QQ group digest from an OneBot message snapshot."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import ipaddress
import http.client
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
import shutil
import socket
import sys
import urllib.error
import uuid
import urllib.parse
import urllib.request
import re
import stat
import time

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = Path(__file__).resolve().parent
sys.path.insert(0, str(DEPLOYMENT))
sys.path.insert(0, str(ROOT / 'agent_console'))

import digest
import daily_report

MAX_WINDOW = 604800
UTC8 = timezone(timedelta(hours=8))
_ALLOWED_HOST_SUFFIXES = ('.qpic.cn', '.qq.com', '.qq.com.cn', '.gtimg.cn')


def _load_caption_module():
    spec = importlib.util.spec_from_file_location('caption_images_qq', DEPLOYMENT / 'caption-images.py')
    if spec is None or spec.loader is None:
        raise RuntimeError('cannot load caption-images.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _private_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name('.' + path.name + '.' + os.urandom(8).hex() + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _private_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)


def _check_url(url: str, *, avatar: bool = False) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower().rstrip('.')
    if (parsed.scheme != 'https' or not host or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError('image URL is not an allowed HTTPS platform URL')
    allowed = ((host == 'p.qlogo.cn' or host == 'qlogo.cn' or host.endswith('.qlogo.cn')) if avatar
               else any(host.endswith(suffix) for suffix in _ALLOWED_HOST_SUFFIXES))
    if not allowed:
        raise ValueError('image URL host is not an allowed Tencent image domain')
    _public_addresses(host)
    return parsed


def _public_addresses(host: str) -> list[str]:
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    except OSError as exc:
        raise ValueError('image URL host could not be resolved') from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError('image URL resolves to a non-public address')
    return addresses

class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, addresses, **kwargs):
        super().__init__(host, **kwargs)
        self._addresses = addresses

    def connect(self):
        last_error = None
        for address in self._addresses:
            try:
                raw = socket.create_connection((address, self.port), self.timeout, self.source_address)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except OSError as exc:
                last_error = exc
        raise OSError('no validated public image address was reachable') from last_error


class _SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        host = urllib.parse.urlsplit(req.full_url).hostname
        addresses = _public_addresses(host)
        return self.do_open(lambda resolved_host, **kwargs:
            _PinnedHTTPSConnection(resolved_host, addresses, **kwargs), req)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _replace_image_rkey(url: str, group_rkey: str) -> str | None:
    if not isinstance(group_rkey, str) or not group_rkey.startswith('&rkey='):
        return None
    value = group_rkey[len('&rkey='):]
    if not value or any(char in value for char in '&?#\r\n') or any(char.isspace() for char in value):
        return None
    parsed = urllib.parse.urlsplit(url)
    fields = parsed.query.split('&')
    positions = [index for index, field in enumerate(fields) if field.partition('=')[0] == 'rkey']
    if len(positions) != 1:
        return None
    index = positions[0]
    key, separator, _old_value = fields[index].partition('=')
    if not separator:
        return None
    fields[index] = key + '=' + value
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                    '&'.join(fields), parsed.fragment))


def _fetch_image_once(url: str, *, avatar: bool = False) -> bytes:
    _check_url(url, avatar=avatar)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SafeHTTPSHandler(), _NoRedirect())
    request = urllib.request.Request(url, headers={'User-Agent': 'QQDigest/1.0', 'Accept': 'image/*'})
    with opener.open(request, timeout=30) as response:
        if response.status != 200:
            raise ValueError(f'image server returned HTTP {response.status}')
        chunks, total = [], 0
        while True:
            chunk = response.read(min(1024 * 1024, 32 * 1024 * 1024 + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 32 * 1024 * 1024:
                raise ValueError('image exceeds 32 MiB')
    data = b''.join(chunks)
    if not data:
        raise ValueError('image response is empty')
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except Exception as exc:
        raise ValueError('image response is not a complete decodable image') from exc
    return data


def _fetch_image(url: str, *, avatar: bool = False, group_rkey: str | None = None) -> bytes:
    try:
        return _fetch_image_once(url, avatar=avatar)
    except urllib.error.HTTPError as exc:
        if exc.code == 400 and not avatar and group_rkey:
            refreshed = _replace_image_rkey(url, group_rkey)
            if refreshed:
                try:
                    return _fetch_image_once(refreshed, avatar=False)
                except urllib.error.HTTPError as retry_error:
                    raise ValueError(f'image fetch failed after group rkey refresh with HTTP {retry_error.code}') from retry_error
        raise ValueError(f'image fetch failed with HTTP {exc.code}') from exc


def _load_group_rkey_file(path: Path | None, runtime: Path, group_id: int) -> str | None:
    if path is None:
        return None
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('group rkey file must be a regular private file')
    root, resolved = runtime.resolve(), path.resolve(strict=True)
    if not resolved.is_relative_to(root) or stat.S_IMODE(resolved.stat().st_mode) != 0o600:
        raise ValueError('group rkey file must stay under runtime and use mode 0600')
    record = json.loads(resolved.read_text(encoding='utf-8'))
    if (not isinstance(record, dict) or record.get('group_id') != group_id
            or record.get('type') != 'group' or not isinstance(record.get('rkey'), str)
            or not record['rkey'].startswith('&rkey=')):
        raise ValueError('group rkey file does not match the configured group')
    token = record['rkey'][len('&rkey='):]
    if not token or any(char in token for char in '&?#\r\n') or any(char.isspace() for char in token):
        raise ValueError('group rkey file contains an invalid ticket')
    return record['rkey']


def _segment_url(item: dict) -> str | None:
    for key in ('url', 'file'):
        value = item.get(key)
        if isinstance(value, str) and value.startswith('https://'):
            return value
    return None


def _load_config(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'model config must contain an object: {path}')
    return value


def _safe_public_snapshot(snapshot: dict) -> dict:
    rows = []
    for message in snapshot['messages']:
        row = dict(message)
        if isinstance(row.get('image_items'), list):
            row['image_items'] = [{key: item[key] for key in ('type', 'cache_key', 'sha256', 'extension') if key in item}
                                  if isinstance(item, dict) else item for item in row['image_items']]
        rows.append(row)
    return {'group_id': snapshot['group_id'], 'group_name': snapshot['group_name'], 'messages': rows}


def _image_info(data: bytes) -> tuple[str, list[int]]:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        ext = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp', 'GIF': 'gif'}.get(image.format)
        if ext is None:
            raise ValueError('unsupported source image format')
        return ext, list(image.size)


class QQImageFetchError(RuntimeError):
    def __init__(self, message_uid: str):
        super().__init__('QQ image fetch failed')
        self.message_uid = message_uid


def _prepare_images(messages: list[dict], runtime: Path, primary: dict, fallback: dict | None,
                    group_rkey: str | None = None) -> tuple[dict, dict]:
    manifest, image_to_message = [], {}
    cache_dir = runtime / 'qq-digest' / 'images'
    index_path = cache_dir / 'descriptors.json'
    index = json.loads(index_path.read_text(encoding='utf-8')) if index_path.is_file() else {}
    if not isinstance(index, dict):
        raise ValueError('QQ image descriptor cache is invalid')
    for message in messages:
        if message.get('type') != 'image':
            continue
        items = message.get('image_items')
        if not isinstance(items, list) or not items:
            raise RuntimeError(f"image message {message['message_uid']} has no platform image item")
        safe_items = []
        for image_index, image_item in enumerate(items):
            url = _segment_url(image_item) if isinstance(image_item, dict) else None
            if not url:
                raise RuntimeError(f"image message {message['message_uid']} has no platform image URL")
            descriptor_key = hashlib.sha256(json.dumps(image_item, ensure_ascii=False, sort_keys=True,
                                                         separators=(',', ':')).encode()).hexdigest()
            cached = index.get(descriptor_key)
            data = None
            if isinstance(cached, dict) and isinstance(cached.get('sha256'), str):
                ext = cached.get('extension')
                cache_path = cache_dir / f"{cached['sha256']}.{ext}"
                if ext in {'jpg', 'png', 'webp', 'gif'} and cache_path.is_file() and not cache_path.is_symlink():
                    candidate = cache_path.read_bytes()
                    if hashlib.sha256(candidate).hexdigest() == cached['sha256']:
                        try:
                            actual_ext, dimensions = _image_info(candidate)
                            if actual_ext == ext:
                                data = candidate
                        except Exception:
                            data = None
            if data is None:
                try:
                    data = _fetch_image(url, group_rkey=group_rkey)
                except Exception as exc:
                    raise QQImageFetchError(message['message_uid']) from exc
                ext, dimensions = _image_info(data)
                sha = hashlib.sha256(data).hexdigest()
                cache_path = cache_dir / f'{sha}.{ext}'
                if cache_path.exists() and (cache_path.is_symlink() or hashlib.sha256(cache_path.read_bytes()).hexdigest() != sha):
                    raise RuntimeError('private image cache failed integrity verification')
                if not cache_path.exists():
                    _private_file(cache_path, data)
                cached = {'sha256': sha, 'extension': ext}
                index[descriptor_key] = cached
                _private_json(index_path, index)
            else:
                sha = cached['sha256']
            message_uid = message['message_uid'] if len(items) == 1 else f"{message['message_uid']}:image-{image_index + 1}"
            manifest.append({'message_uid': message_uid, 'available': True, 'sha256': sha,
                             'image_path': str(cache_path), 'dimensions': dimensions,
                             'source_quality': 'original'})
            image_to_message[message_uid] = message['message_uid']
            safe_items.append({'type': image_item.get('type', 'image'), 'cache_key': descriptor_key,
                               'sha256': sha, 'extension': cached['extension']})
        message['image_items'] = safe_items
    if not manifest:
        return {}, {}

    caption = _load_caption_module()
    cache_path = runtime / 'qq-digest' / 'vision-cache.json'
    failures_path = runtime / 'qq-digest' / 'vision-failures.json'
    rows = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else []
    model_names = {str(config.get('model', '')) for config in (primary, fallback) if config}
    comparison = {row['sha256']: row['result'] for row in rows
                  if isinstance(row, dict) and row.get('provider') in {'gemini', 'qwen'}
                  and row.get('source_quality') in {'original', 'client_full'}
                  and row.get('model') in model_names and isinstance(row.get('sha256'), str)
                  and isinstance(row.get('result'), dict)}
    failures = json.loads(failures_path.read_text(encoding='utf-8')) if failures_path.exists() else {}
    results, added = caption.caption_manifest(manifest, primary, fallback, comparison,
        failures=failures, diagnostics_dir=runtime / 'qq-digest' / 'vision-diagnostics')
    old = {row.get('sha256'): row for row in rows if isinstance(row, dict) and row.get('sha256')}
    old.update(added)
    caption._private_json(cache_path, list(old.values()))
    caption._private_json(failures_path, failures)
    errors = {uid: row.get('reason', row.get('status')) for uid, row in results.items()
              if row.get('status') != 'success'}
    if errors:
        raise RuntimeError('image captioning failed; report was not generated: ' + json.dumps(errors, ensure_ascii=False))

    import ocr_images
    ocr_path = runtime / 'qq-digest' / 'ocr.json'
    ocr_images.process_manifest(manifest, ocr_path, runtime / 'qq-digest' / 'ocr-cache')
    ocr_rows = json.loads(ocr_path.read_text(encoding='utf-8'))
    import ocr_summary
    ocr_results, _ = ocr_summary.process(ocr_rows, primary, runtime / 'qq-digest' / 'ocr-summary-cache')
    auxiliary = {}
    for uid, result in results.items():
        auxiliary.setdefault(image_to_message[uid], []).append(result)
    for uid, result in ocr_results.items():
        if result.get('status') == 'failed':
            raise RuntimeError(f'image OCR summary failed for {uid}: {result.get("reason", "unknown failure")}')
        if result.get('status') == 'pending':
            raise RuntimeError(f'image OCR summary pending for {uid}; report was not generated')
        if result.get('summary'):
            auxiliary.setdefault(image_to_message[uid], []).append({'status': 'success', 'source_kind': 'ocr_ai', 'result': result})
    mapped = {}
    for uid, entries in auxiliary.items():
        captions = [entry['result'] for entry in entries if entry.get('source_kind') != 'ocr_ai']
        ocr_summaries = [entry['result'].get('summary', '') for entry in entries if entry.get('source_kind') == 'ocr_ai']
        mapped[uid] = {'status': 'success', 'result': {
            'description': '\n'.join(str(row.get('description') or '') for row in captions if row),
            'ocr_summary': '\n'.join(value for value in ocr_summaries if value),
            'model': ', '.join(dict.fromkeys(str(row.get('model')) for row in captions if row.get('model')))}}
    return mapped, {'total': len(manifest), 'understood': len(results)}


def _web_context(messages: list[dict], runtime: Path, config: dict) -> dict:
    import web_context
    import web_summary
    rows = web_context.collect(messages, runtime / 'qq-digest' / 'web-cache')
    summarized, _ = web_summary.process(rows, config, runtime / 'qq-digest' / 'web-summary-cache')
    auxiliary = {}
    for uid, record in summarized.items():
        pages = [page for page in record.get('pages', []) if page.get('status') == 'success']
        if any(page.get('status') == 'pending' for page in record.get('pages', [])):
            raise RuntimeError(f'web summary pending for {uid}; report was not generated')
        if pages:
            auxiliary[uid] = {'source_kind': 'web', 'status': 'success', 'pages': [
                {key: page.get(key) for key in ('summary_title', 'points', 'uncertainties', 'content_sha256')}
                for page in pages]}
    return auxiliary


def _build_report(messages: list[dict], snapshot: dict, group_id: int, start: int, end: int,
                  summary: dict, edition: str, avatar: dict) -> dict:
    counts = {}
    for message in messages:
        counts[message['type']] = counts.get(message['type'], 0) + 1
    report = {
        'chat_username': f'qq:{group_id}', 'chat_display': snapshot['group_name'],
        'title_prefix': 'QQ群日报' if edition == 'brief' else 'QQ群总结',
        'edition': edition, 'day': datetime.fromtimestamp(end, UTC8).strftime('%Y-%m-%d'),
        'range_text': datetime.fromtimestamp(start, UTC8).strftime('%Y.%m.%d %H:%M') + ' — ' + datetime.fromtimestamp(end, UTC8).strftime('%Y.%m.%d %H:%M'),
        'generated_at': datetime.now(UTC8).strftime('%Y-%m-%d %H:%M'),
        'one_line': summary['one_line'], 'stats': {'messages': len(messages), 'members': len({m['author_id'] for m in messages if m['author_id']}),
            'text': counts.get('text', 0), 'images': counts.get('image', 0), 'links': counts.get('link_or_file', 0), 'stickers': counts.get('sticker', 0), 'videos': counts.get('video', 0)},
        'user_type_counts': counts, 'insights': [], 'topics': summary['topics'], 'quotes': summary['quotes'],
        'timeline': [], 'rhythm_buckets': [], 'rhythm_bucket_seconds': 600,
        'active_rank': [], 'leaders': {'talk': {'name': '--', 'count': 0}, 'media': {'name': '--', 'count': 0},
            'link': {'name': '--', 'count': 0}, 'peak': {'name': '--', 'count': 0}},
    }
    digest.enrich_report(report, messages, start, end, avatar, text_is_original=True)
    report['stats']['images'] = counts.get('image', 0)
    report['media_understanding'] = {'images': {'total': counts.get('image', 0), 'understood': 0}, 'voice': 'not analyzed', 'video': 'not analyzed'}
    return report


def _group_avatar(runtime: Path, messages: list[dict], group_id: int) -> dict:
    import base64
    from PIL import Image

    avatars = {}
    cache_dir = runtime / 'qq-digest' / 'avatars'
    identities = [f'qq:{group_id}']
    identities.extend(dict.fromkeys(message['author_id'] for message in messages
                                    if message['author_id'].isdecimal()))
    for identity in identities:
        is_group = identity.startswith('qq:')
        uin = str(group_id) if is_group else identity
        cache_path = cache_dir / (hashlib.sha256(identity.encode()).hexdigest() + '.image')
        data = None
        if cache_path.is_file() and not cache_path.is_symlink():
            try:
                data = cache_path.read_bytes()
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
            except Exception:
                data = None
        if data is None:
            try:
                url = (f'https://p.qlogo.cn/gh/{uin}/{uin}/640' if is_group
                       else f'https://q.qlogo.cn/headimg_dl?dst_uin={uin}&spec=100')
                data = _fetch_image(url, avatar=True)
                _private_file(cache_path, data)
            except Exception:
                avatars[identity] = {'available': False, 'reason': 'avatar_unavailable'}
                continue
        with Image.open(io.BytesIO(data)) as image:
            kind = {'JPEG': 'jpeg', 'PNG': 'png', 'WEBP': 'webp', 'GIF': 'gif'}.get(image.format, 'png')
        avatars[identity] = {'available': True,
            'data_uri': f'data:image/{kind};base64,' + base64.b64encode(data).decode('ascii')}
    return avatars


def attach_source_assets(report_paths, sources_snapshot, runtime_dir) -> dict:
    """Attach verified cached source images to report references; leave summaries and PNGs untouched."""
    report_paths = list(map(Path, report_paths))
    source = json.loads(Path(sources_snapshot).read_text(encoding='utf-8')) if isinstance(sources_snapshot, (str, Path)) else sources_snapshot
    if not isinstance(source, dict) or type(source.get('group_id')) is not int or not isinstance(source.get('messages'), list):
        raise ValueError('source snapshot has invalid group identity or message list')
    cache_dir = Path(runtime_dir).resolve() / 'qq-digest' / 'images'
    index_path = cache_dir / 'descriptors.json'
    index = json.loads(index_path.read_text(encoding='utf-8')) if index_path.is_file() else {}
    if not isinstance(index, dict):
        raise ValueError('QQ image descriptor cache is invalid')
    by_message = {message.get('message_uid'): message for message in source['messages'] if isinstance(message, dict)}
    ocr_path = cache_dir.parent / 'ocr.json'
    ocr_rows = json.loads(ocr_path.read_text(encoding='utf-8')) if ocr_path.is_file() else {}
    image_paths = {}
    for message_uid, message in by_message.items():
        refs = []
        items = message.get('image_items', [])
        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            descriptor_key = item.get('cache_key')
            if not isinstance(descriptor_key, str):
                if _segment_url(item):
                    descriptor_key = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True,
                        separators=(',', ':')).encode()).hexdigest()
            cached = index.get(descriptor_key) if isinstance(descriptor_key, str) else None
            if not isinstance(cached, dict):
                ocr_uid = message_uid if len(items) == 1 else f'{message_uid}:image-{item_index + 1}'
                ocr_entry = ocr_rows.get(ocr_uid, {}) if isinstance(ocr_rows, dict) else {}
                sha = ocr_entry.get('image_sha256') if isinstance(ocr_entry, dict) else None
                if isinstance(sha, str):
                    candidate_paths = list(cache_dir.glob(sha + '.*'))
                    cached_path = next((path for path in candidate_paths if path.is_file() and not path.is_symlink()), None)
                    if cached_path:
                        data = cached_path.read_bytes()
                        if hashlib.sha256(data).hexdigest() == sha:
                            try:
                                ext, _dimensions = _image_info(data)
                                cached = {'sha256': sha, 'extension': ext}
                                if isinstance(descriptor_key, str):
                                    index[descriptor_key] = cached
                            except Exception:
                                cached = None
            if not isinstance(cached, dict):
                continue
            sha, ext = cached.get('sha256'), cached.get('extension')
            if not isinstance(sha, str) or ext not in {'jpg', 'png', 'webp', 'gif'}:
                continue
            path = cache_dir / f'{sha}.{ext}'
            if not path.is_file():
                legacy_path = cache_dir / f'{sha}.image'
                path = legacy_path if legacy_path.is_file() else path
            if not path.is_file() or path.is_symlink():
                continue
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != sha or _image_info(data)[0] != ext:
                continue
            refs.append((sha, ext, path))
        image_paths[message_uid] = refs
    _private_json(index_path, index)
    attached = 0
    for report_path in map(Path, report_paths):
        report_path = report_path.resolve()
        report = json.loads(report_path.read_text(encoding='utf-8'))
        if report.get('chat_username') != f"qq:{source['group_id']}":
            raise ValueError('report and source snapshot group IDs do not match')
        asset_dir = report_path.parent / 'assets'
        asset_dir.mkdir(parents=True, exist_ok=True)
        for topic in report.get('topics', []):
            for reference in topic.get('references', []):
                message_uid = reference.get('message_uid')
                rows = image_paths.get(message_uid, [])
                assets = []
                for sha, ext, cache_path in rows:
                    name = f'{sha}.{ext}'
                    destination = asset_dir / name
                    if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != sha:
                        shutil.copyfile(cache_path, destination)
                    os.chmod(destination, 0o644)
                    assets.append('assets/' + name)
                if assets:
                    reference['image_sources'] = list(dict.fromkeys(assets))
                    attached += len(assets)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        html_path = report_path.with_suffix('.html')
        html_path.write_text(daily_report.render_html(report), encoding='utf-8')
    return {'reports': len(report_paths), 'images_attached': attached}


def _output_stem(group_id, start, end, request_id=None):
    request_id = uuid.uuid4().hex if request_id is None else request_id
    if not isinstance(request_id, str) or len(request_id) not in (32, 64) or any(char not in '0123456789abcdef' for char in request_id):
        raise ValueError('request-id must be 32 or 64 lowercase hexadecimal characters')
    return f'qq-{group_id}-{start}-{end}-{request_id}'


class PermanentProviderRequestError(RuntimeError):
    def __init__(self, stage: str, status: int, details: dict[str, str]):
        super().__init__(f'model provider returned HTTP {status} during {stage}')
        self.stage, self.status, self.details = stage, status, details


def _safe_provider_error(value: object, config_path: Path, group_id: int) -> dict[str, str]:
    if isinstance(value, str):
        value = {'message': value}
    if not isinstance(value, dict):
        return {}
    config = _load_config(config_path)
    secrets = [provider.get('api_key') for provider in digest.model_providers(config)
               if isinstance(provider.get('api_key'), str) and provider.get('api_key')]

    def safe_text(item: object, limit: int) -> str | None:
        if not isinstance(item, (str, int)) or isinstance(item, bool):
            return None
        text = str(item)
        for secret in secrets:
            text = text.replace(secret, '[REDACTED]')
        text = re.sub(r'https?://[^\s\"\'<>]+', '[URL]', text, flags=re.I)
        text = re.sub(r'(?i)\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{12,}', '[REDACTED]', text)
        text = re.sub(r'(?i)(ticket|auth[_-]?key|sign|token|api[_-]?key)([\"\']?\s*[:=]\s*[\"\']?)[^\"\'\s,;}]+', r'\1=[REDACTED]', text)
        text = re.sub(r'(?i)(authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]+', r'\1=[REDACTED]', text)
        text = re.sub(rf'\b{re.escape(str(group_id))}\b', '[RECEIVER]', text)
        text = re.sub(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', '[EMAIL]', text)
        text = re.sub(r'\b\d{5,}\b', '[ID]', text)
        text = re.sub(r'([\"\']).{24,}?\1', '[REDACTED]', text)
        text = ''.join(ch if ch.isprintable() else ' ' for ch in text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:limit] or None

    return {field: text for field, limit in (('code', 80), ('type', 80), ('param', 80), ('message', 512))
            if (text := safe_text(value.get(field), limit))}


def _latest_provider_error(runtime: Path, started_ns: int, config_path: Path, group_id: int) -> dict[str, str]:
    directory = runtime / 'qq-digest' / 'summary-diagnostics'
    if not directory.is_dir() or directory.is_symlink():
        return {}
    candidates = []
    for path in directory.glob('summary-*.json'):
        try:
            stamp = int(path.stem.removeprefix('summary-'))
            if stamp > started_ns and path.is_file() and not path.is_symlink():
                candidates.append((stamp, path))
        except ValueError:
            continue
    for _stamp, path in sorted(candidates, reverse=True):
        try:
            diagnostic = json.loads(path.read_text(encoding='utf-8'))
            response = diagnostic.get('response') if isinstance(diagnostic, dict) else None
            body = response.get('body') if isinstance(response, dict) and diagnostic.get('reason') == 'http_400' else None
            envelope = json.loads(body) if isinstance(body, str) else None
            error = envelope.get('error') if isinstance(envelope, dict) else None
            safe = _safe_provider_error(error, config_path, group_id)
            if safe:
                return safe
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            continue
    return {}


class QQDigestStageError(RuntimeError):
    def __init__(self, stage: str, source: str, error_type: str, origin: str,
                 http_status: int | None, message_uid: str | None = None):
        super().__init__(f'QQ digest failed during {stage}')
        self.stage, self.source, self.error_type = stage, source, error_type
        self.origin, self.http_status, self.message_uid = origin, http_status, message_uid


def _stage_call(stage: str, function, *args, **kwargs):
    import traceback
    try:
        return function(*args, **kwargs)
    except (PermanentProviderRequestError, QQDigestStageError):
        raise
    except Exception as exc:
        source_exc = exc.__cause__ if isinstance(exc, QQImageFetchError) and exc.__cause__ else exc
        frames = traceback.extract_tb(source_exc.__traceback__)
        frame = frames[-1] if frames else None
        source = f'{Path(frame.filename).name}:{frame.name}:{frame.lineno}' if frame else 'unknown'
        root = source_exc
        while root.__cause__ is not None:
            root = root.__cause__
        status = getattr(root, 'code', None)
        uid = exc.message_uid if isinstance(exc, QQImageFetchError) else None
        origin = 'provider' if stage.startswith('summary:') else 'qq_digest'
        raise QQDigestStageError(stage, source, type(source_exc).__name__, origin,
                                 status if type(status) is int else None, uid) from exc


def _summary_for_edition(config_path: Path, messages: list[dict], runtime: Path,
                         edition: str, group_id: int) -> dict:
    started_ns = time.time_ns()
    try:
        return digest._summary_with_cache(config_path, messages, runtime / 'qq-digest', edition=edition)
    except RuntimeError as exc:
        if str(exc) == 'AI request failed with HTTP 400':
            details = _latest_provider_error(runtime, started_ns, config_path, group_id)
            raise PermanentProviderRequestError(f'summary:{edition}', 400, details) from exc
        raise


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources-json', type=Path, required=True)
    parser.add_argument('--model-config', type=Path, required=True)
    parser.add_argument('--fallback-config', type=Path)
    parser.add_argument('--runtime-dir', type=Path, required=True)
    parser.add_argument('--group-rkey-file', type=Path)
    parser.add_argument('--group-id', type=int, required=True)
    parser.add_argument("--request-id")
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--start', type=int, required=True)
    parser.add_argument('--end', type=int, required=True)
    return parser.parse_args(argv)


def run(args) -> dict:
    if type(args.group_id) is not int or args.group_id <= 0:
        raise ValueError('--group-id must be a positive integer')
    if not (1 <= args.end - args.start <= MAX_WINDOW):
        raise ValueError('--start/--end must define a window from 1 to 604800 seconds')
    snapshot = json.loads(args.sources_json.read_text(encoding='utf-8'))
    if (not isinstance(snapshot, dict) or type(snapshot.get('group_id')) is not int
            or snapshot.get('group_id') != args.group_id
            or not isinstance(snapshot.get('group_name'), str) or not isinstance(snapshot.get('messages'), list)):
        raise ValueError('sources snapshot group_id must match --group-id and include group_name and messages')
    messages = snapshot['messages']
    if not messages:
        raise ValueError('refusing to generate a report with zero messages')
    required = {'message_uid', 'reference', 'timestamp', 'author_id', 'author', 'type', 'text', 'image_items'}
    normalized = []
    seen = set()
    for index, message in enumerate(messages, 1):
        if not isinstance(message, dict) or not required.issubset(message):
            raise ValueError(f'source message {index} does not have the unified message fields')
        if not isinstance(message['author_id'], str):
            raise ValueError(f'source message {index} author_id must be a string')
        if type(message['timestamp']) is not int:
            raise ValueError(f'source message {index} timestamp must be an integer')
        if not args.start <= message['timestamp'] < args.end:
            continue
        row = {key: message[key] for key in required}
        if not isinstance(row['message_uid'], str) or not row['message_uid'] or row['message_uid'] in seen:
            raise ValueError('message_uid must be unique and non-empty')
        seen.add(row['message_uid'])
        if not isinstance(row['reference'], str) or not row['reference']:
            raise ValueError('message reference must be non-empty')
        if not isinstance(row['author'], str) or not isinstance(row['type'], str) or not isinstance(row['text'], str):
            raise ValueError('message author, type and text must be strings')
        row['auxiliary_context'] = {}
        normalized.append(row)
    if not normalized:
        raise ValueError('refusing to generate a report with zero messages in the selected window')

    runtime, output = args.runtime_dir.resolve(), args.output_dir.resolve()
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime, 0o700)
    os.chmod(output, 0o700)
    primary = _load_config(args.model_config)
    fallback = _load_config(args.fallback_config) if args.fallback_config else None
    group_rkey = _load_group_rkey_file(args.group_rkey_file, runtime, args.group_id)
    image_context, image_counts = _stage_call('image_preparation', _prepare_images, normalized, runtime,
                                              primary, fallback, group_rkey)
    for message in normalized:
        entry = image_context.get(message['message_uid'])
        if entry:
            message['image_understanding'] = {
                'description': entry['result'].get('description', ''),
                'visible_text': '', 'facts': [], 'uncertainties': [],
                'model': entry['result'].get('model', ''), 'source_quality': 'original'}
        else:
            message.pop('image_understanding', None)
    web_context = _stage_call('web_context', _web_context, normalized, runtime, primary)
    auxiliary = {}
    for source in (image_context, web_context):
        for uid, value in source.items():
            auxiliary.setdefault(uid, {}).update(value)
    for message in normalized:
        message['auxiliary_context'] = auxiliary.get(message['message_uid'], {})
    summaries = {edition: _stage_call(f'summary:{edition}', _summary_for_edition,
                                      args.model_config.resolve(), normalized, runtime, edition, args.group_id)
                 for edition in ('brief', 'review')}
    avatars = _stage_call('avatars', _group_avatar, runtime, normalized, args.group_id)
    paths, reports = {}, {}
    base = _output_stem(args.group_id, args.start, args.end, getattr(args, "request_id", None))
    for edition in ('brief', 'review'):
        report = _stage_call(f'report:{edition}', _build_report, normalized, snapshot, args.group_id,
                             args.start, args.end, summaries[edition], edition, avatars)
        if image_counts:
            report['media_understanding']['images'].update(image_counts)
        stem = f'{base}-{edition}'
        html_path, json_path, png_path = output / f'{stem}.html', output / f'{stem}.json', output / f'{stem}.png'
        _stage_call(f'render:{edition}', _render_edition, report, html_path, json_path, png_path)
        paths[edition] = {'html': str(html_path), 'json': str(json_path), 'png': str(png_path)}
        reports[edition] = report
    sources_path = output / f'{base}-sources.json'
    _private_json(sources_path, {'group_id': args.group_id, 'group_name': snapshot['group_name'],
        'window': {'start_unix': args.start, 'end_unix_exclusive': args.end},
        'messages': [{key: value for key, value in message.items() if key != 'auxiliary_context'}
                     for message in _safe_public_snapshot({'group_id': args.group_id,
                         'group_name': snapshot['group_name'], 'messages': normalized})['messages']]})
    _stage_call('source_assets', attach_source_assets,
                [paths[edition]['json'] for edition in ('brief', 'review')], sources_path, runtime)
    return {'files': {'editions': paths, 'sources': str(sources_path)},
            'counts': {'messages': len(normalized), 'images': image_counts.get('total', 0),
                       'members': reports['brief']['stats']['members']}}


def _render_edition(report: dict, html_path: Path, json_path: Path, png_path: Path) -> None:
    html_path.write_text(daily_report.render_html(report), encoding='utf-8')
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    daily_report.render_png_pillow(report, png_path)

def main(argv=None) -> int:
    try:
        result = run(_parse_args(argv))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except PermanentProviderRequestError as exc:
        print(json.dumps({'error': 'model provider rejected the generation request',
                          'error_type': type(exc).__name__, 'stage': exc.stage,
                          'origin': 'model_provider', 'http_status': exc.status,
                          'source': 'deployment/qq_digest.py:_summary_for_edition',
                          'provider_error': exc.details}, ensure_ascii=False), file=sys.stderr)
        return 1
    except QQDigestStageError as exc:
        print(json.dumps({'error': 'QQ digest stage failed', 'error_type': exc.error_type,
                          'stage': exc.stage, 'origin': exc.origin, 'http_status': exc.http_status,
                          'source': exc.source, 'message_uid': exc.message_uid}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({'error': str(exc), 'error_type': type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

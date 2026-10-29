from __future__ import annotations
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zlib


_SENSITIVE_NAMES = {'fullmd5', 'thumbfullmd5', 'md5', 'aeskey', 'encryptkey', 'filekey', 'ticket', 'token'}
_TEXT_TAGS = {'title', 'des', 'description', 'content', 'displayname', 'filename', 'datadesc', 'text', 'label'}


def _text(value):
    if value is None: return ''
    if isinstance(value, bytes):
        for wbits in (zlib.MAX_WBITS, zlib.MAX_WBITS | 16, -zlib.MAX_WBITS):
            try: return zlib.decompress(value, wbits).decode('utf-8', 'replace')
            except zlib.error: pass
        return value.decode('utf-8', 'replace')
    return str(value)


def split_group_sender(value):
    text = _text(value)
    match = re.match(r'^([A-Za-z0-9_@.\-]{1,128}):\n(.*)$', text, re.S)
    return (match.group(1), match.group(2)) if match else ('', text)


def _safe_field(tag, value):
    name = tag.lower().replace('-', '').replace('_', '')
    if any(sensitive.replace('_', '') in name for sensitive in _SENSITIVE_NAMES): return ''
    text = (value or '').strip()
    if not text: return ''
    if name in {'url', 'link'}:
        parsed = urllib.parse.urlsplit(text)
        query_names = {key.lower() for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)}
        if any(word in parsed.netloc.lower() for word in ('cdn', 'qpic')) or any(
                any(token in key for token in ('aeskey', 'ticket', 'token', 'key')) for key in query_names):
            return ''
    return text


def _record_text(item):
    if item is None: return ''
    nested = None
    raw = item.text or ''
    for candidate in (raw, html.unescape(raw)):
        candidate = candidate.strip()
        if candidate.startswith('<'):
            try:
                nested = ET.fromstring(candidate)
                break
            except ET.ParseError:
                continue
    if nested is None:
        nested = item.find('recordinfo')
    if nested is None: return ''
    parts = []
    for node in nested.iter():
        tag = node.tag.rsplit('}', 1)[-1].lower() if isinstance(node.tag, str) else ''
        if tag not in _TEXT_TAGS: continue
        value = _safe_field(tag, node.text)
        if value and value not in parts: parts.append(value)
    return '\n'.join(parts)


def parse_app_message(value):
    text = _text(value)
    start = text.find('<')
    if start < 0: return {}
    try: root = ET.fromstring(text[start:])
    except ET.ParseError: return {}
    app = root.find('.//appmsg')
    refer = root.find('.//refermsg')
    if app is None and refer is None: return {}
    container = app if app is not None else refer
    def find(node, tag):
        child = node.find('.//' + tag)
        return _safe_field(tag, child.text) if child is not None else ''
    title = find(container, 'title') or find(container, 'displayname')
    description = find(container, 'des') or find(container, 'description') or find(container, 'content')
    url = find(container, 'url')
    record = _record_text(container.find('.//recorditem')) if app is not None else ''
    return {'title': title, 'description': description, 'app_url': url,
            'type': container.findtext('type', ''), 'record': record}


def message_index_text(row: dict) -> tuple[str, str]:
    kind = str(row.get('type_label') or 'unknown').lower()
    source = row.get('message_content')
    if source is None: source = row.get('compress_content')
    sender, body = split_group_sender(source)
    if kind in {'appmsg', 'link', 'link_or_file', 'record', 'refer'}:
        app = parse_app_message(body)
        text = '\n'.join(value for value in (app.get('title'), app.get('description'), app.get('app_url'), app.get('record')) if value)
        return sender, text
    if kind in {'image', 'video', 'voice', 'emoji', 'media'}: return sender, '[{}]'.format(kind)
    if kind in {'system', 'recall'}: return '', body
    return sender, body


def decode_record(value):
    return parse_app_message(value)


def parse_refer(value):
    data = parse_app_message(value)
    return {'title': data.get('title', ''), 'content': data.get('description', ''), 'url': data.get('app_url', '')}

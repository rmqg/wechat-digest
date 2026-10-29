"""Displayed body count and compact report statistics; raw metadata stays unchanged."""
import re

_HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ee5f\U0002f800-\U0002fa1f\U00030000-\U0003347f]')


def body_chinese_count(report: dict) -> int:
    parts = [report.get('one_line') or '']
    for topic in report.get('topics') or []:
        parts.extend((topic.get('topic') or '', topic.get('summary') or ''))
        details = topic.get('details')
        parts.extend(details if isinstance(details, list) else topic.get('samples') or [])
    if report.get('edition') != 'brief':
        parts.extend(quote.get('text') or '' for quote in report.get('quotes') or [])
    return sum(1 for part in parts for _ in _HAN.finditer(str(part)))


def peak_time_text(report: dict) -> str:
    name = str(((report.get('leaders') or {}).get('peak') or {}).get('name') or '—')
    return re.sub(r'^\d{4}-(?=\d{2}-\d{2}(?:\s|$))', '', name)

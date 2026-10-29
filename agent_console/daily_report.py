"""Independent HTML and Pillow renderer for public daily reports."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from .report_metrics import body_chinese_count, peak_time_text
except ImportError:  # direct script execution
    from report_metrics import body_chinese_count, peak_time_text

DISPLAY_TZ = ZoneInfo('Asia/Shanghai')

RHYTHM_CHART_CSS = """
.rhythm-chart{width:100%;height:auto;display:block}.rhythm-chart .grid{stroke:#e6eaf0;stroke-width:1}
.rhythm-chart .axis{stroke:#aab4c1;stroke-width:1}.rhythm-chart .line{fill:none;stroke:#5879d8;stroke-width:3;stroke-linejoin:round;stroke-linecap:round}
.rhythm-chart .rhythm-point{fill:#fff;stroke:#5879d8;stroke-width:2}.rhythm-chart text{font:12px sans-serif;fill:#687386}.rhythm-chart .axis-hour{fill:#fff;stroke:#aab4c1;stroke-width:1}
"""
SOURCE_MESSAGES_CSS = """
 .avatar{width:36px;height:36px;border-radius:50%;object-fit:cover;background:#e8edf5;flex:none}.avatar-fallback{display:grid;place-items:center;color:#526078;font-size:13px}.source-body{min-width:0;flex:1}.source-meta{color:#7b8492;font-size:12px;margin-bottom:5px}.source-author{font-weight:650;color:#303947}.source-text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.65;color:#384252;margin:0}.source-quote{margin:8px 0 0;padding:9px 12px;border-radius:9px;background:#f0f1f3;color:#68717c}.source-image{display:block;max-width:min(100%,420px);max-height:360px;object-fit:contain;margin:8px 0;border-radius:8px}.source-unavailable{color:#8992a0;font-size:12px}.sources{list-style:none;padding:0;margin:12px 0;display:grid;gap:12px}.source{display:flex;gap:10px;align-items:flex-start;padding:12px;border:1px solid #e8ebf0;border-radius:12px;background:#fff}.lede{font-size:18px;line-height:1.7}.topic{padding:14px 0;border-bottom:1px solid #edf0f4}.topic h3{margin:0 0 8px}.topic-summary,.topic-details{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7}.rank-list{padding-left:26px}.rank-list li{padding:7px 2px}.rank-list strong{float:right}details>summary{cursor:pointer;color:#5879d8}
 .source-body>b{display:block;font-weight:650;color:#303947}.source-meta{margin:3px 0 6px;color:#7b8492;font-size:12px}.source-gallery{display:flex;flex-wrap:wrap;gap:8px}.source-image-link{display:block}
 .rank{padding-left:26px}.rank li{padding:7px 2px}.rank strong{float:right}
 .chart{width:100%;overflow-x:auto}.topic-time{color:#7a8494;font-size:13px}.topic-summary,.summary{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7}.body-chinese-count{color:#7a8494;font-size:13px}
"""


def _report_timezone(report=None):
    """Use report metadata timezone, then WECHAT_DIGEST_TIMEZONE, then Asia/Shanghai."""
    name = (report or {}).get('timezone') or os.environ.get('WECHAT_DIGEST_TIMEZONE')
    if not name:
        return DISPLAY_TZ
    try:
        return ZoneInfo(str(name))
    except (KeyError, ValueError) as exc:
        raise ValueError(f'Unsupported report timezone: {name}') from exc


def time_text(value, fmt='%m-%d %H:%M', timezone=None):
    try:
        return datetime.fromtimestamp(float(value), timezone or DISPLAY_TZ).strftime(fmt)
    except (TypeError, ValueError, OSError, OverflowError):
        return ''


def topic_time_range(topic, timezone=None):
    start, end = topic.get('start_unix'), topic.get('end_unix')
    if start is None or end is None:
        return ''
    first, last = time_text(start, '%H:%M', timezone), time_text(end, '%H:%M', timezone)
    return f'{first}–{last}' if first and last else ''


def _font_candidates():
    configured = os.environ.get('WECHAT_DIGEST_FONT')
    bundled = Path(__file__).resolve().parent.parent / 'assets' / 'fonts'
    system = [
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'),
        Path('/System/Library/Fonts/PingFang.ttc'),
        Path('/System/Library/Fonts/STHeiti Light.ttc'),
        Path('/Library/Fonts/Arial Unicode.ttf'),
        Path('/usr/share/fonts/truetype/arphic/uming.ttc'),
    ]
    return ([Path(configured)] if configured else []) + [
        bundled / 'ResourceHanRoundedCN-Regular.woff2',
        bundled / 'ResourceHanRoundedCN-Regular.ttf',
    ] + system


def rounded_webfont_css():
    """Inline the configured or bundled full CJK webfont for portable HTML."""
    regular = _font_candidates()
    bundled = Path(__file__).resolve().parent.parent / 'assets' / 'fonts'
    bold = [bundled / 'ResourceHanRoundedCN-Bold.ttf', bundled / 'ResourceHanRoundedCN-Bold.woff2']
    declarations = []
    for weight, candidates in ((400, regular), (700, bold)):
        for candidate in candidates:
            try:
                if candidate.is_file():
                    data = base64.b64encode(candidate.read_bytes()).decode('ascii')
                    suffix = candidate.suffix.lower()
                    fmt = 'woff2' if suffix == '.woff2' else 'truetype' if suffix in ('.ttf', '.ttc') else 'opentype'
                    mime = 'font/woff2' if fmt == 'woff2' else 'font/ttf' if fmt == 'truetype' else 'font/otf'
                    declarations.append(f"@font-face{{font-family:DigestCJK;src:url(data:{mime};base64,{data}) format('{fmt}');font-weight:{weight}}}")
                    break
            except OSError:
                continue
    return ''.join(declarations)


def font_path():
    for candidate in _font_candidates():
        if candidate.suffix.lower() == '.woff2':
            continue
        if candidate.is_file():
            return candidate
    raise RuntimeError('No CJK font found. Set WECHAT_DIGEST_FONT to an installed Pillow-compatible .ttf/.ttc/.otf font.')


def build_rhythm_buckets(timestamps, start, end):
    start, end = int(start), int(end)
    if end <= start:
        return []
    first = (start // 600) * 600
    buckets = []
    cursor = first
    while cursor < end:
        left, right = max(cursor, start), min(cursor + 600, end)
        buckets.append({'start_unix': left, 'end_unix_exclusive': right, 'count': 0,
                        'label': time_text(left, '%Y-%m-%d %H:%M')})
        cursor += 600
    for stamp in timestamps:
        try:
            stamp = float(stamp)
        except (TypeError, ValueError):
            continue
        if start <= stamp < end:
            idx = min(int((stamp - first) // 600), len(buckets) - 1)
            buckets[idx]['count'] += 1
    return buckets


def _rhythm_svg(report):
    timezone = _report_timezone(report)
    values = report.get('rhythm_buckets') or []
    width, height, left, right, top, bottom = 900, 290, 58, 20, 20, 55
    plot_w, plot_h = width-left-right, height-top-bottom
    n = len(values)
    max_value = max([int(x.get('count', 0) or 0) for x in values] or [0])
    y_max = max(1, max_value)
    y = lambda v: top + plot_h * (1 - v/y_max)
    axis_start = int(values[0].get('start_unix', 0)) if values else 0
    axis_end = int(values[-1].get('end_unix_exclusive', axis_start + len(values)*600)) if values else 0
    duration = max(axis_end-axis_start, 1)
    x_at = lambda stamp: left + plot_w * (float(stamp)-axis_start) / duration
    lines = [f'<svg class="rhythm-chart" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="消息节奏">']
    ticks = sorted({int(round(y_max * tick / 4)) for tick in range(5)})
    for v in ticks:
        yy = y(v)
        lines.append(f'<line class="grid" x1="{left}" y1="{yy:.2f}" x2="{width-right}" y2="{yy:.2f}"/><text x="{left-10}" y="{yy+4:.2f}" text-anchor="end">{v}</text>')
    points = [(x_at(item.get('start_unix', axis_start)), y(int(item.get('count', 0) or 0))) for item in values]
    lines.append(f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}"/><line class="axis" x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}"/>')
    # Hourly ticks are drawn at their actual times below; bucket points stay on the curve.
    if points:
        point_str = ' '.join(f'{px:.2f},{py:.2f}' for px, py in points)
        lines.append(f'<polyline class="line" points="{point_str}"/>')
        for px, py in points:
            lines.append(f'<circle class="rhythm-point" cx="{px:.2f}" cy="{py:.2f}" r="3.5"/>')
    if values:
        first_ts = values[0].get('start_unix')
        last_ts = values[-1].get('end_unix_exclusive', values[-1].get('start_unix'))
        if first_ts is not None:
            lines.append(f'<text x="{left}" y="{height-16}" text-anchor="start">{escape(time_text(first_ts, "%Y-%m-%d", timezone))}</text>')
        if last_ts is not None:
            lines.append(f'<text x="{width-right}" y="{height-16}" text-anchor="end">{escape(time_text(last_ts, "%Y-%m-%d", timezone))}</text>')
        # Hour labels and ticks are placed on the actual time axis, including clipped bins.
        hour = ((axis_start + 3599) // 3600) * 3600
        while hour <= axis_end:
            ratio = (hour-axis_start) / max(axis_end-axis_start, 1)
            px = left + plot_w * ratio
            if 0 <= px <= width:
                lines.append(f'<circle class="axis-hour" cx="{px:.2f}" cy="{height-bottom}" r="2"/>')
                local = datetime.fromtimestamp(hour, timezone)
                if local.minute == 0 and local.hour % 2 == 0:
                    lines.append(f'<text class="chart-hour" x="{px:.2f}" y="{height-bottom+22}" text-anchor="middle">{local:%H}</text>')
            hour += 3600
    lines.append('</svg>')
    return ''.join(lines)


def rhythm_chart_contents(report):
    return _rhythm_svg(report)


def _safe_asset(value):
    if not isinstance(value, str):
        return ''
    if re.fullmatch(r'(?:\.\./assets/|\./assets/|assets/)?[A-Za-z0-9_-]{8,128}\.(?:png|jpe?g|webp|gif)', value, re.I):
        return value
    if value.startswith('data:image/') and len(value) <= 2_000_000 and re.fullmatch(r'data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+', value):
        return value
    return ''


def _avatar_html(author_id, author, avatars):
    avatar = (avatars or {}).get(str(author_id or ''), {})
    src = ''
    if isinstance(avatar, dict) and avatar.get('available'):
        src = _safe_asset(avatar.get('asset_src')) or _safe_asset(avatar.get('data_uri'))
    if src:
        return f"<img class='avatar source-avatar' src='{escape(src, quote=True)}' alt='' loading='lazy'>"
    initials = ''.join(str(author).split()[:1])[:2] or '？'
    return f"<span class='avatar avatar-fallback' aria-label='{escape(str(author), quote=True)}'>{escape(initials)}</span>"


def _quote_source(quote):
    """Resolve quote provenance without exposing internal citation identifiers."""
    source = quote.get('source') if isinstance(quote, dict) else None
    if isinstance(source, dict):
        return source
    quote = quote if isinstance(quote, dict) else {}
    identity = quote.get('speaker_id') or quote.get('author_id')
    return {
        'author': quote.get('speaker') or quote.get('author') or '未知成员',
        'author_id': identity,
        'speaker_id': identity,
        'time': quote.get('time') or '',
        'timestamp': quote.get('timestamp'),
        'type': 'text',
        'text': quote.get('text') or '',
    }


def _format_inline_quote(text):
    text = str(text or '')
    match = re.search(r'(?:收到\s*[&＆]?\s*)?引用\s*[^:：\n]{0,80}[:：]\s*(.+)$', text, re.S)
    if not match:
        return escape(text), '', False
    main = text[:match.start()].strip().rstrip('；;:：').strip()
    return escape(main), escape(text[match.start():].strip()), True


def _source_images_html(images):
    entries = []
    for raw in images or []:
        src = _safe_asset(raw)
        if src:
            safe_src = escape(src, quote=True)
            entries.append(f"<a class='source-image-link' href='{safe_src}' target='_blank' rel='noopener'><img class='source-image' src='{safe_src}' alt='聊天图片' loading='lazy'></a>")
    return "<div class='source-gallery'>" + ''.join(entries) + '</div>' if entries else ''


def source_messages_html(messages, avatars=None, timezone=None):
    rendered = []
    for message in messages or []:
        author = str(message.get('author') or message.get('speaker') or '未知成员')
        author_id = message.get('author_id') or message.get('speaker_id')
        timestamp = message.get('timestamp', message.get('time_unix'))
        time = time_text(timestamp, '%m.%d %H:%M', timezone) if timestamp is not None else str(message.get('time') or '')
        text = str(message.get('text') or '')
        images = message.get('image_sources') or ([message.get('image_src')] if message.get('image_src') else [])
        image_html = _source_images_html(images)
        is_image_placeholder = bool(re.fullmatch(r'[【\[]图片[】\]]', text.strip()))
        main_html, quote_text, quoted = _format_inline_quote(text) if text and not is_image_placeholder else ('', '', False)
        quote_html = ("<div class='source-quote' role='blockquote'><p>" + quote_text + '</p>' + image_html + '</div>') if quoted else ''
        unavailable = message.get('image_unavailable_reason')
        unavailable_html = f"<div class='source-unavailable'>{escape(str(unavailable))}</div>" if unavailable and not image_html else ''
        if not (main_html or quote_html or image_html or unavailable_html):
            continue
        time_html = f"<div class='source-meta'>{escape(time)}</div>" if time else ''
        main = f"<p class='source-text'>{main_html}</p>" if main_html else ''
        body = main + quote_html + ('' if quoted else image_html) + unavailable_html
        rendered.append("<li class='source'>" + _avatar_html(author_id, author, avatars) + "<div class='source-body'><b>" + escape(author) + '</b>' + time_html + body + '</div></li>')
    return ''.join(rendered)


def _topic_refs(topic):
    return topic.get('references') if isinstance(topic.get('references'), list) else []


def ranked_participants(report):
    rows = report.get('participants') or report.get('active_rank') or []
    def order(row):
        count = row.get('count', row.get('messages', 0)) or 0
        identity = row.get('username') or row.get('id') or row.get('participant_id') or row.get('account_id') or row.get('user_id') or row.get('author_id') or row.get('speaker_id') or ''
        return -int(count), str(identity)
    return sorted(rows, key=order)[:20]


def rank_rows_html(report):
    avatars = report.get('avatars') or {}
    result = []
    for row in ranked_participants(report):
        name = str(row.get('name') or row.get('display_name') or row.get('author') or '未知成员')
        identity = row.get('username') or row.get('id') or row.get('participant_id') or row.get('account_id') or row.get('user_id') or row.get('author_id') or row.get('speaker_id')
        count = row.get('count', row.get('messages', 0)) or 0
        result.append(f"<li>{_avatar_html(identity, name, avatars)}<span class='rank-name'>{escape(name)}</span><strong>{escape(str(count))}条</strong></li>")
    return ''.join(result)


def render_html(report):
    timezone = _report_timezone(report)
    edition = 'brief' if report.get('edition') == 'brief' else 'review'
    title = str(report.get('report_kind') or '群聊日报') + (' · 简报' if edition == 'brief' else ' · 回顾')
    chat = report.get('chat_display') or report.get('chat_username') or '聊天日报'
    topics = report.get('topics') or []
    avatars = report.get('avatars') or {}
    parts = ["<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>",
             escape(title), ' · ', escape(str(chat)), '</title><style>', rounded_webfont_css(),
             "body{margin:0;background:#f4f6fa;color:#273142;font-family:DigestCJK,'Noto Sans CJK SC',sans-serif}.page{max-width:900px;margin:32px auto;padding:0 22px}.hero,.card{background:#fff;border-radius:18px;padding:24px;margin-bottom:18px;box-shadow:0 3px 18px #25334a0a}.hero h1{margin:0 0 8px}.muted{color:#7a8494}.stats{display:flex;gap:12px;flex-wrap:wrap}.stat{padding:12px 16px;background:#f3f5f9;border-radius:12px}.topic{padding:14px 0;border-bottom:1px solid #edf0f4}.topic:last-child{border:0}.detail{white-space:pre-wrap;line-height:1.7}.rank-list{padding-left:26px}.rank-list li{padding:7px 2px}.rank-list strong{float:right}.brief-count{color:#7a8494;font-size:13px}h2{font-size:19px;margin-top:0}a{color:#5879d8}",
             RHYTHM_CHART_CSS, SOURCE_MESSAGES_CSS,
             '</style></head><body><main class="page"><header class="hero"><h1>', escape(title),
             '</h1><div>', escape(str(chat)), '</div><p class="muted">',
             escape(str(report.get('range_text') or report.get('day') or '')),
             '</p><p class="muted">生成时间：', escape(str(report.get('generated_at') or '')), '</p></header>']
    stats = report.get('stats') or {}
    labels = [('messages', '消息总量'), ('members', '活跃成员'), ('groups', '话题数')]
    parts.append('<section class="card"><h2>今日概览</h2><div class="stats">')
    for key, label in labels:
        if key in stats:
            parts.append(f'<div class="stat"><span>{label}</span> <strong>{escape(str(stats[key]))}</strong></div>')
    parts.append('</div>')
    if report.get('one_line'):
        parts.append(f'<p class="lede">{escape(str(report["one_line"]))}</p>')
    if report.get('leaders', {}).get('peak'):
        peak = report['leaders']['peak']
        parts.append(f'<p>消息峰值：{escape(peak_time_text(report))}（{escape(str(peak.get("count", 0)))}条）</p>')
    parts.append(f'<p class="body-chinese-count">正文汉字：{body_chinese_count(report)}</p></section><section class="card"><h2>消息节奏</h2><div class="chart">{rhythm_chart_contents(report)}</div></section><section class="card"><h2>话题摘要</h2>')
    for topic_index, topic in enumerate(topics, 1):
        parts.append(f'<article class="topic" id="topic-{topic_index}"><h3>' + escape(str(topic.get('topic') or '未命名话题')) + '</h3>')
        time_range = topic_time_range(topic, timezone)
        if time_range:
            parts.append(f'<div class="topic-time">{escape(time_range)}</div>')
        if topic.get('summary'):
            parts.append(f'<p class="topic-summary summary">{escape(str(topic["summary"]))}</p>')
        details = topic.get('details')
        if isinstance(details, list) and details:
            parts.append('<ul class="topic-details details">')
            for detail in details:
                parts.append(f'<li>{escape(str(detail))}</li>')
            parts.append('</ul>')
        elif details:
            parts.append(f'<ul class="topic-details details"><li>{escape(str(details))}</li></ul>')
        refs = _topic_refs(topic)
        if refs:
            parts.append('<details><summary>查看来源</summary><ul class="sources">' + source_messages_html(refs, avatars, timezone) + '</ul></details>')
        parts.append('</article>')
    parts.append('</section>')
    if edition == 'review':
        quotes = report.get('quotes') or []
        if quotes:
            parts.append('<section class="card"><h2>群友金句</h2>')
            for quote in quotes:
                text = quote.get('text')
                if not text:
                    continue
                source = _quote_source(quote)
                speaker = escape(str(quote.get('speaker') or source.get('author') or '群友'))
                raw_time = quote.get('time') or source.get('time')
                if not raw_time and source.get('timestamp') is not None:
                    raw_time = time_text(source.get('timestamp'), '%m.%d %H:%M', timezone)
                when = escape(str(raw_time or ''))
                footer = speaker + (f' · {when}' if when else '')
                if source.get('text') or source.get('image_src') or source.get('image_sources'):
                    footer += '<details><summary>查看来源</summary><ul class="sources">' + source_messages_html([source], avatars, timezone) + '</ul></details>'
                parts.append('<blockquote><p>' + escape(str(text)) + '</p><footer>' + footer + '</footer></blockquote>')
            parts.append('</section>')
        rank = report.get('active_rank') or report.get('participants') or []
        if rank:
            parts.append('<section class="card"><h2>活跃前20名</h2><ol class="rank">' + rank_rows_html(report) + '</ol></section>')
    parts.append('</main></body></html>')
    return ''.join(parts)


def _wrap_text(draw, text, font, max_width):
    lines = []
    for paragraph in str(text).splitlines() or ['']:
        line = ''
        for char in paragraph:
            if line and draw.textlength(line + char, font=font) > max_width:
                lines.append(line)
                line = char
            else:
                line += char
        lines.append(line)
    return lines


def render_png_pillow(report, output_path):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError('Pillow is required for PNG report output') from exc
    timezone = _report_timezone(report)
    font_file = font_path()
    regular = ImageFont.truetype(str(font_file), 20)
    title_font = ImageFont.truetype(str(font_file), 34)
    heading_font = ImageFont.truetype(str(font_file), 25)
    small = ImageFont.truetype(str(font_file), 16)
    width, margin = 1100, 64
    title = str(report.get('report_kind') or '群聊日报') + (' · 简报' if report.get('edition') == 'brief' else ' · 回顾')
    topics = report.get('topics') or []
    stats = report.get('stats') or {}
    stat_labels = {'messages': '消息总量', 'total_messages': '消息总量', 'members': '活跃成员', 'active_members': '活跃成员', 'groups': '话题数量', 'images': '图片数量'}
    stat_text = [f'{stat_labels.get(key, str(key))}：{value}' for key, value in stats.items()]
    measure_texts = [title, str(report.get('chat_display') or report.get('chat_username') or '聊天日报'), str(report.get('range_text') or report.get('day') or ''), str(report.get('generated_at') or ''), str(report.get('one_line') or ''), *stat_text]
    if report.get('leaders', {}).get('peak'):
        peak = report['leaders']['peak']
        measure_texts.append(f"消息峰值：{peak_time_text(report)}（{peak.get('count', 0)}条）")
    for topic in topics:
        measure_texts.extend((str(topic.get('topic') or '未命名话题'), str(topic.get('summary') or ''), topic_time_range(topic, timezone)))
        details = topic.get('details')
        measure_texts.extend(str(item) for item in (details if isinstance(details, list) else [details]))
    if report.get('edition') != 'brief':
        measure_texts.extend(f"{q.get('speaker') or '群友'}：{q.get('text') or ''}" for q in report.get('quotes') or [] if q.get('text'))
        rank_rows = report.get('participants') or report.get('active_rank') or []
        measure_texts.extend(f"{row.get('name') or row.get('display_name') or '未知成员'}　{row.get('count', row.get('messages', 0))}条" for row in rank_rows[:20])
    estimated_height = 1200 + 100 * len(topics)
    for value in measure_texts:
        text = str(value)
        estimated_height += ((len(text) * 34 + width - 2*margin - 1) // (width - 2*margin) + text.count('\n') + 1) * 48
    canvas = Image.new('RGB', (width, estimated_height), '#f4f6fa')
    draw = ImageDraw.Draw(canvas)
    y = 42
    def text_block(text, font=regular, color='#273142', gap=10, x=margin, maxw=width-2*margin):
        nonlocal canvas, draw, y
        for line in _wrap_text(draw, text, font, maxw):
            bbox = draw.textbbox((0, 0), line or '中', font=font)
            h = bbox[3]-bbox[1]+gap
            if y+h+margin > canvas.height:
                raise RuntimeError('Pillow report layout exceeded its measured canvas')
            draw.text((x, y), line, font=font, fill=color)
            y += h
    def section(title):
        nonlocal y
        y += 18
        text_block(title, heading_font, '#384f87', 12)
    text_block(title, title_font, '#243451', 16)
    text_block(str(report.get('chat_display') or report.get('chat_username') or '聊天日报'), heading_font)
    text_block(str(report.get('range_text') or report.get('day') or ''), small, '#778194')
    text_block('生成时间：' + str(report.get('generated_at') or ''), small, '#778194')
    if stat_text:
        section('今日统计')
        text_block('　'.join(stat_text), regular)
    if report.get('leaders', {}).get('peak'):
        peak = report['leaders']['peak']
        text_block(f"消息峰值：{peak_time_text(report)}（{peak.get('count', 0)}条）", regular)
    text_block(f'正文汉字：{body_chinese_count(report)}', small, '#778194')
    if report.get('one_line'):
        section('今日摘要'); text_block(str(report['one_line']))
    section('消息节奏')
    buckets = report.get('rhythm_buckets') or []
    if buckets:
        values = [max(0, int(b.get('count', 0) or 0)) for b in buckets]
        chart_top, chart_height = y+8, 190
        chart_left, chart_right = margin, width-margin
        draw.line((chart_left, chart_top, chart_left, chart_top+chart_height), fill='#8792a3', width=2)
        draw.line((chart_left, chart_top+chart_height, chart_right, chart_top+chart_height), fill='#8792a3', width=2)
        vmax = max(values or [0], default=0)
        for value in sorted({vmax*tick//4 for tick in range(5)}):
            yy = chart_top+chart_height-round(chart_height*value/max(vmax,1))
            draw.line((chart_left, yy, chart_right, yy), fill='#e0e5ed', width=1)
            draw.text((margin-45, yy-9), str(value), font=small, fill='#687386')
        axis_start = int(buckets[0].get('start_unix', 0))
        axis_end = int(buckets[-1].get('end_unix_exclusive', axis_start + len(buckets)*600))
        axis_duration = max(axis_end-axis_start, 1)
        points = []
        for bucket, value in zip(buckets, values):
            px = chart_left+(chart_right-chart_left)*(float(bucket.get('start_unix', axis_start))-axis_start)/axis_duration
            py = chart_top+chart_height-(chart_height*value/max(vmax,1))
            points.append((px, py))
        if len(points)>1: draw.line(points, fill='#5879d8', width=4)
        for px, py in points: draw.ellipse((px-4, py-4, px+4, py+4), fill='#fff', outline='#5879d8', width=2)
        hour = axis_start - (axis_start % 3600)
        while hour <= axis_end:
            if hour >= axis_start:
                px = chart_left + (chart_right-chart_left)*(hour-axis_start)/axis_duration
                local = datetime.fromtimestamp(hour, timezone)
                draw.line((px, chart_top+chart_height, px, chart_top+chart_height+5), fill='#8792a3')
                if local.hour % 2 == 0:
                    label = local.strftime('%H:%M')
                    draw.text((px-draw.textlength(label, font=small)/2, chart_top+chart_height+8), label, font=small, fill='#687386')
            hour += 3600
        first_day = datetime.fromtimestamp(axis_start, timezone).strftime('%m.%d')
        last_day = datetime.fromtimestamp(axis_end, timezone).strftime('%m.%d')
        draw.text((chart_left, chart_top+chart_height+31), first_day, font=small, fill='#687386')
        draw.text((chart_right-draw.textlength(last_day, font=small), chart_top+chart_height+31), last_day, font=small, fill='#687386')
        y = chart_top+chart_height+62
    for topic in report.get('topics') or []:
        section(str(topic.get('topic') or '未命名话题'))
        tr = topic_time_range(topic, timezone)
        if tr: text_block(tr, small, '#778194')
        if topic.get('summary'): text_block(str(topic['summary']))
        details = topic.get('details')
        if isinstance(details, list):
            for detail in details: text_block(str(detail))
        elif details: text_block(str(details))
    if report.get('edition') != 'brief':
        quotes = [q for q in report.get('quotes') or [] if q.get('text')]
        if quotes:
            section('群友金句')
            for q in quotes: text_block(f'{q.get("speaker") or "群友"}：{q["text"]}')
        rank = ranked_participants(report)
        if rank:
            section('活跃前20名')
            for index, row in enumerate(rank, 1): text_block(f'{index}. {row.get("name") or row.get("display_name") or "未知成员"}　{row.get("count", row.get("messages", 0))}条')
    # Final resize removes the unused initial canvas height but keeps every rendered line.
    canvas = canvas.crop((0, 0, width, max(1, y+margin)))
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, format='PNG')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Render complete daily-report metadata as HTML or PNG.')
    parser.add_argument('report', type=Path, help='UTF-8 JSON file containing a report metadata object')
    parser.add_argument('-o', '--output', required=True, type=Path, help='Output file path')
    parser.add_argument('--format', choices=('html', 'png'), help='Output format (otherwise inferred from output extension)')
    args = parser.parse_args(argv)
    output_format = args.format or args.output.suffix.lower().lstrip('.')
    if output_format not in ('html', 'png'):
        parser.error('output format must be html or png (use --format)')
    try:
        with args.report.open(encoding='utf-8') as source:
            report = json.load(source)
        if not isinstance(report, dict):
            raise ValueError('report JSON must contain an object')
        if output_format == 'html':
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(render_html(report), encoding='utf-8')
        else:
            render_png_pillow(report, args.output)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f'error: {exc}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())

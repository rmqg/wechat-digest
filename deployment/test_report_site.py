import base64
import io
from PIL import Image
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from html.parser import HTMLParser
from unittest.mock import patch

import report_site


GROUP = '10000000000@chatroom'


def elements_with_class(source, class_name):
    class Reader(HTMLParser):
        def __init__(self):
            super().__init__()
            self.matches = []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if class_name in attrs.get('class', '').split():
                self.matches.append((tag, attrs))
    reader = Reader()
    reader.feed(source)
    return reader.matches


def add_report(folder: Path, name: str, *, end: int, summary: str = "摘要", html_text: str | None = None,
               group: str = GROUP, png: bool = True, json_file: bool = True) -> None:
    end = 1792962000 + end * 86400
    record = {
        "chat_username": group,
        "day": name,
        "window": {"start_unix": end - 24 * 3600, "end_unix_exclusive": end},
        "range_text": "窗口 & <时间>",
        "stats": {"messages": 12, "members": 3},
        "one_line": summary,
    }
    stem = folder / name
    if json_file:
        stem.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    if html_text is None:
        html_text = '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:; font-src data:"></head><body><p>原报告正文</p></body></html>'
    stem.with_suffix(".html").write_text(html_text, encoding="utf-8")
    if png:
        stem.with_suffix(".png").write_bytes(b"png")


class RankRows(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.active = False
        self.rows = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        if tag == 'ol' and 'rank' in dict(attrs).get('class', '').split():
            self.active = True
        if tag == 'li' and self.active:
            self.rows.append('')

    def handle_endtag(self, tag):
        if tag == 'ol':
            self.active = False

    def handle_data(self, data):
        if self.active and self.rows:
            self.rows[-1] += data


class BuildSiteTests(unittest.TestCase):
    def setUp(self):
        target = patch.object(report_site, 'TARGET_GROUP', GROUP)
        target.start()
        self.addCleanup(target.stop)

    def test_old_ten_person_html_uses_complete_counts_for_top_twenty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'reports', root / 'site'
            source.mkdir()
            add_report(source, 'daily', end=1, html_text='<html><head></head><body><h2>Original topic</h2>'
                '<p>Keep this discussion</p><h2>Old rank</h2><ol class="rank">' + '<li>obsolete</li>' * 10 + '</ol></body></html>')
            path = source / 'daily.json'
            record = json.loads(path.read_text())
            record['participants'] = [{'username': f'u{i:02}', 'name': f'Person-{i}', 'count': i} for i in range(1, 26)]
            record['active_rank'] = record['participants'][:10]
            path.write_text(json.dumps(record))
            report_site.build_site(source, output)
            page = (output / 'reports/daily.html').read_text()
            rows = RankRows(page).rows
            self.assertEqual(len(rows), 20)
            self.assertTrue(all(f'Person-{i}' in row and ''.join(row.split()).endswith(f'{i}条') for i, row in zip(range(25, 5, -1), rows)))
            self.assertIn('Original topic', page)
            self.assertIn('Keep this discussion', page)
            self.assertNotIn('obsolete', page)

    def test_equal_counts_are_ordered_by_account_not_input_order(self):
        people = [{'username': 'z', 'name': 'Last', 'count': 3}, {'username': 'a', 'name': 'First', 'count': 3}]
        ranked = report_site.report_renderer.ranked_participants({'participants': people})
        self.assertEqual([p['username'] for p in ranked], ['a', 'z'])
        self.assertEqual([p['username'] for p in people], ['z', 'a'])

    def test_legacy_snapshot_recovers_actual_people_without_publishing_chat_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'reports', root / 'site'
            source.mkdir()
            add_report(source, 'daily', end=1, html_text='<html><head></head><body><h2>Old rank</h2><ol class="rank"><li>old</li></ol></body></html>')
            record = json.loads((source / 'daily.json').read_text())
            messages = [{'author_id': f'u{i}', 'author': f'Person-{i}', 'text': 'private-source-only'} for i in range(9)]
            record['stats']['messages'] = len(messages)
            (source / 'daily.json').write_text(json.dumps(record))
            snapshot = {'group_id': GROUP, 'window': record['window'], 'messages': messages}
            path = source / 'daily.sources.json'
            path.write_text(json.dumps(snapshot))
            report_site.build_site(source, output)
            page = (output / 'reports/daily.html').read_text()
            self.assertEqual(len(RankRows(page).rows), 9)
            self.assertNotIn('private-source-only', page)
            snapshot['group_id'] = 'another-group'
            path.write_text(json.dumps(snapshot))
            with self.assertRaisesRegex(ValueError, 'does not match'):
                report_site.build_site(source, output)

    def test_short_probes_are_excluded_unless_the_exact_png_was_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'reports', root / 'site'
            source.mkdir()
            add_report(source, 'formal', end=20)
            for name in ('legacy', 'probe'):
                add_report(source, name, end=10)
                path = source / (name + '.json')
                record = json.loads(path.read_text())
                record['window']['start_unix'] = record['window']['end_unix_exclusive'] - 3600
                path.write_text(json.dumps(record))
                (source / (name + '.png')).write_bytes(name.encode())
            receipts = root / 'digest/sent-receipts'
            receipts.mkdir(parents=True)
            (receipts / 'legacy.json').write_text(json.dumps({'status': 'sent', 'chat_id': GROUP,
                'png_sha256': hashlib.sha256(b'legacy').hexdigest()}))
            result = report_site.build_site(source, output)
            self.assertEqual(result['reports'], 2)
            self.assertIn('reports/legacy.html', result['files'])
            self.assertNotIn('reports/probe.html', result['files'])

    def test_renderer_payload_is_not_exposed_in_the_web_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'reports', root / 'site'
            source.mkdir()
            add_report(source, 'formal', end=20, html_text='<html><head></head><body><p>Visible report</p>'
                '<script id="wechatagent-report-data" type="application/json">{"internal":"private-marker"}</script></body></html>')
            report_site.build_site(source, output)
            page = (output / 'reports/formal.html').read_text()
            self.assertIn('Visible report', page)
            self.assertNotIn('private-marker', page)

    def test_sorts_by_exclusive_end_and_publishes_only_complete_target_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "source", root / "site"
            source.mkdir()
            add_report(source, "older", end=100)
            add_report(source, "newer", end=200, summary="<script>alert(1)</script>")
            add_report(source, "missing", end=300, png=False)
            add_report(source, "other-group", end=400, group="other@chatroom")
            (source / "secrets.sources.json").write_text('{"private": true}', encoding="utf-8")
            (source / "secrets.sources.html").write_text("secret", encoding="utf-8")
            (source / "secrets.sources.png").write_bytes(b"secret")
            result = report_site.build_site(source, output)
            index = (output / "index.html").read_text(encoding="utf-8")
            self.assertEqual(result["reports"], 2)
            self.assertLess(index.index("newer"), index.index("older"))
            self.assertIn("消息 12 条 · 成员 3 人", index)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", index)
            self.assertNotIn("missing", index)
            self.assertNotIn("other-group", index)
            self.assertNotIn("private", index)
            self.assertTrue((output / "reports/newer.html").is_file())
            self.assertFalse((output / "reports/missing.html").exists())
            self.assertFalse((output / "secrets.sources.json").exists())

    def test_externalizes_identical_fonts_and_images_once_and_preserves_report_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "source", root / "site"
            source.mkdir()
            resource = base64.b64encode(b"same binary resource").decode("ascii")
            font_uri = "data:font/woff2;base64," + resource
            html_text = ('<html><head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; font-src data:"></head>'
                         '<body><p>原报告文字不变</p><img src="data:image/png;base64,' + resource + '">'
                         '<style>@font-face{src:url(' + font_uri + ')} .x{background:url(data:image/png;base64,' + resource + ')}</style></body></html>')
            add_report(source, "one", end=1, html_text=html_text)
            result = report_site.build_site(source, output)
            external = (output / "reports/one.html").read_text(encoding="utf-8")
            digest = hashlib.sha256(b"same binary resource").hexdigest()
            self.assertEqual(len([f for f in result["files"] if f.startswith("assets/")]), 2)
            self.assertEqual((output / f"assets/{digest}.png").read_bytes(), b"same binary resource")
            self.assertEqual((output / f"assets/{digest}.woff2").read_bytes(), b"same binary resource")
            self.assertNotIn("data:image/", external)
            self.assertNotIn("data:font/", external)
            self.assertIn("../assets/" + digest + ".png", external)
            self.assertIn("../assets/" + digest + ".woff2", external)
            self.assertIn("原报告文字不变", external)
            self.assertIn("img-src 'self'; font-src 'self'", external)

    def test_escapes_metadata_and_returns_only_public_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "source", root / "site"
            source.mkdir()
            add_report(source, "safe", end=10, summary='" onmouseover="alert(1)')
            result = report_site.build_site(source, output)
            index = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn("&quot; onmouseover=&quot;alert(1)", index)
            self.assertEqual(result["files"], sorted(result["files"]))
            self.assertTrue(all(not name.endswith((".json", ".png")) for name in result["files"]))
    def test_legacy_source_numbers_are_rebound_to_snapshot_and_real_image_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'runtime/reports', root / 'site'
            source.mkdir(parents=True)
            html_text = ("<html><head></head><body><article class='topic' id='topic-1'>"
                         "<details><ul class='sources'><li>M0001</li><li>M0002</li></ul></details></article>"
                         "<blockquote><p>保留关键原文</p><footer><details><summary>查看来源</summary>M0002"
                         "</details></footer></blockquote></body></html>")
            add_report(source, 'daily', end=1, html_text=html_text)
            record_path = source / 'daily.json'
            record = json.loads(record_path.read_text())
            record['stats']['messages'] = 2
            record['avatars'] = {'u1': {'available': False}}
            record['topics'] = [{'topic': '讨论', 'message_ids': ['uid-image', 'uid-text'],
                                 'references': [{'reference': 'M0001'}, {'reference': 'M0002'}]}]
            record['quotes'] = [{'message_id': 'uid-text', 'reference': 'M0002', 'speaker': '成员',
                                 'speaker_id': 'u1', 'text': '保留关键原文'}]
            record_path.write_text(json.dumps(record), encoding='utf-8')
            buffer = io.BytesIO()
            Image.new('RGB', (2, 3), (10, 20, 30)).save(buffer, format='PNG')
            pixel = buffer.getvalue()
            image_path = root / 'runtime/digest/images/decoded/original.png'
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(pixel)
            snapshot = {'group_id': GROUP, 'window': record['window'], 'messages': [
                {'message_uid': 'uid-image', 'reference': 'M0001', 'author': '成员', 'author_id': 'u1',
                 'timestamp': 1792962000, 'type': 'image', 'text': '[图片]'},
                {'message_uid': 'uid-text', 'reference': 'M0002', 'author': '成员', 'author_id': 'u1',
                 'timestamp': 1792962060, 'type': 'text', 'text': '保留关键原文'}]}
            (source / 'daily.sources.json').write_text(json.dumps(snapshot), encoding='utf-8')
            sha = hashlib.sha256(pixel).hexdigest()
            cache = {'uid-image': {'item': {'message_uid': 'uid-image', 'available': True,
                     'source_quality': 'original', 'image_path': '/work/images/decoded/original.png', 'sha256': sha}}}
            (root / 'runtime/digest/prepared-image-cache.json').write_text(json.dumps(cache), encoding='utf-8')
            add_report(source, 'donor', end=2)
            donor_path = source / 'donor.json'
            donor = json.loads(donor_path.read_text())
            donor['avatars'] = {'u1': {'available': True, 'data_uri':
                'data:image/png;base64,' + base64.b64encode(pixel).decode()}}
            donor_path.write_text(json.dumps(donor), encoding='utf-8')
            result = report_site.build_site(source, output)
            page = (output / 'reports/daily.html').read_text(encoding='utf-8')
            self.assertNotIn('M0001', page)
            self.assertNotIn('M0002', page)
            self.assertNotIn('[图片]', page)
            self.assertIn('保留关键原文', page)
            self.assertEqual(len(elements_with_class(page, 'avatar')), 3)
            self.assertEqual(len(elements_with_class(page, 'source-image')), 1)
            self.assertIn('../assets/' + sha + '.png', page)
            self.assertIn('assets/' + sha + '.png', result['files'])
            self.assertEqual((output / 'assets' / (sha + '.png')).read_bytes(), pixel)
            self.assertEqual(image_path.read_bytes(), pixel)
            self.assertFalse(any(name.endswith('.sources.json') for name in result['files']))
            self.assertTrue(all('..' not in Path(name).parts for name in result['files']))

    def test_saved_source_text_remains_visible_when_full_snapshot_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'reports', root / 'site'
            source.mkdir()
            add_report(source, 'daily', end=1, html_text="<html><head></head><body>"
                "<article class='topic' id='topic-1'><ul class='sources'><li>M0001</li></ul></article></body></html>")
            path = source / 'daily.json'
            record = json.loads(path.read_text())
            record['topics'] = [{'topic': '已有原文', 'references': [{'reference': 'M0001',
                'author': '原作者', 'author_id': 'u1', 'timestamp': 1792962000,
                'type': 'text', 'text': '已经保存的原始内容'}]}]
            path.write_text(json.dumps(record))
            report_site.build_site(source, output)
            page = (output / 'reports/daily.html').read_text()
            self.assertIn('原作者', page)
            self.assertIn('已经保存的原始内容', page)
            self.assertNotIn('M0001', page)



    def test_on_demand_reports_require_valid_metadata_and_keep_window_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'reports', root / 'site'
            source.mkdir()
            request_id = 'a' * 64
            stem = 'wechat-digest-request-' + request_id
            add_report(source, stem, end=10)
            record_path = source / (stem + '.json')
            record = json.loads(record_path.read_text())
            end = 1792962000 + 10 * 86400
            start = end - 3 * 3600
            record['window'] = {'start_unix': start, 'end_unix_exclusive': end}
            record['request'] = {'kind': 'on_demand', 'id': request_id, 'trigger_timestamp': end}
            record_path.write_text(json.dumps(record), encoding='utf-8')
            bad_stem = 'wechat-digest-request-' + 'b' * 64
            add_report(source, bad_stem, end=11)
            bad_path = source / (bad_stem + '.json')
            bad = json.loads(bad_path.read_text())
            bad['request'] = {'kind': 'on_demand', 'id': 'b' * 64, 'trigger_timestamp': 1}
            bad_path.write_text(json.dumps(bad), encoding='utf-8')
            later_id = 'c' * 64
            later_stem = 'wechat-digest-request-' + later_id
            add_report(source, later_stem, end=10)
            later_path = source / (later_stem + '.json')
            later = json.loads(later_path.read_text())
            later_end = end + 3600
            later['window'] = {'start_unix': later_end - 3600, 'end_unix_exclusive': later_end}
            later['request'] = {'kind': 'on_demand', 'id': later_id, 'trigger_timestamp': later_end}
            later_path.write_text(json.dumps(later), encoding='utf-8')
            result = report_site.build_site(source, output)
            index = (output / 'index.html').read_text(encoding='utf-8')
            self.assertEqual(result['reports'], 2)
            self.assertLess(index.index(later_stem), index.index(stem))
            self.assertNotIn(bad_stem, index)

    def test_topic_titles_and_minute_ranges_refresh_without_replacing_discussion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'reports', root / 'site'
            source.mkdir()
            markup = ("<html><head></head><body><nav><a href='#topic-1'>话题</a></nav>"
                      "<article class='topic' id='topic-1'><h3><span>01</span>话题</h3>"
                      "<p class='summary'>原有讨论不变</p><details><summary>来源</summary>证据</details></article></body></html>")
            add_report(source, 'daily', end=1, html_text=markup)
            path = source / 'daily.json'
            record = json.loads(path.read_text())
            start = record['window']['start_unix'] + 73
            record['topics'] = [{'topic': '网络连接排查', 'start_unix': start, 'end_unix': start + 180}]
            path.write_text(json.dumps(record))
            report_site.build_site(source, output)
            page = (output / 'reports/daily.html').read_text()
            parser = report_site._TopicMarkup(page)
            block = parser.blocks[0]
            self.assertIn('网络连接排查', page[slice(*block['heading'])])
            self.assertIn(report_site.report_renderer.topic_time_range(record['topics'][0]),
                          page[slice(*block['time'])])
            _, link_start, link_end = parser.links[0]
            self.assertEqual(page[link_start:link_end], '网络连接排查')
            self.assertIn('原有讨论不变', page)
            self.assertIn('证据', page)

    def test_legacy_chart_uses_exact_source_timestamps_and_preserves_zero_intervals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'reports', root / 'site'
            source.mkdir()
            add_report(source, 'daily', end=1, html_text="<html><head></head><body><div class='chart'>old</div></body></html>")
            path = source / 'daily.json'
            record = json.loads(path.read_text())
            start = record['window']['start_unix']
            stamps = [start, start + 599, start + 600, start + 1800]
            record['stats']['messages'] = len(stamps)
            path.write_text(json.dumps(record))
            snapshot = {'group_id': GROUP, 'window': record['window'],
                        'messages': [{'timestamp': stamp, 'text': 'private-source-only'} for stamp in stamps]}
            (source / 'daily.sources.json').write_text(json.dumps(snapshot))
            report_site.build_site(source, output)
            page = (output / 'reports/daily.html').read_text()
            self.assertEqual(len(elements_with_class(page, 'rhythm-point')), 144)
            self.assertIn('<polyline ', page)
            self.assertNotIn('private-source-only', page)
            snapshot['window'] = dict(snapshot['window'], start_unix=start + 1)
            (source / 'daily.sources.json').write_text(json.dumps(snapshot))
            with self.assertRaisesRegex(ValueError, 'does not match'):
                report_site.build_site(source, output)

    def test_new_editions_publish_only_as_complete_pair_and_share_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'reports', root / 'site'
            source.mkdir()
            request_id = 'd' * 64
            base = 'wechat-digest-request-' + request_id
            start, end = 1792962000, 1792965600
            shared = {
                'chat_username': GROUP, 'day': '2026-10-27',
                'window': {'start_unix': start, 'end_unix_exclusive': end},
                'request': {'kind': 'on_demand', 'id': request_id, 'trigger_timestamp': end},
                'stats': {'messages': 1, 'members': 1}, 'one_line': '共同摘要',
            }
            html_text = "<html><head></head><body><h1>edition</h1><div class='chart'>stale</div><h2>旧排行</h2><ol class='rank'><li>stale</li></ol></body></html>"
            for edition in ('brief', 'review'):
                record = {**shared, 'edition': edition}
                add_report(source, base + '-' + edition, end=1, html_text=html_text)
                (source / (base + '-' + edition + '.json')).write_text(json.dumps(record), encoding='utf-8')
            snapshot = {'group_id': GROUP, 'window': shared['window'], 'messages': [
                {'timestamp': start + 1, 'author_id': 'u1', 'author': '真实成员', 'text': 'private-source-only'}]}
            (source / (base + '-editions.sources.json')).write_text(json.dumps(snapshot), encoding='utf-8')
            (source / (base + '-review.png')).unlink()
            result = report_site.build_site(source, output)
            self.assertEqual(result['reports'], 0)
            self.assertFalse((output / 'index.html').read_text(encoding='utf-8').find(base) >= 0)
            (source / (base + '-review.png')).write_bytes(b'png')
            result = report_site.build_site(source, output)
            self.assertEqual(result['reports'], 2)
            index = (output / 'index.html').read_text(encoding='utf-8')
            self.assertEqual(index.count('<article>'), 1)
            self.assertLess(index.index('简报'), index.index('回顾'))
            brief = (output / ('reports/' + base + '-brief.html')).read_text(encoding='utf-8')
            review = (output / ('reports/' + base + '-review.html')).read_text(encoding='utf-8')
            self.assertIn(base + '-review.html', brief)
            self.assertIn(base + '-brief.html', review)
            self.assertIn('真实成员', review)
            self.assertNotIn('private-source-only', review)
            self.assertIn('rhythm-point', brief)

    def test_new_edition_request_stem_must_match_edition_suffix(self):
        request_id = 'e' * 64
        end = 1792965600
        record = {'request': {'kind': 'on_demand', 'id': request_id, 'trigger_timestamp': end},
                  'window': {'start_unix': end - 60, 'end_unix_exclusive': end}, 'edition': 'brief'}
        self.assertTrue(report_site._valid_on_demand(record, 'wechat-digest-request-' + request_id + '-brief'))
        self.assertFalse(report_site._valid_on_demand(record, 'wechat-digest-request-' + request_id + '-review'))
        self.assertFalse(report_site._valid_on_demand({**record, 'edition': 'unknown'}, 'wechat-digest-request-' + request_id + '-unknown'))
        legacy = {key: value for key, value in record.items() if key != 'edition'}
        self.assertTrue(report_site._valid_on_demand(legacy, 'wechat-digest-request-' + request_id))
        self.assertFalse(report_site._valid_on_demand(legacy, 'wechat-digest-request-' + request_id + '-brief'))

    def test_classic_inline_samples_become_author_avatar_source_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, output = root / 'runtime/reports', root / 'site'
            folder.mkdir(parents=True)
            source = ('<html><head></head><body><header><h1>主标题</h1></header><p class="lead">概括</p>'
                      '<section class="topic"><div class="topic-head"><b>小标题</b></div>'
                      '<p>[M0001] 作者甲：旧摘录</p></section></body></html>')
            add_report(folder, 'classic', end=1, html_text=source)
            record_path = folder / 'classic.json'
            record = json.loads(record_path.read_text())
            buffer = io.BytesIO()
            Image.new('RGB', (2, 3), (30, 60, 90)).save(buffer, format='PNG')
            uri = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()
            record.update(one_line='概括', stats={'messages': 2},
                          topics=[{'topic': '小标题', 'summary': '未显示的摘要', 'message_ids': ['first', 'second']}],
                          avatars={'a': {'available': True, 'data_uri': uri},
                                   'b': {'available': True, 'data_uri': uri}})
            record_path.write_text(json.dumps(record))
            snapshot_path = folder / 'classic.sources.json'
            snapshot_path.write_text(json.dumps({'group_id': GROUP, 'window': record['window'], 'messages': [
                {'message_uid': 'first', 'reference': 'M0001', 'author_id': 'a', 'author': '作者甲',
                 'timestamp': 1792962000, 'type': 'text', 'text': '完整原文甲'},
                {'message_uid': 'second', 'reference': 'M0002', 'author_id': 'b', 'author': '作者乙',
                 'timestamp': 1792962060, 'type': 'text', 'text': '完整原文乙'},
            ]}))
            report_site.build_site(folder, output)
            page = (output / 'reports/classic.html').read_text()
            self.assertIn('完整原文甲', page)
            self.assertIn('完整原文乙', page)
            self.assertNotIn('M0001', page)
            self.assertNotIn('旧摘录', page)
            self.assertEqual(len(elements_with_class(page, 'avatar')), 2)
            self.assertIn('正文共 5 字（仅计中文）', page)
            snapshot_path.unlink()
            report_site.build_site(folder, output)
            retained = (output / 'reports/classic.html').read_text()
            self.assertIn('作者甲：旧摘录', retained)
            self.assertNotIn('M0001', retained)

    def test_legacy_counter_uses_only_visible_topic_heading_and_body(self):
        source = '''<html><body><header><h1>不计主标题</h1></header>
<p class="lead">前言甲</p><section class="topic"><div class="topic-head"><b>标题乙</b></div>
<p>正文丙丁</p><div class="summary">不可见摘要戊己</div></section>
<p class="stats">统计庚辛</p><section class="metadata"><p>样本壬癸</p></section></body></html>'''
        expected = 3 + 3 + 4
        self.assertEqual(report_site._legacy_body_count(source), expected)
        rendered = report_site._refresh_header_metrics(source, {'topics': [{'summary': '误计字符'}], 'leaders': {}})
        self.assertIn(f'正文共 {expected} 字（仅计中文）', rendered)

if __name__ == "__main__":
    unittest.main()

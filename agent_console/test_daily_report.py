import unittest
from datetime import datetime
from xml.etree import ElementTree as ET
from html.parser import HTMLParser
from unittest.mock import patch

from agent_console import daily_report


def visible_text(source):
    class Reader(HTMLParser):
        def __init__(self):
            super().__init__()
            self.hidden = 0
            self.parts = []

        def handle_starttag(self, tag, attrs):
            if tag in ('script', 'style'):
                self.hidden += 1

        def handle_endtag(self, tag):
            if tag in ('script', 'style'):
                self.hidden -= 1

        def handle_data(self, data):
            if not self.hidden:
                self.parts.append(data)

    reader = Reader()
    reader.feed(source)
    return ' '.join(reader.parts)


class RhythmBucketTests(unittest.TestCase):
    def test_ten_minute_boundaries_zero_buckets_and_conservation(self):
        start = 1_800_000_000
        start -= start % 600
        stamps = [start - 1, start, start + 599, start + 600, start + 1800]

        buckets = daily_report.build_rhythm_buckets(stamps, start, start + 2400)

        self.assertEqual([bucket["count"] for bucket in buckets], [2, 1, 0, 1])
        self.assertEqual(sum(bucket["count"] for bucket in buckets), 4)
        self.assertTrue(all(bucket["end_unix_exclusive"] - bucket["start_unix"] == 600 for bucket in buckets))

    def test_partial_first_and_last_buckets_are_clipped_to_requested_range(self):
        natural = 1_800_000_000
        natural -= natural % 600
        start, end = natural + 125, natural + 600 + 250

        buckets = daily_report.build_rhythm_buckets([start - 1, start, natural + 599, natural + 600, end], start, end)

        self.assertEqual([(item["start_unix"], item["end_unix_exclusive"]) for item in buckets],
                         [(start, natural + 600), (natural + 600, end)])
        self.assertEqual([item["count"] for item in buckets], [2, 1])
        self.assertEqual(sum(item["count"] for item in buckets), 3)
    def test_cross_midnight_range_shows_only_hours_and_minutes(self):
        start = int(datetime(2026, 10, 27, 23, 55, tzinfo=daily_report.DISPLAY_TZ).timestamp())
        end = int(datetime(2026, 10, 28, 0, 12, tzinfo=daily_report.DISPLAY_TZ).timestamp())

        self.assertEqual(daily_report.topic_time_range({"start_unix": start, "end_unix": end}),
                         "23:55–00:12")
        self.assertEqual(daily_report.topic_time_range({"start_unix": start}), "")


    def test_edition_titles_and_brief_omits_deep_read_sections(self):
        report = {
            "edition": "brief",
            "day": "2026-10-27",
            "chat_username": "group",
            "chat_display": "测试群",
            "range_text": "2026-10-27 05:00-2026-10-28 05:00",
            "generated_at": "now",
            "one_line": "简报摘要",
            "stats": {"messages": 12, "members": 3},
            "leaders": {"peak": {"name": "10:00", "count": 4}},
            "rhythm_buckets": [{"start_unix": 1793077200, "count": 1}],
            'topics': [{'topic': '窗口排查', 'summary': '讨论', 'details': [],
                        'samples': ['不应自动展开的原始聊天样本'],
                        'start_unix': 1793077200, 'end_unix': 1793077500}],
            "quotes": [{"speaker": "甲", "text": "金句不应展示"}],
            "active_rank": [{"name": "不应排行", "count": 9}],
        }
        with patch.object(daily_report, 'rounded_webfont_css', return_value=''):
            brief = visible_text(daily_report.render_html(report))
            review = visible_text(daily_report.render_html({**report, 'edition': 'review'}))
            legacy = visible_text(daily_report.render_html({key: value for key, value in report.items() if key != 'edition'}))
            no_quotes = visible_text(daily_report.render_html({**report, 'edition': 'review', 'quotes': []}))
        self.assertIn('消息节奏', brief)
        self.assertIn('消息总量', brief)
        self.assertIn('讨论', brief)
        self.assertNotIn('群友金句', brief)
        self.assertNotIn('活跃前20名', brief)
        self.assertNotIn('金句不应展示', brief)
        self.assertNotIn('不应排行', brief)
        self.assertNotIn('不应自动展开的原始聊天样本', brief)
        self.assertIn('群友金句', review)
        self.assertIn('活跃前20名', review)
        self.assertNotIn('群友金句', no_quotes)
        self.assertIn('活跃前20名', no_quotes)

    def test_repeated_source_avatars_prefer_shared_assets_over_inline_payloads(self):
        import base64
        import io
        from PIL import Image

        buffer = io.BytesIO()
        Image.new('RGB', (128, 128), (10, 20, 30)).save(buffer, format='PNG', compress_level=0)
        data_uri = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()
        asset = '../assets/' + 'a' * 64 + '.png'
        references = [{'author': 'Ada', 'author_id': 'ada', 'timestamp': 1793077200,
                       'type': 'text', 'text': '独立原始消息'} for _ in range(20)]
        html = daily_report.source_messages_html(references, {'ada': {
            'available': True, 'data_uri': data_uri, 'asset_src': asset}})
        class Images(HTMLParser):
            def __init__(self):
                super().__init__()
                self.sources = []
            def handle_starttag(self, tag, attrs):
                if tag == 'img':
                    self.sources.append(dict(attrs).get('src'))
        parsed = Images()
        parsed.feed(html)
        self.assertEqual(parsed.sources, [asset] * 20)
        self.assertLess(len(html.encode()), 20000)

    def test_source_entries_render_identity_text_and_safe_archived_image(self):
        avatar = 'data:image/png;base64,AA=='
        rendered = daily_report.source_messages_html([
            {'message_uid': 'uid-private', 'reference': 'M0042', 'author': '<Ada>', 'author_id': 'ada',
             'timestamp': 1793077200, 'type': 'text', 'text': 'A & <B>'},
            {'message_uid': 'uid-image', 'reference': 'M0043', 'author': 'Bo', 'author_id': 'bo',
             'timestamp': 1793077500, 'type': 'image', 'text': '[图片]',
             'image_src': '../assets/0123456789abcdef.png'},
            {'message_uid': 'uid-bad', 'reference': 'M0044', 'author': 'Cy', 'author_id': 'cy',
             'timestamp': 1793077500, 'type': 'image', 'text': '【图片】',
             'image_src': 'https://example.invalid/private.png'},
        ], {'ada': {'available': True, 'data_uri': avatar}})
        self.assertIn(avatar, rendered)
        self.assertIn(daily_report.time_text(1793077200, '%m.%d %H:%M'), rendered)
        self.assertIn('&lt;Ada&gt;', rendered)
        self.assertIn('A &amp; &lt;B&gt;', rendered)
        self.assertIn("src='../assets/0123456789abcdef.png'", rendered)
        self.assertIn("loading='lazy'", rendered)
        self.assertNotIn('[图片]', rendered)
        self.assertNotIn('https://example.invalid', rendered)
        self.assertNotIn('M004', rendered)

    def test_source_entries_render_multiple_archived_images_without_reference_ids(self):
        rendered = daily_report.source_messages_html([{
            'message_uid': 'forwarding-uid', 'reference': 'M0572', 'author': '转发者',
            'author_id': 'forwarder', 'type': 'appmsg', 'text': '[图片]',
            'image_sources': ['../assets/' + 'a' * 64 + '.png', '../assets/' + 'b' * 64 + '.jpg',
                              '../assets/' + 'c' * 64 + '.webp'],
        }], {})
        self.assertEqual(rendered.count("class='source-image'"), 3)
        self.assertNotIn('[图片]', rendered)
        self.assertNotIn('M0572', rendered)
        missing = daily_report.source_messages_html([{
            'author': '引用者', 'author_id': 'quoting-author', 'type': 'appmsg',
            'text': '[图片]', 'image_unavailable_reason': '原始图片尚未归档，暂不可用'}], {})
        self.assertIn('原始图片尚未归档', missing)
        self.assertNotIn('[图片]', missing)

    def test_reply_quote_keeps_text_and_images_separate_from_ordinary_sources(self):
        class Parts(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.main, self.quoted, self.images = [], [], [], []
                self.frames = 0

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                frame = 'source-quote' in attrs.get('class', '').split()
                quoted = frame or bool(self.stack and self.stack[-1][1])
                self.frames += int(frame)
                if tag == 'img':
                    self.images.append((attrs.get('src'), quoted))
                if tag not in ('img', 'br', 'hr', 'meta', 'link'):
                    self.stack.append((tag, quoted))

            def handle_endtag(self, tag):
                while self.stack:
                    if self.stack.pop()[0] == tag:
                        break

            def handle_data(self, data):
                if any(tag == 'p' for tag, _ in self.stack):
                    (self.quoted if self.stack[-1][1] else self.main).append(data)

        quote_image = '../assets/' + 'a' * 64 + '.jpg'
        ordinary_image = '../assets/' + 'b' * 64 + '.png'
        rendered = daily_report.source_messages_html([
            {'author': '甲', 'text': '收到 & <标记>；引用 🍊: 原话 <script>alert(1)</script>',
             'image_sources': [quote_image]},
            {'author': '乙', 'text': '我在讨论引用规则，但没有引用正文', 'image_src': ordinary_image},
        ], {})
        parsed = Parts()
        parsed.feed(rendered)
        self.assertEqual(parsed.frames, 1)
        self.assertEqual(''.join(parsed.main), '收到 & <标记>我在讨论引用规则，但没有引用正文')
        self.assertEqual(''.join(parsed.quoted), '引用 🍊: 原话 <script>alert(1)</script>')
        self.assertEqual(parsed.images, [(quote_image, True), (ordinary_image, False)])
        self.assertNotIn('<script>', rendered)

    def test_visible_sources_never_fall_back_to_internal_references(self):
        report = {
            'edition': 'review', 'day': '2026-10-27', 'chat_username': 'group', 'chat_display': '测试群',
            'stats': {}, 'topics': [{'topic': '话题', 'summary': '摘要', 'details': [],
                                      'message_ids': ['uid-private'], 'reference_ids': ['M0012'],
                                      'references': [{'message_uid': 'uid-private', 'reference': 'M0012',
                                                      'author': '甲', 'author_id': 'a', 'timestamp': 1793077200,
                                                      'type': 'text', 'text': '可见原文'}]}],
            'quotes': [{'message_id': 'uid-private', 'reference': 'M0012', 'speaker': '甲',
                        'speaker_id': 'a', 'time': '10.00 10:00', 'text': '金句原文'}],
        }
        with patch.object(daily_report, 'rounded_webfont_css', return_value=''):
            html = daily_report.render_html(report)
            text = visible_text(html)
            missing = visible_text(daily_report.render_html({**report, 'topics': [
                {'topic': '无原文', 'summary': '', 'details': [], 'message_ids': ['uid-private'],
                 'reference_ids': ['M0099'], 'references': []}], 'quotes': [
                {'message_id': 'uid-private', 'reference': 'M0099', 'speaker': '甲', 'text': ''}]}))
        self.assertIn('可见原文', text)
        self.assertIn('金句原文', text)
        self.assertIn('查看来源', text)
        self.assertNotIn('M0012', text)
        self.assertNotIn('M0099', missing)

    def test_rhythm_svg_preserves_every_point_and_visible_cross_day_labels(self):
        first = int(datetime(2026, 10, 27, 23, 50, tzinfo=daily_report.DISPLAY_TZ).timestamp())
        chart = daily_report.rhythm_chart_contents({
            "rhythm_buckets": [
                {"start_unix": first, "count": 0},
                {"start_unix": first + 600, "count": 3},
            ],
        })
        svg = ET.fromstring(chart)
        view_width, view_height = map(float, svg.attrib["viewBox"].split()[2:])
        polyline = svg.find('{*}polyline')
        points = [tuple(map(float, point.split(","))) for point in polyline.attrib["points"].split()]
        dots = svg.findall(".//{*}circle[@class='rhythm-point']")
        self.assertEqual(len(points), 2)
        self.assertEqual(len(dots), len(points))
        self.assertEqual([(float(dot.attrib["cx"]), float(dot.attrib["cy"])) for dot in dots], points)
        self.assertLess(points[0][1], view_height)
        self.assertGreater(points[0][1], points[1][1])
        self.assertTrue(all(0 <= x <= view_width and 0 <= y <= view_height for x, y in points))
        texts = {text.text: text for text in svg.findall('.//{*}text')}
        labels = ' '.join(texts)
        self.assertIn('2026-10-27', labels)
        self.assertIn('2026-10-28', labels)
        hour_labels = [text.text for text in svg.findall('.//{*}text') if 'chart-hour' in text.attrib.get('class', '').split()]
        self.assertEqual(hour_labels, ['00'])

    def test_full_day_axis_has_hourly_ticks_and_two_hour_numbers(self):
        start = int(datetime(2026, 10, 27, 5, tzinfo=daily_report.DISPLAY_TZ).timestamp())
        buckets = daily_report.build_rhythm_buckets([start, start + 123], start, start + 86400)
        svg = ET.fromstring(daily_report.rhythm_chart_contents({'rhythm_buckets': buckets}))
        ticks = svg.findall(".//{*}circle[@class='axis-hour']")
        self.assertEqual(len(ticks), 25)
        labels = [text.text for text in svg.findall('.//{*}text') if 'chart-hour' in text.attrib.get('class', '').split()]
        self.assertEqual(labels, ['06', '08', '10', '12', '14', '16', '18', '20', '22', '00', '02', '04'])
        positions = [float(tick.attrib['cx']) for tick in ticks]
        gaps = [b - a for a, b in zip(positions, positions[1:])]
        self.assertLess(max(gaps) - min(gaps), 0.02)


if __name__ == "__main__":
    unittest.main()

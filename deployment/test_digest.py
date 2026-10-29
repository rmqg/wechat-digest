import importlib.util
import json
import unittest
from pathlib import Path
import hashlib
import sqlite3
import tempfile
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("digest.py")
spec = importlib.util.spec_from_file_location("digest", MODULE_PATH)
digest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(digest)


class DigestConsumerRiskTests(unittest.TestCase):
    def test_project_timezone_controls_dates_quotes_peaks_and_naive_cli_times(self):
        start = 1793145600
        messages = [{'message_uid': 'm1', 'reference': 'M0001', 'timestamp': start,
                     'author_id': 'fictional-person', 'author': 'Ada', 'type': 'text', 'text': 'Original words.'}]
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'project.json'
            config.write_text(json.dumps({'timezone': 'America/Los_Angeles'}))
            with patch.dict('os.environ', {'WECHAT_DIGEST_CONFIG': str(config)}):
                summary = digest.validate_summary({'one_line': 'summary', 'topics': [],
                                                   'quotes': [{'message_id': 'M0001'}]}, messages)
                report = digest.enrich_report({'stats': {}, **summary}, messages, start, start + 3600, {})
                self.assertEqual(report['range_text'], '2026.10.27 17:00 — 2026.10.27 18:00')
                self.assertEqual(report['day'], '2026-10-27')
                self.assertEqual(report['quotes'][0]['time'], '10.27 17:00')
                self.assertEqual(report['leaders']['peak']['name'], '2026-10-27 17:00')
                self.assertEqual(digest._parse_time('2026-10-27T17:00', 0), start)

    def message_database(self, root, shard, times, types=None):
        path = Path(root) / "message" / f"message_{shard}.db"
        path.parent.mkdir(exist_ok=True)
        table = "Msg_" + hashlib.md5(b"test@chatroom").hexdigest()
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE Name2Id(user_name TEXT, is_session INTEGER)")
            conn.execute("INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(7,'wxid_test',0)")
            conn.execute(f"CREATE TABLE [{table}](local_id INTEGER,server_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,origin_source INTEGER,source TEXT,message_content TEXT,compress_content TEXT,packed_info_data BLOB,WCDB_CT_message_content INTEGER)")
            for index, stamp in enumerate(times):
                kind = types[index] if types else 1
                conn.execute(f"INSERT INTO [{table}] VALUES(?,NULL,?,7,?,1,NULL,'hello',NULL,NULL,0)", (index + 1, kind, stamp))
        return path

    def test_exact_name_ambiguity_requires_group_id(self):
        groups = [
            {"group_id": "first@chatroom", "name": "Example Study Group"},
            {"group_id": "second@chatroom", "name": "Example Study Group"},
            {"group_id": "other@chatroom", "name": "example study group"},
        ]
        with self.assertRaises(ValueError):
            digest.select_group(groups, group_name="Example Study Group")
        self.assertEqual(digest.select_group(groups, group_id="second@chatroom"), groups[1])

    def test_quotes_are_backfilled_from_cited_source(self):
        source = {"message_uid": "m1", "reference": "M0001", "timestamp": 123, "author": "Ada", "text": "  exact original  "}
        normalized = digest.validate_summary({
            "one_line": "summary",
            "topics": [{"title": "topic", "summary": "detail", "message_ids": ["M0001"]}],
            "quotes": [{"message_id": "M0001"}],
        }, [source])
        self.assertEqual(normalized["quotes"][0]["text"], source["text"])
        self.assertEqual(normalized["quotes"][0]["message_id"], source["message_uid"])
        self.assertEqual(normalized["topics"][0]["message_ids"], [source["message_uid"]])
        with self.assertRaises(ValueError):
            digest.validate_summary({"one_line": "x", "topics": [], "quotes": [{"message_id": "fake"}]}, [source])

    def test_topic_titles_must_be_substantive_and_are_trimmed(self):
        source = {"message_uid": "m1", "reference": "M0001", "timestamp": 123,
                  "author": "Ada", "text": "source", "type": "text"}
        for title in (None, 7, "", "   ", "话题", "话题 1", "话题12"):
            with self.subTest(title=title), self.assertRaises(ValueError):
                digest.validate_summary({"one_line": "x", "topics": [{
                    "title": title, "summary": "detail", "message_ids": ["M0001"]}], "quotes": []}, [source])
        normalized = digest.validate_summary({"one_line": "x", "topics": [{
            "title": "  正常标题  ", "summary": "detail", "message_ids": ["M0001"]}], "quotes": []}, [source])
        self.assertEqual(normalized["topics"][0]["topic"], "正常标题")

    def test_image_description_is_topic_context_but_never_a_quote(self):
        image = {"message_uid": "image-1", "reference": "M0001", "timestamp": 123,
                 "author": "Ada", "text": "[图片]", "type": "image",
                 "image_understanding": {"description": "窗口中有多个自动化测试节点"}}
        topic = digest.validate_summary({"one_line": "summary", "topics": [{"title": "tests", "summary": "layout", "message_ids": ["M0001"]}], "quotes": []}, [image])
        self.assertEqual(topic["topics"][0]["message_ids"], ["image-1"])
        self.assertIn(image["image_understanding"]["description"], topic["topics"][0]["samples"][0])
        with self.assertRaises(ValueError):
            digest.validate_summary({"one_line": "summary", "topics": [], "quotes": [{"message_id": "M0001"}]}, [image])

    def test_quote_candidate_indices_are_not_global_message_indices(self):
        image = {"message_uid": "image", "reference": "M0001", "timestamp": 123,
                 "author": "Ada", "text": "[图片]", "type": "image"}
        text = {"message_uid": "text", "reference": "M0002", "timestamp": 124,
                "author": "Bo", "text": "this is an actual group message", "type": "text"}
        quotes = digest.quote_references([0], [{"message_id": text["reference"]}])
        result = digest.validate_summary({"one_line": "summary", "topics": [], "quotes": quotes}, [image, text])
        self.assertEqual(result["quotes"][0]["message_id"], text["message_uid"])
        self.assertEqual(result["quotes"][0]["text"], text["text"])

    def test_quote_candidate_index_must_be_in_range_integer(self):
        for index in (True, -1, 1, "M0001", {"candidate_index": 0}):
            with self.subTest(index=index), self.assertRaises(ValueError):
                digest.quote_references([index], [{"message_id": "M0001"}])

    def test_quote_indices_map_to_eligible_original_text_after_filtering(self):
        texts = [
            ('image', '图片里识别出来的一段完整文字'),
            ('text', '@机器人 总结最近一天聊天记录'),
            ('text', '项目链接 https://example.invalid/project'),
            ('text', '```python\nprint("hello")\n```'),
            ('text', '  知道边界，才知道优化什么。  '),
            ('text', '请联系 user@example.invalid 了解情况'),
            ('text', 'Authorization: Bearer private-token-value'),
            ('text', '这是很长的机器输出。' * 30),
            ('text', '1234567890'),
            ('text', '：先关掉代码补全'),
        ]
        messages = [{'message_uid': f'uid-{i}', 'reference': f'M{i + 1:04d}',
                     'timestamp': 100 + i, 'author': 'Ada', 'text': text, 'type': kind}
                    for i, (kind, text) in enumerate(texts)]
        candidates = digest._quote_candidates(messages)
        self.assertEqual([item['message_id'] for item in candidates], ['M0005'])
        result = digest.validate_summary({'one_line': '摘要', 'topics': [],
            'quotes': digest.quote_references([0], candidates)}, messages)
        self.assertEqual(result['quotes'][0]['message_id'], 'uid-4')
        self.assertEqual(result['quotes'][0]['text'], '  知道边界，才知道优化什么。  ')

    def test_repeated_sources_and_equivalent_quote_text_are_shown_once(self):
        texts = ['  Optimise the right thing, not every thing.  ',
                 'optimise  the right thing, not every thing.',
                 'Only measure what you can explain.']
        messages = [{'message_uid': f'uid-{i}', 'reference': f'M{i + 1:04d}',
                     'timestamp': 100 + i, 'author': f'Author-{i}', 'text': text, 'type': 'text'}
                    for i, text in enumerate(texts)]
        result = digest.validate_summary({'one_line': '摘要', 'topics': [], 'quotes': [
            {'message_id': 'M0001'}, {'message_id': 'M0001'},
            {'message_id': 'M0002'}, {'message_id': 'M0003'}]}, messages)
        self.assertEqual([q['message_id'] for q in result['quotes']], ['uid-0', 'uid-2'])
        self.assertEqual(result['quotes'][0]['text'], texts[0])

    def _summary_messages(self, count=5):
        return [{"message_uid": f"uid-{i}", "reference": f"M{i:04d}", "timestamp": 100 + i,
                 "author": f"Person {i}", "author_id": f"user-{i}", "text": f"source {i}", "type": "text"}
                for i in range(count)]

    def _config_file(self, root):
        path = Path(root) / "model.json"
        path.write_text(json.dumps({"base_url": "https://model.invalid/v1", "model": "test", "api_key": "test"}))
        return path

    def test_full_summary_is_generated_once_and_cached_without_truncating_topics(self):
        messages = self._summary_messages()
        raw = {'one_line': '完整日报', 'topics': [
            {'title': '技术讨论', 'summary': '保留独立分支',
             'details': [f'具体细节 {i}' for i in range(7)], 'message_ids': ['M0000', 'M0001']},
            {'title': '短小工具分享', 'summary': '一句话也有独立价值',
             'details': ['工具用途与已知限制'], 'message_ids': ['M0004']}], 'quotes': [2]}
        with tempfile.TemporaryDirectory() as root, patch.object(
                digest, '_structured_request', return_value=raw) as request:
            config = self._config_file(root)
            first = digest._summary_with_cache(config, messages, Path(root), edition='review')
            second = digest._summary_with_cache(config, messages, Path(root), edition='review')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual([t['topic'] for t in first['topics']], ['技术讨论', '短小工具分享'])
        self.assertEqual(first['topics'][0]['details'], raw['topics'][0]['details'])
        self.assertEqual(first['topics'][1]['details'], ['工具用途与已知限制'])
        self.assertEqual(first['quotes'][0]['message_id'], 'uid-2')

    def test_invalid_full_response_is_not_saved_as_a_completed_summary(self):
        messages = self._summary_messages(1)
        invalid = {'one_line': '摘要', 'topics': [
            {'title': '话题', 'summary': '缺少真实标题', 'details': ['事实'],
             'message_ids': ['M0000']}], 'quotes': []}
        valid = {'one_line': '摘要', 'topics': [
            {'title': '具体讨论', 'summary': '有来源的内容', 'details': ['事实'],
             'message_ids': ['M0000']}], 'quotes': []}
        with tempfile.TemporaryDirectory() as root, patch.object(
                digest, '_structured_request', side_effect=[invalid, valid]) as request:
            config = self._config_file(root)
            with self.assertRaises(ValueError):
                digest._summary_with_cache(config, messages, Path(root), edition='review')
            completed = digest._summary_with_cache(config, messages, Path(root), edition='review')
            cached = digest._summary_with_cache(config, messages, Path(root), edition='review')
        self.assertEqual(request.call_count, 2)
        self.assertEqual(completed, cached)
        self.assertEqual(completed['topics'][0]['topic'], '具体讨论')

    def test_summary_cache_is_invalidated_by_model_configuration(self):
        messages = self._summary_messages(1)
        def response(title):
            return {'one_line': '摘要', 'topics': [{'title': title, 'summary': '讨论',
                    'details': ['具体信息'], 'message_ids': ['M0000']}], 'quotes': []}
        with tempfile.TemporaryDirectory() as root, patch.object(
                digest, '_structured_request', side_effect=[response('第一版解释'), response('新版解释')]) as request:
            config = self._config_file(root)
            first = digest._summary_with_cache(config, messages, Path(root), edition='review')
            changed = json.loads(config.read_text())
            changed['model'] = 'different-model'
            config.write_text(json.dumps(changed))
            second = digest._summary_with_cache(config, messages, Path(root), edition='review')
        self.assertEqual(request.call_count, 2)
        self.assertEqual(first['topics'][0]['topic'], '第一版解释')
        self.assertEqual(second['topics'][0]['topic'], '新版解释')

    def test_brief_and_review_use_separate_complete_caches(self):
        messages = self._summary_messages(1)
        def response(title):
            return {'one_line': title, 'topics': [{'title': title, 'summary': title,
                    'details': [], 'message_ids': ['M0000']}], 'quotes': []}
        with tempfile.TemporaryDirectory() as root, patch.object(
                digest, '_structured_request', side_effect=[response('简报'), response('完整回顾')]) as request:
            config = self._config_file(root)
            brief = digest._summary_with_cache(config, messages, Path(root), edition='brief')
            review = digest._summary_with_cache(config, messages, Path(root), edition='review')
            again = digest._summary_with_cache(config, messages, Path(root), edition='brief')
        self.assertEqual(request.call_count, 2)
        self.assertEqual(brief['topics'][0]['topic'], '简报')
        self.assertEqual(review['topics'][0]['topic'], '完整回顾')
        self.assertEqual(again, brief)

    def test_brief_prompt_change_preserves_review_success_cache(self):
        messages = self._summary_messages(1)
        raw = {'one_line': '回顾', 'topics': [{'title': '具体回顾', 'summary': '详情',
               'details': ['完整内容'], 'message_ids': ['M0000']}], 'quotes': []}
        with tempfile.TemporaryDirectory() as root, patch.object(
                digest, '_structured_request', return_value=raw) as request:
            config = self._config_file(root)
            digest._summary_with_cache(config, messages, Path(root), edition='review')
            with patch.object(digest, '_SUMMARY_BRIEF_PROMPT', digest._SUMMARY_BRIEF_PROMPT + '新增简报规则。'):
                cached = digest._summary_with_cache(config, messages, Path(root), edition='review')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(cached['topics'][0]['topic'], '具体回顾')

    def test_detailed_topics_and_quotes_are_not_silently_truncated(self):
        messages = [{"message_uid": f"uid-{i}", "reference": f"M{i:04d}", "timestamp": 100 + i,
                     "author": f"Person {i}", "author_id": f"user-{i}", "text": f"source {i}", "type": "text"}
                    for i in range(16)]
        topics = [{"title": f"topic {i}", "summary": "Detailed summary. " * 60,
                   "details": ["A source-grounded detail."], "message_ids": [m["reference"]]}
                  for i, m in enumerate(messages)]
        result = digest.validate_summary({"one_line": "all discussions", "topics": topics,
                                          "quotes": [{"message_id": m["reference"]} for m in messages[:8]]}, messages)
        self.assertEqual([t["topic"] for t in result["topics"]], [t["title"] for t in topics])
        self.assertEqual(result["topics"][-1]["summary"], topics[-1]["summary"])
        self.assertEqual(result["topics"][-1]["details"], topics[-1]["details"])
        self.assertEqual([q["message_id"] for q in result["quotes"]], [m["message_uid"] for m in messages[:8]])

    def test_timeline_order_and_ranges_use_cited_source_times(self):
        messages = [
            {'message_uid': 'late-end', 'reference': 'M0004', 'timestamp': 86600,
             'author': 'Ada', 'text': '后续结果', 'type': 'text'},
            {'message_uid': 'early', 'reference': 'M0001', 'timestamp': 100,
             'author': 'Bo', 'text': '早间问题', 'type': 'text'},
            {'message_uid': 'late-start', 'reference': 'M0003', 'timestamp': 86500,
             'author': 'Ada', 'text': '晚间继续', 'type': 'text'},
            {'message_uid': 'early-end', 'reference': 'M0002', 'timestamp': 200,
             'author': 'Bo', 'text': '早间讨论', 'type': 'text'},
        ]
        summary = {'one_line': '按时间回顾', 'topics': [
            {'title': '同一问题的晚间后续', 'summary': '后续进展', 'details': ['新结果'],
             'message_ids': ['M0004', 'M0003'], 'start_unix': 0, 'end_unix': 1},
            {'title': '早间问题', 'summary': '最初讨论', 'details': ['问题背景'],
             'message_ids': ['M0002', 'M0001'], 'start_unix': 0, 'end_unix': 999999}],
            'quotes': [{'message_id': 'M0003'}, {'message_id': 'M0001'}]}
        result = digest.validate_summary(summary, messages)
        self.assertEqual([t['topic'] for t in result['topics']], ['早间问题', '同一问题的晚间后续'])
        self.assertEqual([(t['start_unix'], t['end_unix']) for t in result['topics']],
                         [(100, 200), (86500, 86600)])
        self.assertEqual([q['message_id'] for q in result['quotes']], ['early', 'late-start'])

    def test_duplicate_topic_citations_do_not_inflate_source_count(self):
        message = {"message_uid": "one", "reference": "M0001", "timestamp": 100,
                   "author": "Same name", "author_id": "person-one", "text": "source", "type": "text"}
        result = digest.validate_summary({"one_line": "x", "topics": [{"title": "x", "summary": "detail",
                    "message_ids": ["M0001", "M0001"]}], "quotes": []}, [message])
        self.assertEqual(result["topics"][0]["count"], 1)
        self.assertEqual(result["topics"][0]["participant_ids"], ["person-one"])

    def test_report_keeps_same_named_people_and_different_dates_separate(self):
        messages = [{"message_uid": "one", "reference": "M0001", "timestamp": 3600,
                     "author": "Same name", "author_id": "person-one", "text": "first", "type": "text"},
                    {"message_uid": "two", "reference": "M0002", "timestamp": 90000,
                     "author": "Same name", "author_id": "person-two", "text": "second", "type": "text"}]
        report = {"stats": {}, "topics": [{"message_ids": ["one"]}], "quotes": [{"message_id": "two", "text": "second"}]}
        digest.enrich_report(report, messages, 0, 93600, {})
        self.assertEqual({p["username"] for p in report["participants"]}, {"person-one", "person-two"})
        self.assertEqual(report["quotes"][0]["speaker_id"], "person-two")
        self.assertEqual(report["topics"][0]["references"][0]["message_uid"], "one")
        self.assertEqual(report["quotes"][0]["source"], {
            "message_uid": "two", "reference": "M0002", "author": "Same name", "author_id": "person-two",
            "timestamp": 90000, "type": "text", "text": "second"})
        self.assertNotIn("image", report["quotes"][0]["source"])
        self.assertEqual([(b['start_unix'], b['count']) for b in report["rhythm_buckets"] if b["count"]], [(3600, 1), (90000, 1)])
        self.assertEqual(report["rhythm_bucket_seconds"], 600)
        self.assertEqual(sum(b["count"] for b in report["rhythm_buckets"]), 2)

    def test_active_rank_keeps_only_twenty_highest_message_counts(self):
        messages = [{"message_uid": f"{person}-{number}", "reference": f"{person}-{number}", "timestamp": 100,
                     "author": f"Person {person}", "author_id": f"user-{person:02d}", "text": "message", "type": "text"}
                    for person in range(1, 24) for number in range(person)]
        report = {"stats": {}, "topics": [], "quotes": []}
        digest.enrich_report(report, messages, 0, 3600, {})
        self.assertEqual([p["username"] for p in report["active_rank"]], [f"user-{i:02d}" for i in range(23, 3, -1)])
        self.assertEqual([p["count"] for p in report["active_rank"]], list(range(23, 3, -1)))
        self.assertEqual(report["stats"]["members"], 23)

    def test_time_window_is_start_inclusive_end_exclusive(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.message_database(root, 0, [99, 100, 199, 200])
            rows = digest._message_rows(path, "test@chatroom", {}, 100, 200)
        self.assertEqual([row["timestamp"] for row in rows], [100, 199])

    def test_numeric_author_resolution_and_system_participation(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.message_database(root, 0, [100, 101], [1, 10000])
            rows = digest._message_rows(path, "test@chatroom", {"wxid_test": {"display_name": "Ada"}}, 0, 200)
        self.assertEqual(rows[0]["author"], "Ada")
        self.assertEqual({row["author_id"] for row in rows if row["author_id"]}, {"wxid_test"})
        self.assertEqual(rows[1]["author_id"], "")

    def test_missing_server_ids_do_not_collide_across_shards(self):
        with tempfile.TemporaryDirectory() as root:
            first = self.message_database(root, 0, [100])
            second = self.message_database(root, 1, [100])
            a = digest._message_rows(first, "test@chatroom", {}, 0, 200)[0]
            b = digest._message_rows(second, "test@chatroom", {}, 0, 200)[0]
        self.assertNotEqual(a["message_uid"], b["message_uid"])


    def test_explicit_integer_timestamp_is_preserved_without_float_or_truncation(self):
        value = 123456789012
        self.assertEqual(digest._parse_time(str(value), 0), value)
        self.assertEqual(digest._parse_time(value, 0), value)

if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import stat
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qq_onebot
import qq_service


class FakeClient:
    def __init__(self, shared, url, token):
        self.shared = shared
        self.url = url
        self.token = token

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def rpc(self, action, params):
        self.shared["rpc"].append((action, dict(params)))
        if action == "get_login_info":
            return {"user_id": self.shared["self_id"]}
        if action == "get_group_info":
            return {"group_id": params["group_id"], "group_name": self.shared["group_name"]}
        if action == "get_rkey":
            return [{"type": "private", "rkey": "private-ticket"},
                    {"type": "group", "rkey": "&rkey=fresh-group-ticket", "created_at": 1, "ttl": 3600}]
        raise AssertionError(f"unexpected RPC: {action}")

    def send_image(self, group_id, path):
        name = Path(path).name
        self.shared["sent"].append((group_id, name))
        failures = self.shared["failures"]
        remaining = failures.get(name, 0)
        if remaining:
            failures[name] = remaining - 1
            error = self.shared.get("failure_error")
            if error is None:
                raise ValueError("known pre-delivery failure")
            raise error
        return f"message-{len(self.shared['sent'])}"

    def send_text(self, group_id, text):
        sent = self.shared.setdefault("sent_text", [])
        sent.append((group_id, text))
        return f"notice-{len(sent)}"


class QQServiceTests(unittest.TestCase):
    def test_chinese_range_is_interpreted_but_bare_summary_keeps_default(self):
        service = self.make_service(intent_interpreter=lambda *args: {'status': 'accepted', 'duration_seconds': 10800})
        ranged = self.message(uid='range-cn', text='总结三小时的聊天记录喵', _at_ids=[self.config['expected_self_id']])
        bare = self.message(uid='bare-summary', timestamp=999_901, text='总结', _at_ids=[self.config['expected_self_id']])
        service.save_messages([ranged, bare], intent_state='pending')
        self.assertEqual(service.process_intents_once(now=self.now), 2)
        with service._connect() as db:
            tasks = {row['request_message_uid']: dict(row) for row in db.execute("SELECT * FROM tasks")}
        self.assertEqual((tasks['range-cn']['start'], tasks['range-cn']['end']), (999_900 - 10800, 999_900))
        self.assertEqual((tasks['bare-summary']['start'], tasks['bare-summary']['end']), (999_901 - 86400, 999_901))

    def test_unparsed_range_failure_does_not_create_a_default_window(self):
        def unavailable(*args):
            raise OSError('synthetic intent transport failure')
        service = self.make_service(intent_interpreter=unavailable)
        message = self.message(text='总结最近三小时', _at_ids=[self.config['expected_self_id']])
        service.save_messages([message], intent_state='pending')
        self.assertEqual(service.process_intents_once(now=self.now), 0)
        self.assertEqual(service.status()['tasks'], [])
        with service._connect() as db:
            row = db.execute('SELECT intent_state,intent_attempts FROM messages WHERE message_uid=?', (message['message_uid'],)).fetchone()
        self.assertEqual(tuple(row), ('pending', 1))

    def test_natural_intent_accepts_complete_fenced_json_and_rejects_truncation(self):
        import io
        from unittest.mock import patch
        config = self.root / 'intent-model.json'
        config.write_text(json.dumps({'base_url': 'https://model.example.invalid', 'model': 'fixture', 'api_key': 'test-only-key'}))
        content = '```json\n{"accepted":true,"duration_seconds":10800}\n```'
        complete = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}).encode()
        with patch.object(qq_service.urllib.request, 'urlopen', return_value=io.BytesIO(complete)):
            result = qq_service._interpret_natural('总结三小时', 'fixture-command', self.now, config)
        self.assertEqual((result['start'], result['end'], result['duration_seconds']), (self.now - 10800, self.now, 10800))
        malformed = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': '```json\n{"accepted":true,'}}]}).encode()
        with patch.object(qq_service.urllib.request, 'urlopen', return_value=io.BytesIO(malformed)):
            with self.assertRaises(RuntimeError):
                qq_service._interpret_natural('总结三小时', 'fixture-command', self.now, config)

    def test_completed_requested_summary_is_eligible_for_a_notice(self):
        access = self.root / 'request-site-access.json'
        access.write_text(json.dumps({'group_id': self.config['group_id'], 'username': 'synthetic-reader',
            'password': 'test-only-password', 'url': f"https://reports.example.invalid/qq/{self.config['group_id']}/",
            'bot_name': self.config['bot_names'][0], 'enabled_from': self.now - 1}))
        access.chmod(0o600)
        self.config['site_access_file'] = access
        service = self.make_service()
        task_id = service.enqueue_request(self.now, 10800, 'synthetic-trigger')
        with service._connect() as db:
            db.execute("UPDATE tasks SET status='complete' WHERE id=?", (task_id,))
            db.execute("UPDATE receipts SET state='sent',message_id='fixture-'||edition,attempts=1 WHERE task_id=?", (task_id,))
        self.assertEqual(service.process_site_access_notices()[0]['status'], 'sent')
        self.assertEqual(service.process_site_access_notices()[0]['status'], 'sent')
        self.assertEqual(len(self.shared['sent_text']), 1)
        self.assertEqual(self.shared['sent'], [])

    def test_daily_notice_waits_for_both_images_and_survives_reopen(self):
        access = self.root / "site-access.json"
        access.write_text(json.dumps({
            "group_id": self.config['group_id'], "username": "synthetic-reader", "password": "test-only-password",
            "url": f"https://reports.example.invalid/qq/{self.config['group_id']}/",
            "bot_name": self.config['bot_names'][0], "enabled_from": self.now - 1}))
        access.chmod(0o600)
        self.config['site_access_file'] = access
        self.service = self.make_service()
        task_id = self.service._insert_task(kind='daily', start=self.now - 86400, end=self.now, now=self.now,
            daily_key=dt.datetime.fromtimestamp(self.now, qq_service.TZ).date().isoformat())
        with self.service._connect() as db:
            db.execute("UPDATE tasks SET status='complete' WHERE id=?", (task_id,))
            db.execute("UPDATE receipts SET state='sent',attempts=1,message_id='confirmed-brief' WHERE task_id=? AND edition='brief'", (task_id,))
        self.assertEqual(self.service.process_site_access_notices(), [])
        self.assertEqual(self.shared.get('sent_text', []), [])
        with self.service._connect() as db:
            db.execute("UPDATE receipts SET state='sent',attempts=1,message_id='confirmed-review' WHERE task_id=? AND edition='review'", (task_id,))
            before = [tuple(row) for row in db.execute("SELECT edition,state,attempts,message_id FROM receipts WHERE task_id=? ORDER BY edition", (task_id,))]
        self.assertEqual(self.service.process_site_access_notices()[0]['status'], 'sent')
        reopened = self.make_service()
        self.assertEqual(reopened.process_site_access_notices()[0]['status'], 'sent')
        self.assertEqual(len(self.shared['sent_text']), 1)
        self.assertEqual(self.shared['sent'], [])
        with reopened._connect() as db:
            after = [tuple(row) for row in db.execute("SELECT edition,state,attempts,message_id FROM receipts WHERE task_id=? ORDER BY edition", (task_id,))]
        self.assertEqual(after, before)

    def test_legacy_unknown_submission_without_manifest_is_not_regenerated(self):
        self.service.save_messages([self.message()])
        task_id = self.service.enqueue_request(1_000_000, 3600)
        with self.service._connect() as db:
            db.execute("UPDATE receipts SET state='unknown' WHERE task_id=? AND edition='brief'", (task_id,))
            db.commit()
        def regenerate(_):
            raise AssertionError('an uncertain legacy submission requires manual verification')
        self.service.digest_runner = regenerate
        self.assertEqual(self.service.process_next_task(now=self.now), 'rejected')
        self.assertEqual(self.shared['sent'], [])
        receipts = {row['edition']: row['state'] for row in self.service.status()['receipts']}
        self.assertEqual(receipts['brief'], 'unknown')

    def test_confirmed_pair_remains_complete_without_a_usable_manifest(self):
        self.service.save_messages([self.message()])
        task_id = self.service.enqueue_request(1_000_000, 3600)
        self.assertEqual(self.service.process_next_task(now=self.now), 'complete')
        self.service._set_meta('prepared:' + task_id, '{}')
        def regenerate(_):
            raise AssertionError('confirmed deliveries need no regeneration')
        self.service.digest_runner = regenerate
        before = list(self.shared['sent'])
        self.assertEqual(self.service.process_task(self.service.status()['tasks'][0], now=self.now + 1), 'complete')
        self.assertEqual(self.shared['sent'], before)

    def test_late_records_do_not_regenerate_partially_delivered_reports(self):
        self.shared['failures']['review.png'] = 1
        self.service.save_messages([self.message()])
        self.service.enqueue_request(1_000_000, 3600)
        self.assertEqual(self.service.process_next_task(now=self.now), 'pending')
        brief = self.service.reports_dir / 'brief.png'
        original_image = brief.read_bytes()
        original_sources = (self.service.reports_dir / 'sources.json').read_bytes()
        self.service.save_messages([self.message(uid='late-sync', timestamp=999_950)])
        def regenerate(_):
            raise AssertionError('a prepared bundle must not be regenerated')
        self.service.digest_runner = regenerate
        self.assertEqual(self.service.process_next_task(now=self.now + 100), 'complete')
        self.assertEqual(brief.read_bytes(), original_image)
        self.assertEqual((self.service.reports_dir / 'sources.json').read_bytes(), original_sources)
        self.assertEqual([name for _, name in self.shared['sent']].count('brief.png'), 1)

    def test_changed_prepared_image_stops_without_resending_or_regenerating(self):
        self.shared['failures']['review.png'] = 1
        self.service.save_messages([self.message()])
        self.service.enqueue_request(1_000_000, 3600)
        self.assertEqual(self.service.process_next_task(now=self.now), 'pending')
        (self.service.reports_dir / 'brief.png').write_bytes(b'changed externally')
        before = list(self.shared['sent'])
        self.assertEqual(self.service.process_next_task(now=self.now + 100), 'rejected')
        self.assertEqual(self.shared['sent'], before)

    def test_only_structured_provider_http_400_is_terminal(self):
        import json
        import stat
        import subprocess
        args = {'task_id': 'a' * 32, 'sources_path': self.root / 'sources.json',
                'output_dir': self.root / 'reports', 'start': 0, 'end': 3600}
        provider = ('{"error_type":"PermanentProviderRequestError","origin":"model_provider",'
                    '"stage":"summary:brief","http_status":400,'
                    '"source":"deployment/qq_digest.py:_summary_for_edition",'
                    '"provider_error":{"code":"invalid_request","type":"bad_request",'
                    '"param":"messages","message":"unsupported field"}}')
        with patch.object(qq_service.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', provider)):
            with self.assertRaisesRegex(qq_service.PermanentTaskError, 'summary:brief'):
                self.service._run_digest_cli(args)
        diagnostic = next((self.service.runtime_dir / 'qq-digest' / 'task-failures').glob('*.json'))
        details = json.loads(diagnostic.read_text(encoding='utf-8'))
        self.assertEqual(details['provider_error'], {'code': 'invalid_request', 'type': 'bad_request',
                                                     'param': 'messages', 'message': 'unsupported field'})
        self.assertEqual(stat.S_IMODE(diagnostic.stat().st_mode), 0o600)
        with patch.object(qq_service.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '{"error":"image fetch failed with HTTP 400"}')):
            with self.assertRaises(RuntimeError):
                self.service._run_digest_cli(args)

    def test_image_generation_passes_only_private_rkey_file_path(self):
        from unittest.mock import patch
        import json
        import stat
        import subprocess
        args = {'task_id': 'b' * 32, 'sources_path': self.root / 'sources.json',
                'output_dir': self.root / 'reports', 'start': 0, 'end': 3600,
                'messages': [{'type': 'image', 'image_items': [{'url': 'https://media.invalid/a?rkey=old'}]}]}
        observed = {}

        def capture(command, **kwargs):
            path = Path(command[command.index('--group-rkey-file') + 1])
            observed.update(path=path, command=command,
                            record=json.loads(path.read_text(encoding='utf-8')),
                            mode=stat.S_IMODE(path.stat().st_mode))
            return subprocess.CompletedProcess(command, 0, '{}', '')

        with patch.object(qq_service.subprocess, 'run', side_effect=capture):
            self.service._run_digest_cli(args)
        self.assertEqual(observed['record'], {'group_id': self.config['group_id'], 'type': 'group',
                                               'rkey': '&rkey=fresh-group-ticket'})
        self.assertEqual(observed['mode'], 0o600)
        self.assertNotIn('fresh-group-ticket', observed['command'])
        self.assertFalse(observed['path'].exists())
        self.assertIn(('get_rkey', {}), self.shared['rpc'])

    def test_image_400_failure_logs_stage_source_and_message_uid(self):
        from unittest.mock import patch
        import json
        import subprocess
        args = {'task_id': 'c' * 32, 'sources_path': self.root / 'sources.json',
                'output_dir': self.root / 'reports', 'start': 0, 'end': 3600}
        stderr = json.dumps({'error_type': 'ValueError', 'stage': 'image_preparation',
                             'origin': 'qq_digest', 'http_status': 400,
                             'source': 'qq_digest.py:_fetch_image:180',
                             'message_uid': 'qq:1000000001:42000123'})
        with patch.object(qq_service.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 1, '', stderr)):
            with self.assertRaises(RuntimeError):
                self.service._run_digest_cli(args)
        diagnostic = next((self.service.runtime_dir / 'qq-digest' / 'task-failures').glob('*.json'))
        details = json.loads(diagnostic.read_text(encoding='utf-8'))
        self.assertEqual(details['stage'], 'image_preparation')
        self.assertEqual(details['source'], 'qq_digest.py:_fetch_image:180')
        self.assertEqual(details['http_status'], 400)
        self.assertEqual(details['message_uid'], 'qq:1000000001:42000123')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        token = self.root / "token"
        token.write_text("private-token", encoding="utf-8")
        self.now = 1_000_000
        self.shared = {
            "self_id": "1000000002",
            "group_name": "合成测试群",
            "rpc": [],
            "sent": [],
            "failures": {},
        }
        self.config = {
            "onebot_url": "ws://onebot.invalid",
            "onebot_token_file": token,
            "expected_self_id": "1000000002",
            "group_id": 1000000001,
            "group_name": self.shared["group_name"],
            "bot_names": ["演示机器人"],
            "runtime_dir": self.root / "runtime",
            "reports_dir": self.root / "reports",
            "model_config": self.root / "model.json",
            "vision_fallback_config": None,
            "daily_hour": 5,
            "enabled_from": 1,
        }
        self.service = self.make_service()

    def test_expected_self_id_has_no_production_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "qq.json"
            config_path.write_text(json.dumps({
                "onebot_url": "ws://onebot.invalid", "onebot_token_file": "token",
                "group_id": 1000000001, "group_name": "合成测试群", "bot_names": ["演示机器人"],
                "runtime_dir": "runtime", "reports_dir": "reports", "model_config": "model.json",
                "enabled_from": 1}), encoding="utf-8")
            with self.assertRaisesRegex(qq_service.ConfigurationError, "expected_self_id"):
                qq_service.load_config(config_path)

    def tearDown(self):
        self.temp.cleanup()

    def make_service(self, *, digest_runner=None, failure_error=None, intent_interpreter=None, history_reader=None):
        self.shared["failure_error"] = failure_error
        return qq_service.QQService(
            self.config,
            client_factory=lambda url, token: FakeClient(self.shared, url, token),
            clock=lambda: self.now,
            digest_runner=digest_runner or self.digest_runner,
            intent_interpreter=intent_interpreter or (lambda text, uid, timestamp, config: {
                "status": "accepted", "duration_seconds": 3600}),
            history_reader=history_reader or (lambda client, group, start, end: []),
        )

    @staticmethod
    def message(uid="msg-1", timestamp=999_900, **extra):
        value = {
            "message_uid": uid,
            "timestamp": timestamp,
            "author_id": "123456",
            "author": "member",
            "type": "text",
            "text": "hello",
            "image_items": [],
            "reference": None,
        }
        value.update(extra)
        return value

    def test_database_and_private_directories_use_restricted_modes(self):
        self.assertEqual(stat.S_IMODE(self.service.runtime_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.service.reports_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.service.db_path.stat().st_mode), 0o600)

    def test_constructor_and_status_preserve_inflight_delivery_state(self):
        task_id = self.service.enqueue_request(self.now, 3600)
        with self.service._connect() as db:
            db.execute("UPDATE tasks SET status='processing' WHERE id=?", (task_id,))
            db.execute("UPDATE receipts SET state='sending' WHERE task_id=? AND edition='brief'", (task_id,))
            db.commit()
        reopened = self.make_service()
        with reopened._connect() as db:
            task = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            receipt = db.execute("SELECT state FROM receipts WHERE task_id=? AND edition='brief'",
                                 (task_id,)).fetchone()
        self.assertEqual((task["status"], receipt["state"]), ("processing", "sending"))

        from contextlib import redirect_stdout
        import io
        config_path = self.root / "service.json"
        config_path.write_text(json.dumps({key: str(value) if isinstance(value, Path) else value
                                           for key, value in self.config.items()}), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(qq_service.main(["--config", str(config_path), "--status"]), 0)
        with reopened._connect() as db:
            task = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            receipt = db.execute("SELECT state FROM receipts WHERE task_id=? AND edition='brief'",
                                 (task_id,)).fetchone()
        self.assertEqual((task["status"], receipt["state"]), ("processing", "sending"))

    def test_second_serve_instance_fails_without_recovering_active_rows(self):
        task_id = self._mark_inflight(self.service)
        second = self.make_service()
        self.service._acquire_serve_lock()
        try:
            with self.assertRaises(RuntimeError):
                second.serve()
            status = second.status()
            task = next(row for row in status["tasks"] if row["id"] == task_id)
            self.assertEqual(task["status"], "processing")
            states = {row["edition"]: row["state"] for row in status["receipts"]}
            self.assertEqual(states["brief"], "sending")
        finally:
            self.service._release_serve_lock()

    def test_only_new_serve_recovers_interrupted_sends(self):
        task_id = self._mark_inflight(self.service)
        restarted = self.make_service()
        before = restarted.status()
        self.assertEqual(next(row for row in before["tasks"] if row["id"] == task_id)["status"], "processing")
        restarted._collector_loop = lambda: restarted.close()
        restarted._intent_loop = lambda: None
        restarted._worker_loop = lambda: None
        restarted.serve()
        after = restarted.status()
        self.assertEqual(next(row for row in after["tasks"] if row["id"] == task_id)["status"], "pending")
        states = {row["edition"]: row["state"] for row in after["receipts"]}
        self.assertEqual(states["brief"], "unknown")

    def test_history_preceding_enablement_is_stored_but_never_intent_processed(self):
        self.service.enabled_from = 500_000
        old = self.message(timestamp=499_999, text="@演示机器人 总结")
        self.assertEqual(self.service.save_messages([old], intent_state="pending"), 1)
        self.assertEqual(self.service.process_intents_once(now=self.now), 1)
        with self.service._connect() as db:
            row = db.execute("SELECT intent_state FROM messages WHERE message_uid=?", (old["message_uid"],)).fetchone()
        self.assertEqual(row["intent_state"], "ignored")

    def test_initial_collector_backfills_one_day_before_enablement_with_overlap(self):


        self.service.enabled_from = 500_000
        reads = []
        service = self.make_service(history_reader=lambda client, group_id, start, end:
                                    reads.append((group_id, start, end)) or [])
        service.enabled_from = 500_000
        service.collect_once(now=self.now)
        self.assertEqual(reads[0], (1000000001, 500_000 - qq_service.DEFAULT_WINDOW - qq_service.OVERLAP_SECONDS,
                                    self.now + 1))

    def test_bot_messages_are_neither_intents_nor_summary_sources(self):
        own = self.message(uid="bot-output", author_id="1000000002", text="@演示机器人 总结")
        human = self.message(uid="human-input")
        self.assertFalse(self.service._is_intent_candidate(own))
        self.service.save_messages([own, human])
        self.assertEqual([message["message_uid"] for message in self.service._task_messages(0, self.now)],
                         [human["message_uid"]])

    @staticmethod
    def digest_runner(args):
        editions = {}
        for edition in ("brief", "review"):
            path = args["output_dir"] / f"{edition}.png"
            path.write_bytes(b"PNG bytes")
            editions[edition] = {"png": str(path), "html": str(path.with_suffix(".html")),
                                 "json": str(path.with_suffix(".json"))}
        return {"files": {"editions": editions, "sources": "sources.json"},
                "counts": {"messages": 1, "images": 0, "members": 1}}
    def _mark_inflight(self, service):
        task_id = service.enqueue_request(1_000_000, 3600)
        with service._connect() as db:
            db.execute("UPDATE tasks SET status='processing' WHERE id=?", (task_id,))
            db.execute("UPDATE receipts SET state='sending' WHERE task_id=? AND edition='brief'", (task_id,))
            db.commit()
        return task_id

    def test_request_window_and_terminal_overlimit_rejection(self):
        regular = self.service.enqueue_request(1_000_000, 3 * 3600)
        too_long = self.service.enqueue_request(1_000_000, 604_801)
        rows = {task["id"]: task for task in self.service.status()["tasks"]}
        self.assertEqual(rows[regular]["start"], 1_000_000 - 10_800)
        self.assertEqual(rows[regular]["end"], 1_000_000)
        self.assertEqual(rows[too_long]["start"], 1_000_000 - 604_801)
        self.assertEqual(rows[too_long]["status"], "rejected")
        self.assertEqual(rows[too_long]["error_type"], "WindowTooLong")

    def test_daily_window_uses_fixed_utc_plus_8_and_is_deduplicated(self):
        local = dt.datetime(2026, 11, 2, 5, 0, tzinfo=qq_service.TZ)
        timestamp = int(local.timestamp())
        first = self.service.schedule_daily(timestamp)
        second = self.service.schedule_daily(timestamp + 600)
        self.assertEqual(first, second)
        row = next(task for task in self.service.status()["tasks"] if task["id"] == first)
        self.assertEqual((row["start"], row["end"]), (timestamp - 86400, timestamp))

    def test_fresh_tasks_are_claimed_before_due_retries(self):
        retry = self.service._insert_task(kind="request", start=900, end=1000, now=10)
        fresh = self.service._insert_task(kind="request", start=950, end=1000, now=20)
        with self.service._connect() as db:
            db.execute("UPDATE tasks SET attempt_count=1,next_attempt_at=0 WHERE id=?", (retry,))
            db.commit()
        claimed = self.service._claim_task(now=1000)
        self.assertEqual(claimed["id"], fresh)
        self.assertEqual(self.service._claim_task(now=1000)["id"], retry)

    def test_overlapping_message_inserts_are_deduplicated(self):
        message = self.message()
        self.assertEqual(self.service.save_messages([message], intent_state="pending"), 1)
        self.assertEqual(self.service.save_messages([message], intent_state="pending"), 0)
        self.assertEqual(len(self.service._task_messages(900_000, 1_000_000)), 1)

    def test_numeric_at_and_explicit_duration_create_exact_window(self):
        message = self.message(text="请总结最近3小时", _at_ids=["1000000002"])
        self.service.save_messages([message], intent_state="pending")
        self.assertEqual(self.service.process_intents_once(now=self.now), 1)
        task = self.service.status()["tasks"][0]
        self.assertEqual(task["kind"], "request")
        self.assertEqual((task["start"], task["end"]), (999_900 - 10_800, 999_900))

    def test_natural_language_interpreter_receives_mention_stripped_text(self):
        received = []
        service = self.make_service(intent_interpreter=lambda text, uid, timestamp, config: (
            received.append(text) or {"status": "accepted", "duration_seconds": 7200}))
        service.save_messages([self.message(text="@演示机器人 请把刚才讨论按时间线归纳给我")], intent_state="pending")
        self.assertEqual(service.process_intents_once(now=self.now), 1)
        self.assertEqual(received[0].strip(), "请把刚才讨论按时间线归纳给我")
        task = service.status()["tasks"][0]
        self.assertEqual((task["start"], task["end"]), (999_900 - 7200, 999_900))

    def test_identity_mismatch_stops_collection_before_history(self):
        self.shared["group_name"] = "different group"
        reads = []
        service = self.make_service(history_reader=lambda *args: reads.append(args) or [])
        with self.assertRaises(PermissionError):
            service.collect_once(now=self.now)
        self.assertEqual(reads, [])
        self.assertEqual(self.shared["rpc"][-1], ("get_group_info", {"group_id": 1000000001}))

    def test_partial_success_resumes_only_the_failed_edition(self):
        self.shared["failures"]["review.png"] = 1
        self.service.save_messages([self.message()])
        self.service.enqueue_request(1_000_000, 3600)
        self.assertEqual(self.service.process_next_task(now=self.now), "pending")
        self.assertTrue((self.service.reports_dir / "brief.png").is_file())
        self.assertTrue((self.service.reports_dir / "sources.json").is_file())
        self.assertEqual([name for _, name in self.shared["sent"]].count("brief.png"), 1)
        self.assertEqual([name for _, name in self.shared["sent"]].count("review.png"), 1)
        self.assertEqual(self.service.process_next_task(now=self.now + 100), "complete")
        self.assertEqual([name for _, name in self.shared["sent"]].count("brief.png"), 1)
        self.assertEqual([name for _, name in self.shared["sent"]].count("review.png"), 2)
        status = self.service.status()
        self.assertEqual(status["tasks"][0]["status"], "complete")
        receipts = {(r["edition"], r["state"]) for r in status["receipts"]}
        self.assertEqual(receipts, {("brief", "sent"), ("review", "sent")})

    def test_unknown_delivery_is_persisted_and_never_retried(self):
        self.shared["failures"]["brief.png"] = 1
        self.service = self.make_service(failure_error=qq_onebot.DeliveryUnknownError("unknown"))
        self.service.save_messages([self.message()])
        self.service.enqueue_request(1_000_000, 3600)
        self.assertEqual(self.service.process_next_task(now=self.now), "partial_unknown")
        sent_before = list(self.shared["sent"])
        task = self.service.status()["tasks"][0]
        self.assertEqual(self.service.process_task(task, now=self.now + 1), "partial_unknown")
        self.assertEqual(self.shared["sent"], sent_before)
        receipts = {r["edition"]: r["state"] for r in self.service.status()["receipts"]}
        self.assertEqual(receipts, {"brief": "unknown", "review": "sent"})


if __name__ == "__main__":
    unittest.main()

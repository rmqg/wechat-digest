import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import qq_site_access_notice as notice


class Client:
    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    def send_text(self, group_id, text):
        self.calls.append((group_id, text))
        if self.error:
            raise self.error
        return 987654321


class QQSiteAccessNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.json"
        self.state_dir = self.root / "receipts"
        self.config = {"group_id": 321, "username": "u@ser", "password": "p/ass?word",
                       "url": "https://reports.example/qq/321/", "bot_name": "FixtureBot",
                       "enabled_from": 100}
        self.save_config()
        self.task = {"id": "daily-task-1", "kind": "daily", "status": "complete",
                     "daily_key": "2026-11-05", "end": 200}
        self.client = Client()
        self.opens = 0
        self.closes = 0

    def tearDown(self):
        self.temp.cleanup()

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def open_verified(self):
        self.opens += 1
        return self.client

    def close_client(self, client):
        self.assertIs(client, self.client)
        self.closes += 1

    def run_notice(self, *, task=None, now=1000):
        return notice.process_notice(self.task if task is None else task,
                                     config_path=self.config_path, group_id=321,
                                     bot_names=["FixtureBot"], state_dir=self.state_dir,
                                     open_verified=self.open_verified, close_client=self.close_client,
                                     now=now)

    def receipt_path(self):
        ident = hashlib.sha256(self.task["id"].encode()).hexdigest()
        return self.state_dir / f"{ident}.json"

    def test_only_completed_daily_eligible_task_sends(self):
        for changes in ({"status": "pending"}, {"status": "partial_unknown"},
                        {"kind": "on_demand"}, {"daily_key": "2026-02-30"}):
            task = {**self.task, **changes}
            self.assertEqual(self.run_notice(task=task)["status"], "ineligible")
        self.assertEqual(self.run_notice(task={**self.task, "end": 99})["status"], "disabled")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.opens, 0)

    def test_success_is_independent_terminal_and_reopen_safe(self):
        self.assertEqual(self.run_notice(), {"status": "sent", "message_id": "987654321"})
        self.assertEqual(self.run_notice()["status"], "sent")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.closes, 1)
        receipt = json.loads(self.receipt_path().read_text())
        self.assertEqual(receipt["status"], "sent")
        self.assertEqual(receipt["attempts"], 1)
        self.assertEqual(receipt["group_id"], 321)
        self.assertEqual(os.stat(self.state_dir).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.receipt_path()).st_mode & 0o777, 0o600)
        serialized = self.receipt_path().read_text()
        for secret in ("u@ser", "p/ass?word", "https://"):
            self.assertNotIn(secret, serialized)

    def test_sending_recovery_and_read_are_non_mutating(self):
        self.state_dir.mkdir(mode=0o700)
        self.receipt_path().write_text(json.dumps({"task_id": self.task["id"], "status": "sending",
                                                   "report_day": self.task["daily_key"], "group_id": 321,
                                                   "attempts": 1, "before_submit": False}), encoding="utf-8")
        before = self.receipt_path().read_bytes()
        self.assertEqual(notice.read_receipts(self.state_dir)[0]["status"], "sending")
        self.assertEqual(self.receipt_path().read_bytes(), before)
        self.assertEqual(self.run_notice()["status"], "unknown")
        self.assertEqual(self.run_notice()["status"], "unknown")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(json.loads(self.receipt_path().read_text())["error_type"], "InterruptedWhileSending")

    def test_post_submit_error_is_unknown_and_not_retried(self):
        self.client.error = RuntimeError("private password and URL")
        self.assertEqual(self.run_notice()["status"], "unknown")
        self.assertEqual(self.run_notice()["status"], "unknown")
        self.assertEqual(len(self.client.calls), 1)
        receipt = self.receipt_path().read_text()
        self.assertNotIn("private password", receipt)
        self.assertNotIn("reports.example", receipt)
        self.assertEqual(json.loads(receipt)["error_type"], "RuntimeError")

    def test_connection_failure_retries_safely_after_backoff(self):
        def fail_open():
            raise OSError("credential-containing error text")
        result = notice.process_notice(self.task, config_path=self.config_path, group_id=321,
                                       bot_names=["FixtureBot"], state_dir=self.state_dir,
                                       open_verified=fail_open, close_client=self.close_client, now=1000)
        self.assertEqual(result, {"status": "failed", "retry_count": 1, "next_attempt_at": 1030})
        state = json.loads(self.receipt_path().read_text())
        self.assertTrue(state["before_submit"])
        self.assertEqual(state["attempts"], 0)
        self.assertNotIn("credential-containing", self.receipt_path().read_text())
        self.assertEqual(self.run_notice(now=1029)["status"], "failed")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.run_notice(now=1030)["status"], "sent")
        self.assertEqual(len(self.client.calls), 1)

    def test_wrong_group_bot_and_unsafe_url_never_connect(self):
        valid = dict(self.config)
        invalid = (("group_id", 999), ("bot_name", "unconfigured\nname"),
                   ("url", "https://u:p@reports.example/qq/321/"),
                   ("url", "https://reports.example/qq/321/?token=private"),
                   ("url", "http://reports.example/qq/321/"),
                   ("url", "https://reports.example/qq/999/"))
        for index, (field, value) in enumerate(invalid):
            with self.subTest(field=field, value=value):
                self.config = {**valid, field: value}
                self.save_config()
                self.state_dir = self.root / f"receipts-{index}"
                self.assertEqual(self.run_notice()["status"], "failed")
                self.assertEqual(self.client.calls, [])
                self.assertEqual(self.opens, 0)
        for path in self.root.glob("receipts-*/*.json"):
            self.assertNotIn("https://u:p@", path.read_text())

    def test_concurrent_processor_does_not_submit_a_second_notice(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        started, release = threading.Event(), threading.Event()
        original = self.client.send_text

        def delayed(group_id, text):
            started.set()
            if not release.wait(5):
                raise TimeoutError("fixture release timed out")
            return original(group_id, text)

        self.client.send_text = delayed
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.run_notice)
            try:
                self.assertTrue(started.wait(3))
                self.assertEqual(self.run_notice()["status"], "busy")
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5)["status"], "sent")
        self.assertEqual(len(self.client.calls), 1)

    def test_receipt_failure_after_confirmed_submit_stays_unknown(self):
        write = notice._atomic_json

        def fail_sent(path, value):
            if value.get("status") == "sent":
                raise OSError("fixture storage failure")
            return write(path, value)

        with patch.object(notice, "_atomic_json", side_effect=fail_sent):
            self.assertEqual(self.run_notice()["status"], "unknown")
        self.assertEqual(self.run_notice()["status"], "unknown")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(json.loads(self.receipt_path().read_text())["location"], "receipt_after_submit")

    def test_access_url_escapes_credentials(self):
        self.assertEqual(notice._access_url(self.config),
                         "https://u%40ser:p%2Fass%3Fword@reports.example/qq/321/")

    def test_absent_receipt_directory_is_read_without_creation(self):
        self.assertEqual(notice.read_receipts(self.state_dir), [])
        self.assertFalse(self.state_dir.exists())


if __name__ == "__main__":
    unittest.main()

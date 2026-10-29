import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import site_access_notice as notice

TEST_GROUP_ID = "fixture10000000000@chatroom"
TEST_GROUP_NAME = "Synthetic Notice Fixture"
TEST_BOT_ID = "fixture-bot-id"
TEST_BOT_NAME = "FixtureBot"
TEST_URL = "https://site.example.invalid/"


class SiteNoticeBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "report-site-access.json"
        self.messages = self.root / "messages"
        self.receipts = self.root / "receipts"
        project = {"group": {"id": TEST_GROUP_ID, "name": TEST_GROUP_NAME},
                   "bot": {"username": TEST_BOT_ID, "names": [TEST_BOT_NAME]},
                   "public_url": TEST_URL, "timezone": "Asia/Shanghai", "daily_hour": 5,
                   "worker_uid": 1234, "worker_gid": 2345}
        project_patch = patch.object(notice.settings, "load_project", return_value=project)
        project_patch.start()
        self.addCleanup(project_patch.stop)
        self.config.write_text(json.dumps({"chat_id": TEST_GROUP_ID, "url": TEST_URL,
                                           "username": "fixture-user", "password": "fixture-secret",
                                           "enabled_from": 100}), encoding="utf-8")
        self.receipt = {"status": "sent", "report_day": "2026-10-26", "chat_id": TEST_GROUP_ID,
                        "server_id": "image-id", "png_sha256": "a" * 64, "sent_at": 200}
        self.paths = (patch.object(notice, "MESSAGES", self.messages), patch.object(notice, "RECEIPTS", self.receipts))
        for item in self.paths:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.temp.cleanup)

    @staticmethod
    def result(value, returncode=0):
        return subprocess.CompletedProcess([], returncode, json.dumps(value), "fixture-secret must not leak")

    def sender(self, response, calls):
        def run(args, **kwargs):
            calls.append(args)
            text_file = Path(args[args.index("--text-file") + 1])
            digest = hashlib.sha256(text_file.read_bytes()).hexdigest()
            value = dict(response)
            value.setdefault("sha256", digest)
            return self.result(value)
        return run

    def test_access_url_encodes_credentials_without_changing_destination(self):
        config = json.loads(self.config.read_text())
        config.update(username='user@测试 /%', password='p@ss:/?#% +口令')
        self.config.write_text(json.dumps(config), encoding='utf-8')
        notice.process_notice(self.receipt, config_path=self.config, runner=self.sender(
            {'status': 'failed', 'sent': False, 'before_submit': True}, []))
        body = next(self.messages.iterdir()).read_text()
        url = urlsplit(re.search(r'https://\S+', body).group())
        self.assertEqual(unquote(url.username), config['username'])
        self.assertEqual(unquote(url.password), config['password'])
        self.assertEqual((url.scheme, url.hostname, url.path, url.query, url.fragment),
                         ('https', urlsplit(TEST_URL).hostname, '', '', ''))

    def test_before_submit_failure_retries_only_text_then_saves_independent_server_receipt(self):
        calls = []
        failed = {"status": "failed", "sent": False, "before_submit": True}
        first = notice.process_notice(self.receipt, config_path=self.config, runner=self.sender(failed, calls))
        self.assertEqual(first["status"], "failed")
        sent = {"status": "sent", "sent": True, "confirmed": True, "server_id": "text-id"}
        second = notice.process_notice(self.receipt, config_path=self.config, runner=self.sender(sent, calls))
        self.assertEqual(second["server_id"], "text-id")
        persisted = json.loads((self.receipts / "2026-10-26.json").read_text())
        self.assertEqual((persisted["status"], persisted["chat_id"], persisted["server_id"]), ("sent", TEST_GROUP_ID, "text-id"))
        self.assertEqual(persisted["png_sha256"], self.receipt["png_sha256"])
        payload = next(self.messages.iterdir()).read_bytes()
        self.assertEqual(persisted["text_sha256"], hashlib.sha256(payload).hexdigest())
        self.assertGreater(persisted["sent_at"], 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(os.stat(next(self.messages.iterdir())).st_mode & 0o777, 0o600)

    def test_sent_and_unknown_are_terminal(self):
        calls = []
        response = {"status": "sent", "sent": True, "confirmed": True, "server_id": "text-id"}
        notice.process_notice(self.receipt, config_path=self.config, runner=self.sender(response, calls))
        notice.process_notice(self.receipt, config_path=self.config, runner=self.sender(response, calls))
        self.assertEqual(len(calls), 1)
        self.receipts.joinpath("2026-10-26.json").write_text(json.dumps({"status": "unknown", "png_sha256": self.receipt["png_sha256"]}))
        notice.process_notice(self.receipt, config_path=self.config, runner=self.sender(response, calls))
        self.assertEqual(len(calls), 1)

    def test_disabled_old_wrong_target_and_tampered_text_never_send(self):
        calls = []
        runner = self.sender({"status": "sent", "sent": True, "confirmed": True, "server_id": "x"}, calls)
        old = dict(self.receipt, sent_at=99)
        self.assertEqual(notice.process_notice(old, config_path=self.config, runner=runner)["status"], "disabled")
        wrong = dict(self.receipt, chat_id="another10000000000@chatroom")
        self.assertEqual(notice.process_notice(wrong, config_path=self.config, runner=runner)["status"], "ineligible")
        notice.MESSAGES.mkdir(parents=True)
        notice.process_notice(self.receipt, config_path=self.config, runner=lambda *a, **k: self.result({"status": "failed", "sent": False, "before_submit": True}))
        msg = next(self.messages.iterdir())
        msg.write_text("changed", encoding="utf-8")
        self.assertEqual(notice.process_notice(self.receipt, config_path=self.config, runner=runner)["status"], "failed")
        self.assertEqual(calls, [])


    def test_manual_notice_uses_request_key_and_preserves_text_hash(self):
        request_id = "b" * 64
        receipt = dict(self.receipt, request_id=request_id)
        response = {"status": "sent", "sent": True, "confirmed": True, "server_id": "manual-text-id"}
        calls = []
        notice.process_notice(receipt, request_id=request_id, config_path=self.config, runner=self.sender(response, calls))
        persisted = json.loads((self.receipts / f"{request_id}.json").read_text())
        payload = (self.messages / f"{request_id}-{persisted['text_sha256']}.txt").read_bytes()
        self.assertEqual(persisted["request_id"], request_id)
        self.assertEqual(persisted["status"], "sent")
        self.assertEqual(persisted["text_sha256"], hashlib.sha256(payload).hexdigest())
        self.assertFalse((self.receipts / "2026-10-26.json").exists())
        self.assertEqual(len(calls), 1)
    def test_interrupted_sending_becomes_unknown_without_retry(self):
        self.receipts.mkdir(parents=True)
        (self.receipts / "2026-10-26.json").write_text(json.dumps({"status": "sending", "png_sha256": self.receipt["png_sha256"]}))
        def forbidden(*args, **kwargs):
            self.fail("ambiguous submission must not be retried")
        self.assertEqual(notice.process_notice(self.receipt, config_path=self.config, runner=forbidden)["status"], "unknown")
        self.assertEqual(json.loads((self.receipts / "2026-10-26.json").read_text())["status"], "unknown")


if __name__ == "__main__":
    unittest.main()

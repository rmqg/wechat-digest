import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import hashlib
import subprocess
import unittest
from unittest.mock import patch

MODULE = Path(__file__).with_name("daily-job.py")
spec = importlib.util.spec_from_file_location("daily_job", MODULE)
daily = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily)


TEST_GROUP_ID = "fixture10000000000@chatroom"
TEST_GROUP_NAME = "Synthetic Delivery Fixture"
TEST_PREFIX = "fixture-report"

class DeliveryStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        project = {"group": {"id": TEST_GROUP_ID, "name": TEST_GROUP_NAME}, "report_prefix": TEST_PREFIX,
                   "timezone": "Asia/Shanghai", "daily_hour": 5, "worker_uid": 1234, "worker_gid": 2345}
        config = patch.object(daily.settings, "load_project", return_value=project)
        config.start()
        self.addCleanup(config.stop)
        for name, value in (("GROUP_ID", TEST_GROUP_ID), ("REPORT_PREFIX", TEST_PREFIX)):
            patcher = patch.object(daily, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        ledger = patch.object(daily, "RECEIPTS", Path(self.temporary.name) / "receipts")
        ledger.start()
        self.addCleanup(ledger.stop)
        notice = patch.object(daily, '_process_site_notice', return_value={'status': 'disabled'})
        notice.start()
        self.addCleanup(notice.stop)
    def bundle(self, root, start, end):
        reports = root / "runtime" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        generated = {"editions": {}}
        prepared = {}
        for edition in ("brief", "review"):
            base = reports / (TEST_PREFIX + "-20260303-0500-" + edition)
            png = base.with_suffix(".png")
            png.write_bytes((edition + " report fixture").encode())
            html = base.with_suffix(".html")
            html.write_text("<html>" + edition + "</html>", encoding="utf-8")
            metadata = base.with_suffix(".json")
            metadata.write_text(json.dumps({"edition": edition, "chat_username": TEST_GROUP_ID,
                "window": {"start_unix": start, "end_unix_exclusive": end}}), encoding="utf-8")
            fingerprint = hashlib.sha256(png.read_bytes()).hexdigest()
            generated["editions"][edition] = {"html": str(html), "png": str(png), "json": str(metadata)}
            prepared[edition] = {"status": "prepared", "html": str(html), "png": str(png), "json": str(metadata),
                                 "png_sha256": fingerprint}
        return generated, prepared

    def test_previous_beijing_day_window_across_utc_midnight(self):
        # 2026-01-01 00:30 Beijing is still 2025-12-31 16:30 UTC.
        now = datetime(2025, 12, 31, 16, 30, tzinfo=timezone.utc)
        day, start, end = daily.day_window(now)
        self.assertEqual(day, "2026-01-01")
        self.assertEqual(datetime.fromtimestamp(start, daily.TZ).isoformat(), "2025-12-31T05:00:00+08:00")
        self.assertEqual(datetime.fromtimestamp(end, daily.TZ).isoformat(), "2026-01-01T05:00:00+08:00")

    def test_local_midnight_changes_report_day_exactly_once(self):
        before = datetime(2026, 6, 1, 15, 59, tzinfo=timezone.utc)
        after = datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(daily.day_window(before)[0], "2026-06-01")
        self.assertEqual(daily.day_window(after)[0], "2026-06-02")

    def test_completion_time_does_not_move_the_fixed_twenty_four_hour_window(self):
        ready = daily.day_window(datetime(2026, 3, 4, 5, 0, tzinfo=daily.TZ))
        late = daily.day_window(datetime(2026, 3, 4, 23, 59, tzinfo=daily.TZ))
        self.assertEqual(ready, late)
        self.assertEqual(late[2] - late[1], 24 * 3600)
        self.assertEqual(datetime.fromtimestamp(late[1], daily.TZ).isoformat(), "2026-03-03T05:00:00+08:00")
        self.assertEqual(datetime.fromtimestamp(late[2], daily.TZ).isoformat(), "2026-03-04T05:00:00+08:00")

    def test_sent_day_is_never_generated_or_sent_again(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            state_path.write_text(json.dumps({"report_day": "2026-03-04", "status": "sent"}))
            with patch.object(daily, "STATE", state_path), \
                 patch.object(daily.subprocess, "run", side_effect=AssertionError("must not retry")):
                result = daily.run_tick(datetime(2026, 3, 4, 10, 1, tzinfo=daily.TZ))
            self.assertTrue(result["already_terminal"])

    def test_unknown_day_is_never_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            state_path.write_text(json.dumps({"report_day": "2026-03-04", "status": "unknown"}))
            with patch.object(daily, "STATE", state_path), \
                 patch.object(daily.subprocess, "run", side_effect=AssertionError("must not retry")):
                result = daily.run_tick(datetime(2026, 3, 4, 10, 1, tzinfo=daily.TZ))
            self.assertEqual(result["status"], "unknown")
            self.assertTrue(result["already_terminal"])

    def test_before_five_local_time_does_not_generate(self):
        now = datetime(2026, 3, 3, 20, 59, 59, tzinfo=timezone.utc)  # 04:59:59 Beijing
        with patch.object(daily.subprocess, "run", side_effect=AssertionError("must wait")):
            result = daily.run_tick(now)
        self.assertEqual(result["status"], "waiting")

    def test_prepared_bundle_sends_brief_then_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = root / "state.json"
            _, start, end = daily.day_window(datetime(2026, 3, 4, 5, 0, tzinfo=daily.TZ))
            _, prepared = self.bundle(root, start, end)
            state_path.write_text(json.dumps({"report_day": "2026-03-04", "status": "prepared",
                "editions": prepared, "window": {"start_unix": start, "end_unix_exclusive": end}}))
            order = []
            def sender(command, **kwargs):
                png = Path(command[2])
                edition = png.stem.rsplit("-", 1)[-1]
                order.append(edition)
                fingerprint = prepared[edition]["png_sha256"]
                receipt = {"status": "sent", "confirmed": True, "server_id": "server-" + edition, "sha256": fingerprint}
                return subprocess.CompletedProcess(command, 0, json.dumps(receipt), "")
            with patch.object(daily, "STATE", state_path), patch.object(daily, "ROOT", root), patch.object(daily.subprocess, "run", side_effect=sender):
                result = daily.run_tick(datetime(2026, 3, 4, 5, 0, tzinfo=daily.TZ))
            self.assertEqual(result["status"], "sent")
            self.assertEqual(order, ["brief", "review"])
            final_state = json.loads(state_path.read_text())
            self.assertEqual(final_state["editions"]["brief"]["server_id"], "server-brief")
            self.assertEqual(final_state["editions"]["review"]["server_id"], "server-review")
            brief = json.loads((daily.RECEIPTS / "2026-03-04.json").read_text())
            review = json.loads((daily.RECEIPTS / "2026-03-04-review.json").read_text())
            self.assertEqual(brief["edition"], "brief")
            self.assertEqual(review["edition"], "review")
    def test_prepare_only_generates_both_editions_without_sending(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = root / "state.json"
            now = datetime(2026, 3, 4, 5, 0, tzinfo=daily.TZ)
            _, start, end = daily.day_window(now)
            generated, _ = self.bundle(root, start, end)
            def generate(command, **kwargs):
                self.assertEqual(command[1], str(daily.GENERATOR))
                return subprocess.CompletedProcess(command, 0, json.dumps(generated), "")
            with patch.object(daily, "STATE", state_path), patch.object(daily, "ROOT", root), patch.object(daily.subprocess, "run", side_effect=generate):
                result = daily.run_tick(now, prepare_only=True)
            self.assertEqual(result["status"], "prepared")
            self.assertEqual(set(result["editions"]), {"brief", "review"})
            self.assertEqual(set(json.loads(state_path.read_text())["editions"]), {"brief", "review"})
    def test_review_pre_submit_failure_resumes_only_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = root / "state.json"
            now = datetime(2026, 3, 4, 5, 0, tzinfo=daily.TZ)
            _, start, end = daily.day_window(now)
            generated, prepared = self.bundle(root, start, end)
            sends = []
            notices = []
            def command_runner(command, **kwargs):
                if command[1] == str(daily.GENERATOR):
                    return subprocess.CompletedProcess(command, 0, json.dumps(generated), "")
                edition = Path(command[2]).stem.rsplit("-", 1)[-1]
                sends.append(edition)
                if edition == "review" and sends.count("review") == 1:
                    return subprocess.CompletedProcess(command, 1,
                        json.dumps({"status": "failed", "sent": False, "before_submit": True}), "")
                return subprocess.CompletedProcess(command, 0, json.dumps({"status": "sent", "confirmed": True,
                    "server_id": "server-" + edition, "sha256": prepared[edition]["png_sha256"]}), "")
            with patch.object(daily, "STATE", state_path), patch.object(daily, "ROOT", root), patch.object(daily.subprocess, "run", side_effect=command_runner), patch.object(daily, "_process_site_notice", side_effect=lambda receipt: notices.append(receipt["edition"]) or {"status": "sent"}):
                self.assertEqual(daily.run_tick(now)["status"], "failed")
                self.assertEqual(json.loads(state_path.read_text())["editions"]["brief"]["status"], "sent")
                self.assertEqual(json.loads(state_path.read_text())["editions"]["review"]["status"], "failed")
                self.assertEqual(daily.run_tick(now)["status"], "sent")
            self.assertEqual(sends, ["brief", "review", "review"])
            self.assertEqual(notices, ["brief"])

    def test_unknown_brief_blocks_review_and_is_never_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = root / "state.json"
            now = datetime(2026, 3, 4, 5, 0, tzinfo=daily.TZ)
            _, start, end = daily.day_window(now)
            generated, _ = self.bundle(root, start, end)
            commands = []
            def command_runner(command, **kwargs):
                commands.append(command)
                if command[1] == str(daily.GENERATOR):
                    return subprocess.CompletedProcess(command, 0, json.dumps(generated), "")
                return subprocess.CompletedProcess(command, 1, json.dumps({"status": "unknown"}), "")
            with patch.object(daily, "STATE", state_path), patch.object(daily, "ROOT", root), patch.object(daily.subprocess, "run", side_effect=command_runner):
                self.assertEqual(daily.run_tick(now)["status"], "unknown")
                self.assertEqual(daily.run_tick(now)["status"], "unknown")
            send_commands = [command for command in commands if command[1] == str(daily.SENDER)]
            self.assertEqual(len(send_commands), 1)
            self.assertTrue(send_commands[0][2].endswith("-brief.png"))

    def test_two_persisted_receipts_recover_before_notification_without_resend(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = root / "state.json"
            now = datetime(2026, 3, 4, 5, 0, tzinfo=daily.TZ)
            _, start, end = daily.day_window(now)
            _, prepared = self.bundle(root, start, end)
            for edition in ("brief", "review"):
                prepared[edition]["status"] = "sending"
                daily_path = daily.RECEIPTS / ("2026-03-04" + ("-review" if edition == "review" else "") + ".json")
                daily_path.parent.mkdir(parents=True, exist_ok=True)
                daily_path.write_text(json.dumps({"status": "sent", "edition": edition, "report_day": "2026-03-04",
                    "chat_id": TEST_GROUP_ID, "server_id": "server-" + edition,
                    "png_sha256": prepared[edition]["png_sha256"], "sent_at": 1772571600, "confirmation": {}}))
            state_path.write_text(json.dumps({"report_day": "2026-03-04", "status": "sending", "editions": prepared,
                "window": {"start_unix": start, "end_unix_exclusive": end}}))
            notices = []
            with patch.object(daily, "STATE", state_path), patch.object(daily, "ROOT", root), patch.object(daily.subprocess, "run", side_effect=AssertionError("must not resend")), patch.object(daily, "_process_site_notice", side_effect=lambda receipt: notices.append(receipt["edition"]) or {"status": "sent"}):
                self.assertEqual(daily.run_tick(now)["status"], "sent")
            self.assertEqual(notices, ["brief"])

    def test_invalid_review_artifact_preserves_confirmed_brief_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = root / "state.json"
            now = datetime(2026, 3, 4, 5, 0, tzinfo=daily.TZ)
            _, start, end = daily.day_window(now)
            _, prepared = self.bundle(root, start, end)
            metadata_path = Path(prepared["review"]["json"])
            metadata = json.loads(metadata_path.read_text())
            metadata["edition"] = "brief"
            metadata_path.write_text(json.dumps(metadata))
            state_path.write_text(json.dumps({"report_day": "2026-03-04", "status": "prepared",
                "editions": prepared, "window": {"start_unix": start, "end_unix_exclusive": end}}))
            daily.RECEIPTS.mkdir(parents=True, exist_ok=True)
            (daily.RECEIPTS / "2026-03-04.json").write_text(json.dumps({"status": "sent", "edition": "brief",
                "report_day": "2026-03-04", "chat_id": TEST_GROUP_ID, "server_id": "brief-server",
                "png_sha256": prepared["brief"]["png_sha256"], "sent_at": 1772571600, "confirmation": {}}))
            with patch.object(daily, "STATE", state_path), patch.object(daily, "ROOT", root), patch.object(daily.subprocess, "run", side_effect=AssertionError("integrity failure must not regenerate")):
                result = daily.run_tick(now)
            state = json.loads(state_path.read_text())
            self.assertEqual(result["stage"], "integrity")
            self.assertEqual(state["editions"]["brief"]["status"], "sent")
            self.assertIn("review", state["editions"])

    def test_orphaned_brief_receipt_recovers_bundle_and_sends_review_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = root / "state.json"
            now = datetime(2026, 3, 4, 5, 0, tzinfo=daily.TZ)
            _, start, end = daily.day_window(now)
            _, prepared = self.bundle(root, start, end)
            state_path.write_text(json.dumps({"report_day": "2026-03-04", "status": "sending"}))
            daily.RECEIPTS.mkdir(parents=True, exist_ok=True)
            (daily.RECEIPTS / "2026-03-04.json").write_text(json.dumps({"status": "sent", "edition": "brief",
                "report_day": "2026-03-04", "chat_id": TEST_GROUP_ID, "server_id": "brief-server",
                "png_sha256": prepared["brief"]["png_sha256"], "sent_at": 1772571600, "confirmation": {}}))
            sends = []
            def sender(command, **kwargs):
                sends.append(command[2])
                self.assertEqual(command[1], str(daily.SENDER))
                return subprocess.CompletedProcess(command, 0, json.dumps({"status": "sent", "confirmed": True,
                    "server_id": "review-server", "sha256": prepared["review"]["png_sha256"]}), "")
            with patch.object(daily, "STATE", state_path), patch.object(daily, "ROOT", root), patch.object(daily.subprocess, "run", side_effect=sender):
                result = daily.run_tick(now)
            self.assertEqual(result["status"], "sent")
            self.assertEqual(len(sends), 1)
            self.assertTrue(sends[0].endswith("-review.png"))
    def test_legacy_root_receipt_is_terminal(self):
        receipt_path = daily.RECEIPTS / "2026-03-04.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps({"status": "sent", "report_day": "2026-03-04",
            "chat_id": TEST_GROUP_ID, "server_id": "legacy-server", "png_sha256": "old-hash"}))
        with patch.object(daily.subprocess, "run", side_effect=AssertionError("legacy receipt is terminal")), patch.object(daily, "_process_site_notice") as notice:
            result = daily.run_tick(datetime(2026, 3, 4, 10, 0, tzinfo=daily.TZ))
        notice.assert_not_called()
        self.assertTrue(result["already_terminal"])
    def test_stale_prepared_window_is_not_sent_under_new_schedule(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            state_path.write_text(json.dumps({"report_day": "2026-03-04", "status": "prepared",
                                              "png": str(Path(temp) / "wrong-window.png"), "window": {"start_unix": 1, "end_unix_exclusive": 2}}))
            with patch.object(daily, "STATE", state_path), \
                 patch.object(daily.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "generation intentionally unavailable")):
                result = daily.run_tick(datetime(2026, 3, 4, 10, 0, tzinfo=daily.TZ))
            self.assertEqual(result["stage"], "generate")
            self.assertNotIn("png", json.loads(state_path.read_text()))


if __name__ == "__main__":
    unittest.main()

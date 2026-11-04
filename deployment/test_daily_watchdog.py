import importlib.util
import os
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("daily_watchdog", Path(__file__).with_name("daily-watchdog.py"))
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


class DailyWatchdogTests(unittest.TestCase):
    def test_intermediate_global_sent_does_not_count_as_both_editions_sent(self):
        config = {"timezone": "Asia/Shanghai", "daily_hour": 5, "daily_minute": 0, "overdue_minutes": 60}
        delivery = {"report_day": "2026-03-04", "status": "sent", "editions": {
            "brief": {"status": "sent"}, "review": {"status": "prepared"}}}
        now = datetime(2026, 3, 4, 6, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

        day, status = watchdog.observe(delivery, config, now)

        self.assertEqual(day, "2026-03-04")
        self.assertEqual(status, "overdue")

    def test_recovery_notification_waits_for_both_confirmed_editions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path, state_path, delivery_path = root / "config.json", root / "state.json", root / "delivery.json"
            config_path.write_text(json.dumps({"bark_url": "https://bark.example.invalid/push",
                "group": "Synthetic-Delivery", "fail_level": "active", "recovery_level": "passive",
                "timezone": "Asia/Shanghai", "daily_hour": 5, "daily_minute": 0, "overdue_minutes": 60}),
                encoding="utf-8")
            stamp = datetime(2026, 3, 4, 6, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
            state_path.write_text(json.dumps({"report_day": "2026-03-04", "failure_notified": True,
                                             "recovery_notified": False}), encoding="utf-8")
            delivery = {"report_day": "2026-03-04", "status": "sent", "editions": {
                "brief": {"status": "sent"}, "review": {"status": "prepared"}}}
            delivery_path.write_text(json.dumps(delivery), encoding="utf-8")
            calls = []
            notifier = lambda *args: calls.append(args) or True
            project = {"daily_hour": 5, "timezone": "Asia/Shanghai"}
            with patch.object(watchdog.settings, "load_project", return_value=project):
                result = watchdog.tick(config_path, state_path, delivery_path, now=stamp, notifier=notifier)
                self.assertEqual(result["status"], "overdue")
                self.assertEqual(calls, [])
                delivery["editions"]["review"]["status"] = "sent"
                delivery_path.write_text(json.dumps(delivery), encoding="utf-8")
                result = watchdog.tick(config_path, state_path, delivery_path, now=stamp, notifier=notifier)
            self.assertEqual(result["status"], "sent")
            self.assertEqual(len(calls), 1)
            self.assertIn("已恢复", calls[0][1])

    def test_failure_location_is_persisted_and_reused_for_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path, state_path, delivery_path = root / "config.json", root / "state.json", root / "delivery.json"
            config_path.write_text(json.dumps({"bark_url": "https://bark.example.invalid/push",
                "group": "Synthetic-Delivery", "fail_level": "active", "recovery_level": "passive",
                "timezone": "Asia/Shanghai", "daily_hour": 5, "daily_minute": 0, "overdue_minutes": 60}))
            stamp = datetime(2026, 3, 4, 6, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
            delivery = {"report_day": "2026-03-04", "status": "failed", "edition": "review",
                "editions": {"review": {"status": "failed", "confirmation": {
                    "failure_location": {"file": "deployment/send-report.py", "function": "send_report", "line": 147},
                    "stage": "sending", "error_type": "TransportError"}}}}
            delivery_path.write_text(json.dumps(delivery))
            calls = []
            notifier = lambda *args: calls.append(args) or True
            project = {"daily_hour": 5, "timezone": "Asia/Shanghai"}
            with patch.object(watchdog.settings, "load_project", return_value=project):
                watchdog.tick(config_path, state_path, delivery_path, now=stamp, notifier=notifier)
                self.assertIn("deployment/send-report.py:send_report:147", calls[-1][2])
                delivery.update(status="sent", editions={"brief": {"status": "sent"}, "review": {"status": "sent"}})
                delivery_path.write_text(json.dumps(delivery))
                watchdog.tick(config_path, state_path, delivery_path, now=stamp + 1, notifier=notifier)
            self.assertIn("原故障", calls[-1][2])
            self.assertIn("TransportError", calls[-1][2])

    def test_inferred_edition_stage_dotted_function_and_dynamic_machine(self):
        delivery = {"editions": {"brief": {"status": "failed", "confirmation": {
            "failure_location": {"file": "deployment/daily-job.py", "function": "X11.find_window", "line": 91},
            "stage": "source_validation", "error_type": "LookupError"}}}}
        with patch.dict(os.environ, {"WECHAT_DIGEST_MACHINE": "fixture-host"}):
            body = watchdog._diagnostic(delivery, "failed", "2026-03-04")
        self.assertIn("机器fixture-host", body)
        self.assertIn("brief", body)
        self.assertIn("阶段来源校验", body)
        delivery["editions"]["brief"]["confirmation"]["stage"] = "future_stage"
        with patch.dict(os.environ, {"WECHAT_DIGEST_MACHINE": "fixture-host"}):
            body = watchdog._diagnostic(delivery, "failed", "2026-03-04")
        self.assertIn("阶段future_stage", body)

    def test_old_failure_notified_state_gains_context_without_realert(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path, state_path, delivery_path = root / "config.json", root / "state.json", root / "delivery.json"
            config_path.write_text(json.dumps({"bark_url": "https://bark.example.invalid/push",
                "group": "Synthetic-Delivery", "fail_level": "active", "recovery_level": "passive",
                "timezone": "Asia/Shanghai", "daily_hour": 5, "daily_minute": 0, "overdue_minutes": 60}))
            state_path.write_text(json.dumps({"report_day": "2026-03-04", "failure_notified": True, "failure_context": None}))
            delivery_path.write_text(json.dumps({"report_day": "2026-03-04", "status": "failed"}))
            calls = []
            with patch.object(watchdog.settings, "load_project", return_value={"daily_hour": 5, "timezone": "Asia/Shanghai"}):
                result = watchdog.tick(config_path, state_path, delivery_path,
                    now=datetime(2026, 3, 4, 6, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp(),
                    notifier=lambda *args: calls.append(args) or True)
            self.assertFalse(result["notified"])
            self.assertEqual(calls, [])
            self.assertIn("failure_context", json.loads(state_path.read_text()))


if __name__ == "__main__":
    unittest.main()

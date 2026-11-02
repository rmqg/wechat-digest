import importlib.util
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


if __name__ == "__main__":
    unittest.main()

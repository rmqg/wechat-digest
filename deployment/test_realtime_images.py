import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("realtime-images.py")
SPEC = importlib.util.spec_from_file_location("realtime_images", SCRIPT)
realtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(realtime)


class RealtimeImageWorkerTests(unittest.TestCase):
    def test_window_starts_at_0500_yesterday_in_shanghai(self):
        now = int(datetime(2026, 10, 26, 10, 0, tzinfo=realtime.TZ).timestamp())
        start, end = realtime._window(now)
        self.assertEqual(datetime.fromtimestamp(start, realtime.TZ),
                         datetime(2026, 10, 25, 5, 0, tzinfo=realtime.TZ))
        self.assertEqual(end, now + 1)

    def test_window_uses_configured_daily_hour(self):
        with patch.object(realtime.settings, 'load_project', return_value={'daily_hour': 7}):
            now = int(datetime(2026, 10, 26, 10, 0, tzinfo=realtime.TZ).timestamp())
            start, _ = realtime._window(now)
        self.assertEqual(datetime.fromtimestamp(start, realtime.TZ),
                         datetime(2026, 10, 25, 7, 0, tzinfo=realtime.TZ))

    def test_unchanged_idle_state_does_not_start_pipeline(self):
        now = 1_800_000_000
        state = {"source_fingerprint": "same", "last_scan": now,
                 "pending_start": None, "next_attempt_at": now + 3600}
        with patch.object(realtime.time, "time", return_value=now), \
             patch.object(realtime, "_fingerprint", return_value="same"), \
             patch.dict(sys.modules, {"image_pipeline": types.SimpleNamespace(
                 process_window=unittest.mock.Mock(side_effect=AssertionError("pipeline called")))}):
            result, attempted = realtime.process_once(state)
        self.assertFalse(attempted)
        self.assertIs(result, state)

    def test_failed_result_without_pending_start_preserves_earliest_failed_range(self):
        now = 1_800_000_000
        failed_start = now - 200_000
        pipeline = types.SimpleNamespace(process_window=lambda *a, **k: {
            "status": "failed", "pending_start": None, "counts": {"failed": 1}})
        state = {"source_fingerprint": "before", "pending_start": failed_start,
                 "next_attempt_at": now - 1}
        with patch.object(realtime.time, "time", return_value=now), \
             patch.object(realtime, "_fingerprint", return_value="after"), \
             patch.object(realtime, "_save_state"), \
             patch.dict(sys.modules, {"image_pipeline": pipeline}):
            result, attempted = realtime.process_once(state)
        self.assertTrue(attempted)
        self.assertEqual(result["pending_start"], failed_start)
        self.assertEqual(result["last_status"], "failed")

    def test_periodic_rescan_can_discover_late_images_without_mtime_change(self):
        now = 1_800_000_000
        state = {"source_fingerprint": "same", "last_scan": now - realtime.RECHECK_SECONDS,
                 "pending_start": None, "next_attempt_at": now + 3600}
        pipeline = types.SimpleNamespace(process_window=lambda *a, **k: {
            "status": "pending", "pending_start": now - 30, "counts": {"pending": 1}, "next_attempt_at": now + 15})
        with patch.object(realtime.time, "time", return_value=now), \
             patch.object(realtime, "_fingerprint", return_value="same"), \
             patch.object(realtime, "_save_state"), \
             patch.dict(sys.modules, {"image_pipeline": pipeline}):
            result, attempted = realtime.process_once(state)
        self.assertTrue(attempted)
        self.assertEqual(result["pending_start"], now - 30)
        self.assertEqual(result["last_status"], "pending")


if __name__ == "__main__":
    unittest.main()

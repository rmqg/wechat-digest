import importlib.util
import json
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from pathlib import Path

from unittest.mock import patch

TEST_GROUP_ID = "fixture10000000000@chatroom"
TEST_GROUP_NAME = "Synthetic Watchdog Fixture"

def local_bark_server(http_status: int, response_body: bytes):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append({"method": self.command, "path": self.path,
                             "content_type": self.headers.get("Content-Type"),
                             "body": self.rfile.read(int(self.headers.get("Content-Length", "0")))})
            self.send_response(http_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, received


SCRIPT = Path(__file__).with_name("wechat-watchdog.py")
_spec = importlib.util.spec_from_file_location("wechat_watchdog", SCRIPT)
watchdog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watchdog)


class WatchdogTests(unittest.TestCase):

    def setUp(self):
        project = {"group": {"id": TEST_GROUP_ID, "name": TEST_GROUP_NAME},
                   "timezone": "Asia/Shanghai", "daily_hour": 5,
                   "worker_uid": 1234, "worker_gid": 2345}
        self.project_patch = patch.object(watchdog.settings, "load_project", return_value=project)
        self.project_patch.start()
        self.addCleanup(self.project_patch.stop)
    def _notify_with_http_response(self, http_status, body):
        server, thread, requests = local_bark_server(http_status, body)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        config = {"bark_url": f"http://127.0.0.1:{server.server_address[1]}/push",
                  "group": "synthetic-group", "fail_level": "active"}
        result = watchdog._notify(config, "微信状态", "无法确认在线", "active")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual((request["method"], request["path"]), ("POST", "/push"))
        self.assertEqual(request["content_type"], "application/json")
        return result, json.loads(request["body"])

    def test_bark_post_succeeds_only_for_http_success_and_code_200(self):
        result, payload = self._notify_with_http_response(200, b'{"code":200}')
        self.assertTrue(result)
        self.assertEqual(payload, {"title": "微信状态", "body": "无法确认在线",
                                   "group": "synthetic-group", "level": "active"})

    def test_bark_post_rejects_http_error_and_non_200_json_code(self):
        result, _ = self._notify_with_http_response(503, b'{"code":200}')
        self.assertFalse(result)
        result, _ = self._notify_with_http_response(200, b'{"code":500}')
        self.assertFalse(result)
        result, _ = self._notify_with_http_response(200, b'not-json')
        self.assertFalse(result)

    def test_one_failure_per_outage_and_new_failure_after_recovery(self):
        state = {}
        watchdog.transition(state, "offline", 100)
        self.assertEqual(state["pending"], "failure")
        config = {"fail_level": "active", "recovery_level": "passive"}
        self.assertTrue(watchdog._send_pending(state, config, lambda *_: True))
        self.assertTrue(state["failure_notified"])
        watchdog.transition(state, "unavailable", 100000)
        self.assertIsNone(state["pending"])
        watchdog.transition(state, "offline", 100001)
        self.assertIsNone(state["pending"])
        watchdog.transition(state, "online", 100002)
        self.assertEqual(state["pending"], "recovery")
        self.assertTrue(watchdog._send_pending(state, config, lambda *_: True))
        self.assertFalse(state["failure_notified"])
        watchdog.transition(state, "offline", 100003)
        self.assertEqual(state["pending"], "failure")

    def test_recovery_is_suppressed_without_delivered_failure(self):
        state = {"status": "offline", "pending": "failure"}
        watchdog.transition(state, "online", 101)
        self.assertIsNone(state["pending"])

    def test_failed_notice_retries_without_marking_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.json"
            state_path = root / "runtime" / "state.json"
            config.write_text(json.dumps({"bark_url": "https://notify.example.test/token", "group": "synthetic-group",
                                          "fail_level": "active", "recovery_level": "passive"}))
            calls = []

            def notify(*args):
                calls.append(args)
                return len(calls) > 1

            result = watchdog.tick(config, state_path, lambda: "offline", notify, now=10)
            self.assertEqual(result["status"], "offline")
            pending = json.loads(state_path.read_text())
            self.assertEqual(pending["pending"], "failure")
            self.assertEqual(pending["checked_at"], 10)
            self.assertFalse(pending.get("failure_notified", False))
            watchdog.tick(config, state_path, lambda: "offline", notify, now=11)
            delivered = json.loads(state_path.read_text())
            self.assertIsNone(delivered["pending"])
            self.assertTrue(delivered["failure_notified"])
            self.assertNotIn("last_failure_sent_at", delivered)
            self.assertEqual(len(calls), 2)

    def test_main_login_and_unavailable_window_judgment_ignores_position(self):
        main = {"title": "Weixin", "viewable": True, "width": 917, "height": 667,
                "x": 10000, "y": -100}
        self.assertEqual(watchdog.classify_windows([main], (917, 667), (292, 396)), "online")
        self.assertEqual(watchdog.classify_windows(
            [{"title": "Weixin", "viewable": True, "width": 292, "height": 396}],
            (917, 667), (292, 396)), "offline")
        self.assertEqual(watchdog.classify_windows(
            [{"title": "Other app", "viewable": True, "width": 917, "height": 667}],
            (917, 667), (292, 396)), "unavailable")
        self.assertEqual(watchdog.classify_windows(
            [{"title": "Weixin", "viewable": True, "width": 917, "height": 667},
             {"title": "Weixin", "viewable": True, "width": 292, "height": 396}],
            (917, 667), (292, 396)), "offline")
        self.assertEqual(watchdog.classify_windows(
            [{"title": "Weixin", "viewable": False, "width": 917, "height": 667}],
            (917, 667), (292, 396)), "unavailable")


if __name__ == "__main__":
    unittest.main()

import base64
import importlib.util
import json
from http.client import HTTPConnection
import socket
from pathlib import Path
import tempfile
import threading
import unittest

spec = importlib.util.spec_from_file_location("qq_report_site", Path(__file__).with_name("qq_report_site.py"))
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


class _UnixHTTPConnection(HTTPConnection):
    def __init__(self, path):
        self.socket_path = str(path)
        super().__init__("localhost")

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)





class QQReportSiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.a_dir, self.b_dir = base / "a", base / "b"
        self.a_dir.mkdir()
        self.b_dir.mkdir()
        self._report(self.a_dir, "qq-10001-20261102-1200-brief", "qq:10001", "brief")
        self._report(self.a_dir, "qq-10001-20261102-1200-review", "qq:10001", "review")
        self._report(self.b_dir, "qq-20002-20261102-1200-review", "qq:20002", "review")
        self._report(self.b_dir, "qq-20002-20261102-1200-brief", "qq:20002", "brief")
        (self.a_dir / "assets").mkdir()
        (self.a_dir / "assets" / "font.woff2").write_bytes(b"font")
        (self.a_dir / "private.json").write_text('{"private":true}', encoding="utf-8")
        self.groups = {
            "10001": {"name": "群A", "username": "user-a", "password_scrypt": site.hash_password("password-a"), "reports_dir": self.a_dir},
            "20002": {"name": "群B", "username": "user-b", "password_scrypt": site.hash_password("password-b"), "reports_dir": self.b_dir},
        }
        config = {"bind": "127.0.0.1", "port": 0, "groups": self.groups}
        self.server = site.ThreadingHTTPServer(("127.0.0.1", 0), site.make_handler(config))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.port = self.server.server_address[1]

    @staticmethod
    def _report(directory, stem, chat, edition):
        metadata = {"chat_username": chat, "edition": edition, "range_text": "2026-11-01 00:00–01:00",
                    "window": {"start": "00:00", "end": "01:00"}, "stats": {"messages": 3}}
        (directory / f"{stem}.json").write_text(json.dumps(metadata), encoding="utf-8")
        (directory / f"{stem}.html").write_text("<html><body>report text</body></html>", encoding="utf-8")
        (directory / f"{stem}.png").write_bytes(b"PNG fixture")

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _get(self, path, account=None):
        headers = {}
        if account:
            token = base64.b64encode(f"{account[0]}:{account[1]}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        conn = HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_password_hash_is_salted_and_verifies(self):
        one, two = site.hash_password("correct horse"), site.hash_password("correct horse")
        self.assertNotEqual(one, two)
        self.assertTrue(site.verify_password("correct horse", one))
        self.assertFalse(site.verify_password("wrong", one))

    def test_config_rejects_duplicate_and_nested_report_roots(self):
        nested = self.a_dir / "nested"
        nested.mkdir()
        password_hash = site.hash_password("unused-test-password")
        config_path = Path(self.temp.name) / "site.json"
        for reports_b in (self.a_dir, nested):
            with self.subTest(reports_b=reports_b):
                config = {
                    "bind": "127.0.0.1", "port": 8118,
                    "groups": {
                        "10001": {"name": "A", "reports_dir": str(self.a_dir), "username": "a",
                                  "password_scrypt": password_hash},
                        "20002": {"name": "B", "reports_dir": str(reports_b), "username": "b",
                                  "password_scrypt": password_hash},
                    },
                }
                config_path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(ValueError):
                    site.load_config(config_path)

    def test_auth_challenge_is_renderable_and_head_preserves_headers(self):
        for path, realm in (("/", "QQ reports"), ("/qq/10001/", "10001")):
            with self.subTest(path=path):
                status, headers, body = self._get(path)
                self.assertEqual(status, 401)
                self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
                self.assertEqual(headers["WWW-Authenticate"], f'Basic realm="{realm}", charset="UTF-8"')
                self.assertEqual(int(headers["Content-Length"]), len(body))
                self.assertTrue(body.startswith(b"<!doctype html>"))
                self.assertIn(b"<title>", body)
                self.assertNotIn(b"report text", body)
                connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
                try:
                    connection.request("HEAD", path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 401)
                    self.assertEqual(response.getheader("Content-Type"), headers["Content-Type"])
                    self.assertEqual(response.getheader("Content-Length"), headers["Content-Length"])
                    self.assertEqual(response.read(), b"")
                finally:
                    connection.close()

    def test_root_auth_redirects_only_to_matching_group(self):
        status, headers, _ = self._get("/", ("user-a", "password-a"))
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/qq/10001/")
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        self.assertEqual(self._get("/")[0], 401)

    def test_group_auth_is_isolated_and_reports_are_private(self):
        a = ("user-a", "password-a")
        status, headers, body = self._get("/qq/10001/", a)
        self.assertEqual(status, 200)
        self.assertIn("qq-10001-20261102-1200-brief.html", body.decode())
        self.assertNotIn("private.json", body.decode())
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        stem = "qq-10001-20261102-1200-brief"
        status, _, report = self._get(f"/qq/10001/{stem}.html", a)
        self.assertEqual((status, report), (200, b"<html><body>report text</body></html>"))
        self.assertEqual(self._get(f"/qq/10001/{stem}.png", a)[0], 200)
        self.assertEqual(self._get(f"/qq/10001/{stem}.json", a)[0], 404)
        self.assertEqual(self._get("/qq/10001/private.json", a)[0], 404)
        self.assertEqual(self._get("/qq/20002/qq-10001-20261102-1200-review.html", a)[0], 401)
        self.assertEqual(self._get("/qq/10001/")[0], 401)

    def test_path_traversal_and_symlink_escape_are_denied(self):
        account = ("user-a", "password-a")
        outside = Path(self.temp.name) / "secret.png"
        outside.write_bytes(b"secret")
        (self.a_dir / "assets" / "escape.png").symlink_to(outside)
        for path in ("/qq/10001/../20002/day-review.html", "/qq/10001/%2e%2e/secret.png",
                     "/qq/10001/assets/escape.png", "/qq/10001/assets/../../secret.png"):
            with self.subTest(path=path):
                self.assertIn(self._get(path, account)[0], (404, 400))
        status, _, font = self._get("/qq/10001/assets/font.woff2", account)
        self.assertEqual((status, font), (200, b"font"))

    def test_index_rejects_single_cross_window_and_wrong_group_reports(self):
        base = "qq-10001-20261102-1300"
        self._report(self.a_dir, base + "-brief", "qq:10001", "brief")
        self._report(self.a_dir, base + "-review", "qq:10001", "review")
        review_json = self.a_dir / f"{base}-review.json"
        review = json.loads(review_json.read_text(encoding="utf-8"))
        review["range_text"] = "different statistical window"
        review["window"] = {"start": "02:00", "end": "03:00"}
        review_json.write_text(json.dumps(review), encoding="utf-8")

        self._report(self.a_dir, "qq-10001-20261102-1400-brief", "qq:10001", "brief")
        wrong_base = "qq-20002-20261102-1500"
        self._report(self.a_dir, wrong_base + "-brief", "qq:20002", "brief")
        self._report(self.a_dir, wrong_base + "-review", "qq:20002", "review")
        zero_base = "qq-10001-20261102-1600"
        self._report(self.a_dir, zero_base + "-brief", "qq:10001", "brief")
        self._report(self.a_dir, zero_base + "-review", "qq:10001", "review")
        for edition in ("brief", "review"):
            metadata_path = self.a_dir / f"{zero_base}-{edition}.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["stats"]["messages"] = 0
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        _, _, body = self._get("/qq/10001/", ("user-a", "password-a"))
        content = body.decode()
        self.assertIn("qq-10001-20261102-1200-brief.html", content)
        self.assertIn("qq-10001-20261102-1200-review.html", content)
        self.assertNotIn(base, content)
        self.assertNotIn("qq-10001-20261102-1400-brief.html", content)
        self.assertNotIn(wrong_base, content)
        self.assertNotIn(zero_base, content)

    def test_unix_socket_serves_authenticated_requests_and_cleans_up(self):
        path = Path(self.temp.name) / "qq-private.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        stale.close()
        config = {"bind": "127.0.0.1", "port": 0, "socket_path": str(path), "groups": self.groups}
        server = site.create_server(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(path.stat().st_mode & 0o777, 0o660)
            token = base64.b64encode(b"user-a:password-a").decode()
            connection = _UnixHTTPConnection(path)
            connection.request("GET", "/qq/10001/", headers={"Authorization": f"Basic {token}"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("qq-10001-20261102-1200-brief.html", response.read().decode())
            connection.close()

            connection = _UnixHTTPConnection(path)
            connection.request("GET", "/qq/20002/", headers={"Authorization": f"Basic {token}"})
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            connection.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            site.close_server(server)
        self.assertFalse(path.exists())

    def test_unix_socket_path_refuses_symlinks_and_regular_files(self):
        path = Path(self.temp.name) / "occupied"
        config = {"bind": "127.0.0.1", "port": 0, "socket_path": str(path), "groups": self.groups}
        path.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            site.create_server(config)
        self.assertEqual(path.read_text(encoding="utf-8"), "keep")
        target = Path(self.temp.name) / "target"
        target.write_text("keep", encoding="utf-8")
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(FileExistsError):
            site.create_server(config)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()

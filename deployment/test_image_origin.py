import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


SCRIPT = Path(__file__).with_name("image-origin.py")
spec = importlib.util.spec_from_file_location("image_origin", SCRIPT)
image_origin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_origin)


class ImageOriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.assets = cls.root / "assets"
        cls.assets.mkdir()
        cls.name = "a" * 64 + ".png"
        cls.content = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
        (cls.assets / cls.name).write_bytes(cls.content)
        cls.config = {
            "assets_dir": str(cls.assets), "token": "test-only-secret",
            "bind": "127.0.0.1", "port": 0,
        }
        cls.server = image_origin.LoopbackThreadingHTTPServer(("127.0.0.1", 0), image_origin.ImageOriginHandler)
        cls.server.origin_config = cls.config
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def request(self, method="GET", name=None, token="test-only-secret", extra_headers=None):
        path = name if name is not None else self.name
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        headers.update(extra_headers or {})
        request = urllib.request.Request(f"{self.url}/assets/{path}", method=method, headers=headers)
        try:
            return urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            return error

    def test_get_head_and_content_headers(self):
        response = self.request()
        with response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), self.content)
            self.assertEqual(response.headers["Content-Length"], str(len(self.content)))
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        response = self.request("HEAD")
        with response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            self.assertEqual(response.headers["Content-Length"], str(len(self.content)))
            self.assertEqual(response.headers["Content-Type"], "image/png")

    def test_auth_and_forwarded_headers_are_not_trusted(self):
        self.assertEqual(self.request(token="wrong").status, 403)
        self.assertEqual(self.request(token=None).status, 403)
        response = self.request(extra_headers={
            "Forwarded": "for=203.0.113.20", "X-Forwarded-For": "203.0.113.20",
        })
        with response:
            self.assertEqual(response.status, 200)

    def test_methods_reject_non_get_and_head(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"):
            with self.subTest(method=method):
                response = self.request(method)
                self.assertEqual(response.status, 405)
                self.assertEqual(response.headers["Allow"], "GET, HEAD")

    def test_single_byte_ranges_and_unsatisfiable_range(self):
        response = self.request(extra_headers={"Range": "bytes=2-6"})
        with response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), self.content[2:7])
            self.assertEqual(response.headers["Content-Range"], f"bytes 2-6/{len(self.content)}")
            self.assertEqual(response.headers["Content-Length"], "5")
        response = self.request(extra_headers={"Range": "bytes=-4"})
        with response:
            self.assertEqual(response.read(), self.content[-4:])
        response = self.request(extra_headers={"Range": "bytes=0-1,4-5"})
        self.assertEqual(response.status, 416)
        self.assertEqual(response.headers["Content-Range"], f"bytes */{len(self.content)}")

    def test_rejects_paths_non_images_directories_and_symlinks(self):
        outside = self.root / "outside.png"
        outside.write_bytes(b"secret")
        (self.assets / ("b" * 64 + ".png")).symlink_to(outside)
        (self.assets / ("c" * 64 + ".png")).mkdir()
        for suffix in ("txt", "html", "woff", "woff2", "ttf", "otf"):
            (self.assets / ("f" * 64 + "." + suffix)).write_bytes(b"not image")
        for path in (
            "../outside.png", "%2e%2e%2foutside.png", "d" * 64 + ".txt",
            "e" * 64 + ".png/extra", "f" * 64 + ".html", "f" * 64 + ".woff",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request(name=path).status, 404)
        self.assertEqual(self.request(name="b" * 64 + ".png").status, 404)
        self.assertEqual(self.request(name="c" * 64 + ".png").status, 404)

    def test_config_validation(self):
        config_file = self.root / "runtime" / "image-origin.json"
        config_file.parent.mkdir()
        config_file.write_text(json.dumps({"assets_dir": str(self.assets), "token": "temporary"}), encoding="utf-8")
        loaded = image_origin.check_config(config_file)
        self.assertEqual(loaded["port"], 8099)
        self.assertEqual(loaded["bind"], "127.0.0.1")
        for bind in ("::", "0.0.0.0", "127.0.0.2"):
            with self.subTest(bind=bind):
                config_file.write_text(json.dumps({
                    "assets_dir": str(self.assets), "token": "temporary", "bind": bind,
                }), encoding="utf-8")
                with self.assertRaises(image_origin.ConfigError):
                    image_origin.check_config(config_file)


if __name__ == "__main__":
    unittest.main()

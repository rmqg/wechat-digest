import sys
import types
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_context


class WebContextSafetyTests(unittest.TestCase):
    def test_url_validation_rejects_private_metadata_and_credentials(self):
        for url in (
            "http://127.0.0.1/",
            "https://10.0.0.1/private",
            "http://[::1]/",
            "http://169.254.169.254/latest/meta-data/",
            "https://localhost/",
            "https://example.com/?access_token=secret",
            "https://user:pass@example.com/",
            "https://example.com/path/api_key=secret",
        ):
            with self.subTest(url=url), self.assertRaises(web_context.UnsafeURL):
                web_context._canonical_url(url)

    def test_dns_resolution_rejects_any_non_public_answer(self):
        with patch.object(socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 443)),
        ]):
            with self.assertRaises(web_context.UnsafeURL):
                web_context._public_addresses("public.example", 443)

    def test_database_context_uses_configured_group_identity(self):
        group = {"group_id": "synthetic-group-id", "name": "Synthetic test group"}
        fake_digest = types.SimpleNamespace(
            _load_keyfile=lambda _path: {},
            _groups_for_source=lambda *_args: ([group], {}, None, None),
            select_group=unittest.mock.Mock(return_value=group),
        )
        project = {"group": {"id": group["group_id"], "name": group["name"]}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "message").mkdir()
            with patch.dict(sys.modules, {"digest": fake_digest}), \
                    patch.object(web_context.settings, "load_project", return_value=project):
                self.assertEqual(web_context._database_messages(root, Path("synthetic-keys.json"), 0, 1, root), [])
        fake_digest.select_group.assert_called_once_with([group], group["name"], group["group_id"])

    def test_html_extraction_excludes_active_and_navigation_content(self):
        title, text = web_context._extract(
            {"content-type": "text/html; charset=utf-8"},
            b"<title>Title</title><nav>nav-secret</nav><script>evil()</script>"
            b"<main><article><h1>Heading</h1><p>body text</p></article></main>",
        )
        self.assertEqual(title, "Title")
        self.assertIn("Heading", text)
        self.assertIn("body text", text)
        self.assertNotIn("nav-secret", text)
        self.assertNotIn("evil", text)

    def test_page_limit_marks_unfetched_links_pending_without_network(self):
        messages = [{
            "message_uid": "uid-1",
            "timestamp": 1,
            "text": "http://127.0.0.1/a https://[::1]/b",
        }]
        with tempfile.TemporaryDirectory() as directory:
            result = web_context.collect(messages, Path(directory), max_pages=1)
        entry = result["uid-1"]
        self.assertEqual(entry["source_kind"], "web")
        self.assertEqual(len(entry["pages"]), 2)
        self.assertEqual(entry["pages"][0]["status"], "unavailable")
        self.assertEqual(entry["pages"][1]["status"], "pending")
        self.assertFalse(entry["pages"][0]["text_complete"])
        self.assertEqual(entry["status"], "pending")



    def test_oversized_page_is_unavailable_not_partial_content(self):
        with patch.object(web_context, '_fetch', return_value=(200, {'content-type': 'text/plain'},
                b'x' * (web_context.MAX_BYTES + 1), 'https://public.example/article')):
            page = web_context._fetch_page('https://public.example/article')
        self.assertEqual(page['status'], 'unavailable')
        self.assertEqual(page['reason'], 'too_large')
        self.assertFalse(page['text_complete'])
        self.assertEqual(page['text'], '')

    def test_javascript_shell_title_is_not_article_body(self):
        with patch.object(web_context, '_fetch', return_value=(200, {'content-type': 'text/html'},
                b'<head><title>Dashboard</title></head><body><div id="root"></div><script src="app.js"></script></body>',
                'https://public.example/app')):
            page = web_context._fetch_page('https://public.example/app')
        self.assertEqual(page['status'], 'unavailable')
        self.assertFalse(page['text_complete'])

    def test_access_gate_pages_are_detected(self):
        self.assertTrue(web_context._looks_like_access_gate("https://public.example/login", "", "", b""))
        self.assertTrue(web_context._looks_like_access_gate("https://public.example/article", "", "Verify you are human", b""))
        self.assertTrue(web_context._looks_like_access_gate('https://public.example/view', 'Favorites',
            'Unable to view details of Favorites This function requires WeChat 5.3 or later.', b''))

    def test_cached_pages_do_not_consume_fetch_batch_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            cached_url = "https://public.example/article"
            item = {"url": cached_url, "title": "Cached", "text": "complete", "text_complete": True,
                    "fetched_at": web_context.dt.datetime.now(web_context.dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                    'content_sha256': 'cached-hash', 'status': 'success', 'extraction_version': 2}
            web_context._cache_write(web_context._cache_path(cache, cached_url), item)
            messages = [{"message_uid": "uid-cache", "text": cached_url + " http://127.0.0.1/new"}]
            result = web_context.collect(messages, cache, max_pages=1)
        self.assertEqual([page["status"] for page in result["uid-cache"]["pages"]], ["success", "unavailable"])

if __name__ == "__main__":
    unittest.main()

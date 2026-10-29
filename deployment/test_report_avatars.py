import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from PIL import Image

MODULE_PATH = Path(__file__).with_name("report_avatars.py")
spec = importlib.util.spec_from_file_location("report_avatars", MODULE_PATH)
report_avatars = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report_avatars)


def png(size=(300, 200), color=(30, 80, 120, 255)):
    output = io.BytesIO()
    Image.new("RGBA", size, color).save(output, format="PNG")
    return output.getvalue()


class ReportAvatarTests(unittest.TestCase):
    def source_tree(self, root, user_ids, images=None, urls=None):
        source = Path(root) / "source"
        source.mkdir()
        contact = source / "contact.db"
        with sqlite3.connect(contact) as conn:
            conn.execute("CREATE TABLE contact(username TEXT, big_head_url TEXT, small_head_url TEXT, head_img_md5 TEXT)")
            for username in user_ids:
                big, small = (urls or {}).get(username, ("", ""))
                conn.execute("INSERT INTO contact VALUES(?,?,?,?)", (username, big, small, "md5-" + username))
        head = source / "head_image.db"
        with sqlite3.connect(head) as conn:
            conn.execute("CREATE TABLE head_image(username TEXT, image_buffer BLOB)")
            for username, data in (images or {}).items():
                conn.execute("INSERT INTO head_image VALUES(?,?)", (username, data))
        return source

    def test_local_images_are_normalized_and_only_requested_identities_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.source_tree(tmp, ["group@chatroom", "wxid_a", "other"],
                                      {"group@chatroom": png(), "wxid_a": b"not an image"})
            result = report_avatars.collect_avatars(source, {}, ["group@chatroom", "wxid_a"], Path(tmp) / "runtime", decrypted_dir=source)
        self.assertEqual(set(result), {"group@chatroom", "wxid_a"})
        self.assertTrue(result["group@chatroom"]["available"])
        self.assertEqual(result["group@chatroom"]["source"], "head_image")
        decoded = report_avatars.base64.b64decode(result["group@chatroom"]["data_uri"].split(",", 1)[1])
        with Image.open(io.BytesIO(decoded)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (256, 171))
        self.assertFalse(result["wxid_a"]["available"])
        self.assertEqual(result["wxid_a"]["reason"], "avatar_unavailable")

    def test_validated_avatar_url_is_fallback_and_cache_tracks_contact(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.source_tree(tmp, ["wxid_a"], urls={"wxid_a": ("https://wx.qlogo.cn/a", "")})
            runtime = Path(tmp) / "runtime"
            with mock.patch.object(report_avatars, "_download", return_value=png()) as fetch:
                result = report_avatars.collect_avatars(source, {}, ["wxid_a"], runtime, decrypted_dir=source)
                again = report_avatars.collect_avatars(source, {}, ["wxid_a"], runtime, decrypted_dir=source)
            self.assertTrue(result["wxid_a"]["available"])
            self.assertEqual(result["wxid_a"]["source"], "big_head_url")
            self.assertTrue(again["wxid_a"]["available"])
            fetch.assert_called_once()
            avatar_dir = runtime / "avatars"
            self.assertEqual(avatar_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual((avatar_dir / (report_avatars.hashlib.sha256(b"wxid_a").hexdigest() + ".png")).stat().st_mode & 0o777, 0o600)
            meta = json.loads(next(avatar_dir.glob("*.json")).read_text())
            self.assertNotIn("qlogo", json.dumps(meta))

    def test_default_decryption_uses_and_removes_temporary_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.source_tree(tmp, ["wxid_a"], {"wxid_a": png()})
            runtime = Path(tmp) / "runtime"
            decrypt_dirs = []

            def decrypt(path, root, keys, out_dir):
                decrypt_dirs.append(Path(out_dir))
                return path

            with mock.patch.object(report_avatars.digest, "_decrypt", side_effect=decrypt):
                result = report_avatars.collect_avatars(source, {}, ["wxid_a"], runtime)
            self.assertTrue(result["wxid_a"]["available"])
            self.assertTrue(decrypt_dirs)
            self.assertTrue(all(not path.exists() for path in decrypt_dirs))
            self.assertEqual(list(runtime.glob(".avatar-decrypted-*")), [])


    def test_arbitrary_hosts_redirects_and_private_dns_are_rejected(self):
        for url in ("http://wx.qlogo.cn/avatar", "https://evil.example/avatar", "https://wx.qlogo.cn.evil.example/avatar", "https://user@wx.qlogo.cn/avatar"):
            with self.subTest(url=url):
                self.assertIsNone(report_avatars._download(url))

    def test_beijing_day_refreshes_avatar_and_failure_does_not_claim_freshness(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.source_tree(tmp, ["wxid_a"], {"wxid_a": png(color=(0, 255, 0, 255))},
                                      {"wxid_a": ("https://wx.qlogo.cn/avatar", "")})
            runtime = Path(tmp) / "runtime"
            before_midnight = datetime(2026, 10, 25, 15, 59, tzinfo=timezone.utc)
            after_midnight = datetime(2026, 10, 25, 16, 0, tzinfo=timezone.utc)
            following_day = datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)
            with mock.patch.object(report_avatars, "_download", side_effect=[png(color=(255, 0, 0, 255)), png(color=(0, 0, 255, 255)), None]):
                first = report_avatars.collect_avatars(source, {}, ["wxid_a"], runtime, source, now=before_midnight)["wxid_a"]
                same = report_avatars.collect_avatars(source, {}, ["wxid_a"], runtime, source, now=before_midnight)["wxid_a"]
                next_day = report_avatars.collect_avatars(source, {}, ["wxid_a"], runtime, source, now=after_midnight)["wxid_a"]
                failed = report_avatars.collect_avatars(source, {}, ["wxid_a"], runtime, source, now=following_day)["wxid_a"]
            self.assertEqual(first["data_uri"], same["data_uri"])
            self.assertNotEqual(first["data_uri"], next_day["data_uri"])
            self.assertEqual(next_day["refreshed_day"], "2026-10-26")
            self.assertTrue(next_day["refreshed_today"])
            self.assertEqual(failed["data_uri"], next_day["data_uri"])
            self.assertFalse(failed["refreshed_today"])
            self.assertEqual(failed["refreshed_day"], "2026-10-26")
            self.assertEqual(failed["refresh_error"], "avatar_download_failed")


if __name__ == "__main__":
    unittest.main()

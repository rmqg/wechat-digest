import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from memory import decrypt_sync

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "synthetic"


class SyntheticFixtureTests(unittest.TestCase):
    def test_qq_demo_is_fictional_renderer_only_output(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("generate_demo", ROOT / "scripts" / "generate-demo.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            module.generate_qq_demo(output)
            brief = json.loads((output / "qq-brief.json").read_text(encoding="utf-8"))
            review = json.loads((output / "qq-review.json").read_text(encoding="utf-8"))
            self.assertEqual(brief["chat_username"], "qq:1000000001")
            self.assertEqual(brief["title_prefix"], "QQ群日报")
            self.assertEqual(review["title_prefix"], "QQ群总结")
            self.assertIn("虚构", (output / "index.html").read_text(encoding="utf-8"))
            self.assertEqual(len(list((output / "assets").glob("[a-f0-9]*.png"))), 1)
    def test_fixture_authenticates_and_wrong_key_or_tampering_fails(self):
        encrypted = FIXTURE / "synthetic-sqlcipher4.db"
        key_record = json.loads((FIXTURE / "fixture-key.json").read_text(encoding="utf-8"))
        self.assertIn("PUBLIC TEST-ONLY", key_record["warning"])
        key = bytes.fromhex(key_record["derived_enc_key_hex"])
        salt = bytes.fromhex(key_record["salt_hex"])
        self.assertEqual(decrypt_sync._derive_enc_key(key_record["key_utf8"].encode("ascii"), salt), key)
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            clear = temp / "clear.sqlite"
            self.assertGreater(decrypt_sync.full_decrypt(encrypted, clear, key), 0)
            self.assertTrue(decrypt_sync.verify_sqlite(clear))
            self.assertEqual(clear.read_bytes()[20], 80)
            with sqlite3.connect(clear) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM synthetic_messages").fetchone()[0], 2)

            wrong_key, _ = decrypt_sync._keys(decrypt_sync._derive_enc_key(b"wrong-public-test-key", salt), salt)
            with self.assertRaises(ValueError):
                decrypt_sync.full_decrypt(encrypted, temp / "wrong.sqlite", wrong_key)

            damaged = temp / "tampered.db"
            blob = bytearray(encrypted.read_bytes())
            blob[128] ^= 0x01
            damaged.write_bytes(blob)
            with self.assertRaises(ValueError):
                decrypt_sync.full_decrypt(damaged, temp / "tampered.sqlite", key)


if __name__ == "__main__":
    unittest.main()

import contextlib
import io
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "memory"))
MODULE = Path(__file__).with_name("send-report.py")
spec = importlib.util.spec_from_file_location("send_report", MODULE)
sender = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sender)


class TextMessageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = self.root / "messages"
        self.private.mkdir()
        self.file = self.private / "notice.txt"
        self.text = "网址：https://site.example.invalid/\n账号：fixture-user\n密码：fixture-secret"
        self.file.write_text(self.text, encoding="utf-8")
        self.hash = hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def row(self, *, local_id=2, server_id=99, local_type=1, create_time=100,
            origin_source=1, content=None):
        return {"local_id": local_id, "server_id": server_id, "local_type": local_type,
                "create_time": create_time, "origin_source": origin_source,
                "message_content": content if content is not None else "self_sender_id:\n" + self.text,
                "compress_content": None, "WCDB_CT_message_content": 0}

    def test_checked_utf8_text_returns_exact_digest_and_confinement(self):
        checked, text, digest = sender.checked_text(self.file, self.private)
        self.assertEqual(text, self.text)
        self.assertEqual(digest, self.hash)
        escaped = self.root / "outside.txt"
        escaped.write_text(self.text, encoding="utf-8")
        with self.assertRaises(ValueError):
            sender.checked_text(escaped, self.private)

    def test_exact_complete_decoded_text_matches_without_whitespace_normalization(self):
        rows = {"message_0.db": [self.row()]}
        receipt = sender.locate_new_self_text(rows, {"message_0.db": 1}, 100, self.hash)
        self.assertEqual(receipt["server_id"], 99)
        different = self.text.replace("\n", " ")
        self.assertIsNone(sender.locate_new_self_text(
            rows, {"message_0.db": 1}, 100, hashlib.sha256(different.encode()).hexdigest()))

    def test_requires_new_self_authored_in_time_target_type_and_nonzero_server_id(self):
        candidates = [
            self.row(local_id=3, origin_source=0),
            self.row(local_id=4, create_time=99),
            self.row(local_id=5, local_type=3),
            self.row(local_id=6, server_id=0),
        ]
        self.assertIsNone(sender.locate_new_self_text({"target-shard": candidates}, {"target-shard": 1}, 100, self.hash))
        self.assertIsNone(sender.locate_new_self_text(
            {"target-shard": [self.row(local_id=2)]}, {"target-shard": 2}, 100, self.hash))

    def test_rejects_multiple_matching_new_messages(self):
        rows = {"shard-a": [self.row(local_id=3)], "shard-b": [self.row(local_id=4)]}
        self.assertIsNone(sender.locate_new_self_text(rows, {"shard-a": 1, "shard-b": 1}, 100, self.hash))

    def test_database_query_is_scoped_to_the_selected_chat_table(self):
        db = self.root / "messages.db"
        selected = "Msg_" + "a" * 32
        other = "Msg_" + "b" * 32
        with sqlite3.connect(db) as conn:
            for table in (selected, other):
                conn.execute(f"CREATE TABLE [{table}](local_id INTEGER, server_id INTEGER, local_type INTEGER, create_time INTEGER, origin_source INTEGER, message_content TEXT, compress_content TEXT, WCDB_CT_message_content INTEGER)")
            conn.execute(f"INSERT INTO [{other}] VALUES(1,77,1,100,1,?,NULL,0)", ("self_sender_id:\n" + self.text,))
        self.assertEqual(sender.query_group_rows(db, selected, 0, 100, include_content=True, message_type=1), [])
        other_rows = sender.query_group_rows(db, other, 0, 100, include_content=True, message_type=1)
        self.assertEqual(len(other_rows), 1)
        self.assertEqual(other_rows[0]["message_content"], "self_sender_id:\n" + self.text)
        self.assertEqual(sender.query_group_rows(db, "Msg_" + "c" * 32, 0, 100, include_content=True, message_type=1), [])

    def test_prepared_cli_output_never_exposes_message_or_credentials(self):
        output = io.StringIO()
        with patch.object(sender, "TEXT_ROOT", self.private), \
                patch.object(sys, "argv", [str(MODULE), "--text-file", str(self.file)]), \
                contextlib.redirect_stdout(output):
            status = sender.main()
        self.assertEqual(status, 0)
        serialized = output.getvalue()
        self.assertEqual(json.loads(serialized)["sha256"], self.hash)
        for private_value in (self.text, "fixture-user", "fixture-secret"):
            self.assertNotIn(private_value, serialized)


if __name__ == "__main__":
    unittest.main()

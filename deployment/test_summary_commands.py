import unittest
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import summary_commands as commands

TEST_GROUP_ID = "fixture10000000000@chatroom"
TEST_BOT_ID = "fixture-bot-id"
TEST_BOT_NAMES = ["FixtureBot", "FixtureBotAlias"]


class SummaryCommandConsumerTests(unittest.TestCase):
    def candidate(self, text, **overrides):
        values = {"bot_names": TEST_BOT_NAMES, "author_id": "sender-fixture-id",
                  "bot_username": TEST_BOT_ID, "origin_source": 0, "local_type": 1}
        values.update(overrides)
        return commands.is_candidate(text, **values)

    def test_plain_at_name_and_unicode_whitespace_are_accepted(self):
        self.assertTrue(self.candidate("@FixtureBot\u3000总结最近3h聊天记录"))
        self.assertTrue(self.candidate("@FixtureBotAlias\t总结"))

    def test_other_names_and_name_prefixes_do_not_match(self):
        self.assertFalse(self.candidate("@FixtureBot的朋友 总结"))
        self.assertFalse(self.candidate("@其他人 总结"))
        self.assertFalse(self.candidate("@所有人 总结"))

    def test_nontext_and_self_authored_not_triggered(self):
        self.assertFalse(self.candidate("@FixtureBot 总结", local_type=3))
        self.assertFalse(self.candidate("@FixtureBot 总结", author_id=TEST_BOT_ID))
        self.assertFalse(self.candidate("@FixtureBot 总结", origin_source=1))

    def test_summary_request_is_required(self):
        self.assertFalse(self.candidate("@FixtureBot 你好"))

    def test_default_duration_is_one_day(self):
        end = 1_800_000_000
        result = commands.interpret("@FixtureBot 总结", "a" * 64, end, None)
        self.assertEqual(result, {"status": "accepted", "request_id": "a" * 64,
                                  "duration_seconds": 86400, "start": end-86400, "end": end})

    def test_explicit_and_invalid_model_durations(self):
        self.assertEqual(commands._validated_duration({"accepted": True, "duration_seconds": 10800}), 10800)
        for value in (None, 0, -1, 1.5, True, "10800"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                commands._validated_duration({"accepted": True, "duration_seconds": value})
    def test_unconfirmed_command_survives_cursor_advance_and_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / (TEST_BOT_ID + "_account") / "db_storage"
            message = root / 'message'
            message.mkdir(parents=True)
            db = message / 'message_0.db'
            table = 'Msg_' + hashlib.md5(TEST_GROUP_ID.encode()).hexdigest()
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE Name2Id(user_name TEXT)')
                conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES(1,'sender-fixture-id')")
                conn.execute(f'CREATE TABLE [{table}](local_id INTEGER,server_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,origin_source INTEGER,source TEXT,message_content TEXT,compress_content TEXT,WCDB_CT_message_content INTEGER)')
                conn.execute(f'INSERT INTO [{table}] VALUES(1,0,1,1,200,0,NULL,?,NULL,0)', ('sender-fixture-id:\n@FixtureBot 总结',))
                conn.execute(f'INSERT INTO [{table}] VALUES(2,22,1,1,201,0,NULL,?,NULL,0)', ('sender-fixture-id:\n普通聊天',))
            cursor = Path(temporary) / 'cursor.json'
            project = {"group": {"id": TEST_GROUP_ID, "name": "Synthetic Fixture Group"},
                       "bot": {"username": TEST_BOT_ID, "names": TEST_BOT_NAMES},
                       "timezone": "Asia/Shanghai", "worker_uid": 1234, "worker_gid": 2345}
            with patch.object(commands.settings, 'load_project', return_value=project), \
                 patch.object(commands.digest, '_load_keyfile', return_value={}), \
                 patch.object(commands.digest.decrypt_sync, 'key_for_rel', return_value=True), \
                 patch.object(commands.digest, '_decrypt', side_effect=lambda encrypted, *args: encrypted):
                first = commands.scan(root, TEST_BOT_ID, TEST_BOT_NAMES, 100, cursor)
                self.assertEqual(first['commands'], [])
                cursor.write_text(json.dumps(first['cursor']))
                with sqlite3.connect(db) as conn:
                    conn.execute(f'UPDATE [{table}] SET server_id=11 WHERE local_id=1')
                second = commands.scan(root, TEST_BOT_ID, TEST_BOT_NAMES, 100, cursor)
                self.assertEqual([c['server_id'] for c in second['commands']], [11])
                self.assertEqual(second['commands'][0]['trigger_timestamp'], 200)
                cursor.write_text(json.dumps(second['cursor']))
                third = commands.scan(root, TEST_BOT_ID, TEST_BOT_NAMES, 100, cursor)
                self.assertEqual(third['commands'], [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qq_onebot as qq


class FakeSocket:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.sent = []
        self.closed = False

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self, timeout=None):
        return json.dumps(self.responses.pop(0))

    def close(self):
        self.closed = True


def message(seq, timestamp, *, group=42, segments=None, author=7, message_id=None):
    return {
        "message_seq": seq,
        "message_id": message_id if message_id is not None else seq,
        "group_id": group,
        "time": timestamp,
        "sender": {"user_id": author, "nickname": "Member"},
        "message": segments if segments is not None else [
            {"type": "text", "data": {"text": str(seq)}}
        ],
    }


class NormalizeTests(unittest.TestCase):
    def test_text_images_reply_and_forward_are_preserved_without_execution(self):
        raw = message(5, 100, segments=[
            {"type": "text", "data": {"text": "hello"}},
            {"type": "image", "data": {"file": "x.png", "url": "https://example.invalid/x"}},
            {"type": "reply", "data": {"id": "message-4"}},
            {"type": "forward", "data": {"id": "node-1"}},
            {"type": "text", "data": {"text": " world"}},
        ])
        result = qq.normalize_messages([raw], 42)[0]
        self.assertEqual(result, {
            "message_uid": "qq:42:5",
            "reference": "Q5",
            "_at_ids": [],
            "reply_references": [
                {"type": "reply", "data": {"id": "message-4"}},
                {"type": "forward", "data": {"id": "node-1"}},
            ],
            "timestamp": 100,
            "author_id": "7",
            "author": "Member",
            "type": "image",
            "text": "hello world",
            "image_items": [{"file": "x.png", "url": "https://example.invalid/x"}],
        })
        self.assertRaises(ValueError, qq.normalize_messages, [raw], 43)

    def test_only_numeric_at_targets_enter_trigger_metadata(self):
        normalized = qq.normalize_messages([message(8, 100, segments=[
            {"type": "at", "data": {"qq": "123456789"}},
            {"type": "at", "data": {"qq": "all"}},
        ])], 42)[0]
        self.assertEqual(normalized["_at_ids"], ["123456789"])
        self.assertEqual(normalized["text"], "@123456789@all")

    def test_rejects_sender_or_timestamp_that_cannot_be_normalized(self):
        raw = message(1, 2)
        raw["sender"] = {"nickname": "unknown"}
        with self.assertRaises(ValueError):
            qq.normalize_messages([raw], 42)
        raw = message(1, 2)
        raw["time"] = "2"
        with self.assertRaises(ValueError):
            qq.normalize_messages([raw], 42)


class RPCTests(unittest.TestCase):
    def test_connect_authorization_echo_matching_and_retcode_validation(self):
        socket = FakeSocket([
            {"echo": "unrelated", "retcode": 0, "data": {"wrong": True}},
            {"echo": "1", "retcode": 0, "data": {"ok": True}},
        ])
        with patch.object(qq, "connect", return_value=socket) as connect:
            with qq.QQClient("ws://napcat:3001", "secret") as client:
                self.assertEqual(client.rpc("get_login_info", {}), {"ok": True})
                self.assertEqual(socket.sent[0], {
                    "action": "get_login_info", "params": {}, "echo": "1"
                })
            self.assertTrue(socket.closed)

        socket = FakeSocket([{"echo": "1", "retcode": 100, "data": None}])
        client = qq.QQClient("ws://napcat:3001", "secret")
        client._ws = socket
        with self.assertRaises(qq.RPCError):
            client.rpc("send_group_msg", {"group_id": 42, "message": "x"})


class HistoryTests(unittest.TestCase):
    def client_with_pages(self, pages):
        client = qq.QQClient("ws://napcat:3001", "secret")
        calls = []

        def rpc(action, params):
            calls.append((action, params))
            return pages.pop(0)

        client.rpc = rpc
        return client, calls

    def test_half_open_window_and_duplicate_pages_are_deduplicated(self):
        page = [message(3, 11), message(2, 10), message(1, 9)]
        client, calls = self.client_with_pages([{"messages": page}, {"messages": []}])
        values = client.history(42, 10, 12)
        self.assertEqual([item["message_uid"] for item in values], ["qq:42:2", "qq:42:3"])

    def test_sparse_pages_follow_time_order_not_numeric_ids_and_no_progress_fails(self):
        page = [message(900, 100), message(1, 200)]
        older = [message(500, 49)]
        client, calls = self.client_with_pages([{"messages": page}, {"messages": older}])
        values = client.history(42, 50, 201)
        self.assertEqual(calls[1][1]["message_seq"], 900)
        self.assertTrue(calls[1][1]["reverseOrder"])
        self.assertEqual([v['timestamp'] for v in values], [100, 200])
        duplicate, _ = self.client_with_pages([{"messages": page}, {"messages": page}])
        with self.assertRaises(qq.HistoryIncompleteError):
            duplicate.history(42, 50, 201)

    def test_missing_sequence_or_wrong_group_never_returns_supposed_complete_history(self):
        missing, _ = self.client_with_pages([{"messages": [message(1, 1)]}])
        delpage = [message(1, 1)]
        del delpage[0]["message_seq"]
        missing.rpc = lambda *_: {"messages": delpage}
        with self.assertRaises(qq.HistoryIncompleteError):
            missing.history(42, 1, 2)
        wrong, _ = self.client_with_pages([{"messages": [message(1, 1, group=43)]}])
        with self.assertRaises(qq.HistoryIncompleteError):
            wrong.history(42, 1, 2)


class SendTests(unittest.TestCase):
    def test_sends_exact_png_and_verifies_group_sender_and_image_receipt(self):
        client = qq.QQClient("ws://napcat:3001", "secret")
        sent = []
        original = b"\x89PNG\r\n\x1a\noriginal-png-bytes"
        client.rpc = lambda action, params: sent.append((action, params)) or {
            "message_id": 123
        } if action == "send_group_msg" else (
            {"user_id": 1000000002} if action == "get_login_info" else
            message(123, 10, author=1000000002, segments=[
                {"type": "image", "data": {"file": "uploaded.png"}}
            ])
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(original)
            self.assertEqual(client.send_image(42, path), 123)
        self.assertEqual(sent[0], ("send_group_msg", {
            "group_id": 42,
            "message": [{'type': 'image', 'data': {'file': 'base64://' + base64.b64encode(original).decode('ascii')}}],
        }))

    def test_text_exact_receipt_and_unverified_send_are_not_retried(self):
        client = qq.QQClient("ws://napcat:3001", "secret")
        calls = []

        def rpc(action, params):
            calls.append(action)
            if action == "send_group_msg":
                return {"message_id": 10}
            if action == "get_login_info":
                return {"user_id": 9}
            return message(10, 1, author=9, segments=[
                {"type": "text", "data": {"text": "exact body"}}
            ])

        client.rpc = rpc
        self.assertEqual(client.send_text(42, "exact body"), 10)
        self.assertEqual(calls, ["send_group_msg", "get_login_info", "get_msg"])

        calls.clear()
        def lost_response(action, params):
            calls.append(action)
            raise qq.RPCError("timeout")
        client.rpc = lost_response
        with self.assertRaises(qq.DeliveryUnknownError):
            client.send_text(42, "do not retry")
        self.assertEqual(calls, ["send_group_msg"])


if __name__ == "__main__":
    unittest.main()

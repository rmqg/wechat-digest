import json
import hashlib
import importlib.util
import io
from pathlib import Path
import struct
import time
import unittest
import tempfile
import urllib.error
from unittest.mock import Mock, patch

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

spec = importlib.util.spec_from_file_location("fetch_originals", Path(__file__).with_name("fetch-originals.py"))
fetch_originals = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_originals)


class Response(io.BytesIO):
    status = 200


class OriginalDownloadIntegrityTests(unittest.TestCase):
    def transfer(self, changes=None, expected_md5=None, transport_failure=False):
        plain = b"synthetic original media payload"
        key = b"0123456789abcdef"
        encrypted = AES.new(key, AES.MODE_ECB).encrypt(pad(plain, 16))
        fields = {"retcode": b"0", "seq": b"1", "rangestart": b"0",
                  "rangeend": str(len(encrypted) - 1).encode(),
                  "totalsize": str(len(encrypted)).encode(), "filedata": encrypted}
        fields.update(changes or {})
        opener = Mock()
        response = Response(fetch_originals.pack_fields(fields))
        if transport_failure:
            opener.open.side_effect = [urllib.error.URLError("temporary connection failure"), response]
        else:
            opener.open.return_value = response
        descriptor = {"expected_length": len(plain), "expected_md5": expected_md5 or hashlib.md5(plain).hexdigest(),
                      "key": key, "filetype": 1, "fileid": "synthetic-private-reference"}
        route = {"version": 1, "uin": 1, "auth": b"synthetic", "server_ips": ["203.0.113.1", "203.0.113.2"]}
        with patch.object(fetch_originals.urllib.request, "build_opener", return_value=opener):
            return fetch_originals.download(route, descriptor, time.monotonic() + 10)[0]

    def test_original_request_is_bound_to_supplied_synthetic_project(self):
        project = {"group": {"id": "synthetic-group-id", "name": "Synthetic test group"}}
        request = {"version": 1, "chat_id": project["group"]["id"], "chat_name": project["group"]["name"],
                   "targets": [{"message_uid": "synthetic-message-uid"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            self.assertEqual(fetch_originals.safe_request(path, project), request)
            request["chat_id"] = "different-synthetic-group"
            path.write_text(json.dumps(request), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "wrong version or target"):
                fetch_originals.safe_request(path, project)

    def test_replayed_range_sequence_is_rejected(self):
        with self.assertRaises(ValueError):
            self.transfer({"seq": b"2"})

    def test_decrypted_bytes_must_match_message_original_checksum(self):
        with self.assertRaises(ValueError):
            self.transfer(expected_md5="0" * 32)

    def test_rejected_download_is_not_a_successful_empty_image(self):
        with self.assertRaises(RuntimeError):
            self.transfer({"retcode": b"-3", "filedata": b""})

    def test_transport_failure_can_use_another_official_endpoint_without_losing_integrity(self):
        self.assertEqual(self.transfer(transport_failure=True), b"synthetic original media payload")

    def test_duplicate_cdn_response_fields_are_rejected(self):
        field = struct.pack(">I", 7) + b"retcode" + struct.pack(">I", 1) + b"0"
        with self.assertRaises(ValueError):
            fetch_originals.unpack_fields(field + field)

    def test_truncated_cdn_value_is_rejected(self):
        raw = struct.pack(">I", 8) + b"filedata" + struct.pack(">I", 100) + b"short"
        with self.assertRaises(ValueError):
            fetch_originals.unpack_fields(raw)


if __name__ == "__main__":
    unittest.main()

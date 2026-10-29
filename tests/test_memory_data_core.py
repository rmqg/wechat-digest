import hashlib
import io
import sqlite3
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'memory'))
import decrypt_sync
import media_sync
import memory_ingest
import message_parse


class MessageDecodeTests(unittest.TestCase):
    def test_content_hash_is_stable_and_boundary_safe(self):
        self.assertEqual(memory_ingest.content_hash('ab', 'c'), memory_ingest.content_hash('ab', 'c'))
        self.assertNotEqual(memory_ingest.content_hash('ab', 'c'), memory_ingest.content_hash('a', 'bc'))

    def test_zlib_content_and_64_bit_local_type(self):
        self.assertEqual(memory_ingest.decompress_content(zlib.compress('hello'.encode())), 'hello')
        value = (0x12345678 << 32) | 1
        self.assertEqual(memory_ingest.split_msg_type(value), (1, 0x12345678))
        self.assertEqual(memory_ingest.msg_type_label(value), 'text')

    def test_uncompressed_blob_is_utf8_and_zstd_failures_are_not_hidden(self):
        raw = 'synthetic UTF-8 中文'.encode()
        self.assertEqual(memory_ingest.decompress_content(raw, 0), 'synthetic UTF-8 中文')
        self.assertEqual(memory_ingest.decompress_content(raw), 'synthetic UTF-8 中文')
        with self.assertRaisesRegex(ValueError, 'zstd'):
            memory_ingest.decompress_content(b'\x28\xb5\x2f\xfdinvalid')

    def test_contact_and_session_alias_columns_are_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'metadata.db'
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE contact(user_name TEXT, nick_name TEXT, remark TEXT)')
                db.execute("INSERT INTO contact VALUES('wxid_demo','Demo Nick','Demo Remark')")
                db.execute('CREATE TABLE SessionTable(user_name TEXT, nick_name TEXT, unread_count INTEGER)')
                db.execute("INSERT INTO SessionTable VALUES('wxid_demo','Session Nick',3)")
            contact = memory_ingest.load_contact_names(database)['wxid_demo']
            session = memory_ingest.load_sessions(database)['wxid_demo']
        self.assertEqual((contact['user_name'], contact['nick_name'], contact['remark'], contact['display_name']),
                         ('wxid_demo', 'Demo Nick', 'Demo Remark', 'Demo Remark'))
        self.assertEqual((session['user_name'], session['username'], session['nick_name']),
                         ('wxid_demo', 'wxid_demo', 'Session Nick'))

    def test_group_sender_and_appmsg_are_plain_text_only(self):
        sender, text = message_parse.message_index_text({'message_content': 'wxid_demo:\nhello', 'type_label': 'text'})
        self.assertEqual((sender, text), ('wxid_demo', 'hello'))
        app = '<msg><appmsg><title>Title</title><des>Summary</des><url>https://example.invalid/x</url></appmsg></msg>'
        sender, text = message_parse.message_index_text({'message_content': app, 'type_label': 'appmsg'})
        self.assertEqual(sender, '')
        self.assertIn('Title', text)
        self.assertIn('https://example.invalid/x', text)
        self.assertEqual(message_parse.parse_app_message('<broken'), {})

    def test_nested_record_and_refer_are_normalized_without_xml_or_cdn_tickets(self):
        record = ('<msg><appmsg><type>19</type><title>Forwarded album</title><recorditem>'
                  '&lt;recordinfo&gt;&lt;dataitem datatype="1"&gt;&lt;title&gt;A useful link'
                  '&lt;/title&gt;&lt;datadesc&gt;https://example.invalid/?a=1&amp;amp;b=2'
                  '&lt;/datadesc&gt;&lt;/dataitem&gt;&lt;dataitem datatype="2"&gt;'
                  '&lt;fullmd5&gt;aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&lt;/fullmd5&gt;'
                  '&lt;cdnthumburl&gt;https://cdn.example.invalid/ticket-secret&lt;/cdnthumburl&gt;'
                  '&lt;/dataitem&gt;&lt;/recordinfo&gt;</recorditem></appmsg></msg>')
        _, text = message_parse.message_index_text({'message_content': record, 'type_label': 'link_or_file'})
        self.assertIn('Forwarded album', text)
        self.assertIn('A useful link', text)
        self.assertNotIn('recordinfo', text)
        self.assertNotIn('fullmd5', text)
        self.assertNotIn('ticket-secret', text)
        refer = '<msg><appmsg><type>57</type><refermsg><displayname>Ada</displayname><content>quoted words</content></refermsg></appmsg></msg>'
        _, refer_text = message_parse.message_index_text({'message_content': refer, 'type_label': 'appmsg'})
        self.assertIn('Ada', refer_text)
        self.assertIn('quoted words', refer_text)


class MediaDecodeTests(unittest.TestCase):
    @staticmethod
    def jpeg():
        output = io.BytesIO()
        Image.new('RGB', (19, 13), (70, 110, 150)).save(output, format='JPEG')
        return output.getvalue()

    @staticmethod
    def hybrid(payload, key, xor_key, prefix_size=64):
        prefix = payload[:prefix_size]
        middle = payload[prefix_size:-12]
        suffix = payload[-12:]
        encrypted_prefix = AES.new(key, AES.MODE_ECB).encrypt(pad(prefix, 16))
        encrypted_suffix = bytes(value ^ xor_key for value in suffix)
        header = media_sync.V2_MAGIC_FULL + struct.pack('<II', len(prefix), len(suffix)) + b'\x00'
        return header + encrypted_prefix + middle + encrypted_suffix

    def test_local_xor_dat_round_trip_requires_complete_image(self):
        payload = self.jpeg()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'demo.dat'
            source.write_bytes(bytes(value ^ 0x73 for value in payload))
            self.assertEqual(media_sync.decrypt_dat(source, None, 0x73), (payload, 'jpg'))
            source.write_bytes(bytes(value ^ 0x73 for value in payload[:-2]))
            self.assertEqual(media_sync.decrypt_dat(source, None, 0x73), (None, None))

    def test_v2_hybrid_has_encrypted_prefix_clear_middle_and_xor_tail(self):
        payload = self.jpeg()
        key, xor_key = b'0123456789abcdef', 0x5a
        encrypted = self.hybrid(payload, key, xor_key)
        self.assertEqual(media_sync.decrypt_v2(encrypted, key.decode(), xor_key), (payload, 'jpg'))
        self.assertEqual(media_sync.decrypt_v2(encrypted, key, xor_key), (payload, 'jpg'))

    def test_v2_rejects_truncated_jpeg_even_when_header_and_cipher_are_valid(self):
        payload = self.jpeg()[:-2]
        encrypted = self.hybrid(payload, b'0123456789abcdef', 0x5a)
        self.assertEqual(media_sync.decrypt_v2(encrypted, b'0123456789abcdef', 0x5a), (None, None))

    def test_xml_parser_returns_none_for_invalid_payload(self):
        self.assertIsNone(media_sync.parse_xml_payload('not xml'))
        xml = media_sync.parse_xml_payload('<msg><img md5="abc"/></msg>')
        self.assertEqual(xml.find('.//img').attrib['md5'], 'abc')


class WalShrinkTests(unittest.TestCase):
    @staticmethod
    def checksum(data, state=(0, 0)):
        s1, s2 = state
        for offset in range(0, len(data), 8):
            x = int.from_bytes(data[offset:offset+4], 'little')
            y = int.from_bytes(data[offset+4:offset+8], 'little')
            s1 = (s1 + x + s2) & 0xffffffff
            s2 = (s2 + y + s1) & 0xffffffff
        return s1, s2

    def test_last_committed_shrink_truncates_after_growth_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, wal, target = root / 'encrypted.db', root / 'encrypted.db-wal', root / 'plain.db'
            source.write_bytes(b's' * 16 + bytes(4096))
            target.write_bytes(bytes(4 * 4096))
            magic, version, page_size, checkpoint, salt1, salt2 = 0x377f0682, 3007000, 4096, 0, 12, 34
            header = struct.pack('>6I', magic, version, page_size, checkpoint, salt1, salt2)
            state = self.checksum(header)
            wal_bytes = bytearray(header + struct.pack('>2I', *state))
            for page_no, db_size in ((4, 4), (1, 2)):
                page = bytes([page_no]) * 4096
                frame_prefix = struct.pack('>4I', page_no, db_size, salt1, salt2)
                state = self.checksum(frame_prefix[:8] + page, state)
                wal_bytes.extend(frame_prefix + struct.pack('>2I', *state) + page)
            wal.write_bytes(wal_bytes)
            with patch.object(decrypt_sync, '_decrypt_page', side_effect=lambda page, number, *_: bytes([number]) * 4096):
                self.assertEqual(decrypt_sync.patch_wal_for_source(source, wal, target, bytes(32)), 2)
            self.assertEqual(target.stat().st_size, 2 * 4096)


if __name__ == '__main__':
    unittest.main()

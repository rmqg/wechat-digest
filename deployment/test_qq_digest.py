import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment import qq_digest


class QQDigestInputTests(unittest.TestCase):
    def test_window_must_be_positive_and_at_most_seven_days(self):
        with tempfile.TemporaryDirectory() as root:
            args = argparse.Namespace(group_id=10001, start=100, end=100, sources_json=Path(root) / 'missing')
            with self.assertRaisesRegex(ValueError, '1 to 604800'):
                qq_digest.run(args)
            args.end = 100 + qq_digest.MAX_WINDOW + 1
            with self.assertRaisesRegex(ValueError, '1 to 604800'):
                qq_digest.run(args)

    def test_empty_snapshot_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'sources.json'
            source.write_text(json.dumps({'group_id': 10001, 'group_name': '测试群', 'messages': []}))
            args = argparse.Namespace(group_id=10001, start=100, end=200, sources_json=source)
            with self.assertRaisesRegex(ValueError, 'zero messages'):
                qq_digest.run(args)
    def test_snapshot_group_must_match_explicit_group_id(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'sources.json'
            source.write_text(json.dumps({'group_id': 10002, 'group_name': '别的群', 'messages': []}))
            args = argparse.Namespace(group_id=10001, start=100, end=200, sources_json=source)
            with self.assertRaisesRegex(ValueError, 'match --group-id'):
                qq_digest.run(args)


    def test_only_https_tencent_image_urls_are_accepted(self):
        with patch.object(qq_digest.socket, 'getaddrinfo', return_value=[('x', 1, 1, '', ('8.8.8.8', 443))]):
            self.assertEqual(qq_digest._check_url('https://gchat.qpic.cn/a').hostname, 'gchat.qpic.cn')
            for value in ('http://gchat.qpic.cn/a', 'https://example.com/a',
                          'https://user@gchat.qpic.cn/a', 'https://127.0.0.1/a'):
                with self.subTest(url=value), self.assertRaises(ValueError):
                    qq_digest._check_url(value)

    def test_source_snapshot_redacts_image_url_fields(self):
        value = qq_digest._safe_public_snapshot({'group_id': 10001, 'group_name': '测试群',
            'messages': [{'image_items': [{'url': 'https://gchat.qpic.cn/private?token=secret', 'file': 'x', 'type': 'image'}]}]})
        rendered = json.dumps(value)
        self.assertNotIn('secret', rendered)
        self.assertNotIn('gchat.qpic.cn', rendered)
        self.assertEqual(value['messages'][0]['image_items'], [{'type': 'image'}])
    def test_group_avatar_uses_group_avatar_endpoint(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(qq_digest, '_fetch_image', return_value=b'image') as fetch:
                with patch('PIL.Image.open') as image_open, patch.object(qq_digest, '_private_file'):
                    image_open.return_value.__enter__.return_value.format = 'PNG'
                    qq_digest._group_avatar(Path(root), [], 10001)
            self.assertIn('https://p.qlogo.cn/gh/10001/10001/640', fetch.call_args.args[0])
    def test_attach_source_assets_adds_clickable_ref_without_changing_body_or_png(self):
        import hashlib
        import io
        from PIL import Image

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            runtime = root / 'runtime'
            cache = runtime / 'qq-digest' / 'images'
            cache.mkdir(parents=True)
            data_stream = io.BytesIO()
            Image.new('RGB', (1, 1), 'red').save(data_stream, format='PNG')
            data = data_stream.getvalue()
            sha = hashlib.sha256(data).hexdigest()
            (cache / f'{sha}.png').write_bytes(data)
            (cache / 'descriptors.json').write_text(json.dumps({'opaque-key': {'sha256': sha, 'extension': 'png'}}))
            report_dir = root / 'group' / 'reports'
            report_dir.mkdir(parents=True)
            report_path = report_dir / 'brief.json'
            png_path = report_dir / 'brief.png'
            png_path.write_bytes(b'unchanged')
            report_path.write_text(json.dumps({'chat_username': 'qq:10001', 'topics': [{'references': [
                {'message_uid': 'm1', 'type': 'image', 'text': 'unchanged body'}]}]}))
            snapshot = {'group_id': 10001, 'messages': [{'message_uid': 'm1', 'image_items': [
                {'cache_key': 'opaque-key'}]}]}
            with patch.object(qq_digest.daily_report, 'render_html', return_value='<html/>'):
                qq_digest.attach_source_assets([report_path], snapshot, runtime)
            value = json.loads(report_path.read_text())
            ref = value['topics'][0]['references'][0]
            self.assertEqual(ref['text'], 'unchanged body')
            self.assertEqual(ref['image_sources'], [f'assets/{sha}.png'])
            self.assertEqual(png_path.read_bytes(), b'unchanged')
            self.assertEqual((report_dir / 'assets' / f'{sha}.png').read_bytes(), data)


if __name__ == '__main__':
    unittest.main()

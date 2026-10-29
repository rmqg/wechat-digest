import hashlib
import html
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

import source_media


class SourceMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reports = self.root / "runtime" / "reports"
        self.reports.mkdir(parents=True)
        self.decoded = self.root / "runtime" / "digest" / "images" / "decoded"
        self.decoded.mkdir(parents=True)
        self.assets = self.root / "site" / "assets"
        self.assets.mkdir(parents=True)

    def _image(self, name, fmt):
        path = self.decoded / name
        Image.new("RGB", (23, 17), (34, 78, 120)).save(path, format=fmt)
        return path

    def _cache(self, path, uid="u1", *, sha=None):
        content_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        return {uid: {"item": {"message_uid": uid, "available": True, "source_quality": "original",
                               "image_path": str(path), "sha256": sha or content_sha}}}

    def test_referenced_png_and_jpeg_publish_once_with_original_dimensions(self):
        png = self._image("p.png", "PNG")
        jpg = self._image("j.jpg", "JPEG")
        png_before, jpg_before = png.read_bytes(), jpg.read_bytes()
        refs = [{"message_uid": "png", "type": "image"}, {"message_uid": "jpg", "type": "image"},
                {"message_uid": "png", "type": "image"}]
        cache = self._cache(png, "png") | self._cache(jpg, "jpg")
        sources, deps, assets = source_media.publish_referenced_images(self.reports, refs, self.assets, cache)
        self.assertEqual(len(assets), 2)
        self.assertEqual(len(sources), 2)
        self.assertTrue(sources["png"].endswith(".png"))
        self.assertTrue(sources["jpg"].endswith(".jpg"))
        self.assertTrue(all(Path(dep).is_file() for dep in deps))
        with Image.open(self.assets / sources["png"].split("/")[-1]) as result:
            self.assertEqual(result.size, (23, 17))
        self.assertEqual(png.read_bytes(), png_before)
        self.assertEqual(jpg.read_bytes(), jpg_before)

    def test_uid_binding_hash_mismatch_and_path_escape_are_rejected(self):
        image = self._image("verified.png", "PNG")
        outside = self.root / "outside.png"
        outside.write_bytes(image.read_bytes())
        cache = self._cache(image, "wrong-hash", sha="0" * 64)
        cache.update(self._cache(outside, "escape"))
        cache["uid-mismatch"] = {"item": {"message_uid": "different", "available": True,
            "source_quality": "original", "image_path": str(image), "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}}
        for uid in ('wrong-hash', 'escape', 'uid-mismatch'):
            with self.subTest(uid=uid), self.assertRaises(ValueError):
                source_media.publish_referenced_images(
                    self.reports, [{'message_uid': uid, 'type': 'image'}], self.assets, cache)
            self.assertEqual(list(self.assets.iterdir()), [])

    def test_relative_escape_is_rejected(self):
        image = self._image("p.png", "PNG")
        cache = self._cache(image, "escape")
        cache["escape"]["item"]["image_path"] = "../../outside.png"
        with self.assertRaises(ValueError):
            source_media.publish_referenced_images(
                self.reports, [{'message_uid': 'escape', 'type': 'image'}], self.assets, cache)
        self.assertEqual(list(self.assets.iterdir()), [])


    def test_same_image_across_messages_and_repeated_builds_has_one_asset(self):
        image = self._image('original-name.png', 'PNG')
        before = image.read_bytes()
        cache = self._cache(image, 'first') | self._cache(image, 'second')
        refs = [{'message_uid': uid, 'type': 'image'} for uid in ('first', 'second')]
        first, _, files = source_media.publish_referenced_images(self.reports, refs, self.assets, cache)
        again, _, repeated_files = source_media.publish_referenced_images(self.reports, refs, self.assets, cache)
        self.assertEqual(first['first'], first['second'])
        self.assertEqual(first, again)
        self.assertEqual(files, repeated_files)
        self.assertEqual(len(files), 1)
        self.assertEqual(len(list(self.assets.iterdir())), 1)
        self.assertEqual(image.read_bytes(), before)

    def test_corrupt_source_cannot_replace_a_previously_published_image(self):
        image = self._image('original.png', 'PNG')
        cache = self._cache(image)
        refs = [{'message_uid': 'u1', 'type': 'image'}]
        sources, _, _ = source_media.publish_referenced_images(self.reports, refs, self.assets, cache)
        target = self.assets / Path(sources['u1']).name
        published = target.read_bytes()
        image.write_bytes(b'corrupted cache contents')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            source_media.publish_referenced_images(self.reports, refs, self.assets, cache)
        self.assertEqual(target.read_bytes(), published)
        self.assertEqual(list(self.assets.glob('.source-image-*')), [])

    def test_browser_unsupported_format_is_converted_without_resizing_original(self):
        image = self._image('original.bmp', 'BMP')
        before = image.read_bytes()
        sources, _, _ = source_media.publish_referenced_images(
            self.reports, [{'message_uid': 'u1', 'type': 'image'}], self.assets, self._cache(image))
        self.assertTrue(sources['u1'].endswith('.png'))
        with Image.open(self.assets / Path(sources['u1']).name) as result:
            self.assertEqual(result.size, (23, 17))
            self.assertEqual(result.convert('RGB').getpixel((0, 0)), (34, 78, 120))
        self.assertEqual(image.read_bytes(), before)

    def test_forwarded_images_quotes_cycles_and_thumb_fingerprint_rules(self):
        hashes = ['1' * 32, '2' * 32, '3' * 32]
        cache = {f'image-{index}': {'item': {'message_uid': f'image-{index}', 'available': True,
                 'source_quality': 'original', 'image_path': f'/decoded/{index}.png', 'md5': digest}}
                 for index, digest in enumerate(hashes)}
        record = '<msg><appmsg><type>19</type><recorditem><recordinfo>' + ''.join(
            f'<dataitem datatype="2" fullmd5="{digest}" thumbfullmd5="{("f" * 32)}"/>'
            for digest in hashes) + '</recordinfo></recorditem><url>a&amp;b</url></appmsg></msg>'
        messages = [
            {'message_uid': 'forward', 'server_id': '10', 'type': 'appmsg', 'raw_content': record},
            {'message_uid': 'quote-forward', 'server_id': '11', 'type': 'appmsg',
             'raw_content': '<msg><appmsg><type>57</type><refermsg><svrid>10</svrid></refermsg></appmsg></msg>'},
            {'message_uid': 'quote-cycle-a', 'server_id': '12', 'type': 'appmsg',
             'raw_content': '<msg><appmsg><type>57</type><refermsg><svrid>13</svrid></refermsg></appmsg></msg>'},
            {'message_uid': 'quote-cycle-b', 'server_id': '13', 'type': 'appmsg',
             'raw_content': '<msg><appmsg><type>57</type><refermsg><svrid>12</svrid></refermsg></appmsg></msg>'},
            {'message_uid': 'thumb-only', 'server_id': '14', 'type': 'appmsg',
             'raw_content': '<msg><appmsg><type>19</type><recorditem><recordinfo>'
                            '<dataitem datatype="2" thumbfullmd5="' + hashes[0] + '"/>'
                            '</recordinfo></recorditem></appmsg></msg>'},
        ]
        resolved = source_media.source_image_uids(messages, cache)
        expected = ['image-0', 'image-1', 'image-2']
        self.assertEqual(resolved['forward'], expected)
        self.assertEqual(resolved['quote-forward'], expected)
        self.assertEqual(resolved['quote-cycle-a'], [])
        self.assertEqual(resolved['quote-cycle-b'], [])
        self.assertEqual(resolved['thumb-only'], [])

    def test_escaped_forward_child_md5_preserves_xml_entities_and_missing_image_signal(self):
        digest = 'a' * 32
        nested = ('<recordinfo><dataitem datatype="2"><fullmd5>' + digest + '</fullmd5>'
                  '<thumbfullmd5>' + 'b' * 32 + '</thumbfullmd5></dataitem>'
                  '<dataitem datatype="1"><datadesc>https://example.invalid/?a=1&amp;b=2</datadesc></dataitem>'
                  '</recordinfo>')
        forward = '<msg><appmsg><type>19</type><recorditem>' + html.escape(nested) + '</recorditem></appmsg></msg>'
        messages = [{'message_uid': 'forward', 'server_id': '10', 'type': 'link_or_file', 'raw_content': forward},
                    {'message_uid': 'reply', 'server_id': '11', 'type': 'link_or_file',
                     'raw_content': '<msg><appmsg><type>57</type><refermsg><svrid>10</svrid></refermsg></appmsg></msg>'}]
        image = self._image('full.png', 'PNG')
        cache = self._cache(image, uid='original')
        cache['original']['item']['md5'] = digest
        self.assertEqual(source_media.source_image_uids(messages, cache),
                         {'forward': ['original'], 'reply': ['original']})
        self.assertEqual(source_media.source_image_uids(messages, {}), {'forward': [], 'reply': []})
        self.assertEqual(source_media.source_image_requests(messages), {'forward': True, 'reply': True})

    def test_forward_placeholder_without_attachment_does_not_invent_an_image(self):
        messages = [
            {'message_uid': 'forward', 'server_id': '1', 'type': 'link_or_file', 'text': '转发内容[图片]',
             'raw_content': '<msg><appmsg><type>19</type><recorditem><recordinfo><dataitem datatype="1"/></recordinfo></recorditem></appmsg></msg>'},
            {'message_uid': 'reply', 'server_id': '2', 'type': 'link_or_file', 'text': '引用[图片]',
             'raw_content': '<msg><appmsg><type>57</type><refermsg><svrid>1</svrid></refermsg></appmsg></msg>'},
            {'message_uid': 'ordinary-text', 'type': 'text', 'text': '我写了[图片]这几个字'},
        ]
        self.assertEqual(source_media.source_image_uids(messages, {}),
                         {'forward': [], 'reply': [], 'ordinary-text': []})
        self.assertEqual(source_media.source_image_requests(messages),
                         {'forward': True, 'reply': True, 'ordinary-text': False})

    def test_image_candidates_are_deduplicated_and_cache_escape_is_rejected(self):
        image = self._image('shared.png', 'PNG')
        cache = self._cache(image, uid='image-uid')
        refs = [{'message_uid': 'forward', 'image_source_uids': ['image-uid', 'image-uid']},
                {'message_uid': 'quote', 'image_source_uids': ['image-uid']}]
        published, _, assets = source_media.publish_referenced_images(self.reports, refs, self.assets, cache)
        self.assertEqual(list(published), ['image-uid'])
        self.assertEqual(len(assets), 1)
        cache['image-uid']['item']['image_path'] = str(image.parent.parent / 'outside.png')
        with self.assertRaisesRegex(ValueError, 'escapes'):
            source_media.publish_referenced_images(self.reports, refs, self.assets, cache)

if __name__ == "__main__":
    unittest.main()

import hashlib
import importlib.util
import io
import struct
import sys
import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from PIL import Image

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "memory"))
import media_sync

spec = importlib.util.spec_from_file_location("prepare_images", HERE / "prepare-images.py")
prepare_images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_images)


class FullImageTests(unittest.TestCase):
    def jpeg(self):
        output = io.BytesIO()
        Image.new("RGB", (19, 13), (70, 110, 150)).save(output, format="JPEG")
        return output.getvalue()

    def envelope(self, payload):
        key = b"0123456789abcdef"
        header = media_sync.V2_MAGIC_FULL + struct.pack("<II", len(payload), 0) + b"\x00"
        return header + AES.new(key, AES.MODE_ECB).encrypt(pad(payload, 16)), key

    def test_complete_jpeg_with_trailing_client_metadata_is_decoded(self):
        payload = self.jpeg() + b"wechat-client-metadata"
        encrypted, key = self.envelope(payload)
        decoded, fmt = media_sync.decrypt_v2(encrypted, key, 0)
        self.assertEqual(decoded, payload)
        self.assertEqual(fmt, "jpg")
        with Image.open(io.BytesIO(decoded)) as image:
            image.load()
            self.assertEqual(image.size, (19, 13))

    def test_truncated_jpeg_is_not_accepted_as_metadata_variant(self):
        payload = self.jpeg()[:-2] + b"wechat-client-metadata"
        encrypted, key = self.envelope(payload)
        self.assertEqual(media_sync.decrypt_v2(encrypted, key, 0), (None, None))

    def test_original_requires_message_hash_and_variant_length(self):
        payload = self.jpeg()
        md5 = hashlib.md5(payload).hexdigest()
        attributes = {"md5": md5, "length": str(len(payload)), "hdlength": "0"}
        self.assertEqual(prepare_images.full_quality(Path(f"{md5}.dat"), md5, payload, attributes), "original")
        attributes["hdlength"] = str(len(payload) + 1)
        self.assertEqual(prepare_images.full_quality(Path(f"{md5}.dat"), md5, payload, attributes), "client_full")
        attributes["hdlength"] = str(len(payload))
        attributes["md5"] = "0" * 32
        self.assertEqual(prepare_images.full_quality(Path(f"{md5}.dat"), md5, payload, attributes), "client_full")

    def test_thumbnail_is_never_promoted_by_matching_hash(self):
        payload = self.jpeg()
        md5 = hashlib.md5(payload).hexdigest()
        attributes = {"md5": md5, "length": str(len(payload))}
        self.assertIsNone(prepare_images.full_quality(Path(f"{md5}_t.dat"), md5, payload, attributes))

    def test_missing_full_image_requires_download_without_original_ticket(self):
        self.assertTrue(prepare_images.requires_download({"available": False}, False))

    def test_mid_image_does_not_satisfy_an_advertised_original(self):
        mid = {"available": True, "source_quality": "client_full", "provenance": {"cache_file": "image.dat"}}
        self.assertTrue(prepare_images.requires_download(mid, True))
        self.assertFalse(prepare_images.requires_download(mid, False))

    def test_converted_h_cache_does_not_prove_the_advertised_original(self):
        converted = {"available": True, "source_quality": "client_full", "provenance": {"cache_file": "image_h.dat"}}
        self.assertTrue(prepare_images.requires_download(converted, True))

    def test_verified_original_does_not_need_another_download(self):
        original = {"available": True, "source_quality": "original", "provenance": {"cache_file": "image.dat"}}
        self.assertFalse(prepare_images.requires_download(original, True))

    def test_original_cache_can_use_message_hash_instead_of_resource_hash(self):
        payload = self.jpeg()
        original_md5 = hashlib.md5(payload).hexdigest()
        resource_md5 = "a" * 32
        attributes = {"md5": original_md5.upper(), "hdlength": str(len(payload))}
        path = Path(f"{original_md5}_h.dat")
        self.assertEqual(prepare_images.full_quality(path, resource_md5, payload, attributes), "original")

    def test_message_image_alias_cannot_escape_cache_namespace(self):
        self.assertEqual(prepare_images.image_aliases("a" * 32, {"md5": "../" * 12}), {"a" * 32})

    def test_heic_model_input_preserves_full_image_dimensions(self):
        from pillow_heif import register_heif_opener
        register_heif_opener()
        source = Image.new("RGB", (96, 64), (40, 120, 200))
        raw = io.BytesIO()
        source.save(raw, format="HEIF", quality=-1)
        payload = raw.getvalue()
        self.assertEqual(prepare_images.source_image_format(payload), "heic")
        converted, fmt, dimensions = prepare_images.model_image(payload, "heic")
        self.assertEqual(dimensions, (96, 64))
        self.assertEqual(fmt, "png")
        with Image.open(io.BytesIO(converted)) as decoded:
            decoded.load()
            self.assertEqual(decoded.size, source.size)
            self.assertLessEqual(max(abs(a - b) for a, b in zip(decoded.getpixel((48, 32)), source.getpixel((48, 32)))), 3)

    def test_iso_video_container_is_not_misidentified_as_heic_image(self):
        self.assertIsNone(prepare_images.source_image_format(b"\x00\x00\x00\x20ftypmp42" + b"\x00" * 20))

    def test_unchanged_complete_image_reuses_verified_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = self.jpeg()
            first = prepare_images.cached_model_image(source, "jpg", directory)
            with patch.object(prepare_images, "model_image", side_effect=AssertionError("unchanged original must not be decoded again")):
                second = prepare_images.cached_model_image(source, "jpg", directory)
            self.assertEqual(second, first)
            self.assertEqual(second[2], (19, 13))

    def test_corrupt_converted_cache_is_rebuilt_from_original_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = self.jpeg()
            _, _, _, cached = prepare_images.cached_model_image(source, "jpg", directory)
            cached.write_bytes(b"corrupt cache")
            data, fmt, dimensions, path = prepare_images.cached_model_image(source, "jpg", directory)
            self.assertEqual(data, source)
            self.assertEqual(path.read_bytes(), source)
            self.assertEqual((fmt, dimensions), ("jpg", (19, 13)))

    def test_prepared_image_fast_path_invalidates_changed_source_or_output(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            output = directory / "model.jpg"
            output.write_bytes(self.jpeg())
            item = {"image_path": str(output), "available": True, "download_required": False, "source_quality": "original"}
            signature = {"message_sha256": "source-one"}
            entry = {"inputs": signature, "output": prepare_images.file_signature(output), "item": item}
            self.assertEqual(prepare_images.prepared_cache_hit(entry, signature, directory), item)
            self.assertIsNone(prepare_images.prepared_cache_hit(entry, {"message_sha256": "source-two"}, directory))
            output.write_bytes(b"changed image bytes")
            self.assertIsNone(prepare_images.prepared_cache_hit(entry, signature, directory))

    def test_prepared_image_fast_path_does_not_hide_an_original_download_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            output = directory / "model.jpg"
            output.write_bytes(self.jpeg())
            signature = {"message_sha256": "source"}
            item = {"image_path": str(output), "available": True, "download_required": True, "source_quality": "client_full"}
            entry = {"inputs": signature, "output": prepare_images.file_signature(output), "item": item}
            self.assertIsNone(prepare_images.prepared_cache_hit(entry, signature, directory))


if __name__ == "__main__":
    unittest.main()

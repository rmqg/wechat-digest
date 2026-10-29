import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ocr_images


class OCRImagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.image = self.root / "full.png"
        from PIL import Image
        Image.new("RGB", (40, 40), (255, 0, 0)).save(self.image)
        self.sha = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.item = {"message_uid": "uid-1", "available": True,
                     "source_quality": "original", "image_path": str(self.image), "sha256": self.sha}

    def tearDown(self):
        self.temp.cleanup()

    def test_unavailable_and_thumbnail_are_failed_and_retained(self):
        stats = ocr_images.process_manifest(
            [{"message_uid": "missing", "available": False},
             {"message_uid": "thumb", "available": True, "source_quality": "thumbnail"}],
            self.root / "out.json", self.root / "cache")
        result = json.loads((self.root / "out.json").read_text())
        self.assertEqual(stats["failed"], 2)
        self.assertEqual(set(result), {"missing", "thumb"})
        self.assertTrue(all(item["status"] == "failed" for item in result.values()))

    def test_limit_marks_unprocessed_eligible_entries_pending(self):
        with patch.object(ocr_images, "_create_ocr", return_value=object()), \
             patch.object(ocr_images, "_recognize", return_value=("识别结果", [], 0.9)):
            stats = ocr_images.process_manifest(
                [dict(self.item), {**self.item, "message_uid": "uid-2"}],
                self.root / "out.json", self.root / "cache", max_images=1)
        result = json.loads((self.root / "out.json").read_text())
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(result["uid-2"]["status"], "pending")

    def test_wrong_sha_fails_without_ocr(self):
        item = {**self.item, "sha256": "0" * 64}
        stats = ocr_images.process_manifest([item], self.root / "out.json", self.root / "cache")
        result = json.loads((self.root / "out.json").read_text())["uid-1"]
        self.assertEqual(stats["failed"], 1)
        self.assertIn("SHA-256", result["reason"])

    def test_cached_result_uses_source_sha_and_engine_version(self):
        cache = self.root / "cache"
        cache.mkdir()
        cache_file = cache / f"{self.sha}-{ocr_images.ENGINE}-{ocr_images.ENGINE_VERSION}.json"
        cache_file.write_text(json.dumps({"schema": ocr_images.CACHE_SCHEMA,
                                         "image_sha256": self.sha, "engine": ocr_images.ENGINE,
                                         "engine_version": ocr_images.ENGINE_VERSION,
                                         "text": "缓存文字", "lines": [{"text": "缓存文字", "confidence": 0.9}],
                                         "confidence": 0.9, "low_confidence": False}))
        with patch.dict("sys.modules", {"rapidocr_onnxruntime": None}):
            stats = ocr_images.process_manifest([self.item], self.root / "out.json", cache)
        self.assertEqual(stats["ok"], 1)
        result = json.loads((self.root / "out.json").read_text())["uid-1"]
        self.assertEqual(result["text"], "缓存文字")
        self.assertTrue(result["cache_hit"])

    def test_malformed_image_does_not_reuse_legacy_cache(self):
        bad_image = self.root / "malformed.png"
        bad_image.write_bytes(b"not an image")
        sha = hashlib.sha256(bad_image.read_bytes()).hexdigest()
        cache = self.root / "cache"
        cache.mkdir()
        cache_file = cache / f"{sha}-{ocr_images.ENGINE}-{ocr_images.ENGINE_VERSION}.json"
        cache_file.write_text(json.dumps({"schema": ocr_images.CACHE_SCHEMA,
                                         "image_sha256": sha, "engine": ocr_images.ENGINE,
                                         "engine_version": ocr_images.ENGINE_VERSION,
                                         "text": "stale", "lines": [],
                                         "confidence": None, "low_confidence": True}))
        item = {**self.item, "image_path": str(bad_image), "sha256": sha}

        with patch.object(ocr_images, "_create_ocr") as create_ocr:
            stats = ocr_images.process_manifest([item], self.root / "out.json", cache)

        result = json.loads((self.root / "out.json").read_text())["uid-1"]
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("text", result)
        create_ocr.assert_not_called()

    def test_cached_prefix_does_not_starve_later_images(self):
        second = self.root / 'second.png'
        from PIL import Image
        Image.new("RGB", (40, 40), (0, 0, 255)).save(second)
        items = [self.item, {**self.item, 'message_uid': 'uid-2', 'image_path': str(second),
                             'sha256': hashlib.sha256(second.read_bytes()).hexdigest()}]
        with patch.object(ocr_images, '_create_ocr', return_value=object()), \
             patch.object(ocr_images, '_recognize', return_value=('识别结果', [], 0.9)):
            first = ocr_images.process_manifest(items, self.root / 'out.json', self.root / 'cache', max_images=1)
            self.assertEqual((first['ok'], first['pending']), (1, 1))
            second_batch = ocr_images.process_manifest(items, self.root / 'out.json', self.root / 'cache', max_images=1)
        self.assertEqual((second_batch['ok'], second_batch['pending']), (2, 0))
        rows = json.loads((self.root / 'out.json').read_text())
        self.assertTrue(rows['uid-1']['cache_hit'])
        self.assertFalse(rows['uid-2']['cache_hit'])

    def test_cache_reuses_small_legacy_but_reprocesses_large_legacy(self):
        from PIL import Image

        cache = self.root / "cache"
        cache.mkdir()
        at_limit = self.root / "at-limit.png"
        above_limit = self.root / "above-limit.png"
        Image.new("RGB", (2000, 1000), (10, 20, 30)).save(at_limit)
        Image.new("RGB", (800, 3000), (30, 20, 10)).save(above_limit)
        items = []
        for uid, image_path, text in (("small", at_limit, "small legacy"),
                                      ("large", above_limit, "large legacy")):
            sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
            items.append({"message_uid": uid, "available": True, "source_quality": "original",
                          "image_path": str(image_path), "sha256": sha})
            cache_file = cache / f"{sha}-{ocr_images.ENGINE}-{ocr_images.ENGINE_VERSION}.json"
            cache_file.write_text(json.dumps({"schema": ocr_images.CACHE_SCHEMA,
                                              "image_sha256": sha, "engine": ocr_images.ENGINE,
                                              "engine_version": ocr_images.ENGINE_VERSION,
                                              "text": text, "lines": [{"text": text, "confidence": 0.9}],
                                              "confidence": 0.9, "low_confidence": False}))

        with patch.object(ocr_images, "_create_ocr", return_value=object()), \
             patch.object(ocr_images, "_recognize", return_value=("new OCR", [], 0.9)) as recognize:
            stats = ocr_images.process_manifest(items, self.root / "out.json", cache)

        rows = json.loads((self.root / "out.json").read_text())
        self.assertEqual(stats["ok"], 2)
        self.assertTrue(rows["small"]["cache_hit"])
        self.assertEqual(rows["small"]["text"], "small legacy")
        self.assertFalse(rows["large"]["cache_hit"])
        self.assertEqual(rows["large"]["text"], "new OCR")
        recognize.assert_called_once()

    def test_long_image_is_tiled_without_touching_original_or_losing_lines(self):
        from PIL import Image

        source = self.root / "tall.png"
        Image.new("RGB", (160, 5001), (12, 34, 56)).save(source)
        original = source.read_bytes()
        observed = {"shapes": []}

        def recognize(image):
            index = len(observed["shapes"])
            observed["shapes"].append(image.shape)
            observed["pixel"] = image[0, 0].tolist()
            center = image.shape[0] // 2
            box = [[0, center - 5], [10, center - 5],
                   [10, center + 5], [0, center + 5]]
            return [[box, f"片段{index}", 0.9]], None

        text, lines, confidence = ocr_images._recognize(original, recognize)
        expected = "\n".join(f"片段{index}" for index in range(len(observed["shapes"])))

        self.assertGreater(len(observed["shapes"]), 1)
        self.assertTrue(all(max(shape[:2]) <= 431 for shape in observed["shapes"]))
        self.assertEqual(observed["shapes"][0][2], 3)
        self.assertEqual(observed["shapes"][0][0], 431)
        self.assertEqual(text, expected)
        self.assertEqual([line["text"] for line in lines], expected.split("\n"))
        self.assertAlmostEqual(confidence, 0.9)
        self.assertEqual(observed["pixel"], [56, 34, 12])
        self.assertEqual(source.read_bytes(), original)

    def test_thin_long_image_keeps_bounded_ocr_tiles(self):
        from PIL import Image

        thin = self.root / "thin.png"
        Image.new("RGB", (100, 5001), (12, 34, 56)).save(thin)
        observed = []

        def recognize(image):
            observed.append(image.shape)
            return [], None

        text, lines, confidence = ocr_images._recognize(thin.read_bytes(), recognize)

        self.assertGreater(len(observed), 1)
        self.assertTrue(all(shape[0] > 0 and shape[1] > 0 and max(shape[:2]) <= 269
                            for shape in observed))
        self.assertEqual(text, "")
        self.assertEqual(lines, [])
        self.assertIsNone(confidence)

    def test_output_permissions_and_atomic_cache_record(self):
        ocr_images._private_json(self.root / "private/out.json", {"x": 1})
        self.assertEqual((self.root / "private/out.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "private").stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()

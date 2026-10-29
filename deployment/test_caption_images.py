import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import vision  # noqa: E402

MODULE_PATH = HERE / "caption-images.py"
spec = importlib.util.spec_from_file_location("caption_images", MODULE_PATH)
caption_images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(caption_images)


class OriginalImagePolicyTests(unittest.TestCase):
    def test_available_non_original_sources_are_unavailable_without_captioning(self):
        manifest = [
            {"message_uid": quality, "available": True, "sha256": quality,
             "image_path": "/unused", "source_quality": quality}
            for quality in ("thumbnail", "local_full", "unknown")
        ]
        manifest.append({"message_uid": "missing-quality", "available": True,
                         "sha256": "missing", "image_path": "/unused"})

        with patch.object(caption_images, "describe_image", side_effect=AssertionError("caption API invoked")):
            output, cache = caption_images.caption_manifest(manifest, {}, None, {})

        self.assertEqual(cache, {})
        self.assertEqual({item["status"] for item in output.values()}, {"unavailable"})
        self.assertTrue(all("reason" in item for item in output.values()))

    def test_comparison_cache_requires_original_quality_and_actual_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.json"
            rows = [
                {"provider": "gemini", "sha256": "good", "source_quality": "original",
                 "result": {"model": "served-model", "description": "cached"}},
                {"provider": "gemini", "sha256": "thumbnail", "source_quality": "thumbnail",
                 "result": {"model": "served-model"}},
                {"provider": "gemini", "sha256": "missing-quality",
                 "result": {"model": "served-model"}},
                {"provider": "gemini", "sha256": "wrong-model", "source_quality": "original",
                 "result": {"model": "other-model"}},
            ]
            path.write_text(json.dumps(rows), encoding="utf-8")
            self.assertEqual(caption_images._load_cache(path, "served-model"),
                             {"good": {"model": "served-model", "description": "cached"}})

    def test_main_rejects_saved_cache_without_original_quality_or_matching_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            primary = root / "primary.json"
            comparison = root / "comparison.json"
            output = root / "output.json"
            cache_output = root / "cache.json"
            manifest.write_text(json.dumps([{
                "message_uid": "original", "available": True, "source_quality": "original",
                "sha256": "same", "image_path": "/original.png",
            }]), encoding="utf-8")
            primary.write_text(json.dumps({"model": "configured-model"}), encoding="utf-8")
            comparison.write_text("[]", encoding="utf-8")
            cache_output.write_text(json.dumps({
                "same": {"model": "configured-model", "source_quality": "thumbnail",
                         "result": {"model": "configured-model", "description": "stale"}},
                "other": {"model": "configured-model", "source_quality": "original",
                          "result": {"model": "different-model", "description": "stale"}},
            }), encoding="utf-8")
            with patch.object(caption_images, "describe_image", side_effect=RuntimeError("offline")):
                self.assertEqual(caption_images.main([
                    "--manifest", str(manifest), "--primary", str(primary),
                    "--fallback", str(root / "missing-fallback.json"),
                    "--comparison", str(comparison), "--output", str(output),
                    "--cache-output", str(cache_output),
                ]), 1)

            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["original"]["status"], "failed")

    def test_approved_fallback_cache_remains_usable_without_recalling_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {name: root / (name + ".json") for name in ("manifest", "primary", "fallback", "comparison", "cache", "output")}
            files["manifest"].write_text(json.dumps([{"message_uid": "m1", "available": True, "source_quality": "original",
                                                     "sha256": "original-sha", "image_path": "/not-needed.png"}]))
            files["primary"].write_text(json.dumps({"model": "primary-model"}))
            files["fallback"].write_text(json.dumps({"model": "fallback-model"}))
            files["comparison"].write_text("[]")
            files["cache"].write_text(json.dumps({"original-sha": {"model": "fallback-model", "source_quality": "original",
                "result": {"model": "fallback-model", "description": "validated original image", "visible_text": "sign",
                           "facts": ["visible sign"], "uncertainties": []}}}))
            args = [value for key, path in files.items() for value in ["--cache-output" if key == "cache" else "--" + key, str(path)]]
            with patch.object(caption_images, "describe_image", side_effect=AssertionError("must reuse approved cached image")):
                self.assertEqual(caption_images.main(args), 0)
            result = json.loads(files["output"].read_text())["m1"]
            self.assertEqual(result["model"], "fallback-model")
            self.assertEqual(result["provider"], "qwen")
            self.assertEqual(result["source_quality"], "original")

    def test_max_images_marks_unselected_rows_pending_and_deduplicates_hashes(self):
        manifest = [
            {"message_uid": "first", "available": True, "source_quality": "original",
             "sha256": "same", "image_path": "/first.png"},
            {"message_uid": "duplicate", "available": True, "source_quality": "original",
             "sha256": "same", "image_path": "/duplicate.png"},
            {"message_uid": "later", "available": True, "source_quality": "original",
             "sha256": "later", "image_path": "/later.png"},
        ]
        calls = []
        checkpoints = []

        def describe(path, config, quality):
            calls.append(path)
            return {"model": "served", "description": "ok"}

        with patch.object(caption_images, "describe_image", side_effect=describe):
            output, cache = caption_images.caption_manifest(
                manifest, {"model": "primary"}, None, {}, max_images=1,
                on_result=lambda entry, row: checkpoints.append((entry, row)))

        self.assertEqual(calls, ["/first.png"])
        self.assertEqual(output["first"]["status"], "success")
        self.assertEqual(output["duplicate"]["status"], "success")
        self.assertEqual(output["later"]["status"], "pending")
        self.assertEqual(set(cache), {"same"})
        self.assertTrue(any(row and row["sha256"] == "same" for _, row in checkpoints))



    def test_refusal_does_not_switch_to_fallback_or_retry(self):
        class Refusal(RuntimeError):
            category = "refusal"
            retryable = False
            status_code = 200
            retry_after = None
            evidence_path = "/private/evidence.json"

        manifest = [{"message_uid": "refused", "available": True, "source_quality": "original",
                    "sha256": "sha", "image_path": "/image.png"}]
        failures = {}
        with patch.object(caption_images, "describe_image", side_effect=Refusal("content refused")) as describe:
            output, _ = caption_images.caption_manifest(
                manifest, {"model": "primary"}, {"model": "fallback"}, {}, failures=failures)
            repeated, _ = caption_images.caption_manifest(
                manifest, {"model": "primary"}, {"model": "fallback"}, {}, failures=failures)

        self.assertEqual(describe.call_count, 1)
        self.assertEqual(output["refused"]["status"], "failed")
        self.assertEqual(repeated["refused"]["status"], "failed")
        self.assertIn("refusal", output["refused"]["reason"])

    def test_rate_limit_cools_provider_for_other_images(self):
        class Limited(RuntimeError):
            category = "rate_limited"
            retryable = True
            status_code = 429
            retry_after = 600

        config = {"model": "primary", "base_url": "https://provider.example"}
        failures = {}
        first = [{"message_uid": "one", "available": True, "source_quality": "original",
                  "sha256": "sha-one", "image_path": "/one.png"}]
        second = [{"message_uid": "two", "available": True, "source_quality": "original",
                   "sha256": "sha-two", "image_path": "/two.png"}]
        with patch.object(caption_images, "describe_image", side_effect=Limited("limited")) as describe:
            output, _ = caption_images.caption_manifest(first, config, None, {}, failures=failures)
            pending, _ = caption_images.caption_manifest(second, config, None, {}, failures=failures)

        self.assertEqual(describe.call_count, 1)
        self.assertEqual(output["one"]["status"], "failed")
        self.assertEqual(pending["two"]["status"], "pending")
        cooldown = next(iter(failures["_provider_cooldowns"].values()))
        self.assertEqual(cooldown["retry_after"], 600)
if __name__ == "__main__":
    unittest.main()

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

MODULE_PATH = Path(__file__).with_name("vision.py")
spec = importlib.util.spec_from_file_location("vision", MODULE_PATH)
vision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vision)


class VisionContractTests(unittest.TestCase):
    def test_missing_or_blank_configuration_is_rejected(self):
        for config in ({}, {"base_url": "https://example.test", "model": "m", "api_key": " "}, None):
            with self.subTest(config=config), self.assertRaises(ValueError):
                vision._validate_config(config)

    def test_invalid_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.bin"
            path.write_bytes(b"not an image")
            with self.assertRaises(ValueError):
                vision._load_image(path)


    def test_malformed_response_is_not_fallback_result(self):
        with self.assertRaises(vision.VisionError):
            vision._parse_response(b"not-json", "requested-model")

    def test_response_requires_exact_fields_and_types(self):
        content = {"description": "图像", "visible_text": "无可辨认文字", "facts": [], "uncertainties": []}
        envelope = {"model": "served-model", "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}
        self.assertEqual(vision._parse_response(json.dumps(envelope).encode(), "requested-model")["model"], "served-model")
        content["identity"] = "猜测"
        envelope["choices"][0]["message"]["content"] = json.dumps(content)
        with self.assertRaises(vision.VisionError):
            vision._parse_response(json.dumps(envelope).encode(), "requested-model")
        del content["identity"]
        content["facts"] = [1]
        envelope["choices"][0]["message"]["content"] = json.dumps(content)
        with self.assertRaises(vision.VisionError):
            vision._parse_response(json.dumps(envelope).encode(), "requested-model")

    def test_fenced_model_json_is_parsed_without_accepting_unrelated_prose(self):
        content = {"description": "统计看板", "visible_text": "无可辨认文字", "facts": [], "uncertainties": ["文字过小"]}
        envelope = {"choices": [{"message": {"content": "```json\n" + json.dumps(content) + "\n```"}}]}
        result = vision._parse_response(json.dumps(envelope).encode(), "requested-model")
        self.assertEqual(result["uncertainties"], ["文字过小"])
        envelope["choices"][0]["message"]["content"] = "unrelated prose\n" + json.dumps(content)
        with self.assertRaises(vision.VisionError):
            vision._parse_response(json.dumps(envelope).encode(), "requested-model")

    def test_valid_looking_json_is_rejected_when_upstream_reports_truncation(self):
        result = {"description": "image", "visible_text": "none", "facts": [], "uncertainties": []}
        raw = json.dumps({"choices": [{"finish_reason": "length", "message": {"content": json.dumps(result)}}]}).encode()
        with self.assertRaises(vision.VisionError) as raised:
            vision._parse_response(raw, "requested")
        self.assertEqual(raised.exception.category, "truncated")

    def test_refusal_is_not_treated_as_a_caption_or_retryable_transport_error(self):
        raw = json.dumps({"choices": [{"finish_reason": "content_filter", "message": {"content": "", "refusal": "refused"}}]}).encode()
        with self.assertRaises(vision.VisionError) as raised:
            vision._parse_response(raw, "requested")
        self.assertEqual(raised.exception.category, "refusal")
        self.assertFalse(raised.exception.retryable)

    def test_error_diagnostics_are_private_and_redact_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            secret = "sk-" + "sensitive" * 6
            path = vision._save_diagnostic(Path(temp), {"category": "http"}, json.dumps({"message": secret}).encode(), secret, True)
            saved = Path(path)
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(secret, saved.read_text())
            self.assertIn("[REDACTED]", saved.read_text())

    def test_non_object_message_is_classified(self):
        raw = json.dumps({"choices": [{"message": None}]}).encode()
        with self.assertRaises(vision.VisionError) as raised:
            vision._parse_response(raw, "requested")
        self.assertEqual(raised.exception.category, "malformed_response")


if __name__ == "__main__":
    unittest.main()

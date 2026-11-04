import importlib.util
import json
from pathlib import Path
import unittest

MODULE = Path(__file__).with_name("send-report.py")
spec = importlib.util.spec_from_file_location("diagnostic_sender", MODULE)
sender = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sender)


class SenderDiagnosticTests(unittest.TestCase):
    def test_deepest_business_frame_is_reported_without_payload_or_absolute_path(self):
        namespace = {}
        code = "def outer():\n    inner()\ndef inner():\n    raise RuntimeError('https://alice:private-pass@provider.invalid/?api_key=private-key')\n"
        exec(compile(code, str(MODULE), "exec"), namespace)
        try:
            namespace["outer"]()
        except RuntimeError as exc:
            context = sender._failure_context(exc, "image_delivery")
        self.assertEqual(context["failure_location"], {
            "file": "deployment/send-report.py", "function": "inner", "line": 4})
        self.assertEqual(context["error_type"], "RuntimeError")
        self.assertEqual(context["stage"], "image_delivery")
        serialized = json.dumps(context)
        self.assertNotIn("private-pass", serialized)
        self.assertNotIn("private-key", serialized)
        self.assertNotIn(str(MODULE.parent), serialized)

    def test_external_error_keeps_business_callsite_and_separate_origin(self):
        external = {}
        exec(compile("def provider_call():\n    raise ValueError('private draft content')\n",
                     "/private/secret-root/provider.py", "exec"), external)
        business = {"provider_call": external["provider_call"]}
        exec(compile("def business_call():\n    provider_call()\n", str(MODULE), "exec"), business)
        try:
            business["business_call"]()
        except ValueError as exc:
            context = sender._failure_context(exc, "host_preparation")
        self.assertEqual(context["failure_location"], {
            "file": "deployment/send-report.py", "function": "business_call", "line": 2})
        self.assertEqual(context["failure_origin"], {
            "file": "provider.py", "function": "provider_call", "line": 2})
        serialized = json.dumps(context)
        self.assertNotIn("private draft content", serialized)
        self.assertNotIn("/private/secret-root", serialized)


if __name__ == "__main__":
    unittest.main()

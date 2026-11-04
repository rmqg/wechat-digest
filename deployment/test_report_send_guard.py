import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).with_name("send-report.py")
spec = importlib.util.spec_from_file_location("report_sender_guard", MODULE)
sender = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sender)


class ReportSendGuardTests(unittest.TestCase):
    def delivery_tree(self, root):
        runtime = root / "runtime"
        reports = runtime / "reports"
        reports.mkdir(parents=True)
        png = reports / "fixture-report-example.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        png.with_suffix(".html").write_text("<html>fixture</html>", encoding="utf-8")
        png.with_suffix(".json").write_text("{}", encoding="utf-8")
        text_root = runtime / "digest" / "site-access-messages"
        text_root.mkdir(parents=True)
        text_file = text_root / "notice.txt"
        text_file.write_text("Synthetic delivery fixture", encoding="utf-8")
        for name in ("gui", "keyscan", "ui-calibration", "python-deps"):
            (runtime / name).mkdir(parents=True)
        (runtime / "keyscan" / "all_keys.json").write_text("{}", encoding="utf-8")
        (runtime / "selection.json").write_text("{}", encoding="utf-8")
        (runtime / "ui-calibration.json").write_text("{}", encoding="utf-8")
        for name in ("deployment", "memory", "agent_console", "assets"):
            (root / name).mkdir()
        config_path = root / "project.json"
        config_path.write_text("{}", encoding="utf-8")
        return runtime, png, text_file, text_root, config_path

    def invoke_send(self, kind, png, text_file):
        if kind == "image":
            return sender.host_send(png, True, "fixture10000000000@chatroom")
        return sender.host_send_text(text_file, True, "fixture10000000000@chatroom")

    def patches(self, root, runtime, text_root, config_path):
        project = {"desktop_container": "fixture-desktop", "worker_uid": 1200,
                   "worker_gid": 1201, "timezone": "Asia/Shanghai"}
        return (
            patch.object(sender, "ROOT", root),
            patch.object(sender, "RUNTIME", runtime),
            patch.object(sender, "TEXT_ROOT", text_root),
            patch.object(sender, "REPORT_PREFIX", "fixture-report"),
            patch.object(sender, "_delivery_config", return_value=project),
            patch.object(sender, "_verify_target", return_value=("Synthetic Fixture", "fixture10000000000@chatroom")),
            patch.object(sender.settings, "runtime_image", return_value="sha256:" + "b" * 64),
            patch.object(sender.settings, "CONFIG_PATH", config_path),
        )

    def test_nonrunning_desktop_blocks_image_and_text_before_worker_launch(self):
        for kind in ("image", "text"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                runtime, png, text_file, text_root, config_path = self.delivery_tree(root)
                with self.enter_patches(self.patches(root, runtime, text_root, config_path)), \
                        patch.object(sender.subprocess, "run", return_value=subprocess.CompletedProcess(
                            [], 0, stdout="false\n", stderr="")) as run:
                    result = self.invoke_send(kind, png, text_file)
                self.assertEqual(result["status"], "failed")
                self.assertIs(result["sent"], False)
                self.assertIs(result["before_submit"], True)
                run.assert_called_once()

    def test_missing_worker_receipt_is_unknown_for_image_and_text(self):
        for kind in ("image", "text"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                runtime, png, text_file, text_root, config_path = self.delivery_tree(root)
                with self.enter_patches(self.patches(root, runtime, text_root, config_path)), \
                        patch.object(sender.os, "chown"), patch.object(sender.subprocess, "run", side_effect=[
                            subprocess.CompletedProcess([], 0, stdout="true\n", stderr=""),
                            subprocess.CompletedProcess([], 0, stdout="", stderr="")]) as run:
                    result = self.invoke_send(kind, png, text_file)
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["sent"])
                self.assertIs(result["before_submit"], False)

    @staticmethod
    def enter_patches(patches):
        from contextlib import ExitStack
        stack = ExitStack()
        for item in patches:
            stack.enter_context(item)
        return stack


if __name__ == "__main__":
    unittest.main()

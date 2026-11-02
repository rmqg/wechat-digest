import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import qq_publish


GROUP_ID = 1000000001


class QQPublishTests(unittest.TestCase):
    def report_bundle(self, root: Path, *, group_id=GROUP_ID, messages=4, review_window=None):
        root.mkdir(parents=True, exist_ok=True)
        window = {"start_unix": 100, "end_unix_exclusive": 200}
        for edition in ("brief", "review"):
            selected_window = review_window if edition == "review" and review_window is not None else window
            stem = f"qq-{group_id}-sample-{edition}"
            (root / f"{stem}.html").write_text(f"<html>{edition}</html>", encoding="utf-8")
            (root / f"{stem}.png").write_bytes(edition.encode())
            (root / f"{stem}.json").write_text(json.dumps({
                "chat_username": f"qq:{group_id}", "chat_display": "Synthetic fixture",
                "edition": edition, "window": selected_window,
                "range_text": "fictional", "day": "2000-01-01", "generated_at": "synthetic",
                "stats": {"messages": messages}, "topics": [{"references": ["private data"]}],
                "raw": "must not publish"}), encoding="utf-8")

    def test_bundle_contains_only_complete_matching_pair_minimal_metadata_and_hash_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "reports"
            self.report_bundle(root)
            asset = root / "assets" / ("a" * 64 + ".png")
            asset.parent.mkdir()
            asset.write_bytes(b"synthetic image")
            self.report_bundle(root, group_id=1000000099)
            self.report_bundle(root, group_id=1000000098, review_window={"start_unix": 1, "end_unix_exclusive": 2})
            destination = Path(temporary) / "stage"
            destination.mkdir()

            files = qq_publish.build_bundle(root, GROUP_ID, destination)

            self.assertEqual(len(files), 7)
            names = {path.name for path in destination.glob("*.json")}
            self.assertEqual(names, {"qq-1000000001-sample-brief.json", "qq-1000000001-sample-review.json"})
            metadata = json.loads((destination / "qq-1000000001-sample-brief.json").read_text())
            self.assertEqual(set(metadata), {"chat_username", "chat_display", "edition", "window",
                                             "range_text", "day", "generated_at", "stats"})
            self.assertEqual(metadata["stats"], {"messages": 4})
            self.assertEqual((destination / "assets" / asset.name).read_bytes(), b"synthetic image")

    def test_empty_incomplete_and_wrong_group_bundles_are_omitted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "reports"
            self.report_bundle(root, messages=0)
            for path in root.glob("*-review.*"):
                path.unlink()
            destination = Path(temporary) / "stage"
            destination.mkdir()

            self.assertEqual(qq_publish.build_bundle(root, GROUP_ID, destination), [])
            self.assertEqual(list(destination.iterdir()), [])

    def test_publish_uses_pinned_ssh_host_keys_and_skips_identical_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports, state = root / "reports", root / "state"
            self.report_bundle(reports)
            config = root / "publish.json"
            config.write_text(json.dumps({
                "group_id": GROUP_ID, "reports_dir": str(reports), "state_dir": str(state),
                "host": "qq-reports.example.invalid", "user": "qq_demo_account", "port": 2222,
                "identity_file": str(root / "id_ed25519"), "known_hosts": str(root / "known_hosts")}),
                encoding="utf-8")
            with patch.object(qq_publish.subprocess, "run") as run:
                run.return_value.returncode = 0
                result = qq_publish.publish(config)
                self.assertEqual(result["status"], "published")
                command = run.call_args.args[0]
                self.assertIn("StrictHostKeyChecking=yes", command[command.index("-e") + 1])
                self.assertIn(str(root / "known_hosts"), command[command.index("-e") + 1])
                self.assertEqual(qq_publish.publish(config)["status"], "unchanged")
                self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()

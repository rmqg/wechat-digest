import fcntl
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import publish_reports
import report_site


class PublishReportsTests(unittest.TestCase):
    def setUp(self):
        target = patch.object(report_site, 'TARGET_GROUP', '10000000000@chatroom')
        target.start()
        self.addCleanup(target.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.reports = self.runtime / "reports"
        self.reports.mkdir()
        self.output = self.runtime / "report-site"
        self.output.mkdir()
        self.config_path = self.runtime / "report-site.json"
        self.identity = self.runtime / "ssh_key"
        self.known_hosts = self.runtime / "known_hosts"
        self.identity.write_text("private key fixture")
        self.known_hosts.write_text("example.invalid ssh-ed25519 fixture")
        self._write_config()

    def _write_config(self, **overrides):
        config = {
            "host": "example.invalid",
            "user": "report-publisher",
            "port": 2222,
            "identity_file": "ssh_key",
            "known_hosts": "known_hosts",
            "remote_path": ".",
        }
        config.update(overrides)
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

    def _build_site(self, files=None, *, css=b"body{}", html=b"<main>report</main>"):
        assets = self.output / "assets"
        reports = self.output / "reports"
        assets.mkdir(exist_ok=True)
        reports.mkdir(exist_ok=True)
        (self.output / "index.html").write_bytes(b"<title>Digest</title>")
        (assets / "site.css").write_bytes(css)
        (reports / "daily.html").write_bytes(html)
        if files is None:
            files = ["assets/site.css", "reports/daily.html", "index.html"]
        return {"reports": 1, "files": files}

    def _publish(self):
        return publish_reports.publish_reports(self.config_path, root=self.root)

    def test_absent_config_disables_without_building(self):
        self.config_path.unlink()
        with patch.object(publish_reports, "build_site") as build:
            self.assertEqual(self._publish(), {"status": "disabled"})
            build.assert_not_called()

    def _real_report(self, text, stem='daily', end=200):
        end = 1792962000 + end * 86400
        metadata = {'chat_username': '10000000000@chatroom', 'one_line': text,
                    'window': {'start_unix': end - 24 * 3600, 'end_unix_exclusive': end}, 'stats': {'messages': 1}}
        (self.reports / (stem + '.json')).write_text(json.dumps(metadata))
        (self.reports / (stem + '.html')).write_text('<html><head></head><body>' + text + '</body></html>')
        (self.reports / (stem + '.png')).write_bytes(b'completed report fixture')

    def test_successful_unchanged_inputs_do_not_rebuild_or_upload(self):
        self._real_report('first')
        with patch.object(publish_reports, '_rsync_stage'):
            self.assertEqual(self._publish()['status'], 'published')
        with patch.object(publish_reports, 'build_site', side_effect=AssertionError('unchanged inputs rebuilt')), \
             patch.object(publish_reports, '_rsync_stage', side_effect=AssertionError('unchanged inputs uploaded')):
            self.assertEqual(self._publish()['status'], 'unchanged')

    def test_changed_report_and_new_target_are_published(self):
        remote = {}
        def transfer(config, output, files):
            target = remote.setdefault(config['host'], {})
            target.update({name: (output / name).read_bytes() for name in files})
        with patch.object(publish_reports, '_rsync_stage', side_effect=transfer):
            self._real_report('first')
            self._publish()
            self._real_report('changed body')
            self.assertEqual(self._publish()['status'], 'published')
            self.assertIn(b'changed body', remote['example.invalid']['reports/daily.html'])
            self._write_config(host='new.example.invalid')
            self.assertEqual(self._publish()['status'], 'published')
        self.assertEqual(remote['example.invalid'], remote['new.example.invalid'])

    def test_interrupted_upload_keeps_old_homepage_until_retry_succeeds(self):
        remote = {}
        fail_index = False
        def transfer(config, output, files):
            if fail_index and 'index.html' in files:
                raise publish_reports.PublishError('network')
            remote.update({name: (output / name).read_bytes() for name in files})
        with patch.object(publish_reports, '_rsync_stage', side_effect=transfer):
            self._real_report('first')
            self._publish()
            old_index = remote['index.html']
            self._real_report('new issue', stem='second', end=300)
            fail_index = True
            with self.assertRaises(publish_reports.PublishError):
                self._publish()
            self.assertEqual(remote['index.html'], old_index)
            self.assertFalse((self.runtime / 'report-site-state/success.json').exists())
            fail_index = False
            self.assertEqual(self._publish()['status'], 'published')
        self.assertIn(b'new issue', remote['index.html'])
        self.assertIn('reports/second.html', remote)

    def test_builder_manifest_rejects_secrets_traversal_and_non_static_files(self):
        invalid_manifests = [
            ["index.html", "../sources.json"],
            ["index.html", "sources.json"],
            ["index.html", "reports/daily.json"],
            ["index.html", "assets/sources.json"],
            ["index.html", "index.html"],
            ["reports/daily.html"],
        ]
        for files in invalid_manifests:
            with self.subTest(files=files), patch.object(
                    publish_reports, "build_site", side_effect=lambda _reports, _output, manifest=files:
                    self._build_site(files=manifest)), patch.object(publish_reports, "_rsync_stage") as stage:
                with self.assertRaises(publish_reports.PublishError):
                    self._publish()
                stage.assert_not_called()

    def test_raster_asset_names_must_be_addressable_by_origin(self):
        result = self._build_site()
        for name in ('assets/photo.png', 'assets/nested/' + 'a' * 64 + '.jpg'):
            with self.subTest(name=name):
                image = self.output / name
                image.parent.mkdir(parents=True, exist_ok=True)
                image.write_bytes(b'image fixture')
                with self.assertRaisesRegex(publish_reports.PublishError, 'not addressable by the origin'):
                    publish_reports._safe_files(self.output, [*result['files'], name])

    def test_manifest_rejects_symlink_escape(self):
        outside = self.root / "private.json"
        outside.write_text("secret")
        (self.output / "assets").mkdir()
        (self.output / "assets" / "escape.css").symlink_to(outside)
        (self.output / "index.html").write_text("index")
        files = ["assets/escape.css", "index.html"]
        with patch.object(publish_reports, "build_site", return_value={"reports": 0, "files": files}), patch.object(
                publish_reports, "_rsync_stage") as stage:
            with self.assertRaises(publish_reports.PublishError):
                self._publish()
            stage.assert_not_called()

    def test_config_rejects_command_injection_and_invalid_port(self):
        for change in ({"port": True}, {"port": 0}, {"remote_path": "../root"},
                       {"remote_path": ".;touch${IFS}bad"}, {"host": "host;command"}):
            with self.subTest(change=change):
                self._write_config(**change)
                with self.assertRaises(publish_reports.PublishError):
                    publish_reports._load_config(self.config_path)

    def test_lock_reports_busy_without_building(self):
        state_dir = self.runtime / "report-site-state"
        state_dir.mkdir()
        lock_path = state_dir / "publish.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(publish_reports, "build_site") as build:
                self.assertEqual(self._publish(), {"status": "busy"})
                build.assert_not_called()

    def test_input_stamp_tracks_only_persisted_source_media_dependencies(self):
        media = self.runtime / 'digest/images/decoded/referenced.png'
        unrelated = self.runtime / 'digest/images/decoded/unreferenced.png'
        prepared_cache = self.runtime / 'digest/prepared-image-cache.json'
        media.parent.mkdir(parents=True)
        media.write_bytes(b'referenced image')
        unrelated.write_bytes(b'unrelated image')
        prepared_cache.write_text('{"referenced": {}, "other": 1}')
        config = publish_reports._load_config(self.config_path)
        first = publish_reports._input_stamp(self.root, config, [str(media)])
        unrelated.write_bytes(b'unrelated image changed')
        prepared_cache.write_text('{"referenced": {}, "other": 2}')
        self.assertEqual(first, publish_reports._input_stamp(self.root, config, [str(media)]))
        media.write_bytes(b'referenced image changed')
        self.assertNotEqual(first, publish_reports._input_stamp(self.root, config, [str(media)]))


if __name__ == "__main__":
    unittest.main()

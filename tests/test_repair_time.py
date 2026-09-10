import json
import subprocess
import time
from unittest.mock import patch
from test_importer import Workspace, FakeServer, TRIO
from camera_importer.config import Config
from camera_importer.runner import execute
from camera_importer.repair_time import repair, parser


class RepairTest(Workspace):
    def setUp(self):
        super().setUp()
        self.server = FakeServer()
        self.config = Config(companion_root=self.root / 'companions', manifest_root=self.root / 'manifests',
                             api_url=self.server.url, api_key='test-secret', capture_timezone='America/Los_Angeles',
                             verify_timeout=0.03, poll_interval=0.005)
        self.patches = [patch('camera_importer.metadata.probe', return_value={'FileType': 'MP4'}),
                        patch('camera_importer.api.subprocess.run', return_value=subprocess.CompletedProcess([], 0))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.server.close()
        super().tearDown()

    def seed(self, names=TRIO):
        for name in names:
            self.put(name)
        result = execute(self.source, self.config)
        self.assertEqual(result['exitCode'], 0, result['errors'])
        for a in self.server.assets.values():
            a['info']['exifInfo'].update(dateTimeOriginal='2026-09-09T02:17:00Z', timeZone='UTC')
            a['info']['localDateTime'] = '2026-09-09T02:17:00Z'
        self.server.calls.clear()

    def test_preview_apply_rerun_preserve_stack_and_bytes(self):
        self.seed()
        before = {p.name: p.read_bytes() for p in self.source.iterdir()}
        stacks = dict(self.server.stacks)
        # Hashes come from the index the import left behind: originals are not re-read.
        with patch('camera_importer.repair_time.hashes', side_effect=AssertionError('no re-hash expected')):
            report = repair(self.source, self.config, time_source='filename')
        self.assertEqual(report['exitCode'], 0, report['errors'])
        self.assertEqual([r['status'] for r in report['items']], ['would_update'] * 3)
        self.assertEqual([r['hashSource'] for r in report['items']], ['index'] * 3)
        self.assertFalse(self.server.date_updates)
        self.assertFalse((self.config.manifest_root / 'time-repairs').exists())
        report = repair(self.source, self.config, True, 'filename')
        self.assertEqual(report['exitCode'], 0, report['errors'])
        self.assertEqual(len(self.server.date_updates), 3)
        self.assertTrue(all(set(b) == {'dateTimeOriginal'} for b in self.server.date_updates))
        self.assertEqual(stacks, self.server.stacks)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.source.iterdir()})
        self.assertEqual(len(self.server.uploads), 3)
        from pathlib import Path
        audit = json.loads(Path(report['auditReport']).read_text())
        self.assertTrue(all(r['before']['timeZone'] == 'UTC' for r in audit['items']))
        self.assertNotIn('test-secret', json.dumps(audit))
        report = repair(self.source, self.config, True, 'filename')
        self.assertEqual(report['exitCode'], 0)
        self.assertEqual(len(self.server.date_updates), 3)

    def test_library_copy_matches_by_name_and_size_without_hashing(self):
        import shutil
        self.seed()
        library = self.root / 'library' / '2026' / '2026-01-27'
        library.mkdir(parents=True)
        for name in TRIO:
            shutil.copyfile(self.source / name, library / name)
        with patch('camera_importer.repair_time.hashes', side_effect=AssertionError('no hashing expected')):
            report = repair(library, self.config, time_source='filename')
        self.assertEqual(report['exitCode'], 0, report['errors'])
        self.assertEqual([r['status'] for r in report['items']], ['would_update'] * 3)
        self.assertEqual([r['hashSource'] for r in report['items']], ['name+size'] * 3)
        self.assertEqual({r['assetId'] for r in report['items']}, set(self.server.assets))
        self.assertFalse(any(path == '/api/assets/bulk-upload-check' for _, path in self.server.calls))
        # Two assets with the same name and size are ambiguous: fall back to hashing.
        duplicate = dict(next(iter(self.server.assets.values()))['info'], id='11111111-1111-4111-8111-111111111111')
        self.server.assets[duplicate['id']] = {'info': duplicate, 'xmp': None, 'media': b''}
        report = repair(library, self.config, time_source='filename')
        self.assertEqual(report['exitCode'], 0, report['errors'])
        self.assertEqual([r['hashSource'] for r in report['items']], ['read', 'name+size', 'name+size'])
        # Forced checksum matching always hashes, index and name search unused.
        report = repair(library, self.config, time_source='filename', match='checksum')
        self.assertEqual(report['exitCode'], 0, report['errors'])
        self.assertEqual([r['hashSource'] for r in report['items']], ['read'] * 3)

    def test_missing_asset_aborts_all_changes(self):
        self.seed()
        self.put('DJI_20260908182440_0001_D.MP4')
        report = repair(self.source, self.config, True, 'filename')
        self.assertEqual(report['exitCode'], 1)
        self.assertFalse(self.server.date_updates)

    def test_permission_failure_journal_and_rerun(self):
        self.seed()
        self.server.reject_date_updates = True
        report = repair(self.source, self.config, True, 'filename')
        self.assertEqual(report['exitCode'], 1)
        self.assertIn('auditReport', report)
        self.server.reject_date_updates = False
        self.assertEqual(repair(self.source, self.config, True, 'filename')['exitCode'], 0)

    def test_no_false_success_when_server_dates_do_not_change(self):
        self.seed()
        self.server.ignore_date_updates = True
        report = repair(self.source, self.config, True, 'filename')
        self.assertEqual(report['exitCode'], 1)
        self.assertTrue(any('did not store' in e for e in report['errors']))

    def test_slow_metadata_job_is_a_pending_warning_not_a_per_asset_wait(self):
        self.seed()
        self.server.defer_jobs = True
        self.config.verify_timeout = 0.5  # a per-asset wait would take three times this
        started = time.monotonic()
        report = repair(self.source, self.config, True, 'filename')
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertEqual(report['exitCode'], 0, report['errors'])
        self.assertEqual([r['status'] for r in report['items']], ['dates_set_local_time_pending'] * 3)
        self.assertTrue(report['result'].startswith('DATES SET; TIMELINE REFRESH PENDING'))
        self.assertEqual(len(report['warnings']), 1)
        self.assertEqual(len(self.server.date_updates), 3)
        # Preview sees the edit already applied and only the refresh outstanding.
        report = repair(self.source, self.config, time_source='filename')
        self.assertEqual([r['status'] for r in report['items']], ['refresh_pending'] * 3)
        self.server.run_jobs()
        report = repair(self.source, self.config, True, 'filename')
        self.assertEqual(report['exitCode'], 0, report['errors'])
        self.assertEqual([r['status'] for r in report['items']], ['already_correct'] * 3)
        self.assertEqual(len(self.server.date_updates), 3)

    def test_photos_are_repaired_too(self):
        self.seed(['DJI_20260908182440_0001_D.JPG', 'DJI_20260908182440_0001_D.DNG'])
        report = repair(self.source, self.config, True, 'filename')
        self.assertEqual(report['exitCode'], 0, report['errors'])
        self.assertEqual([r['status'] for r in report['items']], ['updated_dates_verified'] * 2)
        self.assertTrue(all(r['target'] == '2026-09-08T18:24:40-07:00' for r in report['items']))
        self.assertEqual(len(self.server.date_updates), 2)
        self.assertEqual({a['info']['type'] for a in self.server.assets.values()}, {'IMAGE'})

    def test_wrong_owner_cannot_be_edited(self):
        self.seed()
        next(iter(self.server.assets.values()))['info']['ownerId'] = 'other-owner'
        report = repair(self.source, self.config, True, 'filename')
        self.assertEqual(report['exitCode'], 1)
        self.assertFalse(self.server.date_updates)

    def test_missing_timezone_blocks_apply(self):
        self.seed()
        self.config.capture_timezone = ''
        self.assertEqual(repair(self.source, self.config, True, 'filename')['exitCode'], 1)
        self.assertFalse(self.server.date_updates)

    def test_audit_failure_prevents_mutation(self):
        self.seed()
        with patch('camera_importer.repair_time.atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                repair(self.source, self.config, True, 'filename')
        self.assertFalse(self.server.date_updates)

    def test_unknown_aborts_apply(self):
        self.seed()
        self.put('future.NEW')
        report = repair(self.source, self.config, True, 'filename')
        self.assertEqual(report['exitCode'], 1)
        self.assertFalse(self.server.date_updates)

    def test_default_is_preview(self):
        self.assertFalse(parser().parse_args([]).apply)
        self.assertTrue(parser().parse_args(['--apply']).apply)

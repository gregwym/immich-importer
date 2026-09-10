import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from camera_importer.capture_time import choose, localize, time_matches
from camera_importer.model import Item, ImportFailure
from camera_importer.metadata import make_xmp
from camera_importer.cli import parser
from camera_importer.config import load_config
from unittest.mock import patch


class CaptureTimeTest(unittest.TestCase):
    def item(self, channel='10', tags=None):
        name = 'VID_20260908_182440_' + channel + '_054.insv'
        item = Item(Path(name), name, 'timeline', 'test')
        item.time_tags = tags or {}
        return item

    def test_filename_bundle_ignores_unzoned_quicktime_and_mtime(self):
        items = [self.item('00', {'CreateDate': '2026:09:09 02:17:00'}), self.item('10')]
        value, source = choose(items, 'America/Los_Angeles')
        self.assertEqual(value.isoformat(), '2026-09-08T18:24:40-07:00')
        self.assertIn('filename', source)

    def test_explicit_metadata_shared_with_missing_members(self):
        date = '2026:09:08 18:24:40-07:00'
        value, source = choose([self.item('00'), self.item('10', {'CreationDate': date})], '')
        self.assertEqual(value.isoformat(), '2026-09-08T18:24:40-07:00')
        self.assertIn('metadata', source)

    def test_missing_zone_and_conflicts_fail(self):
        with self.assertRaises(ImportFailure):
            choose([self.item()], '')
        with self.assertRaises(ImportFailure):
            choose([self.item(tags={'CreationDate': '2026:09:09 02:17:00Z'})], '')
        with self.assertRaises(ImportFailure):
            choose([self.item('00', {'CreationDate': '2026:09:08 18:24:40-07:00'}),
                    self.item('10', {'CreationDate': '2026:09:08 18:24:40+08:00'})], '')

    def test_dst_and_environment_restore(self):
        previous = os.environ.get('TZ')
        self.assertEqual(str(localize(datetime(2026, 1, 8, 18), 'America/Los_Angeles').utcoffset()), '-1 day, 16:00:00')
        for date in (datetime(2026, 11, 1, 1, 30), datetime(2026, 3, 8, 2, 30)):
            with self.assertRaises(ImportFailure):
                localize(date, 'America/Los_Angeles')
        self.assertEqual(os.environ.get('TZ'), previous)
        self.assertEqual(localize(datetime(2026, 11, 1, 1, 30), '-07:00').isoformat(), '2026-11-01T01:30:00-07:00')

    def test_zone_without_system_zoneinfo_or_tzset_uses_bundled_rules(self):
        from camera_importer import capture_time
        import time as time_module
        with patch.object(capture_time, 'ZONEINFO_ROOTS', ('/nonexistent-zoneinfo',)), \
                patch.dict(os.environ, {'TZDIR': '/nonexistent-tzdir'}), patch.object(time_module, 'tzset', None, create=True):
            self.assertEqual(capture_time.posix_rule('America/Los_Angeles'), 'PST8PDT,M3.2.0,M11.1.0')
            self.assertEqual(localize(datetime(2026, 9, 8, 18, 24, 40), 'America/Los_Angeles').isoformat(), '2026-09-08T18:24:40-07:00')
            self.assertEqual(localize(datetime(2026, 1, 8, 18), 'Asia/Shanghai').isoformat(), '2026-01-08T18:00:00+08:00')
            self.assertEqual(localize(datetime(2026, 1, 8, 18), 'PST8PDT,M3.2.0,M11.1.0').isoformat(), '2026-01-08T18:00:00-08:00')
            self.assertEqual(localize(datetime(2026, 1, 8, 18), 'Australia/Sydney').isoformat(), '2026-01-08T18:00:00+11:00')
            self.assertEqual(localize(datetime(2026, 7, 8, 18), 'Australia/Sydney').isoformat(), '2026-07-08T18:00:00+10:00')
            self.assertEqual(localize(datetime(2026, 7, 8, 18), 'Europe/London').isoformat(), '2026-07-08T18:00:00+01:00')
            with self.assertRaisesRegex(ImportFailure, 'Unknown capture timezone'):
                localize(datetime(2026, 1, 8, 18), 'Mars/Olympus_Mons')
            for date in (datetime(2026, 11, 1, 1, 30), datetime(2026, 3, 8, 2, 30)):
                with self.assertRaises(ImportFailure):
                    localize(date, 'America/Los_Angeles')
            with self.assertRaises(ImportFailure):
                localize(datetime(2026, 4, 5, 2, 30), 'Australia/Sydney')  # repeated hour
        self.assertIsNone(capture_time.posix_rule('rm -rf /'))

    def test_posix_rules_agree_with_zoneinfo_for_every_bundled_zone(self):
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            self.skipTest('zoneinfo unavailable')
        from camera_importer.capture_time import PosixZone
        from camera_importer.tzrules import RULES
        checked = 0
        # Morocco keeps explicit Ramadan transitions in the TZif body through 2087;
        # its footer ("permanent +01") is what a rule-only host can know.
        irregular = {'Africa/Casablanca', 'Africa/El_Aaiun'}
        for zone, rule in RULES.items():
            if zone in irregular:
                continue
            try:
                reference = ZoneInfo(zone)
            except Exception:
                continue
            evaluator = PosixZone(rule)
            for month in range(1, 13):
                for day, hour in ((1, 12), (15, 3), (28, 23)):
                    naive = datetime(2026, month, day, hour, 17)
                    expected = naive.replace(tzinfo=reference)
                    ours = evaluator.localize(naive)
                    if expected.utcoffset() != naive.replace(tzinfo=reference, fold=1).utcoffset():
                        # PEP 495: fold offsets differ for both repeated and skipped wall times.
                        gap = expected.astimezone(timezone.utc).astimezone(reference).replace(tzinfo=None) != naive
                        self.assertEqual(len(ours), 0 if gap else 2, (zone, rule, naive))
                        continue
                    self.assertEqual([o.utcoffset() for o in ours], [expected.utcoffset()], (zone, rule, naive))
                    checked += 1
        self.assertGreater(checked, 10000)

    def test_verifies_instant_wall_clock_and_offset(self):
        item = self.item()
        item.capture_time = '2026-09-08T18:24:40-07:00'
        info = {'exifInfo': {'dateTimeOriginal': '2026-09-09T01:24:40Z', 'timeZone': 'UTC-7'},
                'localDateTime': '2026-09-08T18:24:40Z'}
        self.assertTrue(time_matches(item, info))
        info['exifInfo']['timeZone'] = 'UTC'
        self.assertFalse(time_matches(item, info))
        info['exifInfo']['timeZone'] = 'America/Los_Angeles'
        self.assertTrue(time_matches(item, info))
        info['localDateTime'] = '2026-09-09T02:17:00Z'
        self.assertFalse(time_matches(item, info))

    def test_xmp_preserves_unrelated_fields(self):
        original = b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" dc:description="keep"/></rdf:RDF>'
        output = make_xmp({'make': 'DJI', 'model': 'DJI OsmoPocket3'}, original, '2026-09-08T18:24:40-07:00')
        self.assertIn(b'18:24:40-07:00', output)
        self.assertIn(b'keep', output)

    def test_cli_timezone_overrides_environment(self):
        with patch.dict(os.environ, {'CAMERA_CAPTURE_TIMEZONE': 'UTC'}):
            config = load_config(parser().parse_args(['--capture-timezone', 'America/Los_Angeles']))
        self.assertEqual(config.capture_timezone, 'America/Los_Angeles')

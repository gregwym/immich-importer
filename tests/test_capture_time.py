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
        self.assertEqual(choose([self.item()], ''), (None, 'no timezone evidence'))
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

    def photo(self, tags=None):
        name = 'DJI_20260908182440_0001_D.JPG'
        item = Item(Path(name), name, 'timeline', 'test')
        item.time_tags = tags or {}
        return item

    def test_bare_exif_is_respected_and_only_a_shift_changes_it(self):
        from datetime import timedelta
        bare = {'DateTimeOriginal': '2026:09:08 18:24:40'}
        self.assertEqual(choose([self.photo(bare)], 'America/Los_Angeles'), (None, 'metadata:naive:respected'))
        value, source = choose([self.photo(bare)], 'America/Los_Angeles', timedelta(hours=1))
        self.assertEqual((value.isoformat(), value.tzinfo), ('2026-09-08T19:24:40', None))
        self.assertIn('naive:shifted', source)
        # A photo without any EXIF date falls back to the filename clock.
        value, source = choose([self.photo()], 'America/Los_Angeles')
        self.assertEqual(value.isoformat(), '2026-09-08T18:24:40-07:00')

    def test_utc_clock_jitter_and_dst_blind_cameras(self):
        from datetime import timedelta
        from camera_importer.capture_time import utc_clock_offset
        def video(stamp, name='VID_20240315_181418_10_004.mp4'):
            item = Item(Path(name), name, 'timeline', 'test')
            item.time_tags = {'CreateDate': stamp}
            return item
        # One second of jitter between the two clocks still proves -08:00.
        self.assertEqual(utc_clock_offset(video('2024:03:16 02:14:19')), timedelta(hours=-8))
        self.assertIsNone(utc_clock_offset(video('2024:03:16 02:19:19')))  # 5 minutes off: no evidence
        # 15 March 2024 is PDT, but the Pocket 3's "UTC" uses fixed PST: the configured zone wins.
        value, source = choose([video('2024:03:16 02:14:19')], 'America/Los_Angeles')
        self.assertEqual(value.isoformat(), '2024-03-15T18:14:18-07:00')
        self.assertIn('ignores DST', source)
        # Same file without a configured zone: the file's own offset is all there is.
        value, source = choose([video('2024:03:16 02:14:19')], '')
        self.assertEqual((value.isoformat(), source), ('2024-03-15T18:14:18-08:00', 'metadata:utc-vs-filename'))
        # A genuinely different offset (shot in Tokyo) beats the configured home zone.
        value, source = choose([video('2024:03:15 09:14:18')], 'America/Los_Angeles')
        self.assertEqual(value.isoformat(), '2024-03-15T18:14:18+09:00')
        self.assertIn('differs from configured timezone', source)
        # Agreement in winter: plain evidence.
        value, source = choose([video('2024:01:16 02:14:19', 'VID_20240115_181418_10_004.mp4')], 'America/Los_Angeles')
        self.assertEqual((value.isoformat(), source), ('2024-01-15T18:14:18-08:00', 'metadata:utc-vs-filename'))

    def test_utc_create_date_proves_the_offset_for_videos(self):
        value, source = choose([self.item('00', {'CreateDate': '2026:09:09 01:24:40'})], '')
        self.assertEqual((value.isoformat(), source), ('2026-09-08T18:24:40-07:00', 'metadata:utc-vs-filename'))
        # Camera wrote local time into the UTC field: proves nothing, configured zone applies.
        value, source = choose([self.item('00', {'CreateDate': '2026:09:08 18:24:40'})], 'Asia/Shanghai')
        self.assertEqual((value.isoformat(), source), ('2026-09-08T18:24:40+08:00', 'filename + configured timezone'))
        self.assertEqual(choose([self.item('00', {'CreateDate': '2026:09:08 18:24:40'})], ''), (None, 'no timezone evidence'))
        with self.assertRaises(ImportFailure):
            choose([self.item('00', {'CreateDate': '2026:09:09 01:24:40'}), self.item('10', {'CreateDate': '2026:09:09 02:24:40'})], '')

    def test_clock_shift_parsing_and_application(self):
        from datetime import timedelta
        from camera_importer.capture_time import parse_shift, describe_shift
        self.assertEqual(parse_shift('221d00:34:12'), timedelta(days=221, minutes=34, seconds=12))
        self.assertEqual(parse_shift('-1h30m'), -timedelta(hours=1, minutes=30))
        self.assertEqual(parse_shift('P221DT34M12S'), timedelta(days=221, minutes=34, seconds=12))
        self.assertEqual(parse_shift('45s'), timedelta(seconds=45))
        self.assertIsNone(parse_shift(''))
        self.assertEqual(describe_shift(timedelta(days=221, minutes=34, seconds=12)), '+221d00:34:12')
        with self.assertRaises(ImportFailure):
            parse_shift('yesterday')
        shift = timedelta(days=221, minutes=34, seconds=12)
        # Filename clock: shifted before the timezone is applied (DST differs between Jan and Sep).
        value, source = choose([self.item('00'), self.item('10')], 'America/Los_Angeles', shift)
        self.assertEqual(value.isoformat(), '2027-04-17T18:58:52-07:00')
        self.assertIn('clock shifted +221d00:34:12', source)
        # Zoned metadata: same camera clock, same shift.
        value, _ = choose([self.item('10', {'CreationDate': '2026:09:08 18:24:40-07:00'})], '', shift)
        self.assertEqual(value.isoformat(), '2027-04-17T18:58:52-07:00')

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

"""Explicit video capture dates. Never infer capture time from host TZ or mtime."""
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import ImportFailure

TIME_TAGS = ('DateTimeOriginal', 'SubSecDateTimeOriginal', 'CreationDate',
             'OffsetTimeOriginal', 'CreateDate', 'MediaCreateDate', 'TrackCreateDate')


def parse_date(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r'^(\d{4}):(\d{2}):(\d{2})', r'\1-\2-\3', value)
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.year >= 1970 else None
    except ValueError:
        return None


def localize(value, zone):
    if not zone:
        raise ImportFailure('NEEDS REVIEW: capture timezone missing; set --capture-timezone')
    if zone in ('UTC', 'Z') or re.fullmatch(r'[+-]\d{2}:\d{2}', zone):
        offset = 0
        if zone not in ('UTC', 'Z'):
            hours, minutes = map(int, zone[1:].split(':'))
            if hours > 14 or minutes > 59 or (hours == 14 and minutes):
                raise ImportFailure('Invalid capture timezone offset')
            offset = (hours * 60 + minutes) * (1 if zone[0] == '+' else -1)
        return value.replace(tzinfo=timezone(timedelta(minutes=offset)))
    # Python 3.8 has no zoneinfo. DSM/Linux libc uses the installed zoneinfo DB.
    # This importer is single-threaded; restore TZ even on conversion failure.
    if not re.fullmatch(r'[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)+', zone):
        raise ImportFailure('Invalid IANA capture timezone')
    roots = ('/usr/share/zoneinfo', '/usr/share/zoneinfo.default', '/usr/lib/zoneinfo')
    zonefile = next((str(Path(root) / zone) for root in roots if (Path(root) / zone).is_file()), None)
    if not zonefile or not hasattr(time, 'tzset'):
        raise ImportFailure('Capture timezone unavailable in system zoneinfo: ' + zone)
    previous = os.environ.get('TZ')
    candidates = {}
    try:
        os.environ['TZ'] = ':' + zonefile
        time.tzset()
        for dst in (-1, 0, 1):
            stamp = time.mktime(value.timetuple()[:8] + (dst,))
            wall = datetime.fromtimestamp(stamp)
            if wall == value.replace(microsecond=0):
                utc = datetime.fromtimestamp(stamp, timezone.utc).replace(tzinfo=None)
                delta = wall - utc
                candidates[stamp] = value.replace(tzinfo=timezone(delta))
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()
    if len(candidates) != 1:
        raise ImportFailure('NEEDS REVIEW: ambiguous/nonexistent DST capture time; supply explicit UTC offset')
    return next(iter(candidates.values()))


def filename_date(item):
    match = re.match(r'^(?:DJI_|(?:PRO_)?(?:VID|LRV)_)(\d{8})_?(\d{6})_', item.path.name, re.I)
    return datetime.strptime(''.join(match.groups()), '%Y%m%d%H%M%S') if match else None


def explicit_date(tags):
    for key in ('SubSecDateTimeOriginal', 'DateTimeOriginal', 'CreationDate'):
        value = parse_date(tags.get(key))
        if value:
            if value.tzinfo is None and key != 'CreationDate' and tags.get('OffsetTimeOriginal'):
                value = localize(value, str(tags['OffsetTimeOriginal']))
            if value.tzinfo is not None:
                return value, 'metadata:' + key
    return None


def choose(members, zone):
    dates = []
    for item in members:
        selected = explicit_date(item.time_tags)
        if selected:
            dates.append(selected)
    if dates:
        value, source = dates[0]
        if any(abs((other - value).total_seconds()) > 1 or other.utcoffset() != value.utcoffset()
               for other, _ in dates[1:]):
            raise ImportFailure('NEEDS REVIEW: conflicting capture timestamps in 360 bundle')
        # Filename is a local clock cross-check, not a second UTC timestamp.
        for item in members:
            named = filename_date(item)
            if named and abs((named - value.replace(tzinfo=None)).total_seconds()) > 2:
                raise ImportFailure('NEEDS REVIEW: capture metadata conflicts with filename clock')
        return value, source
    named = filename_date(members[0])
    if named is None:
        raise ImportFailure('NEEDS REVIEW: no reliable video capture date')
    return localize(named, zone), 'filename + configured timezone'


def time_matches(item, info):
    if not item.capture_time:
        return True
    expected = parse_date(item.capture_time)
    exif = info.get('exifInfo') or {}
    actual = parse_date(exif.get('dateTimeOriginal'))
    local = parse_date(info.get('localDateTime'))
    zone = exif.get('timeZone')
    if not actual or actual.tzinfo is None or not local or not zone:
        return False
    try:
        normalized = re.sub(r'^UTC', '', zone)
        if normalized in ('', '+0', '+00', '+00:00'):
            normalized = 'UTC'
        elif re.fullmatch(r'[+-]\d{1,2}(?::\d{2})?', normalized):
            sign, parts = normalized[0], normalized[1:].split(':')
            normalized = sign + parts[0].zfill(2) + ':' + (parts[1] if len(parts) == 2 else '00')
        offset = localize(expected.replace(tzinfo=None), normalized).utcoffset()
    except (ImportFailure, ValueError, OverflowError):
        return False
    return (abs((actual - expected).total_seconds()) < 1 and offset == expected.utcoffset()
            and abs((local.replace(tzinfo=None) - expected.replace(tzinfo=None)).total_seconds()) < 1)

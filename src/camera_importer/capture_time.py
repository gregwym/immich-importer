"""Explicit video capture dates. Never infer capture time from host TZ or mtime."""
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import ImportFailure
from .tzrules import RULES

TIME_TAGS = ('DateTimeOriginal', 'SubSecDateTimeOriginal', 'CreationDate',
             'OffsetTimeOriginal', 'CreateDate', 'MediaCreateDate', 'TrackCreateDate')
ZONEINFO_ROOTS = ('/usr/share/zoneinfo', '/usr/share/zoneinfo.default', '/usr/lib/zoneinfo', '/usr/share/lib/zoneinfo')
NAME = r'(?:<[A-Za-z0-9+-]+>|[A-Za-z]{3,})'
OFFSET = r'[+-]?\d{1,3}(?::\d{1,2}(?::\d{1,2})?)?'
DATE = r'(?:J\d{1,3}|\d{1,3}|M\d{1,2}\.\d\.\d)'
POSIX_RULE = re.compile('^(' + NAME + ')(' + OFFSET + ')(?:(' + NAME + ')(' + OFFSET + ')?,(' + DATE + ')(?:/(' + OFFSET + '))?,('
                        + DATE + ')(?:/(' + OFFSET + '))?)?$')


def tzif_footer(path):
    """Current POSIX rule stored at the end of a TZif v2+ file, or None."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if not data.startswith(b'TZif') or not data.endswith(b'\n'):
        return None
    footer = data.rstrip(b'\n').rsplit(b'\n', 1)[-1]
    return footer.decode('ascii', 'replace') if footer and not footer.startswith(b'TZif') else None


def posix_rule(zone):
    """POSIX TZ rule for an IANA name or an explicit rule; None if unknown.

    Pure Python: DSM ships neither a zoneinfo database nor a Python built with
    time.tzset. A system TZif file's footer is preferred (newest tzdata), then
    the bundled table, then a rule given directly.
    """
    if re.fullmatch(r'[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)+', zone):
        roots = ([os.environ['TZDIR']] if os.environ.get('TZDIR') else []) + list(ZONEINFO_ROOTS)
        for root in roots:
            if (Path(root) / zone).is_file():
                footer = tzif_footer(Path(root) / zone)
                if footer and POSIX_RULE.match(footer):
                    return footer
        return RULES.get(zone)
    return zone if POSIX_RULE.match(zone) else None


def _seconds(text, default=0):
    if text is None:
        return default
    sign = -1 if text.startswith('-') else 1
    parts = [int(x) for x in text.lstrip('+-').split(':')]
    return sign * (parts[0] * 3600 + (parts[1] if len(parts) > 1 else 0) * 60 + (parts[2] if len(parts) > 2 else 0))


def _rule_date(spec, year):
    """Calendar date of a POSIX transition spec (Jn, n or Mm.w.d) in a year."""
    if spec.startswith('M'):
        month, week, weekday = (int(x) for x in spec[1:].split('.'))
        first = datetime(year, month, 1)
        # POSIX weekday 0 = Sunday; Python Monday = 0.
        day = 1 + (weekday - (first.weekday() + 1) % 7) % 7 + (week - 1) * 7
        days_in_month = (datetime(year + (month == 12), month % 12 + 1, 1) - first).days
        while day > days_in_month:
            day -= 7
        return datetime(year, month, day)
    if spec.startswith('J'):
        day = int(spec[1:])
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return datetime(year, 1, 1) + timedelta(days=day - 1 + (1 if leap and day >= 60 else 0))
    return datetime(year, 1, 1) + timedelta(days=int(spec))


class PosixZone:
    """Offsets of a POSIX TZ rule, computed without libc or tzdata."""
    def __init__(self, rule):
        match = POSIX_RULE.match(rule)
        if not match:
            raise ImportFailure('Invalid POSIX timezone rule: ' + rule)
        std_name, std, dst_name, dst, start, start_time, end, end_time = match.groups()
        # POSIX offsets are west-positive; store seconds east of UTC.
        self.std = -_seconds(std)
        self.dst = (-_seconds(dst) if dst else self.std + 3600) if dst_name else None
        self.start, self.start_time = start, _seconds(start_time, 7200)
        self.end, self.end_time = end, _seconds(end_time, 7200)

    def transitions(self, year):
        # Start is given in standard local time, end in daylight local time.
        start = _rule_date(self.start, year) + timedelta(seconds=self.start_time - self.std)
        end = _rule_date(self.end, year) + timedelta(seconds=self.end_time - self.dst)
        return start, end

    def is_dst(self, utc):
        if self.dst is None:
            return False
        year = (utc + timedelta(seconds=self.std)).year
        start, end = self.transitions(year)
        if start < end:
            return start <= utc < end
        return not (end <= utc < start)

    def localize(self, naive):
        """Zoned datetimes for a wall-clock time; two if ambiguous, none if skipped."""
        offsets = [self.std] if self.dst is None or self.dst == self.std else [self.std, self.dst]
        found = []
        for offset in offsets:
            utc = naive - timedelta(seconds=offset)
            if self.is_dst(utc) == (offset == self.dst and len(offsets) > 1):
                found.append(naive.replace(tzinfo=timezone(timedelta(seconds=offset))))
        return found


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
    rule = posix_rule(zone)
    if rule is None:
        raise ImportFailure('Unknown capture timezone: ' + zone + ' (use an IANA name such as America/Los_Angeles, '
                            'a POSIX rule such as PST8PDT,M3.2.0,M11.1.0, or a fixed offset such as -07:00)')
    candidates = PosixZone(rule).localize(value.replace(microsecond=0))
    if len(candidates) != 1:
        raise ImportFailure('NEEDS REVIEW: ambiguous/nonexistent DST capture time; supply explicit UTC offset')
    return value.replace(tzinfo=candidates[0].tzinfo)


def filename_date(item):
    match = re.match(r'^(?:DJI_|(?:PRO_)?(?:VID|LRV|IMG)_)(\d{8})_?(\d{6})_', item.path.name, re.I)
    return datetime.strptime(''.join(match.groups()), '%Y%m%d%H%M%S') if match else None


PHOTO_SUFFIXES = ('.jpg', '.jpeg', '.dng', '.insp')


def explicit_date(tags):
    """A date the file states together with its timezone."""
    for key in ('SubSecDateTimeOriginal', 'DateTimeOriginal', 'CreationDate'):
        value = parse_date(tags.get(key))
        if value:
            if value.tzinfo is None and key != 'CreationDate' and tags.get('OffsetTimeOriginal'):
                value = localize(value, str(tags['OffsetTimeOriginal']))
            if value.tzinfo is not None:
                return value, 'metadata:zoned:' + key
    return None


def naive_exif_date(item):
    """EXIF DateTimeOriginal without an offset (EXIF < 2.31 or camera omits it)."""
    if item.path.suffix.lower() not in PHOTO_SUFFIXES:
        return None
    for key in ('SubSecDateTimeOriginal', 'DateTimeOriginal'):
        value = parse_date(item.time_tags.get(key))
        if value and value.tzinfo is None:
            return value
    return None


def utc_clock_offset(item):
    """Offset the file proves itself: QuickTime CreateDate is UTC, the filename is the local clock.

    Returns a timedelta when the difference is a plausible timezone offset
    (multiple of 15 minutes, within +-14h, not zero: a zero difference means the
    camera wrote local time into the UTC field and proves nothing).
    """
    named = filename_date(item)
    for key in ('CreateDate', 'MediaCreateDate', 'TrackCreateDate'):
        stamp = parse_date(item.time_tags.get(key))
        if named is None or stamp is None or stamp.tzinfo is not None:
            continue
        seconds = round((named - stamp).total_seconds())
        # The two clocks are written a second or so apart; snap to the 15-minute grid.
        snapped = round(seconds / 900) * 900
        if snapped and abs(seconds - snapped) <= 10 and abs(snapped) <= 14 * 3600:
            return timedelta(seconds=snapped)
    return None


def from_utc(utc, zone):
    """Zoned local datetime of a UTC instant in an IANA zone, POSIX rule or fixed offset."""
    if zone in ('UTC', 'Z') or re.fullmatch(r'[+-]\d{2}:\d{2}', zone):
        return utc.astimezone(localize(datetime(2000, 1, 1), zone).tzinfo)
    rule = posix_rule(zone)
    if rule is None:
        raise ImportFailure('Unknown capture timezone: ' + zone)
    parsed = PosixZone(rule)
    naive = utc.replace(tzinfo=None)
    offset = parsed.dst if parsed.dst is not None and parsed.is_dst(naive) else parsed.std
    return (naive + timedelta(seconds=offset)).replace(tzinfo=timezone(timedelta(seconds=offset)))


def standard_offset(zone):
    """The zone's standard (non-DST) offset, or None for fixed offsets and unknown zones."""
    rule = posix_rule(zone) if zone and not re.fullmatch(r'[+-]\d{2}:\d{2}', zone) and zone not in ('UTC', 'Z') else None
    if not rule:
        return None
    parsed = PosixZone(rule)
    return timedelta(seconds=parsed.std) if parsed.dst is not None else None


SHIFT = re.compile(r'^([+-])?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$')
SHIFT_CLOCK = re.compile(r'^([+-])?(?:(\d+)d)?(\d{1,3}):(\d{2})(?::(\d{2}))?$')
SHIFT_ISO = re.compile(r'^([+-])?P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$')


def parse_shift(text):
    """Camera clock correction: 221d00:34:12, -1h30m, 45s, P221DT34M12S. None when empty."""
    if text is None or not str(text).strip():
        return None
    text = str(text).strip()
    for pattern in (SHIFT_CLOCK, SHIFT, SHIFT_ISO):
        match = pattern.match(text)
        if match and any(g for g in match.groups()[1:]):
            sign = -1 if match.group(1) == '-' else 1
            parts = [int(g or 0) for g in match.groups()[1:]]
            if pattern is SHIFT_CLOCK:
                days, hours, minutes, seconds = parts[0], parts[1], parts[2], parts[3]
            else:
                days, hours, minutes, seconds = parts
            return sign * timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    raise ImportFailure('Invalid clock shift: ' + text + ' (use e.g. 221d00:34:12, -1h30m or P221DT34M12S)')


def describe_shift(shift):
    sign = '-' if shift < timedelta(0) else '+'
    total = int(abs(shift).total_seconds())
    days, rest = divmod(total, 86400)
    return sign + str(days) + 'd' + '%02d:%02d:%02d' % (rest // 3600, rest % 3600 // 60, rest % 60)


def choose(members, zone, shift=None):
    """Capture instant for one file or 360 bundle: (datetime or None, source).

    What the file states is authoritative, in this order: a zoned date; a bare
    EXIF date (respected as written, so None: nothing to deliver); a UTC
    QuickTime clock whose difference from the filename clock proves the offset;
    the filename clock interpreted in the configured timezone. A file that
    states nothing about its timezone and has no configured one is left to
    Immich (None). `shift` corrects a wrong camera clock and is the only way a
    stated wall-clock time is ever changed.
    """
    shift = shift or timedelta(0)
    dates = [explicit_date(i.time_tags) for i in members]
    dates = [d for d in dates if d]
    if dates:
        value, source = dates[0]
        if any(abs((other - value).total_seconds()) > 1 or other.utcoffset() != value.utcoffset()
               for other, _ in dates[1:]):
            raise ImportFailure('NEEDS REVIEW: conflicting capture timestamps in 360 bundle')
        for item in members:
            named = filename_date(item)
            if named and abs((named - value.replace(tzinfo=None)).total_seconds()) > 2:
                raise ImportFailure('NEEDS REVIEW: capture metadata conflicts with filename clock')
        return value + shift, _shifted(source, shift)
    naive = [naive_exif_date(i) for i in members]
    if any(naive):
        if not shift:
            return None, 'metadata:naive:respected'
        return next(n for n in naive if n) + shift, 'metadata:naive:shifted ' + describe_shift(shift)
    offsets = {utc_clock_offset(i) for i in members} - {None}
    if len(offsets) > 1:
        raise ImportFailure('NEEDS REVIEW: members of one 360 bundle imply different UTC offsets')
    named = filename_date(members[0])
    if named is None:
        return None, 'no camera clock'
    if offsets:
        offset = offsets.pop()
        if zone:
            try:
                configured = localize(named + shift, zone)
            except ImportFailure:
                configured = None
            if configured is not None and configured.utcoffset() != offset:
                if offset == standard_offset(zone):
                    # The camera was synced before DST began and never moved its
                    # displayed clock: its UTC stayed correct, the filename clock lags
                    # an hour. Trust the file's UTC and express it in the configured zone.
                    utc = (named + shift).replace(tzinfo=timezone(offset)).astimezone(timezone.utc)
                    return from_utc(utc, zone), _shifted('metadata:utc + configured timezone (camera clock not adjusted for DST)', shift)
                # A genuinely different offset: the file was shot elsewhere; the file wins.
                return (named + shift).replace(tzinfo=timezone(offset)), _shifted('metadata:utc-vs-filename (differs from configured timezone)', shift)
        return (named + shift).replace(tzinfo=timezone(offset)), _shifted('metadata:utc-vs-filename', shift)
    if zone:
        return localize(named + shift, zone), _shifted('filename + configured timezone', shift)
    return None, 'no timezone evidence'


def _shifted(source, shift):
    return source + ('; camera clock shifted ' + describe_shift(shift) if shift else '')


def zone_offset(zone, expected):
    """UTC offset Immich's timeZone string implies at the expected wall time, or None."""
    try:
        normalized = re.sub(r'^UTC', '', zone)
        if normalized in ('', '+0', '+00', '+00:00'):
            normalized = 'UTC'
        elif re.fullmatch(r'[+-]\d{1,2}(?::\d{2})?', normalized):
            sign, parts = normalized[0], normalized[1:].split(':')
            normalized = sign + parts[0].zfill(2) + ':' + (parts[1] if len(parts) == 2 else '00')
        return localize(expected.replace(tzinfo=None), normalized).utcoffset()
    except (ImportFailure, ValueError, OverflowError):
        return None


def exif_matches(item, info):
    """dateTimeOriginal instant and timeZone offset: updated synchronously by a date edit."""
    if not item.capture_time:
        return True
    expected = parse_date(item.capture_time)
    exif = info.get('exifInfo') or {}
    actual = parse_date(exif.get('dateTimeOriginal'))
    if not actual or actual.tzinfo is None:
        return False
    if expected.tzinfo is None:
        # A bare wall-clock time: Immich stores it as UTC and keeps no timezone.
        return abs((actual - expected.replace(tzinfo=timezone.utc)).total_seconds()) < 1
    zone = exif.get('timeZone')
    return bool(zone) and abs((actual - expected).total_seconds()) < 1 and zone_offset(zone, expected) == expected.utcoffset()


def local_matches(item, info):
    """localDateTime (timeline day/hour): refreshed by Immich's metadata job after SidecarWrite."""
    if not item.capture_time or parse_date(item.capture_time).tzinfo is None:
        return True
    expected = parse_date(item.capture_time)
    local = parse_date(info.get('localDateTime'))
    return bool(local) and abs((local.replace(tzinfo=None) - expected.replace(tzinfo=None)).total_seconds()) < 1


def time_matches(item, info):
    return exif_matches(item, info) and local_matches(item, info)


def datable(item):
    """Camera photo or video whose name carries the camera clock."""
    return item.route in ('timeline', 'probe') and filename_date(item) is not None


def supplied_by_file(origin):
    """True when Immich extracts the same value itself (zoned metadata): nothing to deliver."""
    return origin.startswith('metadata:zoned')


def plan_dates(items, zone):
    """One shared decision for import and repair, photos and videos alike.

    Members of a 360 bundle share one date. Returns {item: (datetime, origin)}
    and raises per group; the caller records failures for the whole group.
    """
    groups = {}
    for item in items:
        groups.setdefault(item.bundle or item.relative, []).append(item)
    return [(members, choose(members, zone)) for members in groups.values()]

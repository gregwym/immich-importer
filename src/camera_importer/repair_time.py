"""Repair existing video dates via Immich's supported date-edit/sidecar workflow."""
import argparse
import base64
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import __version__, hashcache
from .api import Immich
from .capture_time import TIME_TAGS, choose, exif_matches, filename_date
from .config import authenticate, load_config
from .files import atomic_json, changed, hashes, locked, validate_roots
from .metadata import probe, probe_batch
from .model import ImportFailure
from .scan import scan


def parser():
    p = argparse.ArgumentParser(description='Repair capture dates of imported camera photos and videos; defaults to read-only preview')
    p.add_argument('path', nargs='?', default='.', help='Original media directory; assets matched by checksum')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Apply reviewed date changes through the Immich API')
    mode.add_argument('--dry-run', action='store_true', help='Read-only preview (default); connects to Immich')
    p.add_argument('--time-source', choices=('auto', 'filename'), default='auto',
                   help='auto: reliable zoned metadata then filename; filename: explicitly disregard embedded dates')
    p.add_argument('--capture-timezone', help='Shooting timezone for filename dates, e.g. America/Los_Angeles')
    p.add_argument('--match', choices=('auto', 'checksum'), default='auto',
                   help='auto: hash index, then a unique Immich asset with the same file name and size, then hashing; checksum: always hash')
    p.add_argument('--config')
    p.add_argument('--manifest-root', help='Parent directory for durable time-repairs audit reports')
    p.add_argument('--api-url')
    p.add_argument('--auth-dir')
    p.add_argument('--verify-timeout', type=float)
    p.add_argument('--json', action='store_true')
    p.add_argument('--verbose', action='store_true')
    p.add_argument('--quiet', action='store_true')
    p.add_argument('--version', action='version', version=__version__)
    return p


def dates(info):
    exif = info.get('exifInfo') or {}
    return {
        'dateTimeOriginal': exif.get('dateTimeOriginal'), 'timeZone': exif.get('timeZone'),
        'localDateTime': info.get('localDateTime'), 'fileCreatedAt': info.get('fileCreatedAt')}


def unaffected(info):
    exif = info.get('exifInfo') or {}
    return {'visibility': info.get('visibility'), 'stack': info.get('stack'),
            'camera': {k: exif.get(k) for k in ('make', 'model', 'lensModel')},
            'location': {k: exif.get(k) for k in ('latitude', 'longitude')},
            'description': exif.get('description')}


def kind(item):
    return 'VIDEO' if item.path.suffix.lower() in ('.mp4', '.insv') else 'IMAGE'


def find_by_name(api, item):
    """Unique existing asset with the same original file name, size and type, else None."""
    candidates = [a for a in api.search_by_name(item.path.name)
                  if a.get('type') == kind(item) and a.get('isTrashed') is False
                  and (a.get('exifInfo') or {}).get('fileSizeInByte') == item.size]
    if len(candidates) != 1:
        return None
    asset = candidates[0]
    try:
        checksum = base64.b64decode(asset['checksum'], validate=True).hex()
    except (KeyError, ValueError, TypeError):
        return None
    return asset, checksum


def repair(source, config, apply=False, time_source='auto', log=lambda s: None, match='auto'):
    validate_roots(source, config.companion_root, config.manifest_root)
    plan = scan(source)
    report = {'source': str(source), 'apply': apply, 'timeSource': time_source, 'match': match,
              'captureTimezone': config.capture_timezone, 'items': [], 'errors': list(plan.errors),
              'xmpPersistence': 'Immich queues SidecarWrite after date edits; no independent completion receipt'}
    report['errors'].extend('UNKNOWN FILE: ' + i.relative for i in plan.unknown)
    # Camera photos and videos with a clock in their name (DJI_, VID_/LRV_, IMG_).
    media = [i for i in plan.items if i.route in ('timeline', 'probe') and filename_date(i) is not None]
    if not media:
        report['errors'].append('No recognized camera photos or videos in source')
    report['warnings'] = []
    groups = {}
    tags_by_path = probe_batch([i.path for i in media], config.exiftool_bin) if time_source == 'auto' and media else {}
    for item in media:
        row = {'path': item.relative, 'assetId': None, 'status': 'planned'}
        report['items'].append(row)
        groups.setdefault(item.bundle or item.relative, []).append((item, row))
        try:
            if item.error:
                raise ImportFailure(item.error)
            log('Read capture metadata: ' + item.relative)
            tags = tags_by_path.get(item.path)
            if time_source == 'auto':
                tags = tags if tags is not None else probe(item.path, config.exiftool_bin)
                item.time_tags = {k: tags[k] for k in TIME_TAGS if tags.get(k)}
            if changed(item.path, item.fingerprint):
                raise ImportFailure('Source changed during metadata read')
        except (ImportFailure, OSError, ValueError) as error:
            row.update(status='failed', error=str(error))
    for members in groups.values():
        try:
            if any(row['status'] == 'failed' for _, row in members):
                raise ImportFailure('Bundle member cannot be reviewed')
            value, origin = choose([i for i, _ in members], config.capture_timezone)
            for item, row in members:
                item.capture_time = value.isoformat()
                row.update(target=item.capture_time, timeSource=origin)
        except (ImportFailure, OSError, ValueError) as error:
            for _, row in members:
                row.update(status='failed', error=str(error))
    authenticate(config)
    api = Immich(config, log)
    api.preflight()
    report.update(server=config.api_url, ownerId=api.owner_id)
    pairs = list(zip(media, report['items']))
    # Identify each asset with the cheapest reliable evidence: the hash index
    # (same file seen before), then a unique Immich asset with the same original
    # file name and byte size (the usual case for Immich's own library copy, where
    # hashing would only compare Immich's file with itself), and only then hashing.
    now = datetime.now(timezone.utc)
    with locked(config.manifest_root):
        index = hashcache.load(config.manifest_root)
        for item, row in pairs:
            if row['status'] == 'failed':
                continue
            try:
                cached = hashcache.lookup(index, item) if match == 'auto' else None
                found = None
                if cached:
                    item.sha1, item.sha256 = cached
                    item.hash_source = 'index'
                elif match == 'auto' and (found := find_by_name(api, item)):
                    asset, checksum = found
                    item.asset_id, item.sha1, item.hash_source = asset['id'], checksum, 'name+size'
                    log('Matched by name and size: ' + item.relative)
                else:
                    log('Hash original: ' + item.relative)
                    item.sha1, item.sha256 = hashes(item.path, item.fingerprint)
                    item.hash_source = 'read'
                    hashcache.record(index, item, now)
                row.update(sha1=item.sha1, sha256=item.sha256 or None, hashSource=item.hash_source)
            except (ImportFailure, OSError, ValueError) as error:
                row.update(status='failed', error=str(error))
        try:
            hashcache.save(config.manifest_root, index, now)
        except (OSError, ImportFailure):
            report['errors'].append('Failed to persist hash index')
    eligible = [(i, r) for i, r in pairs if r['status'] != 'failed']
    matches = api.check([i for i, _ in eligible if not i.asset_id])
    seen = {}
    for item, row in eligible:
        try:
            if not item.asset_id:
                found = matches.get(item.relative)
                if not found:
                    raise ImportFailure('No existing asset matches source checksum; nothing uploaded')
                item.asset_id, trashed = found
                if trashed:
                    raise ImportFailure('Matching asset is in trash')
            row['assetId'] = item.asset_id
            info = api.info(item.asset_id)
            api.validate_identity(item, info)
            if info.get('type') != kind(item):
                raise ImportFailure('Matching asset is not a ' + kind(item).lower())
            row.update(before=dates(info), preserved=unaffected(info))
            if item.asset_id in seen:
                previous = seen[item.asset_id]
                if previous['target'] != row['target']:
                    previous.update(status='failed', error='Same asset has conflicting capture targets')
                    raise ImportFailure('Same asset has conflicting capture targets')
                row.update(status='same_asset', duplicateOf=previous['path'])
            else:
                seen[item.asset_id] = row
                row['status'] = 'already_correct' if exif_matches(item, info) else 'would_update'
        except (ImportFailure, OSError, ValueError) as error:
            row.update(status='failed', error=str(error))
    report['errors'].extend(r['path'] + ': ' + r['error'] for r in report['items'] if r.get('error'))
    # Complete the review before the first API mutation. Ambiguous/missing assets
    # abort the whole apply, avoiding a partially reviewed repair batch.
    report['result'] = 'PREVIEW OK' if not report['errors'] else 'NEEDS REVIEW'
    report['exitCode'] = 1 if report['errors'] else 0
    if not apply or report['errors']:
        return report
    journal = config.manifest_root / 'time-repairs' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex + '.json')
    report.update(auditReport=str(journal), result='IN PROGRESS', exitCode=1)
    atomic_json(journal, report)  # Write before any date mutation, including old values.
    # One PUT per asset is the whole repair: Immich stores dateTimeOriginal and
    # timeZone synchronously and locks them. Everything after that is Immich's
    # own asynchronous work (SidecarWrite, metadata refresh of localDateTime and,
    # with a date-based storage template, moving the file to its new folder).
    for item, row in pairs:
        if row['status'] != 'would_update':
            continue
        try:
            if changed(item.path, item.fingerprint):
                raise ImportFailure('Source changed after planning')
            current = api.info(item.asset_id)
            api.validate_identity(item, current)
            if dates(current) != row['before'] or unaffected(current) != row['preserved']:
                raise ImportFailure('Asset changed after preview; rerun to review')
            row['status'] = 'updating'
            atomic_json(journal, report)
            log('Set date: ' + item.relative)
            response = api.request('PUT', '/assets/' + item.asset_id, json={'dateTimeOriginal': item.capture_time})
            row['xmpWrite'] = 'queued_by_immich_not_independently_verified'
            if isinstance(response, dict) and response.get('exifInfo') is not None:
                # The response carries the stored fields; no extra request or polling.
                row['after'] = dates(response)
                if not exif_matches(item, response):
                    raise ImportFailure('Server did not store the requested dateTimeOriginal/timeZone: ' + json.dumps(row['after']))
            row['status'] = 'updated'
        except (ImportFailure, OSError, ValueError) as error:
            row.update(status='failed', error=str(error))
            report['errors'].append(item.relative + ': ' + str(error))
        atomic_json(journal, report)
    # No source re-check: Immich may already be moving edited files to their new
    # date folder under the storage template.
    report['result'] = 'DATES UPDATED; XMP WRITE AND FILE MOVE ARE ASYNCHRONOUS' if not report['errors'] else 'REPAIR INCOMPLETE'
    report['exitCode'] = 1 if report['errors'] else 0
    atomic_json(journal, report)
    return report


def main(argv=None):
    args = parser().parse_args(argv)
    log = (lambda s: None) if args.quiet else (lambda s: print(s, file=sys.stderr))
    try:
        config = load_config(args)
        source = Path(args.path).expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ImportFailure('Source must be a directory')
        report = repair(source, config, args.apply, args.time_source, log, args.match)
    except (ImportFailure, OSError, ValueError) as error:
        report = {'result': 'REPAIR INCOMPLETE', 'exitCode': 1, 'errors': [str(error)], 'items': []}
    except KeyboardInterrupt:
        report = {'result': 'INTERRUPTED; inspect audit report before rerun', 'exitCode': 130, 'items': []}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for row in report['items']:
            print(row['status'] + ': ' + row['path'])
            if 'before' in row:
                print('  before: ' + json.dumps(row['before']))
            if 'target' in row:
                print('  target: ' + row['target'])
        for warning in report.get('warnings', []):
            print('WARNING: ' + warning)
        for error in report.get('errors', []):
            print('ERROR: ' + error)
        if report.get('auditReport'):
            print('Audit: ' + report['auditReport'])
        print('RESULT: ' + report['result'])
    return report['exitCode']


if __name__ == '__main__':
    sys.exit(main())

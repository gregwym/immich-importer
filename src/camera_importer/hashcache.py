"""Fingerprint-keyed hash index: skips re-reading unchanged source files.

An entry only says what the bytes hashed to last time. Whether they are in
Immich is still decided by bulk-upload-check and the post-upload checksum
comparison, so a hit never weakens dedup or integrity; a miss costs one read.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from .files import atomic_json, readonly
from .model import ImportFailure

FILE = "hashes.json"
MAX_AGE = timedelta(days=180)
MAX_ENTRIES = 200000
HEX = re.compile(r"^[0-9a-f]+$")


def key(item):
    # Path plus the stable identity fields (size, mtime); inode and ctime are
    # not dependable on FAT/exFAT card mounts and would only cause misses.
    return str(item.size) + ":" + str(item.fingerprint[3]) + ":" + str(item.path)


def load(root):
    path = root / FILE
    if not path.exists() and not path.is_symlink():
        return {}
    try:
        with readonly(path) as stream:
            data = stream.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024:
            raise ValueError()
        value = json.loads(data)
        entries = value.get("entries") if isinstance(value, dict) else None
        if not isinstance(entries, dict):
            raise ValueError()
        return {k: v for k, v in entries.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        raise ImportFailure("Cannot read hash index: " + str(path)) from None


def lookup(entries, item):
    entry = entries.get(key(item)) if item.fingerprint else None
    if not entry or entry.get("size") != item.size:
        return None
    sha1, sha256 = entry.get("sha1"), entry.get("sha256")
    if not (isinstance(sha1, str) and isinstance(sha256, str) and len(sha1) == 40 and len(sha256) == 64
            and HEX.match(sha1) and HEX.match(sha256)):
        return None
    return sha1, sha256


def record(entries, item, now):
    if item.fingerprint and item.sha1 and item.sha256:
        entries[key(item)] = {"sha1": item.sha1, "sha256": item.sha256, "size": item.size,
                              "path": str(item.path), "seen": now.isoformat()}


def prune(entries, now):
    def seen(entry):
        try:
            return datetime.fromisoformat(entry.get("seen"))
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)
    kept = {k: v for k, v in entries.items() if now - seen(v) <= MAX_AGE}
    if len(kept) > MAX_ENTRIES:
        newest = sorted(kept, key=lambda k: seen(kept[k]), reverse=True)[:MAX_ENTRIES]
        kept = {k: kept[k] for k in newest}
    return kept


def save(root, entries, now):
    kept = prune(entries, now)
    path = root / FILE
    if kept or path.exists():
        atomic_json(path, {"schemaVersion": 1, "entries": kept})
    return len(kept)

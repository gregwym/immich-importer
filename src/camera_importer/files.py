"""Filesystem safety and durable, no-clobber companion storage (Linux/DSM)."""
import contextlib
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from .model import ImportFailure

CHUNK = 1024 * 1024


FIELDS = ("dev", "inode", "size", "mtime_ns", "ctime_ns")
# Device and inode numbers are not stable on FAT/exFAT USB mounts (Linux hands
# out inode numbers per lookup and forgets them under cache pressure) and ctime
# is synthesized there, so identity rests on size and mtime; the rest is kept
# for diagnostics only.
IDENTITY = (2, 3)


def fingerprint(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def same(observed, expected):
    return all(observed[i] == expected[i] for i in IDENTITY)


def describe_change(observed, expected):
    return ", ".join(FIELDS[i] + " " + str(expected[i]) + "->" + str(observed[i])
                     for i in range(len(FIELDS)) if observed[i] != expected[i]) or "no visible difference"


def changed(path, expected):
    """Message describing a source that no longer matches its planned identity, else None."""
    observed = snapshot(path)
    if same(observed, expected):
        return None
    return "SOURCE CHANGED: " + str(path) + " (" + describe_change(observed, expected) + ")"


def snapshot(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        raise ImportFailure("Not a regular file: " + str(path))
    return fingerprint(st)


@contextlib.contextmanager
def readonly(path, expected=None):
    # O_NOFOLLOW prevents replacing a planned source with a symlink.
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ImportFailure("Cannot safely open regular file: " + str(path)) from None
    with os.fdopen(fd, "rb") as stream:
        initial = fingerprint(os.fstat(stream.fileno()))
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ImportFailure("Not a regular file: " + str(path))
        if expected is not None and not same(initial, expected):
            raise ImportFailure("SOURCE CHANGED: " + str(path) + " (" + describe_change(initial, expected) + ")")
        yield stream
        final = fingerprint(os.fstat(stream.fileno()))
        if not same(final, initial):
            raise ImportFailure("SOURCE CHANGED: " + str(path) + " (" + describe_change(final, initial) + ")")
        message = changed(path, initial)
        if message:
            raise ImportFailure(message)


class Hasher:
    """SHA-1 (Immich checksum) and SHA-256 (manifest) over one pass of bytes."""
    def __init__(self):
        self.sha1, self.sha256, self.size = hashlib.sha1(), hashlib.sha256(), 0

    def update(self, block):
        self.sha1.update(block)
        self.sha256.update(block)
        self.size += len(block)

    def digests(self):
        return self.sha1.hexdigest(), self.sha256.hexdigest()


def hashes(path, expected=None):
    hasher = Hasher()
    with readonly(path, expected) as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            hasher.update(block)
    return hasher.digests()


def within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_roots(source, companion, manifest):
    source = source.resolve()
    roots = [companion.resolve(), manifest.resolve()]
    for root in roots:
        if within(root, source) or within(source, root):
            raise ImportFailure("Source and output directories must not overlap")
        # Never permit writes into the known Immich managed storage tree.
        if within(root, Path("/volume1/immich/media")):
            raise ImportFailure("Output directory must not be in Immich managed storage")
    if within(roots[0], roots[1]) or within(roots[1], roots[0]):
        raise ImportFailure("Companion and manifest directories must not overlap")


def safe_directory(path):
    """Reject symlink components, including pre-existing output directories."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            current.mkdir()
        except FileExistsError:
            if not stat.S_ISDIR(current.lstat().st_mode):
                raise ImportFailure("Output directory is not a real directory: " + str(current))


def fsync_directory(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def locked(root):
    safe_directory(root)
    fd = os.open(str(root / ".camera-import.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ImportFailure("Another importer is using: " + str(root)) from None
        yield
    finally:
        os.close(fd)


def archive_wav(item, root):
    destination = root / "pocket3" / item.date[:4] / item.date / item.path.name
    safe_directory(destination.parent)
    if os.path.lexists(destination):
        if not item.sha256:
            item.sha1, item.sha256 = hashes(item.path, item.fingerprint)
        if hashes(destination)[1] != item.sha256:
            raise ImportFailure("COLLISION: " + str(destination))
        return "already_archived"
    fd, name = tempfile.mkstemp(prefix=".camera-import-", suffix=".partial", dir=destination.parent)
    temporary = Path(name)
    try:
        # One read of the source: hash the same bytes that are written out, then
        # confirm the copy independently by hashing the destination side.
        hasher = Hasher()
        with os.fdopen(fd, "wb") as out, readonly(item.path, item.fingerprint) as inp:
            for block in iter(lambda: inp.read(CHUNK), b""):
                hasher.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        streamed = hasher.digests()
        if item.sha256 and item.sha256 != streamed[1]:
            raise ImportFailure("Source bytes differ from the indexed hash: " + item.relative)
        item.sha1, item.sha256 = streamed
        if hashes(temporary)[1] != item.sha256:
            raise ImportFailure("Companion SHA-256 mismatch: " + item.relative)
        try:
            # Atomic publication that fails if a destination already exists.
            os.link(temporary, destination)
        except FileExistsError:
            if hashes(destination)[1] != item.sha256:
                raise ImportFailure("COLLISION: " + str(destination)) from None
            return "already_archived"
        fsync_directory(destination.parent)
        if hashes(destination)[1] != item.sha256:
            raise ImportFailure("Companion destination SHA-256 mismatch: " + item.relative)
        return "copied"
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    safe_directory(path.parent)
    if path.is_symlink():
        raise ImportFailure("Refusing symlink output: " + str(path))
    fd, name = tempfile.mkstemp(prefix=".camera-import-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)

import contextlib
import os
import subprocess
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

from . import manifest
from .api import Immich
from .config import authenticate, describe
from .files import archive_wav, atomic_json, hashes, locked, snapshot, validate_roots
from .metadata import BATCH, prepare_metadata, probe_batch
from .model import ImportFailure
from .scan import scan

QUIET = lambda message: None


def fail(item, error):
    item.status, item.error = "failed", str(error)


def build_plan(source, config, log, progress=QUIET):
    progress("Scanning " + str(source))
    plan = scan(source)
    if not plan.items and not plan.errors:
        plan.errors.append("No files found; source cannot be reviewed as imported")
    elif not any(i.route in ("timeline", "archive", "probe", "companion") for i in plan.items):
        plan.errors.append("No preservable camera media found; ignored files alone do not establish an archive")
    targets = []
    for item in plan.items:
        log(item.relative + ": " + item.route + " (" + item.reason + ")")
        if item.error:
            item.status = "failed"
        elif item.route in ("timeline", "archive", "probe"):
            targets.append(item)
    progress("Scanned " + str(len(plan.items)) + " files (" + str(len(plan.unknown)) + " unknown); reading camera metadata for "
             + str(len(targets)) + " with " + config.exiftool_bin)
    done = 0
    for offset in range(0, len(targets), BATCH):
        chunk = targets[offset:offset + BATCH]
        try:
            tags_by_path = probe_batch([i.path for i in chunk], config.exiftool_bin)
        except ImportFailure as error:
            log(str(error))
            tags_by_path = {}
        for item in chunk:
            done += 1
            progress("[" + str(done) + "/" + str(len(targets)) + "] metadata: " + item.relative)
            try:
                prepare_metadata(item, config.exiftool_bin, tags_by_path.get(item.path))
            except (ImportFailure, OSError, ValueError) as error:
                fail(item, error)
                if item.route == "probe":
                    item.route, item.reason = "unknown", "Photo metadata requires review"
    return plan


def summarize(plan, config, dry_run, metadata_verify, manifests=0):
    errors = list(plan.errors)
    errors.extend(i.relative + ": " + i.error for i in plan.items if i.error)
    if plan.unknown:
        errors.append("UNKNOWN FILES: " + str(len(plan.unknown)))
    if plan.incomplete:
        errors.append("INCOMPLETE 360 BUNDLE: " + str(len(plan.incomplete)))
    if not metadata_verify:
        errors.append("Metadata verification disabled; debug runs cannot establish safety")
    if not dry_run:
        for item in plan.items:
            if item.route in ("timeline", "archive", "companion") and item.status == "planned":
                errors.append("NOT PROCESSED: " + item.relative)
    success = not errors
    routes = {}
    for route in ("timeline", "archive", "companion"):
        counts = Counter(i.status for i in plan.items if i.route == route)
        routes[route] = dict(counts)
    return {"schemaVersion": 1, "source": str(plan.source), "dryRun": dry_run, "config": describe(config),
            "result": ("DRY RUN OK" if dry_run else "SAFE TO REVIEW FOR CARD FORMAT") if success else "NOT SAFE TO FORMAT SOURCE",
            "exitCode": 0 if success else 1, "counts": routes,
            "cameraMetadata": {"verified": sum(i.verified for i in plan.assets),
                               "failed": sum(i.status == "failed" for i in plan.assets),
                               "xmpPrepared": sum(i.xmp is not None for i in plan.assets)},
            "bundles": {"complete": len(plan.bundles) - len(set(plan.incomplete) | set(plan.lrv_missing)),
                        "lrvMissing": list(plan.lrv_missing), "incomplete": plan.incomplete,
                        "manifestsWrittenOrVerified": manifests},
            "ignored": dict(Counter(i.reason for i in plan.items if i.route == "ignored")),
            "skippedDirectories": list(plan.skipped),
            "unknown": [i.relative for i in plan.unknown], "errors": errors,
            "items": [i.public() for i in plan.items]}


def tool_version(executable, flag):
    try:
        result = subprocess.run([executable, flag], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise ImportFailure("not runnable: " + executable) from None
    if result.returncode:
        raise ImportFailure("exited with status " + str(result.returncode) + ": " + executable)
    output = (result.stdout or b"").decode("utf-8", "replace").strip()
    return output.splitlines()[0] if output else "ok"


def describe_root(path):
    if path.is_symlink():
        raise ImportFailure("symlink is not allowed: " + str(path))
    if path.is_dir():
        if not os.access(path, os.W_OK | os.X_OK):
            raise ImportFailure("exists but is not writable: " + str(path))
        return "exists, writable"
    if path.exists():
        raise ImportFailure("exists but is not a directory: " + str(path))
    parent = path
    while not parent.exists():
        parent = parent.parent
    if not os.access(parent, os.W_OK | os.X_OK):
        raise ImportFailure("cannot be created; " + str(parent) + " is not writable")
    return "will be created under " + str(parent)


def check_config(source, config, log=QUIET, api_factory=Immich):
    """Validate configuration, tools and Immich access without touching media or outputs."""
    checks = []

    def check(name, action):
        try:
            detail = action()
            checks.append({"name": name, "status": "ok", "detail": detail})
            return True
        except (ImportFailure, OSError, ValueError) as error:
            checks.append({"name": name, "status": "failed", "detail": str(error)})
            return False

    check("output roots do not overlap source or Immich storage",
          lambda: (validate_roots(source, config.companion_root, config.manifest_root), "ok")[1])
    check("companion_root " + str(config.companion_root), lambda: describe_root(config.companion_root))
    check("manifest_root " + str(config.manifest_root), lambda: describe_root(config.manifest_root))
    check("exiftool " + config.exiftool_bin, lambda: "version " + tool_version(config.exiftool_bin, "-ver"))
    check("immich CLI " + config.immich_bin, lambda: "version " + tool_version(config.immich_bin, "--version"))
    if check("Immich credentials", lambda: (authenticate(config), "API " + config.api_url)[1]):
        def preflight():
            api = api_factory(config, log)
            try:
                api.preflight()
                return "server 3.1.0, account " + config.expected_user_name + " (" + api.owner_id + "), " + str(len(api.media_types)) + " media types"
            finally:
                api.close()
        check("Immich server, account and permissions", preflight)
    ok = all(c["status"] == "ok" for c in checks)
    return {"schemaVersion": 1, "source": str(source), "config": describe(config), "checks": checks,
            "result": "CONFIG OK" if ok else "CONFIG INVALID", "exitCode": 0 if ok else 1,
            "errors": [c["name"] + ": " + c["detail"] for c in checks if c["status"] != "ok"]}


def execute(source, config, dry_run=False, strict=False, metadata_verify=True,
            log=QUIET, api_factory=Immich, progress=QUIET):
    validate_roots(source, config.companion_root, config.manifest_root)
    api = None
    try:
        if not dry_run:
            progress("Connecting to Immich")
            authenticate(config)
            api = api_factory(config, log)
            api.preflight()
        plan = build_plan(source, config, log, progress)
        if dry_run or (strict and (plan.unknown or plan.incomplete or plan.errors or any(i.error for i in plan.items))):
            return summarize(plan, config, dry_run, metadata_verify)
        written = 0
        with contextlib.ExitStack() as stack:
            stack.enter_context(locked(config.manifest_root))
            stack.enter_context(locked(config.companion_root))
            # Hash and preflight ALL identities before any upload. Same bytes
            # cannot satisfy incompatible camera/visibility roles for one owner.
            identities = {}
            hashing = [i for i in plan.assets if not i.error]
            for index, item in enumerate(hashing, 1):
                progress("[" + str(index) + "/" + str(len(hashing)) + "] hashing: " + item.relative)
                try:
                    if item.path.suffix.lower() not in api.media_types:
                        raise ImportFailure("Format is not supported by this Immich server")
                    item.sha1, item.sha256 = hashes(item.path, item.fingerprint)
                    log(item.relative + " SHA-256 " + item.sha256)
                    identities.setdefault(item.sha1, []).append(item)
                except (OSError, ImportFailure) as error:
                    fail(item, error)
            for matches in identities.values():
                contracts = {(i.sha256, i.route, tuple(sorted(i.expected.items()))) for i in matches}
                if len(contracts) > 1:
                    for item in matches:
                        fail(item, "Same Immich checksum has conflicting content, visibility or camera metadata")
            for key, members in plan.bundles.items():
                try:
                    previous = manifest.load(config.manifest_root / "insta360" / (key + ".json"))
                    manifest.merge(previous, key, api.owner_id, config.api_url, members)
                except (OSError, ImportFailure) as error:
                    for item in members:
                        fail(item, error)
            for item in plan.items:
                if item.route == "companion" and not item.error:
                    progress("companion: " + item.relative)
                    try:
                        item.status = archive_wav(item, config.companion_root)
                        item.verified = True
                        log(item.relative + ": " + item.status)
                    except (OSError, ImportFailure) as error:
                        fail(item, error)
            pending = sorted([i for i in plan.assets if not i.error], key=lambda i: i.route == "archive")
            try:
                existing = api.check(pending) if pending else {}
            except ImportFailure as error:
                existing = {}
                for item in pending:
                    fail(item, error)
            # Avoid redundant sends for repeated identical source copies.
            resolved = {}
            for index, item in enumerate(pending, 1):
                if item.error:
                    continue
                progress("[" + str(index) + "/" + str(len(pending)) + "] upload/verify: " + item.relative)
                try:
                    duplicate = existing.get(item.relative)
                    if duplicate:
                        item.asset_id, trashed = duplicate
                        if trashed:
                            raise ImportFailure("Matching asset is in the trash")
                        item.status = "already_present"
                    elif item.sha1 in resolved:
                        item.asset_id, item.status = resolved[item.sha1], "already_present"
                    else:
                        item.asset_id, status = api.upload(item)
                        item.status = "uploaded" if status == "created" else "already_present"
                    resolved[item.sha1] = item.asset_id
                    api.verify(item, metadata_verify)
                    log(item.relative + ": " + item.status)
                except (OSError, ImportFailure) as error:
                    fail(item, error)
            for key, members in plan.bundles.items():
                if len({m.role for m in members}) != len(members):
                    continue
                try:
                    # Persist verified IDs and recoverable partial progress.
                    # A manifest never changes the safety result of a failed item.
                    if any(i.sha256 for i in members):
                        manifest.save(config.manifest_root, key, api.owner_id, config.api_url, members)
                        written += 1
                except (OSError, ImportFailure) as error:
                    plan.errors.append(str(error))
            # Catch source changes affecting even ignored files or old assets.
            for item in plan.items:
                if item.fingerprint is not None:
                    try:
                        if snapshot(item.path) != item.fingerprint:
                            raise ImportFailure("SOURCE CHANGED: " + item.relative)
                    except (OSError, ImportFailure) as error:
                        fail(item, error)
            # Detect files added/removed while importing. No successful review of
            # a directory that is still being written by a camera or another job.
            final_scan = scan(source)
            if final_scan.errors or {i.relative for i in final_scan.items} != {i.relative for i in plan.items}:
                plan.errors.append("Source directory changed or became unreadable during import")
            report = summarize(plan, config, False, metadata_verify, written)
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
            try:
                atomic_json(config.manifest_root / "runs" / (run_id + ".json"), report)
            except (OSError, ImportFailure):
                plan.errors.append("Failed to persist run report")
                report = summarize(plan, config, False, metadata_verify, written)
            return report
    finally:
        if api:
            api.close()

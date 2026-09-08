import contextlib
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

from . import manifest
from .api import Immich
from .config import authenticate
from .files import archive_wav, atomic_json, hashes, locked, snapshot, validate_roots
from .metadata import prepare_metadata
from .model import ImportFailure
from .scan import scan


def fail(item, error):
    item.status, item.error = "failed", str(error)


def build_plan(source, config, log):
    plan = scan(source)
    if not plan.items and not plan.errors:
        plan.errors.append("No files found; source cannot be reviewed as imported")
    elif not any(i.route in ("timeline", "archive", "probe", "companion") for i in plan.items):
        plan.errors.append("No preservable camera media found; ignored files alone do not establish an archive")
    for item in plan.items:
        log(item.relative + ": " + item.route + " (" + item.reason + ")")
        if item.error:
            item.status = "failed"
            continue
        if item.route in ("timeline", "archive", "probe"):
            try:
                prepare_metadata(item, config.exiftool_bin)
            except (ImportFailure, OSError, ValueError) as error:
                fail(item, error)
                if item.route == "probe":
                    item.route, item.reason = "unknown", "Photo metadata requires review"
    return plan


def summarize(plan, dry_run, metadata_verify, manifests=0):
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
    return {"schemaVersion": 1, "source": str(plan.source), "dryRun": dry_run,
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


def execute(source, config, dry_run=False, strict=False, metadata_verify=True,
            log=lambda message: None, api_factory=Immich):
    validate_roots(source, config.companion_root, config.manifest_root)
    api = None
    try:
        if not dry_run:
            authenticate(config)
            api = api_factory(config, log)
            api.preflight()
        plan = build_plan(source, config, log)
        if dry_run or (strict and (plan.unknown or plan.incomplete or plan.errors or any(i.error for i in plan.items))):
            return summarize(plan, dry_run, metadata_verify)
        written = 0
        with contextlib.ExitStack() as stack:
            stack.enter_context(locked(config.manifest_root))
            stack.enter_context(locked(config.companion_root))
            # Hash and preflight ALL identities before any upload. Same bytes
            # cannot satisfy incompatible camera/visibility roles for one owner.
            identities = {}
            for item in plan.assets:
                if item.error:
                    continue
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
            for item in pending:
                if item.error:
                    continue
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
            report = summarize(plan, False, metadata_verify, written)
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
            try:
                atomic_json(config.manifest_root / "runs" / (run_id + ".json"), report)
            except (OSError, ImportFailure):
                plan.errors.append("Failed to persist run report")
                report = summarize(plan, False, metadata_verify, written)
            return report
    finally:
        if api:
            api.close()

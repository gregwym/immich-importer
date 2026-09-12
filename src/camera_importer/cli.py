import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import format_config, load_config
from .model import ImportFailure
from .runner import check_config, execute


def parser():
    p = argparse.ArgumentParser(description="Read-only DJI Pocket 3 / Insta360 ONE RS import into Immich 3.1.0")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--dry-run", action="store_true", help="Plan locally; no API calls or persistent writes")
    p.add_argument("--verbose", action="store_true", help="Diagnostic progress on stderr; never log credentials")
    p.add_argument("--quiet", action="store_true", help="No progress lines on stderr")
    p.add_argument("--json", action="store_true", help="Exactly one JSON report on stdout")
    p.add_argument("--check-config", action="store_true", help="Validate configuration, tools and Immich access; touches no media")
    p.add_argument("--strict", action="store_true", help="Abort before writes when planning finds any issue")
    p.add_argument("--no-metadata-verify", action="store_true", help="Debug only; always produces a non-success safety result")
    p.add_argument("--config", help="Configuration JSON (default ~/.config/camera-import/config.json)")
    p.add_argument("--companion-root")
    p.add_argument("--manifest-root")
    p.add_argument("--api-url")
    p.add_argument("--capture-timezone", help="Where the footage was shot (America/Los_Angeles or +09:00): decides the wall time shown; also how a camera clock without timezone evidence is read")
    p.add_argument("--clock-shift", help="Correct a wrong camera clock for this run, e.g. 221d00:34:12 or -1h30m")
    p.add_argument("--clock-timezone", help="Zone the camera clock was displaying when it differs from --capture-timezone (e.g. home time while abroad)")
    p.add_argument("--auth-dir", help="Directory containing Immich CLI auth.yml")
    p.add_argument("--verify-timeout", type=float, help="Metadata polling timeout per asset in seconds")
    return p


def capture_line(item):
    """How the capture date will reach Immich, for one dry-run item."""
    source = item.get("captureTimeSource") or "no camera clock"
    if not item.get("captureTime"):
        if "respected" in source:
            return "capture: EXIF date kept as written, Immich reads it (" + source + ")"
        return "capture: left to Immich (" + source + ")"
    delivery = "Immich extracts it from the file" if source.startswith("metadata:zoned") else "delivered in XMP DateTimeOriginal"
    return "capture: " + item["captureTime"] + " (" + source + ") -> " + delivery


def display(report):
    if "checks" in report:
        print("Configuration Check\n===================")
        for check in report["checks"]:
            print(("  OK   " if check["status"] == "ok" else "  FAIL ") + check["name"] + ": " + check["detail"])
        print("\nRESULT: " + report["result"])
        return
    print("Camera Import Summary\n=====================")
    print("Source: " + report.get("source", ""))
    if "items" in report:
        if report.get("dryRun"):
            for route, label in (("timeline", "IMMICH / TIMELINE"), ("companion", "COMPANION ARCHIVE"),
                                 ("ignored", "IGNORED"), ("sidecar", "SOURCE XMP")):
                print("\n" + label + ":")
                for item in report["items"]:
                    if item["route"] == route:
                        print("  + " + item["path"])
                        if route == "timeline":
                            print("    embedded " + json.dumps(item["embeddedMetadata"], ensure_ascii=False)
                                  + ("  times " + json.dumps(item.get("embeddedTimeMetadata") or {}, ensure_ascii=False)
                                     if item.get("embeddedTimeMetadata") else "  times {}"))
                            print("    expected " + json.dumps(item["expectedMetadata"], ensure_ascii=False)
                                  + (" -> XMP sidecar" if item["xmpPrepared"] else " (as-is)")
                                  + ("; stack with " + item["stackKey"] if item["stackKey"] and item["stackKey"] != item["path"] else ""))
                            print("    " + capture_line(item))
                        if item.get("error"):
                            print("    ERROR " + item["error"])
        for route, counts in report["counts"].items():
            print("\n" + route + ": " + (", ".join(k + "=" + str(v) for k, v in counts.items()) or "0"))
        print("\nCamera metadata: " + json.dumps(report["cameraMetadata"]))
        print("Hash sources: " + json.dumps(report.get("hashSources", {})))
        print("Capture date sources: " + json.dumps(report.get("captureSources", {}), ensure_ascii=False))
        print("360 bundles: complete=" + str(report["bundles"]["complete"]) +
              ", lrvMissing=" + str(len(report["bundles"]["lrvMissing"])) +
              ", incomplete=" + str(len(report["bundles"]["incomplete"])))
        for key in report["bundles"]["lrvMissing"]:
            print("  NO LRV (master-00 leads the stack): " + key)
        for key, missing in report["bundles"]["incomplete"].items():
            print("  INCOMPLETE 360 BUNDLE: " + key)
            for name in missing:
                print("    Missing: " + name)
        print("Ignored: " + json.dumps(report["ignored"]))
        print("Immich stacks (RAW+JPEG, 360 bundles): " + str(len(report.get("stacks", {}))))
        print("Skipped hidden directories: " + str(len(report["skippedDirectories"])))
        for path in report["skippedDirectories"]:
            print("  SKIPPED: " + path)
        print("Unknown: " + str(len(report["unknown"])))
        for path in report["unknown"]:
            print("  UNKNOWN FILE: " + path)
    for warning in report.get("warnings", []):
        print("WARNING: " + warning)
    for error in report.get("errors", []):
        print("ERROR: " + error)
    print("\nRESULT: " + report["result"])


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        source = Path(args.path).expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ImportFailure("Source must be a directory")
        config = load_config(args)
        if not args.json:
            print("Configuration:\n" + format_config(config) + "\n", flush=True)
        stderr = lambda message: print(message, file=sys.stderr, flush=True)
        log = stderr if args.verbose else (lambda message: None)
        progress = (lambda message: None) if args.quiet else stderr
        if args.check_config:
            report = check_config(source, config, log)
        else:
            report = execute(source, config, args.dry_run, args.strict, not args.no_metadata_verify, log, progress=progress)
    except KeyboardInterrupt:
        report = {"result": "NOT SAFE TO FORMAT SOURCE", "exitCode": 130, "errors": ["Interrupted; rerun to verify partial progress"]}
    except (ImportFailure, OSError) as error:
        report = {"result": "NOT SAFE TO FORMAT SOURCE", "exitCode": 1, "errors": [str(error)]}
    except Exception as error:
        # Never leak requests/YAML exception content or auth through a traceback.
        report = {"result": "NOT SAFE TO FORMAT SOURCE", "exitCode": 1,
                  "errors": ["Unexpected failure (" + type(error).__name__ + "); no safety conclusion"]}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        display(report)
    return report["exitCode"]


if __name__ == "__main__":
    sys.exit(main())

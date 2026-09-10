import base64
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from camera_importer import manifest
from camera_importer.api import Immich
from camera_importer.cli import main, parser
from camera_importer.config import Config, authenticate, load_config, read_auth_scalars
from camera_importer.files import archive_wav, hashes, readonly, snapshot, validate_roots
from camera_importer.metadata import NS, make_xmp, prepare_metadata
from camera_importer.model import ImportFailure
from camera_importer.runner import execute
from camera_importer.scan import ONERS, POCKET, classify, scan

OWNER = "a0a0a0a0-1111-4111-8111-111111111111"
TRIO = ["VID_20260127_170725_00_052.insv", "VID_20260127_170725_10_052.insv", "LRV_20260127_170725_11_052.insv"]
KEY = "20260127_170725_052"


class FakeServer:
    """HTTP contract fixture, not a claim of a real Immich integration test."""
    def __init__(self):
        self.assets, self.uploads, self.calls = {}, [], []
        self.stacks = {}
        self.ignore_sidecar = False
        self.upload_failure = False
        self.drop_response = False
        self.redirect = False
        self.bad_visibility = False
        self.bad_checksum = False
        self.forbid_jobs = False
        self.quota_exceeded = False
        self.user_name = "Camera Archive"
        self.version = {"major": 3, "minor": 1, "patch": 0}
        self.on_upload = lambda: None
        self.date_updates = []
        self.reject_date_updates = False
        self.ignore_date_updates = False
        self.defer_jobs = False
        self.pending_jobs = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, payload=None, status=200):
                encoded = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):
                state.calls.append(("GET", self.path))
                if self.headers.get("x-api-key") != "test-secret":
                    return self.reply({}, 401)
                if state.redirect:
                    self.send_response(302)
                    self.send_header("Location", "/do-not-follow")
                    self.end_headers()
                    return
                if self.path == "/api/users/me":
                    return self.reply({"id": OWNER, "name": state.user_name})
                if self.path == "/api/server/version":
                    return self.reply(state.version)
                if self.path == "/api/server/media-types":
                    return self.reply({"image": [".jpg", ".jpeg", ".dng", ".insp"], "video": [".mp4", ".insv"], "sidecar": [".xmp"]})
                identifier = self.path.rsplit("/", 1)[-1]
                if self.path.startswith("/api/stacks/") and identifier in state.stacks:
                    stack = state.stacks[identifier]
                    return self.reply({"id": identifier, "primaryAssetId": stack[0],
                                       "assets": [state.assets[a]["info"] for a in stack]})
                if identifier in state.assets:
                    asset = dict(state.assets[identifier]["info"])
                    if state.bad_checksum:
                        asset["checksum"] = base64.b64encode(b"bad").decode()
                    stack = next((s for s, members in state.stacks.items() if identifier in members), None)
                    asset["stack"] = {"id": stack, "primaryAssetId": state.stacks[stack][0], "assetCount": len(state.stacks[stack])} if stack else None
                    return self.reply(asset)
                return self.reply({}, 404)

            def do_POST(self):
                state.calls.append(("POST", self.path))
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/api/assets/bulk-upload-check":
                    results = []
                    for row in json.loads(body)["assets"]:
                        found = next((a["info"] for a in state.assets.values() if base64.b64decode(a["info"]["checksum"]).hex() == row["checksum"]), None)
                        result = {"id": row["id"], "action": "reject" if found else "accept"}
                        if found:
                            result.update(assetId=found["id"], isTrashed=found["isTrashed"], reason="duplicate")
                        results.append(result)
                    return self.reply({"results": results})
                if self.path == "/api/search/metadata":
                    query = json.loads(body)
                    items = [a["info"] for a in state.assets.values() if a["info"]["originalFileName"] == query.get("originalFileName")]
                    return self.reply({"assets": {"items": items, "nextPage": None, "total": len(items)}})
                if self.path == "/api/stacks":
                    ids = json.loads(body)["assetIds"]
                    identifier = str(uuid4())
                    state.stacks[identifier] = list(ids)
                    return self.reply({"id": identifier, "primaryAssetId": ids[0],
                                       "assets": [state.assets[a]["info"] for a in ids]}, 201)
                if self.path == "/api/assets/jobs":
                    if state.forbid_jobs:
                        return self.reply({"message": "Missing required permission: job.create", "error": "Forbidden", "statusCode": 403}, 403)
                    for identifier in json.loads(body)["assetIds"]:
                        state.extract(identifier)
                    return self.reply(None, 204)
                if self.path == "/api/assets":
                    if state.upload_failure:
                        return self.reply({"error": "test-secret must not leak"}, 500)
                    if state.quota_exceeded:
                        return self.reply({"message": "Quota has been exceeded!", "error": "Bad Request", "statusCode": 400}, 400)
                    message = BytesParser(policy=default).parsebytes(
                        ("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
                    fields = {}
                    for part in message.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        fields[name] = (part.get_filename(), part.get_payload(decode=True))
                        # v3.1.0 canUploadFile: every file part is checked against
                        # body.filename (if already parsed) or its own filename.
                        if name in ("assetData", "sidecarData"):
                            checked = fields["filename"][1].decode() if "filename" in fields else part.get_filename()
                            ok = checked.lower().endswith(".xmp") if name == "sidecarData" else checked.lower().endswith((".mp4", ".insv", ".jpg", ".jpeg", ".dng", ".insp"))
                            if not ok:
                                return self.reply({"message": "Unsupported file type " + checked, "error": "Bad Request", "statusCode": 400}, 400)
                    filename, media = fields["assetData"]
                    xmp = fields.get("sidecarData", (None, None))[1]
                    checksum = base64.b64encode(hashlib.sha1(media).digest()).decode()
                    found = next((a["info"] for a in state.assets.values() if a["info"]["checksum"] == checksum), None)
                    if found:
                        # Real v3.1.0 duplicate upload does NOT attach the sidecar.
                        return self.reply({"id": found["id"], "status": "duplicate"})
                    identifier = str(uuid4())
                    info = {"id": identifier, "ownerId": OWNER, "checksum": checksum,
                            "type": "VIDEO" if filename.lower().endswith((".mp4", ".insv")) else "IMAGE",
                            "visibility": fields["visibility"][1].decode(), "isTrashed": False, "isOffline": False,
                            "libraryId": None, "exifInfo": {"fileSizeInByte": len(media)}, "originalFileName": filename}
                    state.assets[identifier] = {"info": info, "xmp": xmp, "media": media}
                    state.uploads.append(fields)
                    state.extract(identifier)  # Immich queues extraction itself after upload.
                    state.on_upload()
                    if state.drop_response:
                        self.close_connection = True
                        return
                    return self.reply({"id": identifier, "status": "created"}, 201)
                return self.reply({}, 404)

            def do_DELETE(self):
                state.calls.append(("DELETE", self.path))
                identifier = self.path.rsplit("/", 1)[-1]
                if self.path.startswith("/api/stacks/") and identifier in state.stacks:
                    del state.stacks[identifier]
                    return self.reply(None, 204)
                return self.reply({}, 404)

            def do_PUT(self):
                state.calls.append(("PUT", self.path))
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                identifier = self.path.rsplit("/", 1)[-1]
                if "dateTimeOriginal" in body:
                    if state.reject_date_updates:
                        return self.reply({"message": "Missing required permission: asset.update"}, 403)
                    state.date_updates.append(body)
                    asset = state.assets[identifier]
                    exif = asset["info"]["exifInfo"]
                    if not state.ignore_date_updates:
                        # v3.1.0 updateExif: exif columns synchronously, luxon fixed-zone name,
                        # then SidecarWrite -> AssetExtractMetadata refreshes localDateTime later.
                        from datetime import datetime as dt, timezone as tz
                        date = dt.fromisoformat(body["dateTimeOriginal"])
                        offset = int(date.utcoffset().total_seconds() // 60)
                        name = "UTC" + ("+" if offset >= 0 else "-") + str(abs(offset) // 60) + (":" + str(abs(offset) % 60).zfill(2) if abs(offset) % 60 else "")
                        exif.update(dateTimeOriginal=date.astimezone(tz.utc).isoformat(), timeZone=name)
                        camera = {k: exif.get(k) or "" for k in ("make", "model", "lensModel")}
                        asset["xmp"] = make_xmp(camera, asset["xmp"], body["dateTimeOriginal"])
                        if state.defer_jobs:
                            state.pending_jobs.append(identifier)
                        else:
                            state.extract(identifier)
                elif not state.bad_visibility:
                    state.assets[identifier]["info"]["visibility"] = body["visibility"]
                return self.reply(state.assets[identifier]["info"])

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.http.server_port) + "/api"

    def extract(self, identifier):
        asset = self.assets[identifier]
        if not self.ignore_sidecar and asset["xmp"]:
            root = ET.fromstring(asset["xmp"])
            props = {}
            for element in root.iter():
                props.update({k.rsplit("}", 1)[-1]: v for k, v in element.attrib.items()})
            camera = {"make": props.get("Make"), "model": props.get("Model"), "lensModel": props.get("LensID", props.get("LensModel"))}
            # Like Immich, a sidecar without camera fields leaves the extracted ones alone.
            asset["info"]["exifInfo"].update({k: v for k, v in camera.items() if v})
            if props.get("DateTimeOriginal"):
                from datetime import datetime, timezone
                date = datetime.fromisoformat(props["DateTimeOriginal"])
                asset["info"]["exifInfo"].update(dateTimeOriginal=date.astimezone(timezone.utc).isoformat(),
                                               timeZone="UTC" + date.strftime("%z")[:3] + ":" + date.strftime("%z")[3:])
                asset["info"]["localDateTime"] = date.replace(tzinfo=timezone.utc).isoformat()

    def run_jobs(self):
        while self.pending_jobs:
            self.extract(self.pending_jobs.pop(0))

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def put(self, name, data=None):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if data is not None else (name + " data").encode())
        return path


class FilesTest(Workspace):
    def test_routes_and_bundle(self):
        names = TRIO + ["DJI_20251226075842_0001_D.MP4", "DJI_20251226075842_0001_D.WAV",
                        "DJI_20251226075842_0001_D.LRF", "DJI_20251226080057_0003_D.JPG",
                        "VID_20250518_101759_00_001.mp4", "LRV_20250518_101759_01_001.mp4",
                        "DJI_20251226075842_0001_D.AAC", "DJI_20251226080057_0003_D.DNG"]
        for name in names:
            self.put(name)
        plan = scan(self.source)
        routes = {i.path.name: i.route for i in plan.items}
        self.assertEqual([routes[n] for n in names], ["timeline"] * 4 + ["companion", "ignored", "timeline", "timeline", "ignored", "companion", "timeline"])
        self.assertFalse(plan.incomplete)
        self.assertEqual(len(plan.bundles[KEY]), 3)
        self.assertEqual(sorted(i.path.name for i in plan.stacks[TRIO[2]]), sorted(TRIO))

    def test_unknown_and_special_files(self):
        for name in ["something.NEW", "DJI_20251226075842_0001_D.SRT", "VID_20250518_101759_99_001.mp4", "Thumb/important.mp4"]:
            self.put(name)
        (self.source / "link").symlink_to(self.root)
        os.mkfifo(self.source / "pipe")
        self.put("Thumb/preview.JPG")
        plan = scan(self.source)
        self.assertEqual(len(plan.unknown), 6)
        self.assertEqual(sum(i.route == "ignored" for i in plan.items), 1)

    def test_missing_bundle(self):
        self.put(TRIO[0])
        plan = scan(self.source)
        self.assertEqual(plan.incomplete[KEY], [TRIO[1]])
        self.assertEqual(plan.lrv_missing, [KEY])

    def test_bundle_without_lrv_is_led_by_master(self):
        self.put(TRIO[0])
        self.put(TRIO[1])
        plan = scan(self.source)
        self.assertEqual({i.path.name: i.route for i in plan.items}, {TRIO[0]: "timeline", TRIO[1]: "timeline"})
        self.assertFalse(plan.incomplete)
        self.assertEqual(plan.lrv_missing, [KEY])
        self.assertEqual(list(plan.stacks), [TRIO[0]])
        for name in TRIO:
            self.put(name)
        plan = scan(self.source)
        self.assertEqual(list(plan.stacks), [TRIO[2]])
        self.assertFalse(plan.lrv_missing)
        (self.source / TRIO[0]).unlink()
        (self.source / TRIO[2]).unlink()
        plan = scan(self.source)
        self.assertFalse(plan.stacks)  # a lone member is not a stack
        self.assertEqual(plan.incomplete[KEY], [TRIO[0]])

    def test_hidden_directories_are_skipped(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.put("@eaDir/DJI_20251226075842_0001_D.MP4/SYNOPHOTO_THUMB_M.jpg")
        self.put("@eaDir/DJI_20251226075842_0001_D.MP4@SynoResource")
        self.put("sub/@eaDir/anything.bin")
        self.put(".hidden/anything.bin")
        self.put("sub/DJI_20251226075842_0002_D.MP4")
        plan = scan(self.source)
        self.assertEqual([i.relative for i in plan.items], ["DJI_20251226075842_0001_D.MP4", "sub/DJI_20251226075842_0002_D.MP4"])
        self.assertFalse(plan.unknown)
        self.assertEqual(plan.skipped, [".hidden", "@eaDir", "sub/@eaDir"])

    def test_flat_mp4_channels_and_pro_variants(self):
        names = ["VID_20220404_231807_10_001.mp4", "LRV_20220404_231807_11_001.mp4",
                 "PRO_VID_20250518_154310_00_040.mp4", "PRO_LRV_20250518_154310_01_040.mp4",
                 "VID_20250518_101759_00_001.mp4", "LRV_20250518_101759_01_001.mp4"]
        for name in names:
            self.put(name)
        plan = scan(self.source)
        routes = {i.path.name: i.route for i in plan.items}
        self.assertEqual([routes[n] for n in names], ["timeline", "ignored", "timeline", "ignored", "timeline", "ignored"])
        self.assertFalse(plan.bundles)
        for item in plan.assets:
            self.assertEqual(item.expected, dict(ONERS, lensModel="4K Boost Lens"))
        self.assertIn("HDR/PRO", next(i.reason for i in plan.items if i.path.name.startswith("PRO_VID")))

    def test_pro_360_bundle_and_pocket3_audio_raw(self):
        pro = ["PRO_" + name for name in TRIO]
        names = pro + ["DJI_20240317154525_0083_D.AAC", "DJI_20240317154525_0083_D.DNG"]
        for name in names:
            self.put(name)
        plan = scan(self.source)
        routes = {i.path.name: i.route for i in plan.items}
        self.assertEqual([routes[n] for n in names], ["timeline", "timeline", "timeline", "companion", "timeline"])
        self.assertEqual(list(plan.bundles), ["PRO_" + KEY])
        self.assertFalse(plan.incomplete)
        (self.source / pro[1]).unlink()
        plan = scan(self.source)
        self.assertEqual(plan.incomplete["PRO_" + KEY], [pro[1]])

    def test_batch_probe_uses_one_exiftool_process(self):
        fake = self.root / "exiftool"
        fake.write_text("#!/bin/sh\necho 1 >> \"$0.calls\"\nprintf '['\nsep=''\nfor f in \"$@\"; do case \"$f\" in /*) printf '%s{\"SourceFile\":\"%s\",\"Make\":\"DJI\",\"Model\":\"DJI OsmoPocket3\"}' \"$sep\" \"$f\"; sep=',';; esac; done\nprintf ']'\n")
        fake.chmod(0o755)
        for index in range(3):
            self.put("DJI_2025122607584" + str(index) + "_000" + str(index) + "_D.MP4")
        from camera_importer.runner import build_plan
        plan = build_plan(self.source, Config(exiftool_bin=str(fake), capture_timezone="America/Los_Angeles"), lambda m: None)
        self.assertEqual([i.status for i in plan.assets], ["planned"] * 3)
        self.assertTrue(all(i.xmp is not None for i in plan.assets))
        self.assertEqual((self.root / "exiftool.calls").read_text().count("1"), 1)

    def test_duplicate_bundle_roles_fail(self):
        self.put("a/" + TRIO[0])
        self.put("b/" + TRIO[0])
        plan = scan(self.source)
        self.assertTrue(plan.errors)
        self.assertTrue(all(i.error for i in plan.items))

    def test_invalid_date_and_empty_media(self):
        self.put("DJI_20250230075842_0001_D.MP4")
        self.put("DJI_20251226075842_0001_D.MP4", b"")
        self.assertEqual(len(scan(self.source).unknown), 2)

    def test_companion_no_clobber_and_hash(self):
        path = self.put("DJI_20251226075842_0001_D.WAV")
        item = scan(self.source).items[0]
        out = self.root / "archive"
        self.assertEqual(archive_wav(item, out), "copied")
        self.assertEqual(archive_wav(item, out), "already_archived")
        destination = out / "pocket3/2025/2025-12-26" / path.name
        self.assertEqual(destination.read_bytes(), path.read_bytes())
        destination.write_bytes(b"other contents")
        with self.assertRaisesRegex(ImportFailure, "COLLISION"):
            archive_wav(item, out)
        self.assertEqual(destination.read_bytes(), b"other contents")
        self.assertFalse(list(out.rglob("*.partial")))

    def test_companion_rejects_symlink(self):
        path = self.put("DJI_20251226075842_0001_D.WAV")
        item = scan(self.source).items[0]
        out = self.root / "archive"
        target = out / "pocket3/2025/2025-12-26" / path.name
        target.parent.mkdir(parents=True)
        target.symlink_to(path)
        with self.assertRaises(ImportFailure):
            archive_wav(item, out)

    def test_changed_source_rejected(self):
        path = self.put("DJI_20251226075842_0001_D.WAV")
        item = scan(self.source).items[0]
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ImportFailure, "SOURCE CHANGED: .*size 34->7"):
            archive_wav(item, self.root / "out")

    def test_unstable_inode_or_ctime_is_not_a_change(self):
        from camera_importer.files import changed
        path = self.put("DJI_20251226075842_0001_D.WAV")
        item = scan(self.source).items[0]
        dev, ino, size, mtime, ctime = item.fingerprint
        # FAT/exFAT mounts renumber inodes and synthesize ctime; identity is size + mtime.
        self.assertIsNone(changed(path, (dev + 1, ino + 1, size, mtime, ctime + 1)))
        self.assertIn("mtime_ns", changed(path, (dev, ino, size, mtime + 1, ctime)))
        self.assertEqual(archive_wav(item, self.root / "out"), "copied")

    def test_output_must_not_overlap(self):
        for output in (self.source, self.source / "out", self.root):
            with self.assertRaises(ImportFailure):
                validate_roots(self.source, output, self.root / "manifest")
        with self.assertRaises(ImportFailure):
            validate_roots(self.source, Path("/volume1/immich/media/upload"), self.root / "manifest")

    def test_xmp_preserves_unrelated_properties(self):
        old = b'''<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:tiff="http://ns.adobe.com/tiff/1.0/" xmlns:exif="http://ns.adobe.com/exif/1.0/"><rdf:RDF><rdf:Description tiff:Make="old" exif:GPSLatitude="47,40N"><tiff:Model>old model</tiff:Model><exif:DateTimeOriginal>2025-01-01T12:00:00</exif:DateTimeOriginal></rdf:Description></rdf:RDF></x:xmpmeta>'''
        result = make_xmp(dict(ONERS, lensModel="5.7K 360 Lens"), old)
        root = ET.fromstring(result)
        desc = root.find(".//rdf:Description", NS)
        self.assertEqual(desc.get("{" + NS["tiff"] + "}Make"), "Arashi Vision")
        self.assertEqual(desc.get("{http://ns.adobe.com/exif/1.0/}GPSLatitude"), "47,40N")
        self.assertIn(b"2025-01-01T12:00:00", result)
        self.assertNotIn(b"old model", result)

    def test_unsafe_xml_rejected(self):
        with self.assertRaises(ImportFailure):
            make_xmp(POCKET, b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>')

    def test_correct_embedded_camera_needs_no_xmp(self):
        path = self.put("DJI_20251226075842_0001_D.JPG")
        item = scan(self.source).items[0]
        with patch("camera_importer.metadata.probe", return_value={"Make": "DJI", "Model": "DJI OsmoPocket3", "LensModel": "existing lens"}):
            prepare_metadata(item)
        self.assertIsNone(item.xmp)
        self.assertEqual(snapshot(path), item.fingerprint)

    def test_source_sidecar_association_and_conflict(self):
        name = "DJI_20251226075842_0001_D.MP4"
        self.put(name)
        self.put(name + ".xmp", make_xmp(POCKET))
        plan = scan(self.source)
        self.assertFalse(plan.unknown)
        self.assertTrue(plan.assets[0].sidecar)
        self.put(Path(name).with_suffix(".xmp").name, make_xmp(POCKET))
        self.assertTrue(scan(self.source).assets[0].error)

    def test_photo_detection_does_not_guess_lens(self):
        self.put("holiday.jpg")
        item = scan(self.source).items[0]
        with patch("camera_importer.metadata.probe", return_value={"Make": "Insta360", "Model": "Insta360 ONE RS", "FileType": "JPEG"}):
            prepare_metadata(item)
        # Camera-written EXIF is kept verbatim and verified as-is; no XMP rewrite.
        self.assertEqual(item.expected, {"make": "Insta360", "model": "Insta360 ONE RS"})
        self.assertEqual(item.embedded, {"make": "Insta360", "model": "Insta360 ONE RS"})
        self.assertIsNone(item.xmp)
        self.assertEqual(item.route, "timeline")

    def test_photo_without_embedded_camera_gets_xmp_but_video_is_normalized(self):
        self.put("DJI_20251226075842_0001_D.JPG")
        self.put("DJI_20251226075842_0001_D.MP4")
        plan = scan(self.source)
        photo, video = (next(i for i in plan.items if i.path.suffix == suffix) for suffix in (".JPG", ".MP4"))
        with patch("camera_importer.metadata.probe", return_value={"Make": "DJI", "Model": "OsmoPocket3"}):
            prepare_metadata(photo)
            prepare_metadata(video)
        self.assertIsNone(photo.xmp)
        self.assertEqual(photo.expected, {"make": "DJI", "model": "OsmoPocket3"})
        self.assertIsNotNone(video.xmp)
        self.assertEqual(video.expected, POCKET)
        self.assertEqual(video.embedded, {"make": "DJI", "model": "OsmoPocket3"})
        photo = scan(self.source).items[0]
        with patch("camera_importer.metadata.probe", return_value={"FileType": "JPEG"}):
            prepare_metadata(photo)
        self.assertIsNotNone(photo.xmp)
        self.assertEqual(photo.expected, POCKET)

    def test_photo_dates_follow_the_same_rule_as_videos(self):
        from camera_importer.runner import build_plan
        from camera_importer.capture_time import parse_date
        zoned = {"Make": "DJI", "Model": "PP-101", "FileType": "JPEG", "DateTimeOriginal": "2026:09:08 18:24:40", "OffsetTimeOriginal": "-07:00"}
        naive = {"Make": "DJI", "Model": "PP-101", "FileType": "JPEG", "DateTimeOriginal": "2026:09:08 18:24:40"}
        self.put("DJI_20260908182440_0001_D.JPG")
        self.put("DJI_20260908182440_0002_D.JPG")
        self.put("DJI_20260908182440_0003_D.MP4")
        tags = {"0001": zoned, "0002": naive, "0003": {"Make": "DJI", "Model": "DJI OsmoPocket3", "CreateDate": "2026:09:09 01:24:40"}}
        with patch("camera_importer.runner.probe_batch", side_effect=lambda paths, exe: {p: tags[p.name[19:23]] for p in paths}):
            plan = build_plan(self.source, Config(capture_timezone="America/Los_Angeles"), lambda m: None)
        by_name = {i.path.name[19:23]: i for i in plan.items}
        for key in ("0001", "0002", "0003"):
            self.assertEqual(parse_date(by_name[key].capture_time).isoformat(), "2026-09-08T18:24:40-07:00", key)
        self.assertTrue(by_name["0001"].capture_source.startswith("metadata:"))
        self.assertIsNone(by_name["0001"].xmp)  # reliable EXIF: Immich extracts it, nothing to deliver
        self.assertIn(b"DateTimeOriginal", by_name["0002"].xmp)  # naive EXIF: delivered like a video
        self.assertIn(b"DateTimeOriginal", by_name["0003"].xmp)
        with patch("camera_importer.runner.probe_batch", side_effect=lambda paths, exe: {p: tags[p.name[19:23]] for p in paths}):
            plan = build_plan(self.source, Config(), lambda m: None)
        self.assertEqual([i.error is None for i in sorted(plan.items, key=lambda i: i.path.name)], [True, False, False])

    def test_raw_and_rendered_pairs_form_stacks(self):
        for name in ["DJI_20251226080057_0003_D.JPG", "DJI_20251226080057_0003_D.DNG", "IMG_20250518_101759_00_001.jpg",
                     "IMG_20250518_101759_00_001.dng", "DJI_20251226080058_0004_D.DNG", "other/IMG_20250518_101759_00_001.dng"]:
            self.put(name)
        plan = scan(self.source)
        self.assertEqual(plan.stacks.keys(), {"DJI_20251226080057_0003_D.JPG", "IMG_20250518_101759_00_001.jpg"})
        self.assertEqual({i.path.name for i in plan.items if not i.stack}, {"DJI_20251226080058_0004_D.DNG", "IMG_20250518_101759_00_001.dng"})

    def test_wrong_camera_is_not_renamed(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        item = scan(self.source).items[0]
        with patch("camera_importer.metadata.probe", return_value={"Make": "DJI", "Model": "DJI Mavic 3"}):
            with self.assertRaisesRegex(ImportFailure, "conflicts"):
                prepare_metadata(item)
        # The conflicting values are still reported so the user can see them.
        self.assertEqual(item.embedded, {"make": "DJI", "model": "DJI Mavic 3"})

    def test_pocket3_product_code_is_the_same_camera(self):
        self.put("DJI_20251226080057_0003_D.JPG")
        self.put("holiday.jpg")
        for item in scan(self.source).items:
            with patch("camera_importer.metadata.probe", return_value={"Make": "DJI", "Model": "PP-101", "FileType": "JPEG"}):
                prepare_metadata(item)
            self.assertEqual(item.expected, {"make": "DJI", "model": "PP-101"})
            self.assertIsNone(item.xmp)
            self.assertEqual(item.route, "timeline")

    def test_higher_priority_lens_conflict_is_explicit(self):
        self.put(TRIO[0])
        with patch("camera_importer.metadata.probe", return_value={"LensID": "Unknown (123)"}):
            with self.assertRaisesRegex(ImportFailure, "would mask"):
                prepare_metadata(scan(self.source).items[0])

    def test_manifest_merge_and_conflict(self):
        self.put(TRIO[0])
        item = scan(self.source).items[0]
        item.sha1, item.sha256 = hashes(item.path)
        value = manifest.merge(None, KEY, OWNER, "http://localhost/api", [item])
        item.asset_id = str(uuid4())
        merged = manifest.merge(value, KEY, OWNER, "http://localhost/api", [item])
        self.assertEqual(merged["members"][0]["assetId"], item.asset_id)
        self.assertNotIn("assetId", value["members"][0])
        item.sha256 = "wrong"
        with self.assertRaisesRegex(ImportFailure, "MANIFEST CONFLICT"):
            manifest.merge(merged, KEY, OWNER, "http://localhost/api", [item])

    def test_manifest_visibility_follows_current_route(self):
        self.put(TRIO[0])
        item = scan(self.source).items[0]
        item.sha1, item.sha256 = hashes(item.path)
        value = manifest.merge(None, KEY, OWNER, "http://localhost/api", [item])
        self.assertEqual(value["members"][0]["visibility"], "timeline")
        item.route = "archive"  # An older manifest may still say archive; not an identity conflict.
        merged = manifest.merge(value, KEY, OWNER, "http://localhost/api", [item])
        self.assertEqual(merged["members"][0]["visibility"], "archive")


class IntegrationTest(Workspace):
    def setUp(self):
        super().setUp()
        self.server = FakeServer()
        self.config = Config(companion_root=self.root / "companions", manifest_root=self.root / "manifests",
                             capture_timezone="America/Los_Angeles", api_url=self.server.url, api_key="test-secret", verify_timeout=0.025, poll_interval=0.005)
        self.probe_patch = patch("camera_importer.metadata.probe", return_value={"FileType": "MP4"})
        self.cli_patch = patch("camera_importer.api.subprocess.run", return_value=subprocess.CompletedProcess([], 0))
        self.probe_patch.start()
        self.cli_patch.start()

    def tearDown(self):
        self.probe_patch.stop()
        self.cli_patch.stop()
        self.server.close()
        super().tearDown()

    def run_import(self, **kwargs):
        return execute(self.source, self.config, **kwargs)

    def test_full_flow_and_rerun(self):
        for name in TRIO + ["DJI_20251226075842_0001_D.MP4", "DJI_20251226075842_0001_D.WAV",
                            "LRV_20250518_101759_01_001.mp4", "DJI_20251226075842_0001_D.LRF"]:
            self.put(name)
        before = {p.name: (p.read_bytes(), snapshot(p)) for p in self.source.iterdir()}
        first = self.run_import()
        self.assertEqual(first["exitCode"], 0, first["errors"])
        self.assertEqual(len(self.server.uploads), 4)
        self.assertEqual(first["cameraMetadata"]["verified"], 4)
        self.assertEqual(len(self.server.stacks), 1)
        primary = next(iter(self.server.stacks.values()))[0]
        self.assertEqual(self.server.assets[primary]["info"]["originalFileName"], TRIO[2])
        self.assertEqual({a["info"]["visibility"] for a in self.server.assets.values()}, {"timeline"})
        for upload in self.server.uploads:
            self.assertNotIn("filename", upload)
            self.assertEqual(upload["sidecarData"][0], upload["assetData"][0] + ".xmp")
            self.assertEqual(upload["assetData"][1], before[upload["assetData"][0]][0])
        manifest_path = self.config.manifest_root / "insta360" / (KEY + ".json")
        manifest_before = manifest_path.read_bytes()
        second = self.run_import()
        self.assertEqual(second["exitCode"], 0, second["errors"])
        self.assertEqual(len(self.server.uploads), 4)
        self.assertEqual(second["counts"]["companion"], {"already_archived": 1})
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(before, {p.name: (p.read_bytes(), snapshot(p)) for p in self.source.iterdir()})

    def test_dry_run_no_writes_or_http(self):
        self.put(TRIO[0])
        self.put("DJI_20251226075842_0001_D.WAV")
        report = self.run_import(dry_run=True)
        self.assertEqual(report["exitCode"], 1)
        self.assertFalse(self.config.companion_root.exists())
        self.assertFalse(self.config.manifest_root.exists())
        self.assertFalse(self.server.calls)

    def test_ignored_only_directory_cannot_be_archive_success(self):
        self.put("LRV_20250518_101759_01_001.mp4")
        self.assertEqual(self.run_import()["exitCode"], 1)

    def test_media_stream_is_read_in_bounded_blocks(self):
        self.put("DJI_20251226075842_0001_D.MP4", b"x" * (3 * 1024 * 1024))
        sizes = []

        class Guard:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def read(self, size=-1):
                if size < 0 or size > 1024 * 1024:
                    raise AssertionError("Unbounded media read")
                sizes.append(size)
                return self.stream.read(size)

        @contextlib.contextmanager
        def guarded(path, expected):
            with readonly(path, expected) as stream:
                yield Guard(stream)

        with patch("camera_importer.api.readonly", guarded):
            report = self.run_import()
        self.assertEqual(report["exitCode"], 0, report["errors"])
        self.assertGreater(len(sizes), 1)

    def test_lrv_removed_after_import_keeps_existing_stack(self):
        for name in TRIO:
            self.put(name)
        self.assertEqual(self.run_import()["exitCode"], 0)
        (self.source / TRIO[2]).unlink()
        report = self.run_import()
        self.assertEqual(report["exitCode"], 0, report["errors"])
        self.assertEqual(report["bundles"]["lrvMissing"], [KEY])
        self.assertEqual(len(self.server.stacks), 1)
        self.assertEqual(sum(1 for m, _ in self.server.calls if m == "DELETE"), 0)
        self.assertEqual(len(self.server.uploads), 3)

    def test_partial_bundle_stack_is_rebuilt_when_members_arrive(self):
        self.put(TRIO[0])
        self.put(TRIO[1])
        first = self.run_import()
        self.assertEqual(first["exitCode"], 0, first["errors"])
        old_stack = next(iter(self.server.stacks))
        self.assertEqual(self.server.assets[self.server.stacks[old_stack][0]]["info"]["originalFileName"], TRIO[0])
        self.put(TRIO[2])
        second = self.run_import()
        self.assertEqual(second["exitCode"], 0, second["errors"])
        self.assertEqual(len(self.server.stacks), 1)
        self.assertNotIn(old_stack, self.server.stacks)
        members = next(iter(self.server.stacks.values()))
        self.assertEqual(self.server.assets[members[0]]["info"]["originalFileName"], TRIO[2])
        self.assertEqual(len(members), 3)
        self.assertEqual(len(self.server.assets), 3)

    def test_slow_extraction_does_not_block_stacking(self):
        for name in TRIO:
            self.put(name)
        self.server.ignore_sidecar = True  # extraction never completes for the sidecar fields
        report = self.run_import()
        self.assertEqual(report["exitCode"], 1)
        self.assertEqual(len(self.server.stacks), 1)
        self.assertTrue(all("timed out" in i["error"] for i in report["items"] if i["route"] == "timeline"))
        self.assertTrue(all(i["stackId"] for i in report["items"] if i["route"] == "timeline"))
        # Uploads all happen before any metadata wait: the jobs endpoint is never used for fresh uploads.
        self.assertFalse(any(path == "/api/assets/jobs" for _, path in self.server.calls))

    def test_rerun_fills_missing_manifest_asset_id(self):
        for name in TRIO:
            self.put(name)
        self.assertEqual(self.run_import()["exitCode"], 0)
        path = self.config.manifest_root / "insta360" / (KEY + ".json")
        value = json.loads(path.read_text())
        for member in value["members"]:
            member.pop("assetId")
        path.write_text(json.dumps(value))
        self.assertEqual(self.run_import()["exitCode"], 0)
        self.assertTrue(all(m.get("assetId") for m in json.loads(path.read_text())["members"]))
        self.assertEqual(len(self.server.uploads), 3)

    def test_strict_aborts_before_wav_and_upload(self):
        self.put("DJI_20251226075842_0001_D.WAV")
        self.put("unknown.srt")
        report = self.run_import(strict=True)
        self.assertEqual(report["exitCode"], 1)
        self.assertFalse(self.config.companion_root.exists())
        self.assertFalse(self.config.manifest_root.exists())
        self.assertFalse(self.server.uploads)

    def test_unknown_default_imports_known_but_fails(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.put("unknown.srt")
        report = self.run_import()
        self.assertEqual(report["exitCode"], 1)
        self.assertEqual(len(self.server.uploads), 1)

    def test_metadata_failure_is_not_success(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.server.ignore_sidecar = True
        report = self.run_import()
        self.assertEqual(report["exitCode"], 1)
        self.assertTrue(any("timed out" in e for e in report["errors"]))

    def test_existing_wrong_capture_time_fails_without_reupload(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.assertEqual(self.run_import()["exitCode"], 0)
        asset = next(iter(self.server.assets.values()))
        asset["info"]["localDateTime"] = "2026-09-09T02:17:00Z"
        self.server.ignore_sidecar = True
        result = self.run_import()
        self.assertEqual(result["exitCode"], 1)
        self.assertEqual(len(self.server.uploads), 1)
        self.assertTrue(any("capture date/time/timezone" in error for error in result["errors"]))

    def test_missing_capture_timezone_blocks_video_upload(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.config.capture_timezone = ""
        result = self.run_import()
        self.assertEqual(result["exitCode"], 1)
        self.assertFalse(self.server.uploads)

    def test_existing_missing_xmp_is_not_reuploaded(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.assertEqual(self.run_import()["exitCode"], 0)
        asset = next(iter(self.server.assets.values()))
        asset["xmp"], asset["info"]["exifInfo"] = None, {}
        report = self.run_import()
        self.assertEqual(report["exitCode"], 1)
        self.assertEqual(len(self.server.uploads), 1)

    def test_visibility_is_repaired_and_checked(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.assertEqual(self.run_import()["exitCode"], 0)
        asset = next(iter(self.server.assets.values()))
        asset["info"]["visibility"] = "archive"
        self.assertEqual(self.run_import()["exitCode"], 0)
        self.assertEqual(asset["info"]["visibility"], "timeline")
        asset["info"]["visibility"] = "archive"
        self.server.bad_visibility = True
        self.assertEqual(self.run_import()["exitCode"], 1)

    def test_trashed_asset_fails_without_upload(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.run_import()
        next(iter(self.server.assets.values()))["info"]["isTrashed"] = True
        self.assertEqual(self.run_import()["exitCode"], 1)
        self.assertEqual(len(self.server.uploads), 1)

    def test_checksum_mismatch_fails(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.server.bad_checksum = True
        self.assertEqual(self.run_import()["exitCode"], 1)

    def test_response_loss_rerun_recovers_without_duplicate(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.server.drop_response = True
        self.assertEqual(self.run_import()["exitCode"], 1)
        self.server.drop_response = False
        self.assertEqual(self.run_import()["exitCode"], 0)
        self.assertEqual(len(self.server.assets), 1)
        self.assertEqual(len(self.server.uploads), 1)

    def test_new_file_during_import_fails(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.server.on_upload = lambda: self.put("new.NEW")
        self.assertEqual(self.run_import()["exitCode"], 1)

    def test_server_message_is_reported_but_other_fields_are_not(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.server.quota_exceeded = True
        report = self.run_import()
        self.assertEqual(report["exitCode"], 1)
        self.assertTrue(any("HTTP 400: POST /assets (Quota has been exceeded!)" in e for e in report["errors"]), report["errors"])
        self.assertNotIn("Bad Request", json.dumps(report))

    def test_missing_job_permission_is_a_warning_when_extraction_matches(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.server.forbid_jobs = True
        # A fresh upload never needs the refresh job: Immich extracts on its own.
        report = self.run_import()
        self.assertEqual(report["exitCode"], 0, report["errors"])
        self.assertEqual(report["warnings"], [])
        self.assertFalse(any(path == "/api/assets/jobs" for _, path in self.server.calls))
        # An existing asset with wrong metadata asks for a refresh; without the permission it is a warning.
        next(iter(self.server.assets.values()))["info"]["exifInfo"] = {}
        report = self.run_import()
        self.assertEqual(report["exitCode"], 1)
        self.assertTrue(any("job.create" in w for w in report["warnings"]))
        self.assertTrue(any("timed out" in e for e in report["errors"]))
        # A photo whose embedded EXIF already matches needs no refresh at all.
        self.put("DJI_20251226075842_0001_D.MP4", b"")
        os.unlink(self.source / "DJI_20251226075842_0001_D.MP4")
        self.put("DJI_20251226080057_0003_D.JPG")
        with patch("camera_importer.runner.probe_batch", side_effect=lambda paths, exe: {p: {"Make": "DJI", "Model": "PP-101", "FileType": "JPEG"} for p in paths}):
            self.server.on_upload = lambda: [a["info"]["exifInfo"].update({"make": "DJI", "model": "PP-101"}) for a in self.server.assets.values()]
            report = self.run_import()
        self.assertEqual(report["exitCode"], 0, report["errors"])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["cameraMetadata"]["verified"], 1)

    def test_upload_failure_does_not_leak_secret(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        self.server.upload_failure = True
        report = self.run_import()
        self.assertEqual(report["exitCode"], 1)
        self.assertNotIn("test-secret", json.dumps(report))

    def test_same_checksum_conflicting_roles_fails(self):
        for name in TRIO:
            self.put(name, b"same bytes")
        report = self.run_import()
        self.assertEqual(report["exitCode"], 1)
        # Hashes come from the upload stream now, so the first copy is sent once;
        # the identical members are then all failed and never stacked.
        self.assertEqual(len(self.server.uploads), 1)
        self.assertEqual(len(self.server.stacks), 0)
        self.assertTrue(all(i["error"] for i in report["items"]))
        # Known hashes (from the index) are checked before any upload on rerun.
        report = self.run_import()
        self.assertEqual(report["exitCode"], 1)
        self.assertEqual(len(self.server.uploads), 1)

    def test_hash_index_skips_reads_on_rerun_and_survives_dropped_response(self):
        self.put("DJI_20251226075842_0001_D.MP4", b"m" * 3000000)
        self.put("DJI_20251226075842_0001_D.WAV", b"w" * 20000)
        first = self.run_import()
        self.assertEqual(first["exitCode"], 0, first["errors"])
        self.assertEqual(first["hashSources"], {"upload": 1, "read": 1})
        index = json.loads((self.config.manifest_root / "hashes.json").read_text())["entries"]
        self.assertEqual(len(index), 2)
        real_hashes = hashes

        def destination_only(path, expected=None):
            if str(path).startswith(str(self.source)):
                raise AssertionError("no source read expected")
            return real_hashes(path, expected)
        with patch("camera_importer.runner.hashes", side_effect=AssertionError("no read expected")), \
                patch("camera_importer.files.hashes", side_effect=destination_only):
            second = self.run_import()
        self.assertEqual(second["exitCode"], 0, second["errors"])
        self.assertEqual(second["hashSources"], {"index": 2})
        self.assertEqual(len(self.server.uploads), 1)
        # A modified file misses the index (fingerprint changes) and is re-hashed.
        self.put("DJI_20251226075842_0001_D.MP4", b"n" * 3000000)
        third = self.run_import()
        self.assertEqual(third["hashSources"], {"index": 1, "upload": 1})
        self.assertEqual(len(self.server.uploads), 2)
        # Streamed hashes are indexed even when the upload response is lost.
        self.put("DJI_20251226075843_0002_D.MP4", b"o" * 3000000)
        self.server.drop_response = True
        self.assertEqual(self.run_import()["exitCode"], 1)
        self.server.drop_response = False
        with patch("camera_importer.runner.hashes", side_effect=AssertionError("no read expected")):
            fifth = self.run_import()
        self.assertEqual(fifth["exitCode"], 0, fifth["errors"])
        self.assertEqual(fifth["hashSources"], {"index": 3})
        self.assertEqual(len(self.server.uploads), 3)

    def test_manifest_conflict_prevents_upload(self):
        for name in TRIO:
            self.put(name)
        self.assertEqual(self.run_import()["exitCode"], 0)
        old = (self.config.manifest_root / "insta360" / (KEY + ".json")).read_bytes()
        self.put(TRIO[0], b"changed master content")
        report = self.run_import()
        self.assertEqual(report["exitCode"], 1)
        self.assertEqual(len(self.server.uploads), 3)
        self.assertEqual((self.config.manifest_root / "insta360" / (KEY + ".json")).read_bytes(), old)

    def test_debug_mode_never_safe(self):
        self.put("DJI_20251226075842_0001_D.MP4")
        report = self.run_import(metadata_verify=False)
        self.assertEqual(report["exitCode"], 1)
        self.assertEqual(report["cameraMetadata"]["verified"], 0)

    def test_redirect_not_followed(self):
        self.server.redirect = True
        with self.assertRaisesRegex(ImportFailure, "302"):
            self.run_import()
        self.assertFalse(any(path == "/do-not-follow" for _, path in self.server.calls))

    def test_check_config_reports_each_check_without_writes(self):
        from camera_importer.runner import check_config
        fake = self.root / "tool"
        fake.write_text("#!/bin/sh\necho 13.59\n")
        fake.chmod(0o755)
        self.config.exiftool_bin = self.config.immich_bin = str(fake)
        self.cli_patch.stop()  # Run the fake tools for real; restarted below for tearDown.
        try:
            report = check_config(self.source, self.config)
            self.assertEqual(report["exitCode"], 0, report["errors"])
            self.assertEqual([c["status"] for c in report["checks"]], ["ok"] * 7)
            self.assertIn("13.59", report["checks"][3]["detail"])
            self.assertFalse(self.config.companion_root.exists())
            self.assertNotIn("test-secret", json.dumps(report))
            self.config.exiftool_bin = str(self.root / "missing")
            self.server.user_name = "Other"
            report = check_config(self.source, self.config)
            self.assertEqual(report["exitCode"], 1)
            self.assertEqual([c["name"][:8] for c in report["checks"] if c["status"] == "failed"], ["exiftool", "Immich s"])
        finally:
            self.cli_patch.start()

    def test_wrong_user_or_version_aborts(self):
        self.server.user_name = "Other User"
        with self.assertRaisesRegex(ImportFailure, "account"):
            self.run_import()
        self.server.user_name = "Camera Archive"
        self.server.version["major"] = 4
        with self.assertRaisesRegex(ImportFailure, "version"):
            self.run_import()


class HashIndexTest(unittest.TestCase):
    def test_prune_by_age_and_size(self):
        from datetime import datetime, timedelta, timezone
        from camera_importer import hashcache
        now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        entries = {}
        for day in range(300):
            entries[str(day)] = {"sha1": "a" * 40, "sha256": "b" * 64, "size": 1, "path": "x", "seen": (now - timedelta(days=day)).isoformat()}
        entries["bad"] = {"sha1": "a" * 40, "sha256": "b" * 64, "size": 1, "path": "x", "seen": "not a date"}
        kept = hashcache.prune(entries, now)
        self.assertEqual(len(kept), 181)
        self.assertNotIn("bad", kept)
        with patch.object(hashcache, "MAX_ENTRIES", 10):
            kept = hashcache.prune(entries, now)
        self.assertEqual(sorted(int(k) for k in kept), list(range(10)))


class AuthTest(unittest.TestCase):
    def test_scalar_parser_quotes_comments_and_rejections(self):
        self.assertEqual(read_auth_scalars("url: http://localhost/api # comment\nkey: 'abc''def'\n")["key"], "abc'def")
        self.assertEqual(read_auth_scalars('key: "abc\\u0031" # comment\n')["key"], "abc1")
        for value in ("key: &anchor secret", "key: !!str secret", "key: |\n  secret", "key: a\nkey: b"):
            with self.assertRaises(ValueError):
                read_auth_scalars(value)

    def test_standalone_runs_without_site_packages_or_source_tree(self):
        script = Path(__file__).resolve().parents[1] / "camera-import.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(script, root / "camera-import.py")
            source = root / "card"
            source.mkdir()
            (source / "DJI_20251226075842_0001_D.WAV").write_bytes(b"audio")
            result = subprocess.run([sys.executable, "-I", "-S", str(root / "camera-import.py"), "--dry-run", "--json", str(source)], capture_output=True, check=True, cwd=root)
            self.assertEqual(json.loads(result.stdout)["result"], "DRY RUN OK")
            self.assertEqual(len(list(root.iterdir())), 2)

    def test_read_cli_auth_and_reject_cross_server(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "auth.yml").write_text('url: "http://localhost:2283/api"\nkey: "private-value"\n')
            config = Config(auth_dir=path)
            authenticate(config)
            self.assertEqual(config.api_url, "http://localhost:2283/api")
            self.assertEqual(config.api_key, "private-value")
            with self.assertRaises(ImportFailure):
                authenticate(Config(auth_dir=path, api_url="http://other-host/api"))

    def test_old_cli_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "auth.yml").write_text("instanceUrl: http://localhost:2283\napiKey: value\n")
            config = Config(auth_dir=path)
            authenticate(config)
            self.assertEqual(config.api_url, "http://localhost:2283/api")

    def test_cli_override_beats_env_and_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"companion_root": "/tmp/config"}))
            with patch.dict(os.environ, {"COMPANION_ROOT": "/tmp/env"}):
                config = load_config(parser().parse_args(["--config", str(path), "--companion-root", "/tmp/cli"]))
            self.assertEqual(config.companion_root, Path("/tmp/cli"))

    def test_json_error_is_single_object(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--json", "/nonexistent-camera-import-test-directory"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["result"], "NOT SAFE TO FORMAT SOURCE")


class RealExifToolTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("EXIFTOOL_BIN") or shutil.which("exiftool"), "ExifTool not installed")
    def test_generated_xmp_is_readable_with_exact_lens(self):
        executable = os.environ.get("EXIFTOOL_BIN", "exiftool")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "original.insv.xmp"
            for lens in ("4K Boost Lens", "5.7K 360 Lens"):
                path.write_bytes(make_xmp(dict(ONERS, lensModel=lens), capture_time="2026-09-08T18:24:40-07:00"))
                result = subprocess.run([executable, "-json", str(path)], capture_output=True, check=True)
                tags = json.loads(result.stdout)[0]
                self.assertEqual(tags["Make"], "Arashi Vision")
                self.assertEqual(tags["Model"], "Insta360 OneRS")
                self.assertNotIn("LensID", tags)
                self.assertEqual(tags["LensModel"], lens)
                self.assertEqual(tags["DateTimeOriginal"], "2026:09:08 18:24:40-07:00")

    @unittest.skipUnless((os.environ.get("EXIFTOOL_BIN") or shutil.which("exiftool")) and shutil.which("ffmpeg"), "ExifTool / ffmpeg not installed")
    def test_real_mp4_probe_and_xmp_leave_source_unchanged(self):
        executable = os.environ.get("EXIFTOOL_BIN", "exiftool")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            path = source / "VID_20250518_101759_00_001.mp4"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=size=16x16:rate=1", "-t", "1", "-c:v", "mpeg4", str(path)], check=True)
            before = (path.read_bytes(), snapshot(path))
            item = scan(source).assets[0]
            prepare_metadata(item, executable)
            self.assertIsNotNone(item.xmp)
            self.assertEqual(item.expected["lensModel"], "4K Boost Lens")
            self.assertEqual(before, (path.read_bytes(), snapshot(path)))
            self.assertEqual(list(source.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()

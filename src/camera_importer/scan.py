import os
import re
import stat
from datetime import datetime
from pathlib import Path

from .files import fingerprint
from .model import Item, Plan

# Pocket 3: DJI_YYYYMMDDHHMMSS_NNNN_D.EXT (MP4/JPG/DNG media, WAV/AAC audio backup, LRF proxy).
DJI = re.compile(r"^DJI_(\d{8})(\d{6})_.+\.(jpg|jpeg|dng|mp4|wav|aac|lrf)$", re.I)
# ONE RS: [PRO_]{VID|LRV}_YYYYMMDD_HHMMSS_{00,01,10,11}_XXX.{mp4|insv}; PRO_ marks HDR/PRO
# mode files that Insta360 apps post-process. Channel: first digit = lens, second = proxy.
INSTA = re.compile(r"^(PRO_)?(VID|LRV)_(\d{8})_(\d{6})_(\d{2})_(\d+)\.(mp4|insv)$", re.I)
PHOTO = re.compile(r"^(?:PRO_)?IMG_(\d{8})_(\d{6})_(\d{2})_(\d+)\.(insp|jpg|jpeg|dng)$", re.I)
BUNDLE_ROLES = {"master-00": "VID_{}_00_{}.insv", "master-10": "VID_{}_10_{}.insv", "lrv-11": "LRV_{}_11_{}.insv"}
POCKET = {"make": "DJI", "model": "DJI OsmoPocket3"}
ONERS = {"make": "Insta360", "model": "Insta360 OneRS"}


def capture_date(day, clock):
    return datetime.strptime(day + clock, "%Y%m%d%H%M%S").date().isoformat()


def hidden_directory(name):
    """DSM metadata directories (@eaDir, @Recycle) and dot directories are never camera media."""
    return name.startswith(("@", "."))


def classify(path, relative):
    item = Item(path, relative, "unknown", "Unrecognized file; needs review")
    # Only explicitly recognized thumbnail image types inside a Thumb directory.
    if "thumb" in [p.lower() for p in Path(relative).parts[:-1]] and path.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".thm"):
        item.route, item.reason = "ignored", "thumbnail/cache"
        return item
    match = DJI.fullmatch(path.name)
    if match:
        day, clock, extension = match.groups()
        item.date = capture_date(day, clock)
        item.expected = dict(POCKET)
        extension = extension.lower()
        item.route = "companion" if extension in ("wav", "aac") else "ignored" if extension == "lrf" else "timeline"
        item.reason = "Pocket 3 LRF proxy" if extension == "lrf" else "Pocket 3 " + extension.upper()
        return item
    match = INSTA.fullmatch(path.name)
    if match:
        pro, prefix, day, clock, channel, sequence, extension = match.groups()
        pro, prefix, extension = "PRO_" if pro else "", prefix.upper(), extension.lower()
        mode = "HDR/PRO " if pro else ""
        item.date = capture_date(day, clock)
        item.expected = dict(ONERS)
        if extension == "mp4":
            # 4K Boost flat video: VID master on channel 00 or 10, LRV proxy on 01 or 11.
            if (prefix, channel) in (("VID", "00"), ("VID", "10"), ("LRV", "01"), ("LRV", "11")):
                item.route = "timeline" if prefix == "VID" else "ignored"
                item.reason = "Insta360 4K Boost " + mode + ("video" if prefix == "VID" else "LRV proxy")
                item.expected["lensModel"] = "4K Boost Lens"
        elif (prefix, channel) in (("VID", "00"), ("VID", "10"), ("LRV", "11")):
            item.route = "timeline" if prefix == "LRV" else "archive"
            item.reason = "Insta360 " + mode + "360 bundle"
            item.bundle = f"{pro}{day}_{clock}_{sequence}"
            item.role = "lrv-11" if prefix == "LRV" else "master-" + channel
            item.expected["lensModel"] = "5.7K 360 Lens"
        return item
    if path.suffix.lower() in (".insp", ".jpg", ".jpeg", ".dng"):
        # Metadata must confirm camera AND independent image representation.
        item.route, item.reason = "probe", "Photo requires camera metadata identification"
    return item


def scan(source):
    plan = Plan(source)

    def visit(directory):
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda e: e.name)
        except OSError:
            plan.errors.append("SCAN FAILED: " + str(directory))
            return
        for entry in entries:
            path = Path(entry.path)
            relative = str(path.relative_to(source))
            try:
                st = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode):
                    if hidden_directory(entry.name):
                        plan.skipped.append(relative)
                        continue
                    visit(path)
                elif stat.S_ISREG(st.st_mode):
                    try:
                        item = classify(path, relative)
                    except ValueError:
                        item = Item(path, relative, "unknown", "Invalid date/time in camera filename")
                    item.size, item.fingerprint = st.st_size, fingerprint(st)
                    if st.st_size == 0 and item.route != "ignored":
                        item.route, item.reason = "unknown", "Empty file"
                    plan.items.append(item)
                else:
                    plan.items.append(Item(path, relative, "unknown", "Symlink or special file; not followed"))
            except OSError:
                plan.errors.append("SCAN FAILED: " + relative)

    visit(source)
    by_path = {i.path: i for i in plan.items}
    sidecar_index = {}
    for path in by_path:
        if path.suffix.lower() == ".xmp":
            sidecar_index.setdefault((path.parent, path.name.lower()), []).append(path)
    used = set()
    for item in list(plan.items):
        if item.route not in ("timeline", "archive", "probe"):
            continue
        candidates = []
        for name in ((item.path.name + ".xmp").lower(), item.path.with_suffix(".xmp").name.lower()):
            candidates.extend(sidecar_index.get((item.path.parent, name), []))
        if len(candidates) > 1:
            item.error = "Multiple XMP candidates; refusing ambiguous sidecar selection"
        elif candidates:
            p = candidates[0]
            if p in used or by_path[p].fingerprint is None:
                item.error = "Shared or non-regular XMP sidecar needs review"
            else:
                item.sidecar = p
                item.sidecar_fingerprint = by_path[p].fingerprint
                used.add(p)
                by_path[p].route, by_path[p].reason = "sidecar", "Preserved with associated media"
    for item in plan.items:
        if item.bundle:
            plan.bundles.setdefault(item.bundle, []).append(item)
    for key, members in plan.bundles.items():
        roles = [m.role for m in members]
        pro, stamp = ("PRO_", key[4:]) if key.startswith("PRO_") else ("", key)
        day_clock, seq = stamp.rsplit("_", 1)
        expected = {role: pro + name.format(day_clock, seq) for role, name in BUNDLE_ROLES.items()}
        missing = [name for role, name in expected.items() if role not in roles]
        if "lrv-11" not in roles:
            # The LRV proxy is derivable from the masters, so its absence loses
            # nothing; without it the front master becomes the timeline entry.
            plan.lrv_missing.append(key)
            for member in members:
                if member.role == "master-00":
                    member.route, member.reason = "timeline", "Insta360 360 bundle (no LRV; master shown in timeline)"
            missing = [name for name in missing if "LRV_" not in name]
        if missing:
            plan.incomplete[key] = missing
        if len(roles) != len(set(roles)):
            plan.errors.append("AMBIGUOUS 360 BUNDLE (duplicate roles): " + key)
            for member in members:
                member.error = "Duplicate bundle role; needs review"
    # Multiple same-format photo members may form an undocumented photo bundle.
    photo_groups = {}
    for item in plan.items:
        match = PHOTO.fullmatch(item.path.name)
        if match:
            day, clock, channel, seq, extension = match.groups()
            photo_groups.setdefault((item.path.parent, day, clock, seq, extension.lower()), []).append(item)
    for members in photo_groups.values():
        if len(members) > 1:
            for item in members:
                item.route, item.reason = "unknown", "Possible multi-file Insta360 photo bundle; needs review"
    return plan

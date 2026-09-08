"""Read with ExifTool; merge camera properties into an in-memory XMP document."""
import json
import subprocess
import xml.etree.ElementTree as ET

from defusedxml.ElementTree import fromstring

from .files import readonly, snapshot
from .model import ImportFailure
from .scan import ONERS, POCKET

NS = {"x": "adobe:ns:meta/", "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
      "tiff": "http://ns.adobe.com/tiff/1.0/", "aux": "http://ns.adobe.com/exif/1.0/aux/",
      "exifEX": "http://cipa.jp/exif/1.0/"}
for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)


def probe(path, executable="exiftool"):
    try:
        result = subprocess.run([executable, "-api", "largefilesupport=1", "-json", "-Make", "-Model", "-LensID", "-LensType",
                                 "-LensSpec", "-LensModel", "-Lens", "-FileType", "-NumberOfImages",
                                 "-ProjectionType", "-Error", "-Warning", str(path)],
                                capture_output=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ImportFailure("ExifTool unavailable or timed out; configure EXIFTOOL_BIN") from None
    try:
        entries = json.loads(result.stdout)
        tags = entries[0]
        if result.returncode or not isinstance(tags, dict) or tags.get("Error"):
            raise ValueError()
        return tags
    except (ValueError, IndexError, TypeError):
        raise ImportFailure("ExifTool could not read media metadata: " + path.name) from None


def normalize(value):
    return "".join(c.lower() for c in str(value) if c.isalnum())


def confirm_camera(tags, expected):
    make, model = normalize(tags.get("Make", "")), normalize(tags.get("Model", ""))
    allowed = ({"dji", "szdji"}, {"djiosmopocket3", "osmopocket3", "djipocket3", "pocket3"}) if expected == POCKET else (
        {"insta360", "arashivision"}, {"insta360oners", "oners"})
    if (make and make not in allowed[0]) or (model and model not in allowed[1]):
        raise ImportFailure("Camera metadata conflicts with filename classification")


def identify_photo(item, tags):
    model = normalize(tags.get("Model", ""))
    if model in ("insta360oners", "oners"):
        expected = dict(ONERS)
    elif model in ("djiosmopocket3", "osmopocket3", "djipocket3", "pocket3"):
        expected = dict(POCKET)
    else:
        raise ImportFailure("NEEDS REVIEW: photo camera model is not identifiable")
    confirm_camera(tags, expected)
    if tags.get("FileType") not in ("JPEG", "DNG") or int(tags.get("NumberOfImages", 1)) > 1:
        raise ImportFailure("NEEDS REVIEW: photo is not a confirmed independent JPEG/DNG")
    if expected == ONERS:
        # Never infer a lens from the extension alone.
        values = {str(tags.get(k, "")) for k in ("LensID", "LensType", "LensSpec", "LensModel", "Lens")}
        lenses = values & {"4K Boost Lens", "5.7K 360 Lens"}
        if len(lenses) > 1:
            raise ImportFailure("NEEDS REVIEW: conflicting photo lens metadata")
        if lenses:
            expected["lensModel"] = lenses.pop()
    item.expected, item.route, item.reason = expected, "timeline", "Metadata-confirmed camera photo"


def set_property(root, namespace, name, value):
    key = "{" + NS[namespace] + "}" + name
    # Replace only the exact camera property, preserving unrelated XMP content.
    for element in root.iter():
        element.attrib.pop(key, None)
        for child in list(element):
            if child.tag == key:
                element.remove(child)
    description = root.find(".//rdf:Description", NS)
    if description is None:
        rdf = root if root.tag == "{" + NS["rdf"] + "}RDF" else root.find(".//rdf:RDF", NS)
        if rdf is None:
            raise ImportFailure("XMP has no RDF container")
        description = ET.SubElement(rdf, "{" + NS["rdf"] + "}Description")
        description.set("{" + NS["rdf"] + "}about", "")
    description.set(key, value)


def make_xmp(expected, original=None):
    if original:
        try:
            root = fromstring(original)
        except Exception:
            raise ImportFailure("Invalid or unsafe source XMP") from None
    else:
        root = ET.Element("{" + NS["x"] + "}xmpmeta")
        ET.SubElement(root, "{" + NS["rdf"] + "}RDF")
    set_property(root, "tiff", "Make", expected["make"])
    set_property(root, "tiff", "Model", expected["model"])
    if "lensModel" in expected:
        # aux:LensID expects a numeric identifier. Writing a lens name there
        # creates Composite:LensID = "Unknown (...)" in ExifTool, which masks
        # LensModel in Immich. Use the standard textual property instead.
        set_property(root, "exifEX", "LensModel", expected["lensModel"])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def prepare_metadata(item, executable="exiftool"):
    if snapshot(item.path) != item.fingerprint:
        raise ImportFailure("SOURCE CHANGED: " + item.relative)
    tags = probe(item.path, executable)
    if snapshot(item.path) != item.fingerprint:
        raise ImportFailure("SOURCE CHANGED: " + item.relative)
    if item.route == "probe":
        identify_photo(item, tags)
    else:
        confirm_camera(tags, {k: v for k, v in item.expected.items() if k != "lensModel"})
    if "lensModel" in item.expected:
        higher = next((tags[k] for k in ("LensID", "LensType", "LensSpec") if tags.get(k) is not None), None)
        if higher is not None and higher != item.expected["lensModel"]:
            raise ImportFailure("NEEDS REVIEW: embedded LensID/LensType/LensSpec would mask the required LensModel")
    original = None
    if item.sidecar:
        with readonly(item.sidecar, item.sidecar_fingerprint) as stream:
            original = stream.read(8 * 1024 * 1024 + 1)
        if len(original) > 8 * 1024 * 1024:
            raise ImportFailure("Source XMP exceeds 8 MiB review limit")
    actual = {"make": tags.get("Make"), "model": tags.get("Model")}
    actual["lensModel"] = next((tags[k] for k in ("LensID", "LensType", "LensSpec", "LensModel") if tags.get(k) is not None), None)
    # Existing sidecars may override source camera fields; always normalize their
    # camera properties. Already-correct embedded metadata needs no synthetic XMP.
    if original or any(actual.get(k) != v for k, v in item.expected.items()):
        item.xmp = make_xmp(item.expected, original)

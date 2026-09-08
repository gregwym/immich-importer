import base64
import http.client
import json as jsonlib
import os
from urllib.parse import urlsplit
import subprocess
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4


from .files import Hasher, readonly
from .model import ImportFailure



class Multipart:
    """Known-length multipart iterator; media is read in bounded blocks."""
    def __init__(self, fields, observe=None):
        self.observe = observe  # Called with every streamed media block.
        boundary = "camera-import-" + uuid4().hex
        self.content_type = "multipart/form-data; boundary=" + boundary
        self.parts = []
        self.length = 0
        for name, value in fields.items():
            header = "--" + boundary + '\r\nContent-Disposition: form-data; name="' + name + '"'
            if isinstance(value, tuple):
                filename, body, mime = value
                filename = filename.replace("%", "%25").replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A")
                header += '; filename="' + filename + '"\r\nContent-Type: ' + mime
            else:
                body = value.encode("utf-8")
            header = (header + "\r\n\r\n").encode("utf-8")
            size = len(body) if isinstance(body, bytes) else os.fstat(body.fileno()).st_size - body.tell()
            self.parts.append((header, body, size))
            self.length += len(header) + size + 2
        self.tail = ("--" + boundary + "--\r\n").encode("ascii")
        self.length += len(self.tail)

    def __iter__(self):
        for header, body, size in self.parts:
            yield header
            if isinstance(body, bytes):
                yield body
            else:
                remaining = size
                while remaining:
                    block = body.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ImportFailure("Source became shorter during upload")
                    remaining -= len(block)
                    if self.observe:
                        self.observe(block)
                    yield block
            yield b"\r\n"
        yield self.tail


def asset_id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ImportFailure("Invalid asset ID in Immich response") from None


class HttpFailure(ImportFailure):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def server_message(body):
    """Only the server's own `message` field, printable and bounded; never headers or secrets."""
    try:
        value = jsonlib.loads(body).get("message")
    except (ValueError, UnicodeError, AttributeError):
        return ""
    if isinstance(value, list):
        value = "; ".join(str(v) for v in value)
    if not isinstance(value, str):
        return ""
    text = "".join(c if c.isprintable() else " " for c in value).strip()
    return text[:200]


class Immich:
    def __init__(self, config, log=lambda message: None):
        self.config, self.log = config, log
        self.owner_id = None
        self.media_types = set()
        self.warnings = []

    def close(self):
        pass  # Each request owns and closes its connection.

    def request(self, method, path, json=None, data=None, headers=None):
        self.log(method + " " + path)
        url = urlsplit(self.config.api_url)
        connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(url.hostname, url.port, timeout=self.config.request_timeout)
        outgoing = {"x-api-key": self.config.api_key}
        outgoing.update(headers or {})
        if json is not None:
            data = jsonlib.dumps(json).encode("utf-8")
            outgoing["Content-Type"] = "application/json"
        if isinstance(data, Multipart):
            outgoing["Content-Length"] = str(data.length)
        elif data is not None:
            outgoing["Content-Length"] = str(len(data))
        try:
            connection.request(method, url.path.rstrip("/") + path, body=data, headers=outgoing)
            response = connection.getresponse()
            self.log("API status " + str(response.status))
            if not 200 <= response.status < 300:
                detail = server_message(response.read(64 * 1024))
                raise HttpFailure(response.status, "Immich HTTP " + str(response.status) + ": " + method + " " + path
                                  + (" (" + detail + ")" if detail else ""))
            body = response.read(16 * 1024 * 1024 + 1)
            if len(body) > 16 * 1024 * 1024:
                raise ImportFailure("Immich JSON response exceeds size limit")
            if response.status == 204 or not body:
                return None
            try:
                return jsonlib.loads(body)
            except (ValueError, UnicodeError):
                raise ImportFailure("Immich returned invalid JSON: " + path) from None
        except (OSError, http.client.HTTPException):
            raise ImportFailure("Immich request failed or timed out: " + method + " " + path) from None
        finally:
            connection.close()

    def preflight(self):
        # CLI is used for the requested connection check, never for file upload.
        import os
        env = os.environ.copy()
        env.update(IMMICH_INSTANCE_URL=self.config.api_url, IMMICH_API_KEY=self.config.api_key,
                   IMMICH_CONFIG_DIR=str(self.config.auth_dir))
        self.log("Immich command: " + self.config.immich_bin + " server-info")
        try:
            result = subprocess.run([self.config.immich_bin, "server-info"], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            raise ImportFailure("immich server-info unavailable or timed out") from None
        if result.returncode:
            raise ImportFailure("immich server-info failed (check credentials and permissions)")
        version = self.request("GET", "/server/version")
        if not isinstance(version, dict) or tuple(version.get(k) for k in ("major", "minor", "patch")) != (3, 1, 0):
            raise ImportFailure("This importer is validated against Immich 3.1.0; server version does not match")
        user = self.request("GET", "/users/me")
        if not isinstance(user, dict):
            raise ImportFailure("Invalid Immich user response")
        if self.config.expected_user_id:
            if user.get("id") != self.config.expected_user_id:
                raise ImportFailure("Immich user ID does not match the configured camera account")
        elif user.get("name") != self.config.expected_user_name:
            raise ImportFailure("Immich user is not the configured Camera Archive account")
        self.owner_id = asset_id(user.get("id"))
        media = self.request("GET", "/server/media-types")
        if not isinstance(media, dict) or not isinstance(media.get("image"), list) or not isinstance(media.get("video"), list):
            raise ImportFailure("Invalid supported media types response")
        self.media_types = {str(x).lower() for x in media["image"] + media["video"]}

    def check(self, items):
        found = {}
        for offset in range(0, len(items), 100):
            batch = items[offset:offset + 100]
            data = self.request("POST", "/assets/bulk-upload-check", json={"assets": [
                {"id": str(index), "checksum": item.sha1} for index, item in enumerate(batch)]})
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ImportFailure("Invalid bulk upload check response")
            results = data["results"]
            if len(results) != len(batch):
                raise ImportFailure("Incomplete bulk upload check response")
            seen = set()
            for row in results:
                if not isinstance(row, dict) or row.get("id") not in {str(i) for i in range(len(batch))} or row["id"] in seen:
                    raise ImportFailure("Ambiguous bulk upload check response")
                seen.add(row["id"])
                item = batch[int(row["id"])]
                if row.get("action") == "accept":
                    continue
                if row.get("action") != "reject" or row.get("reason") != "duplicate":
                    raise ImportFailure("Immich rejected an asset for a reason other than duplicate")
                found[item.relative] = (asset_id(row.get("assetId")), bool(row.get("isTrashed")))
        return found

    def upload(self, item):
        timestamp = datetime.fromtimestamp(item.fingerprint[3] / 1e9, timezone.utc).isoformat()
        # These required transport timestamps are filesystem facts. Capture time
        # extraction remains Immich's job; no timezone is guessed from filenames.
        # Unknown hashes are computed from the very bytes being sent, so a new
        # file is read once. The server's checksum is compared against them in
        # validate_identity, which is what proves the stored copy is intact.
        hasher = None if item.sha1 else Hasher()
        with readonly(item.path, item.fingerprint) as media:
            # No `filename` form field: v3.1.0 validates every part, the sidecar
            # included, against body.filename when present, and `x.MP4` is not
            # a sidecar name. The asset part's own filename is what Immich keeps.
            fields = {"assetData": (item.path.name, media, "application/octet-stream"),
                      "fileCreatedAt": timestamp, "fileModifiedAt": timestamp, "visibility": item.route}
            if item.xmp is not None:
                fields["sidecarData"] = (item.path.name + ".xmp", item.xmp, "application/rdf+xml")
            encoder = Multipart(fields, hasher.update if hasher else None)
            try:
                response = self.request("POST", "/assets", data=encoder,
                                        headers={"Content-Type": encoder.content_type})
            finally:
                # Whatever the response, a fully streamed file yielded its hashes;
                # the read-only guard above rejects a source changed meanwhile.
                if hasher and hasher.size == item.size:
                    item.sha1, item.sha256 = hasher.digests()
                    item.hash_source = "upload"
        if not isinstance(response, dict) or response.get("status") not in ("created", "duplicate"):
            raise ImportFailure("Unrecognized Immich upload response")
        return asset_id(response.get("id")), response["status"]

    def stack(self, identifier):
        value = self.request("GET", "/stacks/" + asset_id(identifier))
        if not isinstance(value, dict) or not isinstance(value.get("assets"), list):
            raise ImportFailure("Invalid stack response")
        return value

    def ensure_stack(self, primary, children):
        """Group assets so the timeline shows only the primary. Returns the stack ID.

        A stack already holding every member is accepted as it is. Stacks made
        only of our members (an earlier partial import) are rebuilt with the
        current primary; unstacking never deletes assets. A stack mixing our
        members with foreign assets is a conflict left for review.
        """
        wanted = [primary.asset_id] + [c.asset_id for c in children]
        existing = {}
        for item in (primary, *children):
            stack = self.info(item.asset_id).get("stack")
            if stack is None:
                continue
            if not isinstance(stack, dict):
                raise ImportFailure("Invalid stack field in asset response")
            stack_id = asset_id(stack.get("id"))
            if stack_id not in existing:
                existing[stack_id] = {asset_id(a.get("id")) for a in self.stack(stack_id)["assets"] if isinstance(a, dict)}
        for stack_id, members in existing.items():
            if set(wanted) <= members:
                return stack_id
        if any(not members <= set(wanted) for members in existing.values()):
            raise ImportFailure("STACK CONFLICT: a member already belongs to a stack with other assets")
        for stack_id in existing:
            self.request("DELETE", "/stacks/" + stack_id)
        response = self.request("POST", "/stacks", json={"assetIds": wanted})
        if not isinstance(response, dict):
            raise ImportFailure("Invalid stack creation response")
        stack_id = asset_id(response.get("id"))
        stack = self.stack(stack_id)
        members = {asset_id(a.get("id")) for a in stack["assets"] if isinstance(a, dict)}
        if stack.get("primaryAssetId") != primary.asset_id or not set(wanted) <= members:
            raise ImportFailure("Stack verification failed: " + primary.relative)
        return stack_id

    def info(self, identifier):
        value = self.request("GET", "/assets/" + asset_id(identifier))
        if not isinstance(value, dict):
            raise ImportFailure("Invalid asset info response")
        return value

    def validate_identity(self, item, info):
        try:
            checksum = base64.b64decode(info["checksum"], validate=True).hex()
        except (KeyError, ValueError, TypeError):
            raise ImportFailure("Invalid checksum in asset response") from None
        if info.get("id") != item.asset_id or info.get("ownerId") != self.owner_id or checksum != item.sha1:
            raise ImportFailure("Asset ID / owner / checksum mismatch")
        if info.get("isTrashed") is not False or info.get("isOffline") is not False:
            raise ImportFailure("Asset is trashed, offline, or availability is unverified")
        if "libraryId" not in info or info["libraryId"] is not None:
            raise ImportFailure("Asset is not a confirmed managed-library asset")

    def verify(self, item, metadata=True):
        info = self.info(item.asset_id)
        self.validate_identity(item, info)
        if info.get("visibility") != item.route:
            self.request("PUT", "/assets/" + item.asset_id, json={"visibility": item.route})
            info = self.info(item.asset_id)
            self.validate_identity(item, info)
            if info.get("visibility") != item.route:
                raise ImportFailure("Visibility verification failed")
        if not metadata:
            return
        # Re-extraction checks the persistent source, not a transient DB edit.
        # Immich extracts metadata on its own after a new upload, so a key
        # without job.create still gets verified from that extraction.
        try:
            self.request("POST", "/assets/jobs", json={"assetIds": [item.asset_id], "name": "refresh-metadata"})
        except HttpFailure as error:
            if error.status != 403:
                raise
            warning = "Metadata refresh not permitted (API key lacks job.create); verified Immich's own extraction instead"
            if warning not in self.warnings:
                self.warnings.append(warning)
            self.log(item.relative + ": " + str(error))
        deadline = time.monotonic() + self.config.verify_timeout
        while True:
            info = self.info(item.asset_id)
            self.validate_identity(item, info)
            exif = info.get("exifInfo") or {}
            if info.get("visibility") == item.route and all(exif.get(k) == v for k, v in item.expected.items()):
                self.log("Metadata verified: " + item.relative)
                item.verified = True
                return
            if time.monotonic() >= deadline:
                mismatches = [k for k, v in item.expected.items() if exif.get(k) != v]
                raise ImportFailure("Metadata/visibility verification timed out: " + ", ".join(mismatches) +
                                    "; existing assets are not patched with XMP")
            time.sleep(min(self.config.poll_interval, max(0, deadline - time.monotonic())))

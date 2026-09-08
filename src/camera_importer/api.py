import base64
import io
import subprocess
import time
from datetime import datetime, timezone
from uuid import UUID

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

from .files import readonly
from .model import ImportFailure


def asset_id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ImportFailure("Invalid asset ID in Immich response") from None


class Immich:
    def __init__(self, config, log=lambda message: None):
        self.config, self.log = config, log
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers["x-api-key"] = config.api_key
        self.owner_id = None
        self.media_types = set()

    def close(self):
        self.session.close()

    def request(self, method, path, **kwargs):
        self.log(method + " " + path)
        try:
            response = self.session.request(method, self.config.api_url + path,
                                            timeout=(15, self.config.request_timeout),
                                            allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise ImportFailure("Immich request failed or timed out: " + method + " " + path) from None
        self.log("API status " + str(response.status_code))
        try:
            if not 200 <= response.status_code < 300:
                # Never echo response bodies, URLs with secrets, or HTTP exceptions.
                raise ImportFailure("Immich HTTP " + str(response.status_code) + ": " + method + " " + path)
            if response.status_code == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                raise ImportFailure("Immich returned invalid JSON: " + path) from None
        finally:
            response.close()

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
        with readonly(item.path, item.fingerprint) as media:
            fields = {"assetData": (item.path.name, media, "application/octet-stream"),
                      "filename": item.path.name, "fileCreatedAt": timestamp,
                      "fileModifiedAt": timestamp, "visibility": item.route}
            if item.xmp is not None:
                fields["sidecarData"] = (item.path.name + ".xmp", io.BytesIO(item.xmp), "application/rdf+xml")
            encoder = MultipartEncoder(fields=fields)
            response = self.request("POST", "/assets", data=encoder,
                                    headers={"Content-Type": encoder.content_type})
        if not isinstance(response, dict) or response.get("status") not in ("created", "duplicate"):
            raise ImportFailure("Unrecognized Immich upload response")
        return asset_id(response.get("id")), response["status"]

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
        self.request("POST", "/assets/jobs", json={"assetIds": [item.asset_id], "name": "refresh-metadata"})
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

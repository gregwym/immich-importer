import json

from .files import atomic_json, readonly
from .model import ImportFailure


def load(path):
    if not path.exists() and not path.is_symlink():
        return None
    try:
        with readonly(path) as stream:
            data = stream.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError()
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise ImportFailure("MANIFEST CONFLICT: cannot safely read existing manifest") from None


def merge(previous, key, owner_id, server_url, members):
    expected = {"schemaVersion": 1, "bundleKey": key, "camera": "Insta360 OneRS",
                "ownerId": owner_id, "serverUrl": server_url}
    value = dict(previous) if previous is not None else dict(expected, members=[])
    for field, content in expected.items():
        if field in value and value[field] != content:
            raise ImportFailure("MANIFEST CONFLICT: " + key + " / " + field)
        value[field] = content
    existing = value.get("members")
    if not isinstance(existing, list):
        raise ImportFailure("MANIFEST CONFLICT: invalid members")
    roles = {}
    for row in existing:
        if not isinstance(row, dict) or row.get("role") not in ("master-00", "master-10", "lrv-11") or row["role"] in roles:
            raise ImportFailure("MANIFEST CONFLICT: invalid or duplicate role")
        roles[row["role"]] = dict(row)
    for item in members:
        if not item.sha256:
            continue
        incoming = {"filename": item.path.name, "role": item.role, "size": item.size,
                    "sha256": item.sha256, "immichChecksum": item.sha1,
                    "assetId": item.asset_id, "visibility": item.route}
        old = roles.get(item.role, {})
        for field, content in incoming.items():
            if old.get(field) is not None and content is not None and old[field] != content:
                raise ImportFailure("MANIFEST CONFLICT: " + key + " / " + item.role + " / " + field)
            if content is not None:
                old[field] = content
        old["metadataVerified"] = item.verified
        roles[item.role] = old
    value["members"] = [roles[role] for role in sorted(roles)]
    return value


def save(root, key, owner_id, server_url, members):
    path = root / "insta360" / (key + ".json")
    previous = load(path)
    value = merge(previous, key, owner_id, server_url, members)
    if value != previous:
        atomic_json(path, value)
    return True

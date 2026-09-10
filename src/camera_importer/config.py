import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import re

from .model import ImportFailure


@dataclass
class Config:
    companion_root: Path = Path("/volume1/immich/companions")
    manifest_root: Path = Path("/volume1/immich/manifests")
    immich_bin: str = "immich"
    exiftool_bin: str = "exiftool"
    api_url: str = ""
    api_key: str = field(default="", repr=False)
    auth_dir: Path = Path.home() / ".config/immich"
    expected_user_name: str = "Camera Archive"
    expected_user_id: str = ""
    capture_timezone: str = ""
    clock_shift: str = ""  # Per-run camera clock correction; command line only.
    verify_timeout: float = 180.0
    poll_interval: float = 2.0
    request_timeout: float = 300.0
    # Provenance for display only; never a configuration file key.
    config_path: str = field(default="", compare=False)
    config_found: bool = field(default=False, compare=False)


PUBLIC = ("companion_root", "manifest_root", "immich_bin", "exiftool_bin", "api_url", "auth_dir",
          "expected_user_name", "expected_user_id", "verify_timeout", "poll_interval", "request_timeout", "capture_timezone")


def describe(config):
    """Effective configuration without secrets, for reports and the start-up banner."""
    values = {"config_file": config.config_path + ("" if config.config_found else " (not found; defaults)")}
    for key in PUBLIC:
        values[key] = str(getattr(config, key))
    values["api_url"] = config.api_url or "(from Immich CLI auth.yml)"
    values["api_key"] = "IMMICH_API_KEY" if config.api_key else "Immich CLI auth.yml"
    return values


def format_config(config):
    return "\n".join("  " + key + ": " + value for key, value in describe(config).items())


def load_config(args):
    config_path = Path(args.config).expanduser() if args.config else Path.home() / ".config/camera-import/config.json"
    values = {}
    if config_path.exists() or args.config:
        try:
            values = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                raise ValueError()
        except (OSError, ValueError):
            raise ImportFailure("Cannot read configuration JSON") from None
    if "api_key" in values:
        raise ImportFailure("Store API keys in IMMICH_API_KEY or the CLI auth file, not project configuration")
    unknown = set(values) - set(PUBLIC)
    if unknown:
        raise ImportFailure("Unknown configuration keys: " + ", ".join(sorted(unknown)))
    env = {"CAMERA_CAPTURE_TIMEZONE": "capture_timezone", "COMPANION_ROOT": "companion_root", "MANIFEST_ROOT": "manifest_root",
           "IMMICH_BIN": "immich_bin", "EXIFTOOL_BIN": "exiftool_bin", "IMMICH_API_URL": "api_url",
           "IMMICH_API_KEY": "api_key", "IMMICH_CONFIG_DIR": "auth_dir",
           "IMMICH_EXPECTED_USER_ID": "expected_user_id", "IMMICH_EXPECTED_USER_NAME": "expected_user_name"}
    for key, field in env.items():
        if key in os.environ:
            values[field] = os.environ[key]
    for key in ("companion_root", "manifest_root", "api_url", "auth_dir", "verify_timeout", "capture_timezone", "clock_shift"):
        value = getattr(args, key, None)
        if value is not None:
            values[key] = value
    try:
        config = Config(**values)
        config.config_path, config.config_found = str(config_path), config_path.exists()
        for field in ("companion_root", "manifest_root", "auth_dir"):
            # Keep symlink components for safe_directory's explicit rejection.
            setattr(config, field, Path(os.path.abspath(Path(getattr(config, field)).expanduser())))
        for field in ("verify_timeout", "poll_interval", "request_timeout"):
            value = float(getattr(config, field))
            if not 0 < value <= 86400:
                raise ValueError()
            setattr(config, field, value)
        for field in ("immich_bin", "exiftool_bin", "api_url", "api_key", "expected_user_name", "expected_user_id", "capture_timezone"):
            if not isinstance(getattr(config, field), str):
                raise ValueError()
    except (ValueError, TypeError):
        raise ImportFailure("Invalid configuration value") from None
    return config


def normalize_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ImportFailure("API URL must be HTTP(S), without credentials, query or fragment")
    path = parsed.path.rstrip("/")
    if not path.endswith("/api"):
        path += "/api"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def authenticate(config):
    if config.api_key:
        if not config.api_url:
            raise ImportFailure("IMMICH_API_URL is required with IMMICH_API_KEY")
        config.api_url = normalize_url(config.api_url)
        return
    try:
        auth_path = config.auth_dir / "auth.yml"
        if auth_path.stat().st_size > 64 * 1024:
            raise ValueError()
        auth = read_auth_scalars(auth_path.read_text(encoding="utf-8"))
        url = auth.get("url") or auth.get("instanceUrl")
        key = auth.get("key") or auth.get("apiKey")
        if not isinstance(url, str) or not isinstance(key, str) or not key:
            raise ValueError()
        url = normalize_url(url)
        if config.api_url and normalize_url(config.api_url) != url:
            raise ImportFailure("API URL differs from CLI credentials; provide an explicit IMMICH_API_KEY for this server")
        config.api_url, config.api_key = url, key
    except ImportFailure:
        raise
    except (OSError, ValueError, AttributeError):
        raise ImportFailure("Cannot read Immich CLI auth; use immich login or IMMICH_API_URL + IMMICH_API_KEY") from None


def read_auth_scalars(text):
    """Read the flat scalar mapping emitted by Immich CLI, without YAML code.

    Complex YAML (anchors, tags, multiline values) is deliberately rejected.
    Explicit environment credentials remain available for other auth formats.
    """
    result = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or line.strip() in ("---", "..."):
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*):[ \t]+(.*)", line)
        if not match or match[1] in result:
            raise ValueError("Unsupported auth format")
        key, value = match.groups()
        value = value.strip()
        if value.startswith('"'):
            value, end = json.JSONDecoder().raw_decode(value)
            tail = match[2].strip()[end:].strip()
            if tail and not tail.startswith("#"):
                raise ValueError("Invalid quoted scalar")
        elif value.startswith("'"):
            quoted = re.fullmatch(r"'((?:[^']|'')*)'(?:[ \t]+#.*)?", value)
            if not quoted:
                raise ValueError("Invalid quoted scalar")
            value = quoted[1].replace("''", "'")
        else:
            if not value or value[0] in "!&*|>{[":
                raise ValueError("Unsupported YAML scalar")
            value = re.split(r"[ \t]+#", value, maxsplit=1)[0].rstrip()
        result[key] = value
    return result

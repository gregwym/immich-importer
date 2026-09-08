from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class ImportFailure(Exception):
    """A failure safe to report to the user (never include credentials)."""


@dataclass
class Item:
    path: Path
    relative: str
    route: str
    reason: str
    expected: Dict[str, str] = field(default_factory=dict)
    embedded: Dict[str, str] = field(default_factory=dict)
    stack: Optional[str] = None
    stack_id: Optional[str] = None
    bundle: Optional[str] = None
    role: Optional[str] = None
    date: Optional[str] = None
    sidecar: Optional[Path] = None
    fingerprint: Optional[Tuple[int, ...]] = None
    sidecar_fingerprint: Optional[Tuple[int, ...]] = None
    size: int = 0
    sha1: str = ""
    sha256: str = ""
    xmp: Optional[bytes] = None
    asset_id: Optional[str] = None
    status: str = "planned"
    verified: bool = False
    error: Optional[str] = None

    def public(self):
        return {"path": self.relative, "route": self.route, "reason": self.reason,
                "expectedMetadata": self.expected, "embeddedMetadata": self.embedded,
                "stackKey": self.stack, "stackId": self.stack_id, "bundleKey": self.bundle,
                "role": self.role, "size": self.size, "sha1": self.sha1,
                "sha256": self.sha256, "assetId": self.asset_id,
                "status": self.status, "verified": self.verified,
                "xmpPrepared": self.xmp is not None, "error": self.error}


@dataclass
class Plan:
    source: Path
    items: List[Item] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    bundles: Dict[str, List[Item]] = field(default_factory=dict)
    incomplete: Dict[str, List[str]] = field(default_factory=dict)
    lrv_missing: List[str] = field(default_factory=list)

    @property
    def assets(self):
        return [i for i in self.items if i.route == "timeline"]

    @property
    def stacks(self):
        """Stack groups (RAW + rendered photo, 360 bundle) keyed by the primary asset's path."""
        groups = {}
        for item in self.items:
            if item.stack:
                groups.setdefault(item.stack, []).append(item)
        return groups

    @property
    def unknown(self):
        return [i for i in self.items if i.route == "unknown"]

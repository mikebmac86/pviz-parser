#ui/modes/zones_by_package/contracts.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple


# ------------------------------------------------------------
# Basic coordinate origin (logical and pixel)
# ------------------------------------------------------------
@dataclass
class ZoneOrigin:
    """Logical and pixel anchor for a zone."""
    lane: int = 0          # logical lane (row)
    column: int = 0        # logical column
    px_x: float | None = None
    px_y: float | None = None


# ------------------------------------------------------------
# Metadata describing the zone itself (structure + placement)
# ------------------------------------------------------------
@dataclass
class PackageZoneMeta:
    """
    Metadata about a package zone:

      zone_id:        stable id ('pkg', 'pkg.subpkg', '<root>')
      seq:            reproducible sequence index
      seed_id:        representative node id for anchoring
      package:        dotted import path for this package
      level:          depth in dotted-path hierarchy
      parent_zone_id: dotted parent, "<root>", or None

      origin:         logical origin (lane/column)
      footprint_lanes / columns:
                      final bounding footprint of the zone AFTER
                      node placement & nested-zone composition.

      zone_kind:      distinguishes this mode from downward zones
                      (always: "by-package")

      children:       direct child zone IDs (constructed after build)
    """
    zone_id: str
    seq: int
    seed_id: str
    package: str
    level: int
    parent_zone_id: Optional[str]

    origin: ZoneOrigin = field(default_factory=ZoneOrigin)
    footprint_lanes: int = 1
    footprint_columns: int = 1
    zone_kind: str = "by-package"
    children: List[str] = field(default_factory=list)


# ------------------------------------------------------------
# Layout payload for nodes inside a zone
# ------------------------------------------------------------
@dataclass
class PackageZoneLayout:
    """
    Layout payload for a zone.

      node_ids:        list of node IDs belonging to this package
      member_files:    repo-relative file paths
      node_positions:  mapping: node_id -> (lane, column)
      clusters:        list of node-id lists, one list per discovered cluster
    """
    node_ids: List[str] = field(default_factory=list)
    member_files: List[str] = field(default_factory=list)
    node_positions: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    clusters: List[List[str]] = field(default_factory=list)


# ------------------------------------------------------------
# The zone object
# ------------------------------------------------------------
@dataclass
class PackageZone:
    """
    A single package-scoped zone.

    meta: describes structure and footprint
    layout: describes node membership & placement
    """
    zone_id: str
    meta: PackageZoneMeta
    layout: PackageZoneLayout

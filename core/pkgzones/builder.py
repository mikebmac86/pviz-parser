#core.pkgzones.builder.py
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Tuple, Optional, Any
import math  # still used for footprint estimation

from adapters.canonical import to_posix, canon_module_id
from diagnostics.logging import log_event
from .contracts import (
    PackageZone,
    PackageZoneMeta,
    PackageZoneLayout,
    ZoneOrigin,
)
from .state import SeenSet
from .placement import plan_zone_origins  # dynamic placement planner


# =====================================================================
# Configuration
# =====================================================================
@dataclass
class BuildConfig:
    """
    Tuning knobs for package zone construction.
    """
    min_files_per_zone: int = 1
    max_zones: int = 5000


# =====================================================================
# Helpers: package extraction / hierarchy
# =====================================================================
def _package_name_for_node(node_id: str, meta: Dict[str, Any]) -> Tuple[str, str]:
    """
    Returns (package_name, file_id_posix).

    Special case:
      - package __init__.py files live in the *package's own* bucket
        so that the pkg's __init__ node ends up in the same zone as
        its submodules, not in the parent package zone.

    Robust to missing/implicit packages:
      - If canon_module_id() can't produce a usable dotted name
        (e.g. no __init__.py in parent folders), we fall back to a
        directory-based "pkg.subpkg" derived from the POSIX path.
    """
    path_raw = str((meta or {}).get("path") or "") or node_id
    path_posix = to_posix(path_raw)

    # Always return POSIX path as the file id
    file_id = path_posix

    # Attempt to use canonical module id (e.g. "scene.core.graph_scene.core")
    try:
        mod = canon_module_id(path_raw, None)
    except Exception:
        mod = None

    # Directory-based fallback package name (works even without __init__.py)
    # e.g. "ui/modes/zones_by_package/scene_apply/plan.py"
    #   -> ["ui","modes","zones_by_package","scene_apply","plan.py"]
    #   -> "ui.modes.zones_by_package.scene_apply"
    parts = path_posix.split("/")
    dir_pkg = ".".join(parts[:-1]) if len(parts) > 1 else ""

    # Detect package __init__ files:
    #   "pkg/foo/__init__.py" or just "__init__.py"
    is_pkg_init = path_posix.endswith("/__init__.py") or path_posix == "__init__.py"

    if is_pkg_init:
        # __init__ belongs to the package itself, not the parent
        if mod:
            # e.g. "scene.core.graph_scene.core"
            pkg = mod
        else:
            # No reliable module id (e.g. missing __init__ up the chain);
            # use the directory path as the package name.
            pkg = dir_pkg
    else:
        # Normal modules: bucket by *parent* package.
        # Prefer a parent package derived from the canonical module id
        # when it looks sane (has at least one dot), otherwise fall back
        # to directory-based parent.
        parent_from_mod: Optional[str] = None
        if mod and "." in mod:
            parent_from_mod = mod.rsplit(".", 1)[0]

        pkg = parent_from_mod or dir_pkg

    return pkg, file_id


def _build_package_tree(
    nodes: Dict[str, Dict[str, Any]]
) -> Dict[str, List[Tuple[str, str]]]:
    """
    Returns { package : [(node_id, file_id), ...] }
    """
    out: Dict[str, List[Tuple[str, str]]] = {}
    for nid, meta in nodes.items():
        pkg, file_id = _package_name_for_node(nid, meta)
        out.setdefault(pkg, []).append((nid, file_id))
    return out


def _parent_package(pkg: str) -> Optional[str]:
    if not pkg:
        return None
    if "." not in pkg:
        return ""
    return pkg.rsplit(".", 1)[0]


def _package_level(pkg: str) -> int:
    return pkg.count(".") if pkg else 0


# =====================================================================
# MAIN CONSTRUCTOR
# =====================================================================
def build_package_zones(
    *,
    graph: Dict[str, Any],
    seed: Optional[str] = None,        # not used yet; placeholder for filters
    cfg: Optional[BuildConfig] = None,
) -> Tuple[List[PackageZone], SeenSet]:
    """
    Build hierarchical *package zones* from the full analyzer graph.

    Notes:
      - This DOES NOT compute *pixel* placement for outlines.
        (That happens later in scene_apply / ZoneFrameItem.)
      - This DOES build the hierarchical parent/child structure and
        assigns each package a coarse (lane, column) origin via the
        dynamic placement planner.
      - Returns (zones, seen_set).
    """
    cfg = cfg or BuildConfig()

    # Clamp obviously-bad config (defensive, plus visibility)
    min_files = max(1, int(getattr(cfg, "min_files_per_zone", 1) or 1))
    max_zones = int(getattr(cfg, "max_zones", 5000) or 5000)
    if max_zones <= 0:
        max_zones = 1

    # Graph components
    nodes: Dict[str, Dict[str, Any]] = (graph.get("nodes") or {})
    edges: List[Dict[str, Any]] = list(graph.get("edges") or [])

    log_event(
        "ZONES_PKG:build_begin",
        nodes=len(nodes),
        edges=len(edges),
        seed=seed,
        min_files_per_zone=min_files,
        max_zones=max_zones,
    )

    # ------------------------------------------------------------
    # 0) If nodes are missing/empty, infer node ids from edges
    # ------------------------------------------------------------
    if not nodes and edges:
        inferred: Dict[str, Dict[str, Any]] = {}
        for e in edges:
            try:
                src = str(e.get("src") or "")  # tolerate any shape
                dst = str(e.get("dst") or "")
            except Exception:
                continue

            if src:
                inferred.setdefault(src, {})
            if dst:
                inferred.setdefault(dst, {})

        nodes = inferred
        sample_nodes = sorted(list(nodes.keys()))[:8]
        log_event(
            "ZONES_PKG:build_infer_nodes_from_edges",
            inferred_nodes=len(nodes),
            edges=len(edges),
            sample=sample_nodes,
        )

    # If we still have no nodes, there is nothing to zone.
    if not nodes:
        log_event("ZONES_PKG:build_no_nodes", edges=len(edges))
        return [], SeenSet()

    # ------------------------------------------------------------
    # 1) Group nodes into packages
    # ------------------------------------------------------------
    pkg_map = _build_package_tree(nodes)
    pkg_count = len(pkg_map)
    total_members = sum(len(v) for v in pkg_map.values())

    # Small diagnostic sample of raw buckets
    sample_pkgs = sorted(
        pkg_map.items(),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )[:8]
    log_event(
        "ZONES_PKG:build_pkgs_raw",
        pkgs=pkg_count,
        total_members=total_members,
        sample=[(pkg or "<root>", len(members)) for pkg, members in sample_pkgs],
    )

    # Filter small buckets
    pkg_items_all: List[Tuple[str, List[Tuple[str, str]]]] = list(pkg_map.items())
    pkg_items = [
        (pkg, members)
        for pkg, members in pkg_items_all
        if len(members) >= min_files
    ]
    dropped = len(pkg_items_all) - len(pkg_items)

    log_event(
        "ZONES_PKG:build_pkgs_filtered",
        min_files_per_zone=min_files,
        kept=len(pkg_items),
        dropped=dropped,
    )

    if not pkg_items:
        # Nothing met the threshold; fall back to a single "<root>" zone
        # so the user sees *something* instead of a blank outline.
        all_node_ids = sorted(nodes.keys())
        log_event(
            "ZONES_PKG:build_pkgs_all_dropped_fallback",
            nodes=len(all_node_ids),
            min_files_per_zone=min_files,
        )

        seen = SeenSet()
        seen.add_many(all_node_ids)

        meta = PackageZoneMeta(
            zone_id="<root>",
            seq=1,
            seed_id=all_node_ids[0] if all_node_ids else "",
            package="",
            level=0,
            parent_zone_id=None,
            origin=ZoneOrigin(lane=0, column=0),
            footprint_lanes=1,
            footprint_columns=1,
            zone_kind="by-package",
        )
        layout = PackageZoneLayout(
            node_ids=all_node_ids,
            member_files=[_package_name_for_node(nid, nodes.get(nid, {}))[1] for nid in all_node_ids],
            node_positions={},
            clusters=[],
        )
        z = PackageZone(zone_id="<root>", meta=meta, layout=layout)
        log_event(
            "ZONES_PKG:build_zone",
            zone_id=z.zone_id,
            pkg="<root>",
            files=len(all_node_ids),
            level=0,
            parent=None,
            origin_lane=0,
            origin_col=0,
        )
        log_event("ZONES_PKG:build_done", zones=1, seen=len(seen.node_ids))
        return [z], seen

    # Deterministic sort: by depth, then lexicographically
    pkg_items.sort(key=lambda kv: (_package_level(kv[0]), kv[0]))

    # Track counts before trimming (used by placement)
    pkg_counts: Dict[str, int] = {pkg: len(members) for pkg, members in pkg_items}

    pkg_names = [pkg for pkg, _ in pkg_items][:max_zones]
    trimmed = max(0, len(pkg_items) - len(pkg_names))

    # Log a quick view of the chosen top packages before grid assignment
    log_event(
        "ZONES_PKG:build_pkgs_sorted",
        pkgs=len(pkg_items),
        chosen=len(pkg_names),
        trimmed=trimmed,
        sample=[(pkg or "<root>", pkg_counts.get(pkg, 0)) for pkg in pkg_names[:8]],
    )

    # -----------------------------------------------------------------
    # Dynamic lane/column assignment for all zones we will actually build
    # -----------------------------------------------------------------
    origins = plan_zone_origins(pkg_names, pkg_counts)

    zones: List[PackageZone] = []
    seen = SeenSet()

    # Temporary: capture children structure for post-build linking
    children: Dict[str, List[str]] = {pkg: [] for pkg in pkg_names}

    # ------------------------------------------------------------
    # 2) Create zones
    # ------------------------------------------------------------
    for seq, (pkg, members) in enumerate(pkg_items, start=1):
        if seq > max_zones:
            log_event("ZONES_PKG:build_max_zones_reached", limit=max_zones)
            break

        zone_id = pkg or "<root>"
        parent_pkg = _parent_package(pkg)
        parent_zone_id = (
            (parent_pkg or "<root>") if parent_pkg is not None else None
        )

        level = _package_level(pkg)

        node_ids = [nid for nid, _ in members]
        file_ids = [fid for _, fid in members]

        seen.add_many(node_ids)

        # Origin from planner; default to lane/col = 0 if missing
        origin = origins.get(pkg, ZoneOrigin())

        # Initial footprint (will be refined in step 4)
        lanes = 1
        columns = 1

        meta = PackageZoneMeta(
            zone_id=zone_id,
            seq=seq,
            seed_id=node_ids[0] if node_ids else "",
            package=pkg,
            level=level,
            parent_zone_id=parent_zone_id,
            origin=origin,
            footprint_lanes=lanes,
            footprint_columns=columns,
            zone_kind="by-package",
        )

        layout = PackageZoneLayout(
            node_ids=node_ids,
            member_files=file_ids,
            node_positions={},       # will be filled later
            clusters=[],
        )

        zones.append(PackageZone(zone_id=zone_id, meta=meta, layout=layout))

        # Track parent/child relationship (by *package name*)
        if parent_pkg in children:
            children[parent_pkg].append(pkg)

        log_event(
            "ZONES_PKG:build_zone",
            zone_id=zone_id,
            pkg=pkg or "<root>",
            files=len(node_ids),
            level=level,
            parent=parent_zone_id,
            origin_lane=origin.lane,
            origin_col=origin.column,
        )

    # ------------------------------------------------------------
    # 3) Inject children lists into meta
    # ------------------------------------------------------------
    zone_by_id = {z.zone_id: z for z in zones}
    for z in zones:
        pkg = z.meta.package
        z.meta.children = children.get(pkg, [])

    # ------------------------------------------------------------
    # 4) Adjust footprints based on package size
    # ------------------------------------------------------------
    #
    # With the new area-ratio sizing in `plan_zone_rectangles`, a zone's
    # pixel size is driven primarily by its subtree node population and a
    # target node-density. The `footprint_lanes` / `footprint_columns`
    # here are now just *minimum shape hints* that act as lower bounds on
    # width/height, rather than an explicit padding or "reserved band"
    # for children.
    #
    # We keep this simple and local:
    #   - derive a near-square footprint from the package's own node count
    #   - parents are not given extra lanes for children; the layout
    #     planner's area constraints take care of overall size.
    #
    for z in zones:
        pkg = z.meta.package
        count = max(1, pkg_counts.get(pkg, len(z.layout.node_ids)))

        # Near-square footprint
        side = int(math.ceil(math.sqrt(count)))
        cols = max(1, side)
        lanes = max(1, int(math.ceil(count / cols)))

        z.meta.footprint_columns = cols
        z.meta.footprint_lanes = lanes

    # Light summary of hierarchy
    root_children = children.get("", []) or children.get("<root>", []) or []
    log_event(
        "ZONES_PKG:build_children_summary",
        zones=len(zones),
        roots=len(root_children),
        root_children=root_children[:8],
    )

    log_event("ZONES_PKG:build_done", zones=len(zones), seen=len(seen.node_ids))
    return zones, seen


# =====================================================================
# ARTIFACT EXPORT
# =====================================================================
def zones_to_artifact(
    zones: List[PackageZone],
    *,
    graph_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Convert zones to JSON-serializable artifact (no I/O).
    """
    base_meta: Dict[str, Any] = {
        "version": 1,
        "mode": "zones",
        "zone_kind": "by-package",
    }
    if graph_meta:
        base_meta["graph_meta"] = dict(graph_meta)

    return {
        "meta": base_meta,
        "zones": [asdict(z) for z in zones],
    }

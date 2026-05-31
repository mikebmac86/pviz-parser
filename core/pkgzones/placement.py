# ui/modes/zones_by_package/placement.py
from __future__ import annotations

from typing import Dict, List, Mapping, Optional

from diagnostics.logging import log_event
from .contracts import ZoneOrigin


def _parent_package(pkg: str) -> Optional[str]:
    if not pkg:
        return None
    if "." not in pkg:
        return ""
    return pkg.rsplit(".", 1)[0]


def _package_level(pkg: str) -> int:
    return pkg.count(".") if pkg else 0


def plan_zone_origins(
    pkg_names: List[str],
    pkg_counts: Mapping[str, int],
) -> Dict[str, ZoneOrigin]:
    """
    Assign (lane, column) for each package zone.

    Goals:
      1. No overlap in grid space (unique lane/column bands).
      2. Siblings at the same depth are arranged horizontally.
      3. This pattern recurses for all child clusters (every level).
      4. Lane index increases as we go down the hierarchy; columns fan
         out left->right within each sibling group.

    This is a *coarse* layout that will later be mapped to pixel
    rectangles by scene_apply / ZoneFrameItem. The idea is to get a
    reasonably good "first pass" so that users only need minor tweaks.
    """
    if not pkg_names:
        return {}

    # Pre-compute level + parent for each package
    levels: Dict[str, int] = {}
    parents: Dict[str, Optional[str]] = {}
    for pkg in pkg_names:
        lvl = _package_level(pkg)
        par = _parent_package(pkg)
        levels[pkg] = lvl
        parents[pkg] = par

    all_levels = sorted({levels[p] for p in pkg_names})

    origins: Dict[str, ZoneOrigin] = {}
    lane = 0

    # Walk levels top-down: root-ish packages first, then deeper ones
    for lvl in all_levels:
        # pkgs that live at this level
        level_pkgs = [p for p in pkg_names if levels[p] == lvl]
        if not level_pkgs:
            continue

        # Group by parent package at this level
        by_parent: Dict[Optional[str], List[str]] = {}
        for p in level_pkgs:
            par = parents[p]
            by_parent.setdefault(par, []).append(p)

        # Deterministic parent order (None, "", then lex others)
        def _parent_sort_key(par: Optional[str]) -> str:
            if par is None:
                return ""
            return par

        for par in sorted(by_parent.keys(), key=_parent_sort_key):
            siblings = by_parent[par]

            # Within each sibling group, order by decreasing size, then name
            siblings.sort(
                key=lambda name: (
                    -int(pkg_counts.get(name, 0)),
                    name or "",
                )
            )

            col = 0
            for pkg in siblings:
                if pkg not in origins:
                    origins[pkg] = ZoneOrigin(lane=lane, column=col)
                col += 2  # spacing between siblings in the same lane

            # Step lanes between parent-groups to avoid cross-group mixing
            lane += 2

    # Light diagnostic for sanity
    lanes_used = sorted({o.lane for o in origins.values()})
    sample_pkgs = pkg_names[:8]
    sample = {
        (p or "<root>"): {"lane": origins[p].lane, "col": origins[p].column}
        for p in sample_pkgs
        if p in origins
    }

    log_event(
        "ZONES_PKG:placement_plan_zone_origins",
        pkgs=len(pkg_names),
        lanes=len(lanes_used),
        min_lane=min(lanes_used) if lanes_used else 0,
        max_lane=max(lanes_used) if lanes_used else 0,
        sample=sample,
    )

    return origins

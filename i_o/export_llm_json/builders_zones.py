from __future__ import annotations

from typing import Any, Dict, List, Mapping


def build_zones(zones_data: Mapping[str, Any]) -> List[Dict[str, Any]]:
    zones_raw = zones_data.get("zones", [])
    if not isinstance(zones_raw, list):
        return []

    meta = zones_data.get("meta", {}) if isinstance(zones_data.get("meta"), Mapping) else {}
    default_kind = meta.get("zone_kind", "by-package")

    out: List[Dict[str, Any]] = []

    for z in zones_raw:
        if not isinstance(z, Mapping):
            continue
        z_meta = z.get("meta", {}) if isinstance(z.get("meta"), Mapping) else {}
        layout = z.get("layout", {}) if isinstance(z.get("layout"), Mapping) else {}

        zone_id = z.get("zone_id") or z_meta.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id:
            continue

        member_files = layout.get("member_files") or layout.get("node_ids") or []
        if not isinstance(member_files, list):
            member_files = []

        zone_entry: Dict[str, Any] = {
            "zone_id": zone_id,
            "kind": z_meta.get("zone_kind", default_kind),
            "package": z_meta.get("package", ""),
            "level": z_meta.get("level", 0),
            "parent_zone_id": z_meta.get("parent_zone_id"),
            "children": z_meta.get("children", []),
            "member_files": member_files,
        }
        out.append(zone_entry)

    return out

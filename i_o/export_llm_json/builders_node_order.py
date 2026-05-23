from __future__ import annotations

from typing import Any, List, Mapping


def build_node_order(reachable_data: Mapping[str, Any]) -> List[str]:
    nodes = reachable_data.get("nodes", [])
    if not isinstance(nodes, list) or not nodes:
        return []

    nodes_sorted = sorted(
        [n for n in nodes if isinstance(n, Mapping)],
        key=lambda n: (n.get("gen", 0), n.get("gen_idx", 0)),
    )
    return [n["node_id"] for n in nodes_sorted if isinstance(n.get("node_id"), str) and n.get("node_id")]

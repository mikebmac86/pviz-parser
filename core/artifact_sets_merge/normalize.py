from __future__ import annotations

from typing import Any, Dict, List, Mapping


def as_nodes_map(nodefacts_payload: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(nodefacts_payload, Mapping):
        return {}

    maybe_nodes = nodefacts_payload.get("nodes")
    if isinstance(maybe_nodes, Mapping):
        return {
            str(k): dict(v) if isinstance(v, Mapping) else {"_raw": v}
            for k, v in maybe_nodes.items()
        }

    out: Dict[str, Dict[str, Any]] = {}
    for k, v in nodefacts_payload.items():
        if not isinstance(k, str):
            return {}
        if isinstance(v, Mapping):
            out[k] = dict(v)
    return out


def as_edges_list(edges_payload: Any) -> List[Dict[str, Any]]:
    if isinstance(edges_payload, list):
        return [dict(e) for e in edges_payload if isinstance(e, Mapping)]
    if isinstance(edges_payload, Mapping):
        edges = edges_payload.get("edges")
        if isinstance(edges, list):
            return [dict(e) for e in edges if isinstance(e, Mapping)]
    return []
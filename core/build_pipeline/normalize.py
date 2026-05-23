from __future__ import annotations

from typing import Any, Dict, Iterable, List


def iter_nodes_any(nodes_obj: Any) -> Iterable[Dict[str, Any]]:
    """
    Produce an iterable of node dicts from common shapes:
      - dict[node_id -> node_dict]
      - list[node_dict]
      - {"nodes": <any of the above>}
    """
    if nodes_obj is None:
        return []

    if isinstance(nodes_obj, dict) and "nodes" in nodes_obj and len(nodes_obj) <= 3:
        return iter_nodes_any(nodes_obj.get("nodes"))

    if isinstance(nodes_obj, dict):
        return [v for v in nodes_obj.values() if isinstance(v, dict)]

    if isinstance(nodes_obj, list):
        return [v for v in nodes_obj if isinstance(v, dict)]

    return []


def ensure_nodes_dict(nodes: Any) -> Dict[str, Dict[str, Any]]:
    """
    Canonical node normalization.

    Accepts:
      - dict[node_id -> node_dict]
      - list[{"id": "...", ...}]
      - wrapper dicts containing "nodes"
    Returns:
      - dict[node_id -> node_dict] with node_dict["id"] set
    """
    out: Dict[str, Dict[str, Any]] = {}

    if nodes is None:
        return out

    if isinstance(nodes, dict) and "nodes" in nodes and len(nodes) <= 3:
        nodes = nodes.get("nodes")

    if isinstance(nodes, dict):
        for k, v in nodes.items():
            if not isinstance(v, dict):
                continue
            nid = str(v.get("id") or k)
            vv = dict(v)
            vv["id"] = nid
            out[nid] = vv
        return out

    if isinstance(nodes, list):
        for v in nodes:
            if not isinstance(v, dict):
                continue
            nid = v.get("id") or v.get("node_id") or v.get("mid") or v.get("file")
            if not nid:
                continue
            nid = str(nid)
            vv = dict(v)
            vv["id"] = nid
            out[nid] = vv
        return out

    return out


def edges_list(edges_obj: Any) -> List[Dict[str, Any]]:
    if edges_obj is None:
        return []
    if isinstance(edges_obj, dict) and "edges" in edges_obj and len(edges_obj) <= 3:
        edges_obj = edges_obj.get("edges")
    if isinstance(edges_obj, list):
        return [e for e in edges_obj if isinstance(e, dict)]
    if isinstance(edges_obj, dict):
        return [e for e in edges_obj.values() if isinstance(e, dict)]
    return []

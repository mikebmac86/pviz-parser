# backend/saas_analyzer/core/artifact_sets_merge/scc.py

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Tuple


SCC_FIELDS = {
    "scc_id",
    "scc_size",
    "scc_id_runtime",
    "scc_size_runtime",
}


def _edge_get(edge: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = edge.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _tarjan_scc(
    node_ids: Iterable[str],
    adj: Mapping[str, Iterable[str]],
) -> Tuple[Dict[str, str], Dict[str, int]]:
    index = 0
    stack: List[str] = []
    on_stack: set[str] = set()
    index_of: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    components: List[List[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index

        index_of[v] = index
        lowlink[v] = index
        index += 1

        stack.append(v)
        on_stack.add(v)

        for w in sorted(adj.get(v, ())):
            if w not in index_of:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index_of[w])

        if lowlink[v] == index_of[v]:
            comp: List[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            components.append(sorted(comp))

    node_list = sorted(set(str(n) for n in node_ids if n))

    for node_id in node_list:
        if node_id not in index_of:
            strongconnect(node_id)

    scc_id_by_node: Dict[str, str] = {}
    scc_size_by_id: Dict[str, int] = {}

    for comp in components:
        sid = comp[0]
        size = len(comp)
        scc_size_by_id[sid] = size
        for node_id in comp:
            scc_id_by_node[node_id] = sid

    return scc_id_by_node, scc_size_by_id


def _build_adj_from_edges(
    *,
    node_ids: set[str],
    edges: Iterable[Mapping[str, Any]],
    runtime_only: bool = False,
) -> Dict[str, List[str]]:
    adj: Dict[str, set[str]] = {node_id: set() for node_id in node_ids}

    for edge in edges:
        if not isinstance(edge, Mapping):
            continue

        src = _edge_get(edge, "src", "source", "from", "a")
        dst = _edge_get(edge, "dst", "target", "to", "b")

        if not src or not dst:
            continue
        if src not in node_ids or dst not in node_ids:
            continue
        if src == dst:
            continue

        if runtime_only and _is_type_checking_only_edge(edge):
            continue

        adj[src].add(dst)

    return {node_id: sorted(targets) for node_id, targets in adj.items()}


def _is_type_checking_only_edge(edge: Mapping[str, Any]) -> bool:
    if edge.get("type_checking") is True:
        return True
    if edge.get("conditional") == "TYPE_CHECKING":
        return True
    if edge.get("runtime") is False:
        return True

    subkind = edge.get("subkind")
    if isinstance(subkind, str) and subkind.lower() in {
        "type_checking",
        "typing",
        "type-only",
        "type_only",
    }:
        return True

    evidence = edge.get("evidence")
    if isinstance(evidence, Mapping):
        if evidence.get("type_checking") is True:
            return True
        if evidence.get("runtime") is False:
            return True
        reason = evidence.get("reason")
        if isinstance(reason, str) and "type_checking" in reason.lower():
            return True

    reasons = edge.get("reasons")
    if isinstance(reasons, list):
        for reason in reasons:
            if not isinstance(reason, Mapping):
                continue
            if reason.get("type_checking") is True:
                return True
            if reason.get("runtime") is False:
                return True
            if reason.get("conditional") is True:
                symbols = reason.get("symbols")
                if isinstance(symbols, list) and any(
                    isinstance(s, str) and "TYPE_CHECKING" in s
                    for s in symbols
                ):
                    return True

    return False


def _summary_from_scc_sizes(scc_size_by_id: Mapping[str, int]) -> Dict[str, int]:
    nontrivial = [int(size) for size in scc_size_by_id.values() if int(size) > 1]
    return {
        "cycle_nodes": sum(nontrivial),
        "largest_scc_size": max(nontrivial, default=0),
        "scc_count": len(nontrivial),
    }


def inject_scc_fields_from_edges(
    merged_nodes: MutableMapping[str, MutableMapping[str, Any]],
    merged_edges: Iterable[Mapping[str, Any]],
    *,
    include_runtime: bool = True,
) -> Dict[str, Any]:
    """
    Compute canonical SCC fields after all language artifacts have been merged.

    Mutates merged_nodes in place.

    Writes:
      - scc_id
      - scc_size
      - scc_id_runtime
      - scc_size_runtime

    Returns meta summary:
      {
        "scc_conceptual": {...},
        "scc_runtime": {...},
        "tc_inflation": int,
      }
    """
    node_ids = set(str(node_id) for node_id in merged_nodes.keys() if node_id)
    edge_list = [e for e in merged_edges if isinstance(e, Mapping)]

    adj = _build_adj_from_edges(
        node_ids=node_ids,
        edges=edge_list,
        runtime_only=False,
    )

    scc_id_by_node, scc_size_by_id = _tarjan_scc(node_ids, adj)

    for node_id, node in merged_nodes.items():
        sid = scc_id_by_node.get(node_id, node_id)
        node["scc_id"] = sid
        node["scc_size"] = int(scc_size_by_id.get(sid, 1))

    conceptual_summary = _summary_from_scc_sizes(scc_size_by_id)

    runtime_summary = {
        "cycle_nodes": 0,
        "largest_scc_size": 0,
        "scc_count": 0,
    }

    if include_runtime:
        adj_rt = _build_adj_from_edges(
            node_ids=node_ids,
            edges=edge_list,
            runtime_only=True,
        )

        scc_id_by_node_rt, scc_size_by_id_rt = _tarjan_scc(node_ids, adj_rt)

        for node_id, node in merged_nodes.items():
            sid_rt = scc_id_by_node_rt.get(node_id, node_id)
            node["scc_id_runtime"] = sid_rt
            node["scc_size_runtime"] = int(scc_size_by_id_rt.get(sid_rt, 1))

        runtime_summary = _summary_from_scc_sizes(scc_size_by_id_rt)
    else:
        for node_id, node in merged_nodes.items():
            node["scc_id_runtime"] = node.get("scc_id", node_id)
            node["scc_size_runtime"] = node.get("scc_size", 1)

        runtime_summary = dict(conceptual_summary)

    return {
        "scc_conceptual": conceptual_summary,
        "scc_runtime": runtime_summary,
        "tc_inflation": (
            conceptual_summary["largest_scc_size"]
            - runtime_summary["largest_scc_size"]
        ),
    }
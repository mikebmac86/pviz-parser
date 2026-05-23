from __future__ import annotations

from typing import Any, Dict, Optional
import sys
import importlib.util
from pathlib import Path

__all__ = ["enrich_import_provenance"]

# ---------------------------------------------------------------------------
# Diagnostics / debug hook
# ---------------------------------------------------------------------------

from diagnostics.logging import log_event as _log_event  # type: ignore[attr-defined]

def _log(msg: str, **fields: Any) -> None:
    """
    Lightweight debug helper wired into the central logging controller.

    Uses a dedicated category/kind prefix so it can be filtered or
    silenced independently.
    """
    try:
        _log_event("ANALYZER_BRIDGE:provenance", msg=msg, **fields)
    except Exception:
        # Logging must never affect core behavior.
        pass



# Very lightweight stdlib detection: try importlib.util.find_spec and sys.base_prefix
# This stays heuristic by design; caller should provide meta.root when possible.

def _is_stdlib(modname: str) -> bool:
    try:
        spec = importlib.util.find_spec(modname)
        if not spec or not spec.origin:
            return False
        origin = spec.origin
        # Python places stdlib under base_prefix (rough heuristic)
        return (sys.base_prefix in origin) and ("site-packages" not in origin)
    except Exception:
        return False


def _classify_origin(modname: str, *, project_root: Optional[Path]) -> str:
    # Priority: local (under project), then stdlib, else third_party
    try:
        if project_root:
            # If module resolves to a file inside project_root -> local
            # (the core step already matched modules to node IDs for edges)
            return "local"
    except Exception:
        pass
    if _is_stdlib(modname):
        return "stdlib"
    return "third_party"


def tag_import_provenance(graph: Dict[str, Any]) -> Dict[str, Any]:
    """Tag nodes/edges with meta.import_origin ∈ {'local','stdlib','third_party'}.
    Expects caller to set graph['meta']['root'] when possible for best accuracy.
    """

    def _edge_count(g: Dict[str, Any]) -> int:
        try:
            return len(g.get("edges") or [])
        except Exception:
            return -1

    def _edge_uniques(g: Dict[str, Any]) -> int:
        try:
            s = set()
            for e in (g.get("edges") or []):
                u = e.get("src") or e.get("source") or e.get("u") or e.get("a") or e.get("from") or ""
                v = e.get("dst") or e.get("target") or e.get("v") or e.get("b") or e.get("to") or ""
                k = e.get("kind") or e.get("type") or ""
                s.add((str(u), str(v), str(k)))
            return len(s)
        except Exception:
            return -1

    if not isinstance(graph, dict):
        return graph

    nodes = graph.get("nodes") or {}
    edges = graph.get("edges") or []
    meta = graph.setdefault("meta", {})

    # pre-stats
    ec_in  = _edge_count(graph)
    ecu_in = _edge_uniques(graph)
    _log(f"[PROV] start nodes={len(nodes)} edges={ec_in} unique={ecu_in}")

    root: Optional[Path] = None
    try:
        r = meta.get("root") or graph.get("root")
        root = Path(r).resolve() if isinstance(r, str) else None
    except Exception:
        root = None

    # Tag nodes by module name when available
    for _id, payload in nodes.items():
        mod = payload.get("module")
        if isinstance(mod, str) and mod:
            payload.setdefault("meta", {})["import_origin"] = _classify_origin(mod, project_root=root)

    # Tag edges by provider module (src node)
    # If nodes contain module keys, prefer those
    missing_src, tagged_edges = 0, 0
    for e in edges:
        src_id = e.get("src")
        mod = None
        if src_id in nodes:
            mod = nodes[src_id].get("module")
        else:
            missing_src += 1
        if isinstance(mod, str) and mod:
            e.setdefault("meta", {})["import_origin"] = _classify_origin(mod, project_root=root)
            tagged_edges += 1

    # post-stats
    ec_out  = _edge_count(graph)
    ecu_out = _edge_uniques(graph)
    delta   = ec_out - ec_in
    _log(
        f"[PROV] end   nodes={len(nodes)} edges={ec_out} unique={ecu_out} "
        f"delta={delta} tagged={tagged_edges} missing_src={missing_src}"
    )

    # Guardrail: provenance must not change edge cardinality
    if delta != 0:
        _log(f"[PROV][WARN] edge count changed during provenance tagging (delta={delta})")

    return graph

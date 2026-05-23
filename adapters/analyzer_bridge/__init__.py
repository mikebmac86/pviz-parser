# adapters/analyzer_bridge/__init__.py
from __future__ import annotations

from typing import Any, Dict, Optional
from pathlib import Path

from .core import normalize_graph_for_contracts as normalize_graph_for_contracts
__all__ = [
    "normalize_graph_for_contracts",
]

def enrich_graph(graph: Any, *, repo_root: Optional[Path | str] = None) -> Dict[str, Any]:
    g = graph if isinstance(graph, dict) else {"nodes": {}, "edges": []}
    nodes = g.get("nodes") or {}
    for _id, payload in nodes.items():
        if not isinstance(payload, dict):
            continue
        defs_rows = payload.get("defs_rows") or []
        extra = payload.setdefault("extra", {})
        if "defs_rows" not in extra:
            extra["defs_rows"] = list(defs_rows)
    return g


# Backward-compatible alias
enrich_for_ui = enrich_graph

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


def as_mapping(x: Any) -> Optional[Mapping[str, Any]]:
    return x if isinstance(x, Mapping) else None


def safe_sample(seq: List[Any], n: int = 25) -> List[Any]:
    return seq[:n] if len(seq) > n else seq


def dedupe_preserve_order(items: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for it in items:
        if isinstance(it, str) and it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def node_ids_from_nodes(nodes: Mapping[str, Any]) -> Set[str]:
    return {k for k in (nodes or {}).keys() if isinstance(k, str) and k}


def node_files_from_nodes(nodes: Mapping[str, Any]) -> Set[str]:
    out: Set[str] = set()
    for _nid, n in (nodes or {}).items():
        nm = as_mapping(n)
        if not nm:
            continue
        f = nm.get("file")
        if isinstance(f, str) and f:
            out.add(f.replace("\\", "/"))
    return out


def edge_counts_from_edges(edges: Iterable[Mapping[str, Any]]) -> Tuple[Counter, Counter]:
    """
    For exporter->importer edges:
      - out_by_src[src] = number of importers (how often src is imported)
      - in_by_dst[dst]  = number of dependencies (how many things dst imports)
    """
    out_by_src: Counter = Counter()
    in_by_dst: Counter = Counter()
    for e in edges or []:
        if not isinstance(e, Mapping):
            continue
        s = e.get("src")
        d = e.get("dst")
        if isinstance(s, str) and s:
            out_by_src[s] += 1
        if isinstance(d, str) and d:
            in_by_dst[d] += 1
    return out_by_src, in_by_dst


def normalize_posix(pathish: str) -> str:
    return pathish.replace("\\", "/").strip()

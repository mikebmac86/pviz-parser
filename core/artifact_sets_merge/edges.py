from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .config import LANGUAGE_SPECS, LanguageSpec


def edge_key(e: Mapping[str, Any]) -> Tuple[Any, ...]:
    src = str(e.get("src", ""))
    dst = str(e.get("dst", ""))
    kind = str(e.get("kind", "import"))
    subkind = str(e.get("subkind", "")) if e.get("subkind") is not None else ""
    synthetic = bool(e.get("synthetic", False))

    if kind == "crosstalk" and isinstance(e.get("crosstalk"), Mapping):
        ct = e["crosstalk"]
        return (
            src,
            dst,
            kind,
            subkind,
            synthetic,
            str(ct.get("join", "")),
            str(ct.get("candidate_kind", "")),
        )

    return (src, dst, kind, subkind, synthetic)


def dedupe_edges(edges: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[Tuple[Any, ...]] = set()
    out: List[Dict[str, Any]] = []
    for e in edges:
        if not isinstance(e, Mapping):
            continue
        if not e.get("src") or not e.get("dst"):
            continue
        k = edge_key(e)
        if k in seen:
            continue
        seen.add(k)
        out.append(dict(e))
    return out


def merge_edges_by_language(
    edges_by_lang: Mapping[str, List[Dict[str, Any]]],
    specs: Sequence[LanguageSpec] = LANGUAGE_SPECS,
) -> List[Dict[str, Any]]:
    all_edges: List[Dict[str, Any]] = []
    for spec in specs:
        all_edges.extend(edges_by_lang.get(spec.lang, []))
    return dedupe_edges(all_edges)
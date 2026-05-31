# analyzer_store/edge_pass.py
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import orjson  # speed path
except Exception:  # pragma: no cover
    orjson = None  # type: ignore

import json
from analyzer_store.io_utils import atomic_write_json

try:
    from diagnostics.logging import log_event  # type: ignore
except Exception:  # pragma: no cover
    def log_event(*_a: Any, **_k: Any) -> None:
        return

def _loads_bytes(data: bytes) -> Dict[str, Any]:
    """
    Fast JSON loader for artifact-sized payloads.
    Uses orjson when available; falls back to stdlib json.
    Always returns a dict ({} on error or non-dict root).
    """
    try:
        obj = orjson.loads(data) if orjson else json.loads(
            data.decode("utf-8", errors="ignore")
        )
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _load_json(path: Path) -> Dict[str, Any]:
    """Read JSON from disk using bytes -> fast loads; tolerant to errors."""
    try:
        return _loads_bytes(Path(path).read_bytes())
    except Exception:
        return {}


def _as_map_nodes(nf_nodes: Any) -> Dict[str, Dict[str, Any]]:
    """
    Normalize nodefacts["nodes"] into a mapping:
        { node_id: { ...node_record... }, ... }

    Supports both:
      - dict { node_id: rec }
      - list [{ "node_id": "...", ...}, ...]
    """
    if isinstance(nf_nodes, dict):
        return {k: (v or {}) for k, v in nf_nodes.items() if isinstance(k, str)}
    if isinstance(nf_nodes, list):
        return {
            n.get("node_id"): (n or {})
            for n in nf_nodes
            if isinstance(n, dict) and n.get("node_id")
        }
    return {}


# ---------------------------------------------------------------------------
# Token helpers (support both dotted ids and NodeIds like 'pkg/mod.py')
# ---------------------------------------------------------------------------

def _token_parts(token: str) -> List[str]:
    """
    Split an id into hierarchical parts, handling:
      - NodeIds / file-ids like 'scrapy/core/engine.py'
      - Dotted ids like 'scrapy.core.engine'

    For NodeIds:
      'scrapy/core/engine.py'    -> ['scrapy', 'core', 'engine']
      'scrapy/core/__init__.py'  -> ['scrapy', 'core']
    For dotted:
      'scrapy.core.engine'       -> ['scrapy', 'core', 'engine']
    """
    t = token.replace("\\", "/").rstrip("/")
    # Path-like?
    if "/" in t:
        if t.endswith("/__init__.py"):
            t = t[: -len("/__init__.py")]
        elif t.endswith(".py"):
            t = t[:-3]
        return [p for p in t.split("/") if p]
    # Fallback: dotted
    return [p for p in t.split(".") if p]


def _is_shallower(a: str, b: str) -> bool:
    """
    "Shallower" for our prefix-choice:
      - fewer parts wins
      - if equal, shorter string wins (stable tie-breaker)
    """
    pa = _token_parts(a)
    pb = _token_parts(b)
    if len(pa) != len(pb):
        return len(pa) < len(pb)
    return len(a) < len(b)


# ---------------------------------------------------------------------------
# Edge construction
# ---------------------------------------------------------------------------

def _strict_edges(nf_nodes: Dict[str, Dict[str, Any]], scope: Set[str]) -> List[Dict[str, Any]]:
    """
    Emit edges strictly within `scope`, using canonical direction:
      provider -> consumer.

    IDs here are the canonical NodeIds (file-ids), i.e. repo-relative POSIX
    paths with '.py' or '/__init__.py', as emitted by NodeFacts. Example:

        src = 'scrapy/core/engine.py'
        dst = 'scrapy/core/scraper.py'

    If a consumer node carries per-import metadata under `imports_meta`,
    attach it to the emitted edge as `edge["meta"]`. Expected shape:

        rec["imports_meta"] = {
            "scrapy/core/downloader/middleware.py": {
                "tags": ["delegate"],
                "lineno": 42,
                "scope": "function",
                "conditional": False,
            },
        }

    Tags are normalized into `meta.import_tags` (lowercased, deduped) and
    convenience booleans `meta.is_delegate` / `meta.is_dynamic` are added.
    """
    out: List[Dict[str, Any]] = []
    for consumer_id, rec in nf_nodes.items():
        if consumer_id not in scope:
            continue

        imports_meta = rec.get("imports_meta") or {}

        for provider_id in (rec.get("imports") or []):
            if not isinstance(provider_id, str):
                continue
            if provider_id not in scope:
                continue

            # Base edge
            e: Dict[str, Any] = {
                "src": provider_id,
                "dst": consumer_id,
                "kind": "import",
            }

            # Optional meta
            pm = imports_meta.get(provider_id)
            if isinstance(pm, dict):
                meta = dict(pm)
                tags = meta.get("tags") or []
                if isinstance(tags, (list, tuple)):
                    import_tags = sorted(
                        {
                            str(t).strip().lower()
                            for t in tags
                            if isinstance(t, (str, bytes)) and str(t).strip()
                        }
                    )
                    meta["import_tags"] = import_tags
                    meta["is_delegate"] = "delegate" in import_tags
                    meta["is_dynamic"] = "dynamic" in import_tags
                e["meta"] = meta

            out.append(e)

    return out


def _soft_edges(nf_nodes: Dict[str, Dict[str, Any]], scope: Set[str]) -> List[Dict[str, Any]]:
    """
    Conservative soft mapping within `scope`. If an import target isn't in-scope
    but has an in-scope "child", connect to the shallowest child.

    Works with both:
      - NodeIds / file-ids (preferred): 'scrapy/core/engine.py'
      - Dotted ids (legacy): 'scrapy.core.engine'

    Canonical direction preserved: provider -> consumer. Marks inferred edges
    with `inferred=True`.

    Attaches per-import metadata from `imports_meta` when available (same shape
    and normalization as in _strict_edges).
    """
    ids = sorted(scope)
    idset = set(ids)

    # index by strict prefixes -> best (shallowest) child within scope
    prefix_choice: Dict[str, str] = {}
    for cid in ids:
        parts = _token_parts(cid)
        # For NodeIds, prefixes are dotted or path-ish "modules":
        #   ['scrapy', 'core', 'engine'] -> 'scrapy', 'scrapy.core'
        for k in range(1, len(parts)):
            pref = ".".join(parts[:k])
            best = prefix_choice.get(pref)
            if best is None or _is_shallower(cid, best):
                prefix_choice[pref] = cid

    out: List[Dict[str, Any]] = []
    for consumer_id, rec in nf_nodes.items():
        if consumer_id not in scope:
            continue

        imports_meta = rec.get("imports_meta") or {}

        for mod_token in (rec.get("imports") or []):
            if not isinstance(mod_token, str):
                continue

            # Direct in-scope match (NodeId or dotted)
            if mod_token in idset:
                provider_id = mod_token
                e: Dict[str, Any] = {
                    "src": provider_id,
                    "dst": consumer_id,
                    "kind": "import",
                }
                pm = imports_meta.get(provider_id)
                if isinstance(pm, dict):
                    meta = dict(pm)
                    tags = meta.get("tags") or []
                    if isinstance(tags, (list, tuple)):
                        import_tags = sorted(
                            {
                                str(t).strip().lower()
                                for t in tags
                                if isinstance(t, (str, bytes)) and str(t).strip()
                            }
                        )
                        meta["import_tags"] = import_tags
                        meta["is_delegate"] = "delegate" in import_tags
                        meta["is_dynamic"] = "dynamic" in import_tags
                    e["meta"] = meta
                out.append(e)
                continue

            # Prefix-based nudge to shallowest in-scope child.
            # We interpret the token as a "module-ish" id:
            #   'scrapy.core.downloader' or 'scrapy/core/downloader.py'
            parts_token = _token_parts(mod_token)
            if parts_token:
                pref = ".".join(parts_token)  # full module-ish token as prefix key
                if pref in prefix_choice:
                    provider_id = prefix_choice[pref]
                    e = {
                        "src": provider_id,
                        "dst": consumer_id,
                        "kind": "import",
                        "inferred": True,
                    }
                    pm = imports_meta.get(provider_id)
                    if isinstance(pm, dict):
                        meta = dict(pm)
                        tags = meta.get("tags") or []
                        if isinstance(tags, (list, tuple)):
                            import_tags = sorted(
                                {
                                    str(t).strip().lower()
                                    for t in tags
                                    if isinstance(t, (str, bytes)) and str(t).strip()
                                }
                            )
                            meta["import_tags"] = import_tags
                            meta["is_delegate"] = "delegate" in import_tags
                            meta["is_dynamic"] = "dynamic" in import_tags
                        e["meta"] = meta
                    out.append(e)
                    continue

            # Unique basename fallback (only if unambiguous within scope).
            # For NodeIds we use the stem:
            #   'scrapy/core/engine.py' -> 'engine'
            base_parts = _token_parts(mod_token)
            base = base_parts[-1] if base_parts else ""
            if not base:
                continue

            hits = [cid for cid in ids if _token_parts(cid)[-1] == base]
            if len(hits) == 1:
                provider_id = hits[0]
                e = {
                    "src": provider_id,
                    "dst": consumer_id,
                    "kind": "import",
                    "inferred": True,
                }
                pm = imports_meta.get(provider_id)
                if isinstance(pm, dict):
                    meta = dict(pm)
                    tags = meta.get("tags") or []
                    if isinstance(tags, (list, tuple)):
                        import_tags = sorted(
                            {
                                str(t).strip().lower()
                                for t in tags
                                if isinstance(t, (str, bytes)) and str(t).strip()
                            }
                        )
                        meta["import_tags"] = import_tags
                        meta["is_delegate"] = "delegate" in import_tags
                        meta["is_dynamic"] = "dynamic" in import_tags
                    e["meta"] = meta
                out.append(e)

    return out


# ---------------------------------------------------------------------------
# Post-processing: collapse package-__init__ parent edges
# ---------------------------------------------------------------------------

def _pkg_root_for_init(node_id: str) -> Optional[str]:
    """
    If node_id is a package __init__ file, return its package path
    (directory part), e.g.:

        'diagnostics/__init__.py'     -> 'diagnostics'
        'ui/window/main/__init__.py'  -> 'ui/window/main'

    Otherwise return None.
    """
    s = node_id.replace("\\", "/")
    if s.endswith("/__init__.py"):
        return s[: -len("/__init__.py")]
    return None


def _collapse_pkg_parent_edges(edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    For each consumer (dst), if there are edges from both:

        pkg/__init__.py -> dst
        pkg/...something.py -> dst

    under the same package root, drop the pkg/__init__.py -> dst edges.

    This keeps real package-level imports (only __init__ present) but
    avoids "double edges" where a single logical import resolves to
    both the package __init__ and a more specific child module.
    """
    if not edges:
        return edges

    # Index edges by consumer
    by_dst: Dict[str, List[int]] = {}
    for i, e in enumerate(edges):
        dst = e.get("dst")
        if isinstance(dst, str):
            by_dst.setdefault(dst, []).append(i)

    to_drop: Set[int] = set()

    for dst, idxs in by_dst.items():
        if not idxs:
            continue

        # For this consumer, track:
        #   - package __init__ providers by package root
        #   - all non-init provider ids (for prefix checks)
        init_by_pkg: Dict[str, List[int]] = {}
        noninit_srcs: List[str] = []

        for i in idxs:
            e = edges[i]
            src = e.get("src")
            if not isinstance(src, str):
                continue
            src_norm = src.replace("\\", "/")
            pkg_root = _pkg_root_for_init(src_norm)
            if pkg_root:
                init_by_pkg.setdefault(pkg_root, []).append(i)
            else:
                noninit_srcs.append(src_norm)

        if not init_by_pkg or not noninit_srcs:
            continue

        # For each package root that has a __init__ edge, check if there
        # is any deeper module edge under the same package for this dst.
        pkg_with_child: Set[str] = set()
        for src_norm in noninit_srcs:
            for pkg_root in init_by_pkg.keys():
                # Require a true subpath: 'diagnostics/' prefix for 'diagnostics/logging.py'
                if src_norm.startswith(pkg_root + "/"):
                    pkg_with_child.add(pkg_root)

        # Drop all __init__ edges in those packages for this consumer
        for pkg_root in pkg_with_child:
            for i in init_by_pkg.get(pkg_root, []):
                to_drop.add(i)

    if not to_drop:
        return edges

    return [e for i, e in enumerate(edges) if i not in to_drop]


# ---------------------------------------------------------------------------
# Top-level artifact builder
# ---------------------------------------------------------------------------

def build_edges_artifact(
    *,
    nodefacts_path: Path,
    out_path: Path,
    reachable_path: Optional[Path] = None,
    policy: str = "strict",  # "strict" or "soft"
) -> Dict[str, Any]:
    """
    Build edges.json from:
      - nodefacts.json (NodeFacts; nodes keyed by NodeId)
      - reachable.json (seed + scope nodes)  [optional; metadata only]

    World-1 behavior (global graph):
      - Emit edges for *all* nodes present in nodefacts (global graph),
        regardless of reachable scope.
      - If reachable is provided, record seed and reachable node counts
        for downstream filtering/diagnostics, but do not gate edge emission.

    Both NodeFacts and reachable are expected to be in NodeId space now,
    where NodeId is the canonical file-id (repo-relative POSIX path with
    '.py' or '/__init__.py').

    Extra behavior:
      - After raw edge construction (strict / soft), collapse redundant
        package-__init__ parent edges per consumer, preferring more specific
        child-module providers when both exist for the same dst.
    """
    # ---- Load nodefacts (required)
    nf = _load_json(nodefacts_path)
    nf_nodes = _as_map_nodes(nf.get("nodes") if isinstance(nf, dict) else None)

    # ---- Load reachable (optional; metadata only)
    seed: Optional[str] = None
    reachable_nodes: Set[str] = set()

    if reachable_path is not None and Path(reachable_path).exists():
        try:
            reach = _load_json(Path(reachable_path))
            if isinstance(reach, dict):
                seed = reach.get("seed") if isinstance(reach.get("seed"), str) else None
                for n in (reach.get("nodes") or []):
                    if isinstance(n, str):
                        nid = n
                    elif isinstance(n, dict):
                        nid = n.get("node_id")
                    else:
                        nid = None
                    if isinstance(nid, str) and nid:
                        reachable_nodes.add(nid)
        except Exception as e:
            log_event("EDGES:reachable_load_failed", err=repr(e), path=str(reachable_path))

    # ---- World-1 scope: all nodefacts nodes
    scope_nodes: Set[str] = set(nf_nodes.keys())

    # ---- Policy normalize
    if policy not in ("strict", "soft"):
        log_event("EDGES:unknown_policy_defaulting_strict", policy=str(policy))
        policy = "strict"

    # ---- Debug counters (kept as payload stats; no stdout print)
    total_import_tokens = 0
    kept_in_scope = 0
    dropped_not_in_scope = 0

    for consumer_id, rec in nf_nodes.items():
        # consumer is always in scope_nodes by construction; keep explicit
        if consumer_id not in scope_nodes:
            continue
        for provider_id in (rec.get("imports") or []):
            if not isinstance(provider_id, str):
                continue
            total_import_tokens += 1
            if provider_id in scope_nodes:
                kept_in_scope += 1
            else:
                dropped_not_in_scope += 1

    # ---- Emit global edges within nodefacts scope
    if policy == "soft":
        edges = _soft_edges(nf_nodes, scope_nodes)
    else:
        edges = _strict_edges(nf_nodes, scope_nodes)

    # ---- Collapse redundant package-__init__ parent edges
    edges = _collapse_pkg_parent_edges(edges)

    # ---- Determinism
    edges.sort(
        key=lambda e: (
            e.get("src", ""),
            e.get("dst", ""),
            int(bool(e.get("inferred"))),
        )
    )

    payload: Dict[str, Any] = {
        # Reachable metadata only (may be None/empty)
        "seed": seed,
        "edges": edges,
        "final_stats": {
            "total_edges": len(edges),
            "policy": policy,
            "nodefacts_nodes": len(scope_nodes),
            "reachable_nodes": len(reachable_nodes),
            "imports_total": total_import_tokens,
            "imports_kept_in_scope": kept_in_scope,
            "imports_dropped_not_in_nodefacts": dropped_not_in_scope,
        },
    }

    # ---- Atomic write (pretty/sorted handled by atomic_write_json)
    atomic_write_json(payload, out_path)

    log_event(
        "EDGES:build_edges_artifact_done",
        out=str(out_path),
        total_edges=len(edges),
        policy=policy,
        nodefacts_nodes=len(scope_nodes),
        reachable_nodes=len(reachable_nodes),
        imports_total=total_import_tokens,
        imports_kept=kept_in_scope,
        imports_dropped=dropped_not_in_scope,
    )

    return payload

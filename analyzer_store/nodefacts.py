from __future__ import annotations

"""
NodeFacts schema implementation and persistence helpers.

Schema history
--------------
v1.6
  Baseline position-agnostic, seed-aware subgraph facts.

v1.7
  Added optional detailed metadata fields:
    - classes_detailed
    - functions_detailed
    - globals_detailed
  These provide richer Python symbol metadata such as type hints,
  decorators, signatures, methods, attributes, and line numbers.

v1.8
  Added dual SCC support and related structural metrics:
    - meta["scc_conceptual"]
    - meta["scc_runtime"]
    - meta["tc_inflation"]
    - per-node dynamic attributes:
        - scc_id_runtime
        - scc_size_runtime

v1.9
  Added per-node code metrics sourced from FolderIndex FileEntry:
    - sloc
    - comment_lines
    - blank_lines
    - comment_pct
  These were previously available only as aggregates in the meta block.
  Now populated on every node directly from the folder index at build time.

Compatibility
-------------
Loaders accept:
  - nodefacts@v1.9
  - nodefacts@v1.8
  - nodefacts@v1.7
  - nodefacts@v1.6
  - nodefacts:1.5
  - None

Serializers emit:
  - nodefacts@v1.9
"""

from collections import defaultdict, deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Set, Tuple
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
import os

try:
    import orjson  # fast path
except Exception:  # pragma: no cover
    orjson = None  # type: ignore

from i_o.workspace_io import get_active_workspace
from analyzer_store.types import (
    NODEFACTS_SCHEMA,
    FolderIndex,
    NodeFacts,
    NodeFactsNode,
)
from analyzer_store.io_utils import iso_utc, atomic_write_json

# Parser-driven symbol extraction (v1.7 metadata support)
from analyzer.parse import parse_file
from analyzer.config import AnalyzerCfg, load_config

# Canonical ids: repo-relative file-id is the SSOT for NodeId
from adapters.canonical import canon_node_id

# Diagnostics logging
try:
    from diagnostics.logging import log_event
except Exception:  # pragma: no cover
    def log_event(*_a, **_k):
        return


ACCEPTED_NODEFACTS_SCHEMAS = (
    "nodefacts@v1.9",
    "nodefacts@v1.8",
    "nodefacts@v1.7",
    "nodefacts@v1.6",
    "nodefacts:1.5",
    None,
)


def _build_nodefacts_batch(
    batch_mids: List[str],
    files_by_mid: Dict[str, "FileEntry"],
    root_path: Path,
    cfg_eff: "AnalyzerCfg",
    adj_fwd_mod: Dict[str, List[str]],
    mid_to_node_id: Dict[str, str],
    imports_node: Dict[str, List[str]],
    gen_seq_node: Dict[str, int],
    deg_in_node: Dict[str, int],
    deg_out_node: Dict[str, int],
    scc_id_node: Dict[str, str],
    scc_size_node: Dict[str, int],
    du_node: Dict[str, Optional[int]],
    dd_node: Dict[str, Optional[int]],
    # v1.8: runtime SCC maps for per-node dynamic attributes
    scc_id_node_rt: Optional[Dict[str, str]] = None,
    scc_size_node_rt: Optional[Dict[str, int]] = None,
) -> Dict[str, "NodeFactsNode"]:
    """
    Worker: build NodeFactsNode entries for a batch of module-ids.

    Runs with no per-node prints; progress is reported in the parent
    when each batch completes.

    v1.7:
      Attaches detailed metadata fields when parser output provides them:
        - classes_detailed
        - functions_detailed
        - globals_detailed

    v1.8:
      Attaches runtime SCC fields as dynamic attributes when runtime SCC maps
      are provided:
        - scc_id_runtime
        - scc_size_runtime

    v1.9:
      Populates per-node code metrics from FolderIndex FileEntry:
        - sloc
        - comment_lines
        - blank_lines
        - comment_pct
    """
    nodes: Dict[str, NodeFactsNode] = {}

    for mid in batch_mids:
        nid = mid_to_node_id.get(mid)
        if not nid:
            continue

        fe = files_by_mid.get(mid)
        name = (mid.rsplit(".", 1)[-1]) if mid else mid

        # Stable/simple arrays (v1.6-compatible core fields)
        classes: List[str] = []
        functions: List[str] = []
        globals_: List[str] = []
        exports: List[str] = []

        # v1.7 metadata fields (optional, dynamic)
        classes_detailed: Optional[List[Dict[str, Any]]] = None
        functions_detailed: Optional[List[Dict[str, Any]]] = None
        globals_detailed: Optional[List[Dict[str, Any]]] = None

        crosstalk_candidates_py_v1: Tuple[Mapping[str, Any], ...] = ()
        parse_status = fe.parse_status if fe else "skipped"

        parsed = None
        if fe and fe.file:
            try:
                parse_path = (root_path / fe.file).resolve()
                parsed, warns = parse_file(parse_path, cfg_eff)
                if parsed:
                    # Stable/simple names for backward compatibility
                    classes = [getattr(s, "name", str(s)) for s in (parsed.classes or [])]
                    functions = [getattr(s, "name", str(s)) for s in (parsed.functions or [])]
                    globals_ = [getattr(s, "name", str(s)) for s in (parsed.globals or [])]
                    exports = list(parsed.all_exports or [])

                    # v1.7: detailed metadata extraction if available
                    if hasattr(parsed, "classes_detailed") and parsed.classes_detailed:
                        try:
                            classes_detailed = []
                            for class_info in parsed.classes_detailed:
                                if hasattr(class_info, "__dataclass_fields__"):
                                    class_dict = asdict(class_info)
                                elif isinstance(class_info, dict):
                                    class_dict = class_info
                                else:
                                    continue
                                classes_detailed.append(class_dict)
                        except Exception:
                            classes_detailed = None

                    if hasattr(parsed, "functions_detailed") and parsed.functions_detailed:
                        try:
                            functions_detailed = []
                            for func_info in parsed.functions_detailed:
                                if hasattr(func_info, "__dataclass_fields__"):
                                    func_dict = asdict(func_info)
                                elif isinstance(func_info, dict):
                                    func_dict = func_info
                                else:
                                    continue
                                functions_detailed.append(func_dict)
                        except Exception:
                            functions_detailed = None

                    if hasattr(parsed, "globals_detailed") and parsed.globals_detailed:
                        try:
                            globals_detailed = []
                            for global_info in parsed.globals_detailed:
                                if hasattr(global_info, "__dataclass_fields__"):
                                    global_dict = asdict(global_info)
                                elif isinstance(global_info, dict):
                                    global_dict = global_info
                                else:
                                    continue
                                globals_detailed.append(global_dict)
                        except Exception:
                            globals_detailed = None

                    # Existing crosstalk candidates
                    try:
                        crosstalk_candidates_py_v1 = tuple(parsed.crosstalk_candidates_py_v1 or [])
                    except Exception:
                        crosstalk_candidates_py_v1 = ()

                if warns:
                    parse_status = "warn"
                else:
                    parse_status = parse_status or "ok"
            except Exception:
                parse_status = "error"

        # ── v1.9: per-node code metrics ───────────────────────────────────
        # All five metrics are sourced from the folder index FileEntry,
        # which is the authoritative source for loc/sloc across all languages.
        #
        # parsed.loc_code is NOT used for loc — it contains code-line count
        # (SLOC semantics) for Python, not total LOC. Using it as loc causes
        # node.loc < node.sloc when the two SLOC counters disagree.
        #
        # parsed.loc_code is retained as a last-resort fallback for sloc only,
        # for languages where fe.sloc may be absent.
        loc_value: Optional[int] = None
        sloc_value: Optional[int] = None
        comment_lines_value: Optional[int] = None
        blank_lines_value: Optional[int] = None
        comment_pct_value: Optional[float] = None

        if fe:
            loc_value = getattr(fe, "loc", None)
            sloc_value = getattr(fe, "sloc", None)
            comment_lines_value = getattr(fe, "comment_lines", None)
            blank_lines_value = getattr(fe, "blank_lines", None)
            comment_pct_value = getattr(fe, "comment_pct", None)

        if sloc_value is None and parsed:
            sloc_value = getattr(parsed, "loc_code", None)

        # Remap imports from module-id -> NodeId
        imports_ids = list(imports_node.get(nid, []))

        # Base node uses stable/core schema fields.
        # scc_id / scc_size continue to represent conceptual SCC values.
        node = NodeFactsNode(
            id=nid,
            name=name,
            imports=tuple(sorted(set(imports_ids))),
            exports=tuple(exports),
            classes=tuple(classes),
            functions=tuple(functions),
            globals=tuple(globals_),
            gen_seq=gen_seq_node.get(nid, 0),
            importers_count=deg_in_node.get(nid, 0),
            dependencies_count=deg_out_node.get(nid, 0),
            scc_id=scc_id_node.get(nid, nid),
            scc_size=scc_size_node.get(scc_id_node.get(nid, nid), 1),
            file=fe.file if fe else "",
            hash=fe.hash if fe else None,
            loc=loc_value,
            sloc=sloc_value,
            comment_lines=comment_lines_value,
            blank_lines=blank_lines_value,
            comment_pct=comment_pct_value,
            size_bytes=fe.size_bytes if fe else None,
            mtime=fe.mtime if fe else None,
            du=du_node.get(nid),
            dd=dd_node.get(nid),
            parse_status=parse_status,
            crosstalk_candidates_py_v1=crosstalk_candidates_py_v1,
        )

        # v1.7: attach detailed metadata as dynamic attributes
        if classes_detailed:
            try:
                setattr(node, "classes_detailed", tuple(classes_detailed))
            except Exception:
                pass

        if functions_detailed:
            try:
                setattr(node, "functions_detailed", tuple(functions_detailed))
            except Exception:
                pass

        if globals_detailed:
            try:
                setattr(node, "globals_detailed", tuple(globals_detailed))
            except Exception:
                pass

        # v1.8: attach per-node runtime SCC fields as dynamic attributes
        if scc_id_node_rt is not None and scc_size_node_rt is not None:
            try:
                rt_sid = scc_id_node_rt.get(nid, nid)
                rt_ssz = scc_size_node_rt.get(rt_sid, 1)
                setattr(node, "scc_id_runtime", rt_sid)
                setattr(node, "scc_size_runtime", rt_ssz)
            except Exception:
                pass

        nodes[nid] = node

    return nodes


# ------------------------------ fast JSON -----------------------------------

def _loads_bytes(data: bytes) -> dict:
    """
    Fast, tolerant JSON loader:
      - use orjson when available
      - fallback to stdlib on UTF-8 decode
      - always return a dict ({} for errors or wrong root type)
    """
    try:
        obj = orjson.loads(data) if orjson else json.loads(data.decode("utf-8", errors="ignore"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# SCC summary helper
# ---------------------------------------------------------------------------

def _scc_summary(scc_size: Dict[str, int]) -> Dict[str, Any]:
    """
    Summarize SCC results into a compact dict for meta reporting.

    Returns:
      cycle_nodes:      total nodes participating in any non-trivial SCC
      largest_scc_size: size of the largest SCC (0 if no cycles)
      scc_count:        number of non-trivial SCCs (size > 1)
    """
    multi = {k: v for k, v in scc_size.items() if v > 1}
    return {
        "cycle_nodes": sum(multi.values()),
        "largest_scc_size": max(multi.values(), default=0),
        "scc_count": len(multi),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_nodefacts(
    seed_id: Optional[str],
    idx: FolderIndex,
    cfg: Optional[AnalyzerCfg] = None,
) -> NodeFacts:
    """
    Build NodeFacts from a FolderIndex.

    FolderIndex uses module-like ids as keys (e.g. 'scrapy.core.engine').
    This function:
      1) Builds reachability / SCCs in that module-id space.
      2) Bridges each module-id -> canonical NodeId (repo-relative file-id).
      3) Emits NodeFacts keyed by NodeId, with NodeFactsNode.id == NodeId.

    Version semantics
    -----------------
    v1.7:
      Supports detailed metadata extraction for Python files.

    v1.8:
      Computes both:
        - conceptual SCC: full graph, including TYPE_CHECKING imports
        - runtime SCC: runtime-only graph, excluding TYPE_CHECKING imports

      The conceptual SCC remains the meaning of the stable per-node fields:
        - scc_id
        - scc_size

      Runtime SCC is exposed through:
        - meta["scc_runtime"]
        - dynamic per-node attributes:
            - scc_id_runtime
            - scc_size_runtime

    v1.9:
      Per-node code metrics populated directly from FolderIndex FileEntry:
        - sloc
        - comment_lines
        - blank_lines
        - comment_pct
    """

    log_event("NODEFACTS:build_enter", seed_id=seed_id)

    # ── Effective scan root & config (NodeId canonicalization needs scan_root) ──
    try:
        root_meta = (getattr(idx, "meta", {}) or {}).get("root")
        root_path = Path(root_meta).resolve() if root_meta else get_active_workspace().scan_root
        log_event("NODEFACTS:root_resolved", from_meta=bool(root_meta), root=str(root_path))
    except Exception:
        root_path = Path.cwd().resolve()
        log_event("NODEFACTS:root_cwd_fallback", root=str(root_path))

    try:
        cfg_eff: AnalyzerCfg = cfg or (load_config(root_path) or AnalyzerCfg())
        log_event("NODEFACTS:cfg_resolved")
    except Exception:
        cfg_eff = AnalyzerCfg()
        log_event("NODEFACTS:cfg_fallback_default")

    # ── Build adjacency token source from FolderIndex (module-id space) ──
    # World-1:
    #   seed_id is None -> use imports_all (richer tokens; includes external deps).
    # Seeded mode:
    #   use imports_internal (historical behavior).
    using_all_tokens = seed_id is None

    imports_map: Dict[str, Tuple[str, ...]] = {}
    # v1.8: runtime-only import map (TYPE_CHECKING excluded)
    imports_map_runtime: Dict[str, Tuple[str, ...]] = {}

    for mid, fe in idx.files.items():
        try:
            if using_all_tokens:
                toks = set(getattr(fe, "imports_all", None) or ())
            else:
                toks = set(getattr(fe, "imports_internal", None) or ())

            # v1.8 conceptual graph includes explicit internal imports plus
            # language-specific inferred structural edges, such as Kotlin symbol edges.
            #
            # Runtime graph remains separate below and should not automatically inherit
            # symbol_internal unless a future symbol_runtime_internal channel is added.
            toks.update(getattr(fe, "symbol_internal", ()) or ())

            imports_map[mid] = tuple(sorted(toks))
        except Exception:
            imports_map[mid] = tuple()

        try:
            rt_toks = getattr(fe, "imports_runtime_internal", None) or ()
            imports_map_runtime[mid] = tuple(rt_toks)
        except Exception:
            imports_map_runtime[mid] = tuple()

    log_event(
        "NODEFACTS:imports_map_built",
        count=len(imports_map),
        source=("imports_all" if using_all_tokens else "imports_internal"),
        runtime_count=len(imports_map_runtime),
    )

    # ── Normalize targets into FolderIndex key-space ─────────
    mids_set: Set[str] = set(idx.files.keys())

    # Map NodeId -> mid, using FileEntry.file as the bridge
    nodeid_to_mid: Dict[str, str] = {}
    for mid, fe in idx.files.items():
        try:
            file_rel = getattr(fe, "file", None)
            if not file_rel:
                continue
            nid = canon_node_id(file_rel, scan_root=root_path)
            if nid:
                nodeid_to_mid[nid] = mid
        except Exception:
            continue

    def _shorten_dotted(s: str) -> List[str]:
        """Return dotted candidates from most-specific to least."""
        s = (s or "").strip()
        if not s:
            return []
        for sep in (":", "#", "?"):
            if sep in s:
                s = s.split(sep, 1)[0].strip()

        out = [s]
        while "." in s:
            s = s.rsplit(".", 1)[0]
            out.append(s)
        return out

    def _resolve_to_mid(tok: str, *, consumer_mid: Optional[str]) -> Optional[str]:
        # 1) already a mid
        if tok in mids_set:
            return tok

        # 2) dotted token might include symbols; shorten to a known mid
        for cand in _shorten_dotted(tok):
            if cand in mids_set:
                return cand

        # 3) consumer-aware prefixing
        if consumer_mid:
            parts = consumer_mid.split(".")
            for i in range(len(parts) - 1, 0, -1):
                prefix = ".".join(parts[:i])
                for cand in _shorten_dotted(tok):
                    pc = f"{prefix}.{cand}"
                    if pc in mids_set:
                        return pc

        # 4) already a NodeId
        m = nodeid_to_mid.get(tok)
        if m:
            return m

        # 5) maybe a file-rel / module-ish string -> NodeId -> mid
        try:
            nid = canon_node_id(tok, scan_root=root_path)
            m = nodeid_to_mid.get(nid)
            if m:
                return m
        except Exception:
            pass

        return None

    imports_map_norm: Dict[str, Tuple[str, ...]] = {}
    imports_map_runtime_norm: Dict[str, Tuple[str, ...]] = {}
    dropped = 0
    remapped = 0
    ext_token_total = 0
    ext_token_samples: List[str] = []

    for a_mid, outs in imports_map.items():
        keep: List[str] = []
        for t in outs or ():
            if not isinstance(t, str) or not t:
                continue
            t_mid = _resolve_to_mid(t, consumer_mid=a_mid)
            if t_mid is None:
                dropped += 1
                ext_token_total += 1
                if len(ext_token_samples) < 40:
                    ext_token_samples.append(t)
                continue
            if t_mid != t:
                remapped += 1
            keep.append(t_mid)
        imports_map_norm[a_mid] = tuple(sorted(set(keep)))

    for a_mid, outs in imports_map_runtime.items():
        keep_rt: List[str] = []
        for t in outs or ():
            if not isinstance(t, str) or not t:
                continue
            t_mid = _resolve_to_mid(t, consumer_mid=a_mid)
            if t_mid is not None:
                keep_rt.append(t_mid)
        imports_map_runtime_norm[a_mid] = tuple(sorted(set(keep_rt)))

    log_event(
        "NODEFACTS:imports_map_normalized",
        mids=len(imports_map_norm),
        remapped=remapped,
        dropped=dropped,
        external_tokens=ext_token_total,
        external_samples=ext_token_samples[:20],
        runtime_mids=len(imports_map_runtime_norm),
    )

    imports_map = imports_map_norm
    imports_map_runtime = imports_map_runtime_norm

    # Precompute importers among internal ids (module-id space)
    importers_map: Dict[str, List[str]] = defaultdict(list)
    for a, outs in imports_map.items():
        for b in outs:
            if b in imports_map:
                importers_map[b].append(a)
    log_event("NODEFACTS:importers_map_built", count=len(importers_map))

    # ── Seed handling in module-id space ──────────────────────────────────────
    seed_mod: Optional[str] = seed_id

    if seed_id and seed_id not in imports_map:
        log_event("NODEFACTS:seed_not_in_imports_map_try_nodeid", seed_id=seed_id)
        for mid, fe in idx.files.items():
            try:
                file_rel = getattr(fe, "file", None)
                if not file_rel:
                    continue
                nid = canon_node_id(file_rel, scan_root=root_path)
                if nid == seed_id:
                    seed_mod = mid
                    log_event("NODEFACTS:seed_mapped_to_module_id", seed_mod=mid)
                    break
            except Exception:
                continue

    log_event("NODEFACTS:seed_mod_resolved", seed_mod=seed_mod)

    # ── Reachable set (bidirectional) ────────────────────────────────────────
    if seed_mod and seed_mod in imports_map:
        reach = _bidirectional_reachable(seed_mod, imports_map, importers_map)
        log_event("NODEFACTS:reach_bidirectional", size=len(reach))
    else:
        if seed_mod is None:
            reach = set(imports_map.keys())
            log_event("NODEFACTS:reach_full_internal", size=len(reach))
        else:
            reach = set()
            log_event("NODEFACTS:reach_seed_unmapped_empty")

    if reach:
        r_nodes: List[str] = sorted(reach)
    elif seed_mod is None:
        r_nodes = sorted(imports_map.keys())
    else:
        r_nodes = []
    log_event("NODEFACTS:r_nodes_computed", count=len(r_nodes))

    # If seed provided but not internal / unmappable, emit minimal meta
    if seed_mod and seed_mod not in imports_map:
        log_event("NODEFACTS:seed_not_internal_early_empty_meta", seed_mod=seed_mod)
        meta = {
            "created": iso_utc(),
            "graph_hash": _graph_hash_from_index(idx),
            "gen_seq_source": "seq_id",
            "seed_id": seed_id,
            "no_seed_distances": True,
            "imports_token_source": ("imports_all" if using_all_tokens else "imports_internal"),
            "external_import_tokens": ext_token_total,
            "external_import_samples": ext_token_samples[:20],
        }
        return NodeFacts(schema=NODEFACTS_SCHEMA, meta=meta, nodes={})

    # ── Restrict adjacency to reachable nodes only (module-id space) ───
    adj_fwd_mod: Dict[str, List[str]] = {n: [] for n in r_nodes}
    adj_rev_mod: Dict[str, List[str]] = {n: [] for n in r_nodes}

    for a in r_nodes:
        outs = imports_map.get(a, ())
        keep = [b for b in outs if b in r_nodes]
        adj_fwd_mod[a] = sorted(set(keep))
        for b in adj_fwd_mod[a]:
            adj_rev_mod[b].append(a)

    for b in r_nodes:
        adj_rev_mod[b] = sorted(set(adj_rev_mod[b]))

    # v1.8: runtime-only adjacency restricted to reachable nodes
    adj_fwd_mod_rt: Dict[str, List[str]] = {n: [] for n in r_nodes}
    for a in r_nodes:
        outs_rt = imports_map_runtime.get(a, ())
        keep_rt = [b for b in outs_rt if b in r_nodes]
        adj_fwd_mod_rt[a] = sorted(set(keep_rt))

    log_event(
        "NODEFACTS:adjacency_restricted",
        fwd_nodes=len(adj_fwd_mod),
        rev_nodes=len(adj_rev_mod),
        fwd_nodes_rt=len(adj_fwd_mod_rt),
    )

    # ── Distances (module-id space) ──────────────────────────────────────────
    if seed_mod:
        du_mod = _bfs_distance(seed_mod, adj_rev_mod)
        dd_mod = _bfs_distance(seed_mod, adj_fwd_mod)
        log_event("NODEFACTS:distance_computed_with_seed")
    else:
        du_mod = {n: None for n in r_nodes}
        dd_mod = {n: None for n in r_nodes}
        log_event("NODEFACTS:distance_seedless_all_none")

    # ── SCCs (Tarjan in module-id space; deterministic order) ────────────────
    # Conceptual SCC: full graph including TYPE_CHECKING imports.
    # This remains the meaning of the stable scc_id / scc_size fields.
    scc_id_of_mod, scc_size_of_mod = _tarjan_scc(adj_fwd_mod)
    log_event("NODEFACTS:scc_computed", count=len(scc_id_of_mod))

    # v1.8: Runtime SCC = runtime-only imports (TYPE_CHECKING excluded).
    scc_id_of_mod_rt, scc_size_of_mod_rt = _tarjan_scc(adj_fwd_mod_rt)
    log_event("NODEFACTS:scc_runtime_computed", count=len(scc_id_of_mod_rt))

    # Degrees (module-id space)
    deg_in_mod = {n: len(adj_rev_mod.get(n, ())) for n in r_nodes}
    deg_out_mod = {n: len(adj_fwd_mod.get(n, ())) for n in r_nodes}
    log_event("NODEFACTS:degrees_module_space_computed")

    # gen_seq source
    gen_seq_mod = {n: i for i, n in enumerate(sorted(r_nodes))}
    log_event("NODEFACTS:gen_seq_mod_computed")

    # ── Bridge module-id -> NodeId (canonical file-id) ────────────────────────
    mid_to_node_id: Dict[str, str] = {}

    for mid in r_nodes:
        fe = idx.files.get(mid)
        nid: str
        try:
            if fe and getattr(fe, "file", None):
                nid = canon_node_id(fe.file, scan_root=root_path)
            else:
                nid = canon_node_id(mid, scan_root=root_path)
        except Exception:
            nid = mid

        if nid:
            mid_to_node_id[mid] = nid

    log_event("NODEFACTS:mid_to_node_id_count", count=len(mid_to_node_id))

    if not mid_to_node_id:
        log_event("NODEFACTS:mid_to_node_id_empty_early_return")
        meta = {
            "created": iso_utc(),
            "graph_hash": _graph_hash_from_index(idx),
            "gen_seq_source": "seq_id",
            "seed_id": seed_id or "",
            "no_seed_distances": False if seed_id else True,
            "imports_token_source": ("imports_all" if using_all_tokens else "imports_internal"),
            "external_import_tokens": ext_token_total,
            "external_import_samples": ext_token_samples[:20],
        }
        return NodeFacts(schema=NODEFACTS_SCHEMA, meta=meta, nodes={})

    # ── Build imports in NodeId space ─────────────────────────────────────────
    imports_node: Dict[str, List[str]] = {}

    for a_mid in r_nodes:
        a_nid = mid_to_node_id.get(a_mid)
        if not a_nid:
            continue

        outs_mid = adj_fwd_mod.get(a_mid, [])
        outs_nid: List[str] = []
        for b_mid in outs_mid:
            b_nid = mid_to_node_id.get(b_mid)
            if b_nid:
                outs_nid.append(b_nid)

        imports_node[a_nid] = sorted(set(outs_nid))

    log_event(
        "NODEFACTS:imports_node_built",
        nodes=len(imports_node),
        import_tokens=sum(len(v) for v in imports_node.values()),
        imports_token_source=("imports_all" if using_all_tokens else "imports_internal"),
        external_import_tokens=ext_token_total,
    )

    # ── Remap metrics to NodeId space ────────────────────────────────────────
    node_ids_sorted = sorted(mid_to_node_id.values())
    gen_seq_node: Dict[str, int] = {nid: i for i, nid in enumerate(node_ids_sorted)}
    log_event("NODEFACTS:gen_seq_node_computed")

    deg_in_node: Dict[str, int] = {}
    deg_out_node: Dict[str, int] = {}
    for mid, nid in mid_to_node_id.items():
        deg_in_node[nid] = deg_in_mod.get(mid, 0)
        deg_out_node[nid] = deg_out_mod.get(mid, 0)
    log_event("NODEFACTS:degrees_node_space_computed")

    du_node: Dict[str, Optional[int]] = {}
    dd_node: Dict[str, Optional[int]] = {}
    for mid, nid in mid_to_node_id.items():
        du_node[nid] = du_mod.get(mid)
        dd_node[nid] = dd_mod.get(mid)
    log_event("NODEFACTS:distance_node_space_computed")

    # Conceptual SCCs mapped to NodeId space (stable/backward-compatible fields)
    scc_id_node: Dict[str, str] = {}
    scc_size_node: Dict[str, int] = {}
    for mid, nid in mid_to_node_id.items():
        sid_mid = scc_id_of_mod.get(mid, mid)
        sid_nid = mid_to_node_id.get(sid_mid, sid_mid)
        scc_id_node[nid] = sid_nid
        scc_size_node[sid_nid] = scc_size_of_mod.get(sid_mid, 1)
    log_event("NODEFACTS:scc_node_space_mapped")

    # v1.8: Runtime SCCs mapped to NodeId space
    scc_id_node_rt: Dict[str, str] = {}
    scc_size_node_rt: Dict[str, int] = {}
    for mid, nid in mid_to_node_id.items():
        sid_mid_rt = scc_id_of_mod_rt.get(mid, mid)
        sid_nid_rt = mid_to_node_id.get(sid_mid_rt, sid_mid_rt)
        scc_id_node_rt[nid] = sid_nid_rt
        scc_size_node_rt[sid_nid_rt] = scc_size_of_mod_rt.get(sid_mid_rt, 1)
    log_event("NODEFACTS:scc_runtime_node_space_mapped")

    # ── Compose nodes payload ────────────────────────────────────────────────
    log_event("NODEFACTS:compose_nodes_begin")

    nodes: Dict[str, NodeFactsNode] = {}
    files_by_mid: Dict[str, "FileEntry"] = dict(idx.files)

    mids = list(r_nodes)
    if not mids:
        log_event("NODEFACTS:compose_nodes_empty_reachable")
    else:
        cpu_count = os.cpu_count() or 1
        max_workers = min(cpu_count, len(mids))
        if max_workers <= 1:
            log_event("NODEFACTS:compose_nodes_sequential_begin", count=len(mids))
            batch_nodes = _build_nodefacts_batch(
                mids,
                files_by_mid,
                root_path,
                cfg_eff,
                adj_fwd_mod,
                mid_to_node_id,
                imports_node,
                gen_seq_node,
                deg_in_node,
                deg_out_node,
                scc_id_node,
                scc_size_node,
                du_node,
                dd_node,
                scc_id_node_rt=scc_id_node_rt,
                scc_size_node_rt=scc_size_node_rt,
            )
            nodes.update(batch_nodes)
            log_event("NODEFACTS:compose_nodes_sequential_end", count=len(batch_nodes))
        else:
            def _chunk(seq: List[str], n: int) -> List[List[str]]:
                chunks: List[List[str]] = []
                base = len(seq) // n
                extra = len(seq) % n
                start = 0
                for i in range(n):
                    size = base + (1 if i < extra else 0)
                    if size <= 0:
                        continue
                    end = start + size
                    chunks.append(seq[start:end])
                    start = end
                return chunks

            batches = _chunk(mids, max_workers)
            log_event(
                "NODEFACTS:compose_nodes_multiprocessing_begin",
                workers=max_workers,
                batches=len(batches),
            )

            with ProcessPoolExecutor(max_workers=max_workers) as ex:
                futures = {
                    ex.submit(
                        _build_nodefacts_batch,
                        batch,
                        files_by_mid,
                        root_path,
                        cfg_eff,
                        adj_fwd_mod,
                        mid_to_node_id,
                        imports_node,
                        gen_seq_node,
                        deg_in_node,
                        deg_out_node,
                        scc_id_node,
                        scc_size_node,
                        du_node,
                        dd_node,
                        scc_id_node_rt,
                        scc_size_node_rt,
                    ): idx
                    for idx, batch in enumerate(batches)
                }

                for fut in as_completed(futures):
                    batch_idx = futures[fut]
                    batch_nodes = fut.result()
                    nodes.update(batch_nodes)
                    log_event(
                        "NODEFACTS:compose_worker_done",
                        batch_index=batch_idx,
                        batch_size=len(batch_nodes),
                    )

            log_event(
                "NODEFACTS:compose_nodes_multiprocessing_end",
                total_nodes=len(nodes),
            )

    log_event("NODEFACTS:compose_nodes_end", count=len(nodes))

    # v1.8 dual SCC summaries for meta
    summary_conceptual = _scc_summary(scc_size_of_mod)
    summary_runtime = _scc_summary(scc_size_of_mod_rt)
    tc_inflation = (
        summary_conceptual["largest_scc_size"] - summary_runtime["largest_scc_size"]
    )

    meta = {
        "created": iso_utc(),
        "graph_hash": _graph_hash_from_index(idx),
        "gen_seq_source": "seq_id",
        "seed_id": seed_id or "",
        "no_seed_distances": False if seed_id else True,
        "imports_token_source": ("imports_all" if using_all_tokens else "imports_internal"),
        "external_import_tokens": ext_token_total,
        "external_import_samples": ext_token_samples[:20],
        # v1.8 structural metrics
        "scc_conceptual": summary_conceptual,
        "scc_runtime": summary_runtime,
        "tc_inflation": tc_inflation,
    }
    log_event(
        "NODEFACTS:meta_built",
        nodes=len(nodes),
        scc_conceptual_largest=summary_conceptual["largest_scc_size"],
        scc_runtime_largest=summary_runtime["largest_scc_size"],
        tc_inflation=tc_inflation,
    )
    log_event("NODEFACTS:build_return", nodes=len(nodes))
    return NodeFacts(schema=NODEFACTS_SCHEMA, meta=meta, nodes=nodes)


def save_nodefacts(nf: NodeFacts, path: Path) -> None:
    """
    Save NodeFacts to JSON file.

    v1.7:
      Saves optional detailed metadata fields when present.

    v1.8:
      Saves per-node runtime SCC dynamic attributes when present:
        - scc_id_runtime
        - scc_size_runtime

    v1.9:
      sloc, comment_lines, blank_lines, comment_pct are now dataclass fields
      and are captured automatically by asdict().

    Serializer output schema:
      - nodefacts@v1.9
    """
    nodes_out = {}
    tuple_fields = {
        "imports",
        "exports",
        "classes",
        "functions",
        "globals",
        "classes_detailed",
        "functions_detailed",
        "globals_detailed",
        "crosstalk_candidates_py_v1",
    }

    for k, v in nf.nodes.items():
        d = asdict(v)

        for fld in tuple_fields:
            if fld in d and isinstance(d[fld], (tuple, list)):
                d[fld] = list(d[fld])

        # v1.7 metadata fields (dynamic attributes not captured by asdict)
        for detailed_field in ["classes_detailed", "functions_detailed", "globals_detailed"]:
            if detailed_field not in d and hasattr(v, detailed_field):
                val = getattr(v, detailed_field)
                if val is not None and isinstance(val, (tuple, list)):
                    d[detailed_field] = list(val)

            if d.get(detailed_field) is None:
                d.pop(detailed_field, None)

        # v1.8 runtime SCC fields (dynamic attributes not captured by asdict)
        for rt_field in ["scc_id_runtime", "scc_size_runtime"]:
            if hasattr(v, rt_field):
                try:
                    d[rt_field] = getattr(v, rt_field)
                except Exception:
                    pass

        if "id" in d:
            d["node_id"] = d.pop("id")
        nodes_out[k] = d

    payload = {
        "schema": NODEFACTS_SCHEMA,
        "meta": dict(nf.meta),
        "nodes": nodes_out,
    }
    atomic_write_json(payload, Path(path))


def load_nodefacts(path: Path) -> NodeFacts:
    """
    Load NodeFacts from JSON file.

    Accepted schemas:
      - nodefacts@v1.9
      - nodefacts@v1.8
      - nodefacts@v1.7
      - nodefacts@v1.6
      - nodefacts:1.5
      - None

    v1.7:
      Loads optional detailed metadata fields when present.

    v1.8:
      Loads optional runtime SCC fields when present:
        - scc_id_runtime
        - scc_size_runtime

    v1.9:
      Loads per-node code metrics when present; defaults to None for older
      bundles that predate v1.9:
        - sloc
        - comment_lines
        - blank_lines
        - comment_pct

    Older bundles remain valid; missing newer fields default gracefully.
    """
    data = _loads_bytes(Path(path).read_bytes())
    schema = data.get("schema")

    if schema not in ACCEPTED_NODEFACTS_SCHEMAS:
        raise ValueError(f"Unexpected schema: {schema}")

    nodes_raw = data.get("nodes", [])
    nodes: Dict[str, NodeFactsNode] = {}

    if isinstance(nodes_raw, dict):
        iterable = nodes_raw.items()
    elif isinstance(nodes_raw, list):
        iterable = ((rec.get("node_id"), rec) for rec in nodes_raw)
    else:
        raise TypeError(f"Invalid nodes payload type: {type(nodes_raw)}")

    def _load_detailed(rec: Dict[str, Any], field_name: str) -> Optional[Tuple[Dict[str, Any], ...]]:
        """Load v1.7 detailed metadata field; return None if missing or empty."""
        raw = rec.get(field_name)
        if raw and isinstance(raw, list) and len(raw) > 0:
            return tuple(raw)
        return None

    def _optional_int(rec: Dict[str, Any], key: str) -> Optional[int]:
        val = rec.get(key)
        if val is None:
            return None
        try:
            return int(val)
        except Exception:
            return None

    def _optional_float(rec: Dict[str, Any], key: str) -> Optional[float]:
        val = rec.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except Exception:
            return None

    for nid, rec in iterable:
        if not nid:
            continue

        # Stable/core fields; scc_id / scc_size remain conceptual SCC fields.
        node = NodeFactsNode(
            id=str(rec.get("node_id", nid)),
            name=str(rec.get("name", "")),
            imports=tuple(rec.get("imports", []) or ()),
            exports=tuple(rec.get("exports", []) or ()),
            classes=tuple(rec.get("classes", []) or ()),
            functions=tuple(rec.get("functions", []) or ()),
            globals=tuple(rec.get("globals", []) or ()),
            gen_seq=int(rec.get("gen_seq", 0)),
            importers_count=int(rec.get("importers_count", 0)),
            dependencies_count=int(rec.get("dependencies_count", 0)),
            scc_id=str(rec.get("scc_id", nid)),
            scc_size=int(rec.get("scc_size", 1)),
            file=str(rec.get("file", "")),
            hash=rec.get("hash"),
            loc=_optional_int(rec, "loc"),
            # v1.9: per-node code metrics; None for pre-v1.9 bundles
            sloc=_optional_int(rec, "sloc"),
            comment_lines=_optional_int(rec, "comment_lines"),
            blank_lines=_optional_int(rec, "blank_lines"),
            comment_pct=_optional_float(rec, "comment_pct"),
            size_bytes=rec.get("size_bytes"),
            mtime=rec.get("mtime"),
            du=rec.get("du"),
            dd=rec.get("dd"),
            parse_status=str(rec.get("parse_status", "ok")),
            crosstalk_candidates_py_v1=tuple(rec.get("crosstalk_candidates_py_v1", []) or ()),
        )

        # v1.7 metadata fields (dynamic attributes)
        classes_detailed = _load_detailed(rec, "classes_detailed")
        if classes_detailed:
            try:
                setattr(node, "classes_detailed", classes_detailed)
            except Exception:
                pass

        functions_detailed = _load_detailed(rec, "functions_detailed")
        if functions_detailed:
            try:
                setattr(node, "functions_detailed", functions_detailed)
            except Exception:
                pass

        globals_detailed = _load_detailed(rec, "globals_detailed")
        if globals_detailed:
            try:
                setattr(node, "globals_detailed", globals_detailed)
            except Exception:
                pass

        # v1.8 runtime SCC fields (dynamic attributes)
        if "scc_id_runtime" in rec:
            try:
                setattr(node, "scc_id_runtime", str(rec["scc_id_runtime"]))
            except Exception:
                pass
        if "scc_size_runtime" in rec:
            try:
                setattr(node, "scc_size_runtime", int(rec["scc_size_runtime"]))
            except Exception:
                pass

        nodes[nid] = node

    # Preserve incoming schema for compatibility when loading older bundles.
    return NodeFacts(schema=schema, meta=data.get("meta", {}), nodes=nodes)


# ---------------------------------------------------------------------------
# Helpers — reachable set, BFS distances, SCC
# ---------------------------------------------------------------------------

def _bidirectional_reachable(
    seed: str,
    fwd: Mapping[str, Iterable[str]],
    rev: Mapping[str, Iterable[str]],
) -> Set[str]:
    seen: Set[str] = set()
    dq: Deque[str] = deque([seed])
    while dq:
        u = dq.popleft()
        if u in seen:
            continue
        seen.add(u)
        for v in fwd.get(u, ()):
            if v not in seen:
                dq.append(v)
        for v in rev.get(u, ()):
            if v not in seen:
                dq.append(v)
    return seen


def _bfs_distance(
    src: str,
    adj: Mapping[str, Iterable[str]],
) -> Dict[str, Optional[int]]:
    dist: Dict[str, Optional[int]] = {src: 0}
    dq: Deque[str] = deque([src])
    while dq:
        u = dq.popleft()
        for v in adj.get(u, ()):
            if v not in dist:
                dist[v] = (dist[u] or 0) + 1
                dq.append(v)
    return dist


def _tarjan_scc(
    adj: Mapping[str, Iterable[str]],
) -> Tuple[Dict[str, str], Dict[str, int]]:
    """Tarjan's strongly connected components with stable iteration."""
    index = 0
    stack: List[str] = []
    onstack: Set[str] = set()
    idx_map: Dict[str, int] = {}
    low: Dict[str, int] = {}
    comps: List[List[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index
        idx_map[v] = index
        low[v] = index
        index += 1
        stack.append(v)
        onstack.add(v)
        for w in sorted(adj.get(v, ())):
            if w not in idx_map:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in onstack:
                low[v] = min(low[v], idx_map[w])
        if low[v] == idx_map[v]:
            comp: List[str] = []
            while True:
                w = stack.pop()
                onstack.discard(w)
                comp.append(w)
                if w == v:
                    break
            comps.append(sorted(comp))

    for v in sorted(adj.keys()):
        if v not in idx_map:
            strongconnect(v)

    scc_id_of: Dict[str, str] = {}
    scc_size: Dict[str, int] = {}
    for comp in comps:
        sid = comp[0]
        for n in comp:
            scc_id_of[n] = sid
        scc_size[sid] = len(comp)
    return scc_id_of, scc_size


def _graph_hash_from_index(idx: FolderIndex) -> str:
    # lightweight hash of nodes+edges presence only (order-independent)
    # format: "n:<count>;e:<internal_edge_count>"
    n = len(idx.files)
    e = 0

    for fe in idx.files.values():
        internal_edges = set()

        try:
            internal_edges.update(x for x in fe.imports_internal if x in idx.files)
        except Exception:
            pass

        try:
            internal_edges.update(
                x for x in getattr(fe, "symbol_internal", ()) or ()
                if x in idx.files
            )
        except Exception:
            pass

        e += len(internal_edges)

    return f"n:{n};e:{e}"
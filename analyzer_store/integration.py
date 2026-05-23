from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Literal

try:
    import orjson  # fast path
except Exception:  # pragma: no cover
    orjson = None  # type: ignore

import json
import traceback
import sys

# Core analyzers / IO
from analyzer.fs import ensure_dir_root
from analyzer.config import AnalyzerCfg, load_config
from analyzer_store.folder_index import load_folder_index, build_folder_index, save_folder_index
from analyzer_store.nodefacts import build_nodefacts, save_nodefacts
from analyzer_store.edge_pass import build_edges_artifact  # path-based API
from analyzer_store.io_utils import iso_utc, atomic_write_json

# Planning / adapters
from adapters.analyzer_bridge.core import normalize_graph_for_contracts

# Diagnostics
from diagnostics.events import ANALYZER_ARTIFACTS_BUILT
from diagnostics.logging import log_event


def _log(kind: str, *parts: Any, **fields: Any) -> None:
    """
    Route analyzer_store integration logs through the central logging
    controller. Fall back to a plain stderr print if logging is unavailable.
    """
    try:
        log_event(kind, *parts, **fields)
    except Exception:
        try:
            msg = " ".join(str(p) for p in parts if p is not None)
            kv = " ".join(f"{k}={v!r}" for k, v in fields.items())
            line = f"[{kind}]"
            if msg:
                line += " " + msg
            if kv:
                line += " " + kv
            print(line, file=sys.stderr)
        except Exception:
            pass


def _stamp_normalized(g: dict, *, by: str = "analyzer_store", schema: str = "contracts.v1") -> dict:
    """
    Add an explicit, durable stamp that downstream can assert on quickly.
    Keeps existing `_contracts_format` for back-compat.
    """
    try:
        g.setdefault("_stamp", {})
        g["_stamp"].update({"normalized": True, "by": by, "schema": schema})
        g.setdefault("_contracts_format", "normalized")
    except Exception:
        pass
    return g


# ---------------------------------------------------------------------------
# cfg normalization
# ---------------------------------------------------------------------------

def ensure_cfg(root: Path, cfg_like: Any) -> AnalyzerCfg:
    """
    Coerce cfg_like (AnalyzerCfg | dict | None) into a normalized AnalyzerCfg.
    """
    if isinstance(cfg_like, AnalyzerCfg):
        return cfg_like
    if isinstance(cfg_like, dict):
        base = load_config(root) or AnalyzerCfg()
        for k, v in cfg_like.items():
            if hasattr(base, k):
                setattr(base, k, v)
        return base
    return load_config(root) or AnalyzerCfg()


# ---------------------------------------------------------------------------
# Write guards / helpers
# ---------------------------------------------------------------------------

def _is_under(child: Path, parent: Path) -> bool:
    try:
        c, p = child.resolve(), parent.resolve()
        return p == c or p in c.parents
    except Exception:
        return False


def _assert_write_ok(path: Path, *, scan_root: Path, store_root: Path) -> None:
    """
    Allow writes anywhere under store_root (even if store_root is under scan_root).
    Block writes outside store_root sandbox.
    """
    if _is_under(path, store_root):
        return
    raise PermissionError(
        f"[BLOCK] Attempted write outside store_root sandbox: {path} (store_root={store_root})"
    )


def _loads_bytes(data: bytes) -> dict:
    """
    Fast, tolerant JSON loader:
      - use orjson when available
      - fall back to stdlib json.loads on UTF-8 decode
      - always return a dict ({} for errors or wrong root type)
    """
    try:
        obj = orjson.loads(data) if orjson else json.loads(data.decode("utf-8", errors="ignore"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _load_json(path: Optional[Path]) -> dict:
    if not path:
        return {}
    try:
        return _loads_bytes(Path(path).read_bytes())
    except Exception:
        return {}


def _norm_edge_policy(cfg: AnalyzerCfg) -> str:
    p = str(getattr(cfg, "edge_policy", "strict")).lower().strip() or "strict"
    return p if p in ("strict", "soft") else "strict"


# ---------------------------------------------------------------------------
# Reachable writer (classic: full scope; zones: pass scope explicitly)
# Classic writes nodes with minimal {node_id, gen, gen_idx} so  levels > 0.
# ---------------------------------------------------------------------------

def _write_reachable_json(art_dir: Path, *, seed: Optional[str], scope_nodes: Optional[List[str]]) -> Path:
    nodefacts_path = art_dir / "nodefacts.json"
    reachable_path = art_dir / "reachable.json"

    # If scope not provided, fill from nodefacts (classic full-scope)
    if scope_nodes is None:
        try:
            nf = _load_json(nodefacts_path)
            nodes_obj = nf.get("nodes") or {}
            if isinstance(nodes_obj, dict):
                scope_nodes = sorted(nodes_obj.keys())
            elif isinstance(nodes_obj, list):
                scope_nodes = [
                    n.get("node_id") for n in nodes_obj
                    if isinstance(n, dict) and n.get("node_id")
                ]
            else:
                scope_nodes = []
        except Exception:
            scope_nodes = []

    nodes_payload = [{"node_id": nid, "gen": 0, "gen_idx": i} for i, nid in enumerate(scope_nodes or [])]
    payload = {"seed": seed, "nodes": nodes_payload}

    reachable_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(payload, reachable_path)

    _log(
        "ART:reachable_write",
        path=str(reachable_path),
        seed=seed,
        count=len(nodes_payload),
    )

    return reachable_path


# ---------------------------------------------------------------------------
# Central emission: (optional) plan emit + diagnostics summary
# Reads artifacts from disk; does not re-discover or re-plan.
# ---------------------------------------------------------------------------

def _emit_artifacts(
    *,
    scan_root: Path,
    out_dir: Path,                     # <store_root>/.pviz/artifacts
    nodefacts_path: Path,
    edges_path: Optional[Path],
    seed_id: Optional[str],
    bus: Optional[Any],
    reachable_path: Optional[Path] = None,
    emit_plan: bool = True,
    analyzer_cfg: Optional[AnalyzerCfg] = None,
) -> dict:
    # ---------- logging helper ----------
    def _log(*a: Any, **k: Any) -> None:
        # Keep the original call pattern, but route through the controller.
        # Messages go under the BUILD category so they can be filtered.
        try:
            msg = " ".join(str(p) for p in a if p is not None)
            _log("BUILD:emit_artifacts_debug", msg, **k)
        except Exception:
            # Last-resort print (already handled inside _log, but be defensive)
            try:
                print(msg, file=sys.stderr)
            except Exception:
                pass

    import inspect
    import time as _t
    t0 = _t.perf_counter()
    caller = "<n/a>"
    try:
        fr = inspect.stack()[1]
        caller = f"{fr.filename}:{fr.lineno} in {fr.function}"
    except Exception:
        pass

    # ---------- read inputs ----------
    nf_json = _load_json(nodefacts_path)
    e_json = _load_json(edges_path) if edges_path else {}
    reach_j = _load_json(reachable_path) if reachable_path else {}

    n_nodes_in = len((nf_json or {}).get("nodes") or {})
    n_edges_in = len((e_json or {}).get("edges") or [])

    _log(f"[EMIT] start caller={caller}")
    _log(
        f"[EMIT] inputs nodes@nf={n_nodes_in} edges@edges.json={n_edges_in} "
        f"emit_plan={emit_plan} seed={seed_id!r}"
    )

    # ---------- normalize (single pass) ----------
    graph_in = {
        "nodes": nf_json.get("nodes") or {},
        "edges": (e_json.get("edges") or []),
    }
    t_norm = _t.perf_counter()
    _log(f"[NORM] start; edges_in={len(graph_in['edges'])} caller={caller}")

    graph_norm = normalize_graph_for_contracts(graph_in)
    _stamp_normalized(graph_norm)

    stamp_underscore = graph_norm.get("_contracts_format")
    _log(
        f"[NORM] done; edges_out={len(graph_norm.get('edges') or [])} "
        f"stamped_undersc={stamp_underscore!r} "
        f"elapsed_ms={((_t.perf_counter()-t_norm)*1000.0):0.1f}"
    )
 
    if stamp_underscore is None and not (graph_norm.get("_stamp") or {}).get("normalized"):
        _log("[NORM][WARN] normalized graph appears un-stamped; added local stamp.")

    # ---------- carry analyzer cfg (non-persistent) ----------
    if analyzer_cfg is not None:
        try:
            graph_norm["analyzer_cfg"] = analyzer_cfg
        except Exception:
            pass

    # ---------- emit classic plan/layout (optional) ----------
    placement_dir = None

    # ---------- derive summary ----------
    nodes_list = reach_j.get("nodes") or []
    if isinstance(nodes_list, dict):  # tolerate old shapes
        nodes_list = list(nodes_list.values())
    counts_reachable = len(nodes_list)

    levels = 0
    if nodes_list:
        try:
            gens = []
            for n in nodes_list:
                if isinstance(n, dict):
                    if "gen" in n:
                        gens.append(int(n.get("gen", 0)))
                    elif "lane" in n:
                        gens.append(int(n.get("lane", 0)))
            levels = (max(gens) + 1) if gens else 0
        except Exception:
            levels = 0

    final_stats = e_json.get("final_stats") or {}
    edges_total = int(final_stats.get("total_edges") or 0)
    edge_policy = final_stats.get("policy")

    # FolderIndex meta (eligible, parsed, internal_edge_count)
    try:
        idx_path = out_dir / "folder_index.json"
        idx_json = _load_json(idx_path)
        idx_meta = idx_json.get("meta", {}) if isinstance(idx_json, dict) else {}
        eligible_count = int(idx_meta.get("eligible_count", 0))
        parsed_count = int(idx_meta.get("parsed_count", 0))
        internal_edge_count = int(idx_meta.get("internal_edge_count", 0))
        top_uncovered = list(idx_meta.get("top_uncovered", []) or [])
        parse_issues = list(idx_meta.get("parse_issues", []) or [])
        external_roots = list(idx_meta.get("external_roots", []) or [])
    except Exception:
        eligible_count = parsed_count = internal_edge_count = 0
        top_uncovered = parse_issues = external_roots = []

    by_count_denom = max(1, eligible_count or counts_reachable)
    by_edges_denom = max(1, internal_edge_count or 1)

    # build a compact loc map straight from nodefacts
    loc_map = {}
    try:
        nf_nodes = nf_json.get("nodes") or {}
        if isinstance(nf_nodes, dict):
            for nid, rec in nf_nodes.items():
                v = (rec or {}).get("loc")
                if isinstance(v, int):
                    loc_map[nid] = v
                elif isinstance(v, str) and v.isdigit():
                    loc_map[nid] = int(v)
    except Exception:
        pass

    summary = {
        "kind": ANALYZER_ARTIFACTS_BUILT,
        "meta": {"root": str(scan_root), "seed": seed_id, "created": iso_utc()},
        "paths": {
            "folder_index": str(out_dir / "folder_index.json"),
            "nodefacts": str(nodefacts_path) if nodefacts_path else None,
            "coverage": None,
            "reachable": str(reachable_path) if reachable_path else None,
            "plan": str(placement_dir / "plan.json") if emit_plan and placement_dir else None,
            "layout": str(placement_dir / "layout.json") if emit_plan and placement_dir else None,
            "edges": str(edges_path) if edges_path else None,
            "diagnostics": None,
            # note: nodes.json path is implied; reader prefers it when present
        },
        "counts": {
            "eligible": eligible_count,
            "parsed": parsed_count,
            "reachable": counts_reachable,
        },
        "edges": {
            "internal_total": internal_edge_count,
            "reachable_total": edges_total,
            "policy": edge_policy,
        },
        "scc": {"count": 0, "largest": 0},
        "coverage": {
            "by_count": {
                "numer": counts_reachable,
                "denom": by_count_denom,
                "ratio": (counts_reachable / by_count_denom) if by_count_denom else 0.0,
            },
            "by_edges": {
                "numer": edges_total,
                "denom": by_edges_denom,
                "ratio": (edges_total / by_edges_denom) if by_edges_denom else 0.0,
            },
        },
        "metrics": {"loc": loc_map},
        "top_uncovered": top_uncovered,
        "parse_issues": parse_issues,
        "external_roots": external_roots,
        "layout": {
            "seed": seed_id,
            # Mode reflects whether we’re seeded (zones) or not (classic)
            "mode": "classic" if (seed_id is None) else "zones",
            "levels": levels,
            "nodes": counts_reachable,
            "plan_path": str(placement_dir / "plan.json") if emit_plan and placement_dir else None,
            "layout_path": str(placement_dir / "layout.json") if emit_plan and placement_dir else None,
            "edges_path": str(edges_path) if edges_path else None,
            "diagnostics_path": None,
        },
        # hand the stamped graph back to callers
        "contracts": graph_norm,
        "_contracts_format": graph_norm.get("_contracts_format") or "normalized",
    }

    # ---------- emit + tail diagnostics ----------
    try:
        if bus:
            bus.emit(summary)
            _log(
                "[BUS] emitted ANALYZER_ARTIFACTS_BUILT; "
                f"contracts_edges={len((graph_norm or {}).get('edges') or [])}"
            )
    except Exception as ex:
        _log(f"[BUS][ERR] {type(ex).__name__}: {ex}")

    _log(
        "[EMIT] done; "
        f"elapsed_ms={((_t.perf_counter()-t0)*1000.0):0.1f} "
        f"contracts_edges={len((graph_norm or {}).get('edges') or [])}"
    )

    return summary


# ---------------------------------------------------------------------------
# Contracts adapter
# ---------------------------------------------------------------------------

def _contracts_from_artifacts(artifacts_dir: Path, seed_id: Optional[str], summary: Mapping[str, Any]) -> dict:
    artifacts = Path(artifacts_dir).resolve()

    # 1) Nodes: prefer minimal nodes.json, then fuller artifacts, then seed-scoped reachable, then summary.reachable
    nodes_path: Optional[Path] = None
    for cand in ("nodes.json", "graph_full.json", "graph.json", "nodefacts.json"):
        p = artifacts / cand
        if p.exists():
            nodes_path = p
            break
    if not nodes_path and seed_id:
        p = artifacts / f"reachable_{seed_id}.json"
        if p.exists():
            nodes_path = p
    if not nodes_path:
        reach_path = (summary.get("paths", {}) or {}).get("reachable")
        if reach_path and Path(reach_path).exists():
            nodes_path = Path(reach_path)

    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []

    if nodes_path:
        if nodes_path.name == "nodes.json":
            # Minimal fast-path: file is a raw mapping { node_id: node_record }
            raw_map = _load_json(nodes_path)
            if isinstance(raw_map, dict):
                nodes = {str(k): (dict(v) if isinstance(v, dict) else {}) for k, v in raw_map.items()}
        else:
            data = _load_json(nodes_path)
            raw = data.get("nodes") or {}
            if isinstance(raw, dict):
                nodes = {str(k): (dict(v) if isinstance(v, dict) else {}) for k, v in raw.items()}
            elif isinstance(raw, list):
                tmp = {}
                for rec in raw:
                    if not isinstance(rec, dict):
                        continue
                    nid = rec.get("node_id") or rec.get("id")
                    if isinstance(nid, str) and nid:
                        tmp[str(nid)] = dict(rec)
                nodes = tmp

    # 2) Edges: prefer precomputed edges_{seed}.json (@ fallback), else edges from nodes file
    pre_edges: List[Dict[str, Any]] = []
    edges_path = (summary.get("paths", {}) or {}).get("edges")

    if not edges_path and seed_id:
        ep = artifacts / f"edges_{seed_id}.json"
        if not ep.exists():
            ep = artifacts / f"edges@{seed_id}.json"  # backward-compat fallback
        if ep.exists():
            edges_path = str(ep.resolve())

    if edges_path and Path(edges_path).exists():
        ed = _load_json(Path(edges_path))
        pre_edges = ed.get("edges") if isinstance(ed, dict) else (ed if isinstance(ed, list) else [])

    if pre_edges:
        edges = [e for e in pre_edges if isinstance(e, dict)]
    else:
        data = _load_json(nodes_path) if nodes_path and nodes_path.name != "nodes.json" else {}
        r_edges = data.get("edges") if isinstance(data, dict) else []
        edges = [e for e in (r_edges or []) if isinstance(e, dict)]

    # ---- LOC metrics for heatmap ----
    loc_map: Dict[str, int] = {}
    for nid, rec in nodes.items():
        try:
            v = rec.get("loc", None)
            if isinstance(v, int):
                loc_map[nid] = v
            elif isinstance(v, str) and v.isdigit():
                loc_map[nid] = int(v)
        except Exception:
            pass

    out = {
        "nodes": nodes,
        "edges": edges,
        "meta": {"artifacts_dir": str(artifacts)},
        "_contracts_format": "normalized",
        "metrics": {"loc": loc_map},
    }

    # Add a standard stamp for downstream "is it normalized?" checks.
    out["_stamp"] = {"normalized": True, "by": "analyzer_store/artifacts", "schema": "contracts.v1"}
    return out


# ---------------------------------------------------------------------------
# Public APIs
# ---------------------------------------------------------------------------

def build_artifacts_and_emit(
    *,
    scan_root: str,
    store_root: str,
    files: Optional[List[str]] = None,       # kept for signature compatibility (unused by path API)
    cfg: Optional[Dict[str, Any]] = None,
    bus: Optional[Any] = None,
    seed_id: Optional[str] = None,
    direction: Literal["up", "down", "both", "classic"] = "both",  # kept for compat; discovery is full-scope
    emit_plan: bool = False,
    artifacts_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    # Entry log
    _log(
        "BUILD:artifacts_enter",
        scan_root=scan_root,
        store_root=store_root,
        seed=seed_id,
        direction=direction,
        emit_plan=emit_plan,
    )

    _log(
        "BUILD:artifacts_begin",
        scan_root=scan_root,
        store_root=store_root,
        seed=seed_id,
        direction=direction,
        emit_plan=emit_plan,
    )

    # Normalize roots and sandbox
    sr = ensure_dir_root(Path(scan_root)).resolve()
    st = ensure_dir_root(Path(store_root)).resolve()
    if artifacts_dir is not None:
        art_dir = ensure_dir_root(Path(artifacts_dir)).resolve()
    else:
        art_dir = (st / ".pviz" / "artifacts").resolve()
    art_dir.mkdir(parents=True, exist_ok=True)
    _assert_write_ok(art_dir, scan_root=sr, store_root=st)
    _log(
        "BUILD:roots_normalized",
        scan_root=str(sr),
        store_root=str(st),
        art_dir=str(art_dir),
    )

    # Effective config (single source of truth)
    cfg_eff = ensure_cfg(sr, cfg)
    _log("BUILD:ensure_cfg_done")

    # 1) FolderIndex
    idx_path = art_dir / "folder_index.json"
    exists_idx = idx_path.exists()
    _log(
        "BUILD:folder_index_check",
        path=str(idx_path),
        exists=exists_idx,
    )
    if exists_idx:
        _log("BUILD:folder_index_load_begin", path=str(idx_path))
        idx = load_folder_index(idx_path)
        _log("BUILD:folder_index_load_end", path=str(idx_path))
    else:
        _log("BUILD:folder_index_build_begin", path=str(idx_path))
        idx = build_folder_index(sr, cfg_eff)
        _assert_write_ok(idx_path, scan_root=sr, store_root=st)
        save_folder_index(idx, idx_path)
        _log("BUILD:folder_index_build_and_save_end", path=str(idx_path))

    # 2) NodeFacts (seedless discovery, full universe)
    _log("BUILD:nodefacts_build_begin")
    nf = build_nodefacts(None, idx)
    nf_path = art_dir / "nodefacts.json"
    _assert_write_ok(nf_path, scan_root=sr, store_root=st)
    save_nodefacts(nf, nf_path)
    _log("BUILD:nodefacts_build_and_save_end", path=str(nf_path))

    # 2.1) Minimal nodes.json for fast hydration (plain mapping)
    try:
        _log("BUILD:nodes_json_begin")
        nodes_p = art_dir / "nodes.json"
        _assert_write_ok(nodes_p, scan_root=sr, store_root=st)

        # nf may be a NodeFacts dataclass or a legacy dict:
        nodes_map: Dict[str, Any] = {}

        # Case 1: new-style NodeFacts object with .nodes
        maybe_nodes = getattr(nf, "nodes", None)
        if isinstance(maybe_nodes, dict):
            nodes_map = maybe_nodes
        # Case 2: legacy dict shape { "nodes": { ... } }
        elif isinstance(nf, dict):
            nodes_map = nf.get("nodes") or {}
            if not isinstance(nodes_map, dict):
                nodes_map = {}

        def _node_to_plain(v: Any) -> Any:
            """
            Convert NodeFactsNode / dataclass / pydantic model into JSON-safe primitives.
            Keep this very tolerant: if we can't convert, fall back to string.
            """
            if v is None:
                return None

            # already json-like
            if isinstance(v, (str, int, float, bool)):
                return v
            if isinstance(v, (list, tuple)):
                return [_node_to_plain(x) for x in v]
            if isinstance(v, dict):
                return {str(k): _node_to_plain(x) for k, x in v.items()}

            # dataclass
            try:
                from dataclasses import is_dataclass, asdict
                if is_dataclass(v):
                    return _node_to_plain(asdict(v))
            except Exception:
                pass

            # pydantic v2
            try:
                md = getattr(v, "model_dump", None)
                if callable(md):
                    return _node_to_plain(md())
            except Exception:
                pass

            # pydantic v1
            try:
                d = getattr(v, "dict", None)
                if callable(d):
                    return _node_to_plain(d())
            except Exception:
                pass

            # common "to_dict"
            try:
                td = getattr(v, "to_dict", None)
                if callable(td):
                    return _node_to_plain(td())
            except Exception:
                pass

            # last resort: vars()
            try:
                return _node_to_plain(vars(v))
            except Exception:
                return str(v)

        if isinstance(nodes_map, dict):
            # Convert values to JSON-safe dicts
            nodes_payload = {str(k): _node_to_plain(v) for k, v in nodes_map.items()}

            atomic_write_json(nodes_payload, nodes_p)
            _log("BUILD:nodes_json_wrote", path=str(nodes_p), count=len(nodes_payload))
        else:
            _log("BUILD:nodes_json_skip_non_dict")

    except Exception as ex:
        # Non-fatal; classic flow still has nodefacts.json
        _log("BUILD:nodes_json_exception", err=repr(ex))


    # 3) Edges (classic: full-scope)
    _log("BUILD:edges_reachable_begin")
    reach_path = _write_reachable_json(art_dir, seed=None, scope_nodes=None)
    edges_path = art_dir / "edges.json"
    _assert_write_ok(reach_path, scan_root=sr, store_root=st)
    _assert_write_ok(edges_path, scan_root=sr, store_root=st)
    _log("BUILD:edges_build_begin", reachable=str(reach_path), edges=str(edges_path))
    build_edges_artifact(
        nodefacts_path=nf_path,
        reachable_path=reach_path,
        out_path=edges_path,
        policy=_norm_edge_policy(cfg_eff),
    )
    _log("BUILD:edges_build_end", edges=str(edges_path))

    # 4) Plan + summary
    _log("BUILD:emit_artifacts_begin")
    summary = _emit_artifacts(
        scan_root=sr,
        out_dir=art_dir,
        nodefacts_path=nf_path,
        edges_path=edges_path,
        seed_id=seed_id,
        bus=bus,
        reachable_path=reach_path,
        emit_plan=emit_plan,
        analyzer_cfg=cfg_eff,
    )
    _log("BUILD:emit_artifacts_end")

    # 5) Contracts for immediate consumption
    _log("BUILD:contracts_begin")
    contracts = _contracts_from_artifacts(art_dir, seed_id, summary)
    _log("BUILD:contracts_end")

    _log(
        "BUILD:artifacts_done",
        seed=seed_id,
        reachable=summary.get("counts", {}).get("reachable"),
        edges=summary.get("edges", {}).get("reachable_total"),
    )
    _log("BUILD:artifacts_return")
    return {**summary, "contracts": contracts}


def build_precompute_minimal(
    *,
    scan_root: str,
    store_root: str,
    cfg: Optional[Dict[str, Any]] = None,
    bus: Optional[Any] = None,
) -> Dict[str, Any]:
    _log(
        "BUILD:precompute_minimal",
        scan_root=scan_root,
        store_root=store_root,
    )
    return build_artifacts_and_emit(
        scan_root=scan_root,
        store_root=store_root,
        files=None,
        cfg=cfg,
        bus=bus,
        seed_id=None,
        direction="classic",
        emit_plan=False,
    )

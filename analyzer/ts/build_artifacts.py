# analyzer/ts/build_artifacts.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .model import ResolvedImport

# SSOT canonicalizer for JS/TS
from .canonical_web import (
    canon_web_file_id,
    is_repo_relative,
    to_posix,
    strip_query_and_hash,
)

import time

# ---------------------------------------------------------------------------
# Logging (prefers PViz diagnostics logging; falls back to print)
# ---------------------------------------------------------------------------
try:
    from diagnostics.logging import log_event as _log_event  # type: ignore
except Exception:  # pragma: no cover
    _log_event = None  # type: ignore


def log_event(name: str, **k) -> None:
    if _log_event is not None:
        try:
            _log_event(name, **k)
            return
        except Exception:
            pass
    parts = " ".join(f"{kk}={k[kk]!r}" for kk in sorted(k.keys()))
    print(f"[TS] {name} {parts}".rstrip())


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FileFacts:
    rel_posix: str

    # --- Core metrics ---
    loc: int = 0
    sloc: int = 0
    comment_lines: int = 0
    blank_lines: int = 0
    comment_pct: Optional[float] = None

    parse_status: str = "ok"

    # Raw import/reexport/require/dynamic import specs as extracted from syntax.
    # IMPORTANT:
    #   This is NOT the graph-facing internal dependency list.
    #   build_nodefacts() now exports these as imports_all_raw.
    imports: list[str] = field(default_factory=list)

    exports: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    globals: list[str] = field(default_factory=list)

    facts: dict = field(default_factory=dict)

    crosstalk_candidates_ts_v1: list[dict] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _strip_leading_current_dir(p: str) -> str:
    """
    Remove leading "./" segments and a single leading "/" if present,
    but DO NOT destroy "../" parent traversal.
    """
    p = to_posix(p).strip()
    while p.startswith("./"):
        p = p[2:]
    if p.startswith("/"):
        p = p[1:]
    return p


def _canon_id(rel_posix: str, repo_root: Optional[Path]) -> str:
    """
    Canonicalize a file id into the SSOT JS/TS node id.

    Guarantees:
      - POSIX
      - normalized (no ./ or ../)
      - repo-relative when possible
    """
    if repo_root:
        return canon_web_file_id(rel_posix, repo_root)
    return _strip_leading_current_dir(to_posix(rel_posix).rstrip("/"))


def _lang_for_ext(ext: str) -> str:
    ext = (ext or "").lower()
    return {
        ".ts": "ts",
        ".tsx": "tsx",
        ".js": "js",
        ".jsx": "jsx",
        ".mjs": "mjs",
        ".cjs": "cjs",
    }.get(ext, "js")


def _lang_for_stub_ext(ext: str) -> str:
    ext = (ext or "").lower()
    if ext == ".json":
        return "json"
    if ext in (".css", ".scss", ".sass", ".less"):
        return "css"
    if ext in (".yml", ".yaml"):
        return "yaml"
    if ext == ".toml":
        return "toml"
    if ext == ".md":
        return "md"
    if ext == ".svg":
        return "svg"
    return "asset"


def _inner_facts(ff: FileFacts) -> Dict[str, Any]:
    """
    You currently store facts as {"facts": {...}} for compatibility.
    Return the inner dict (or {}).
    """
    if not ff.facts:
        return {}
    inner = ff.facts.get("facts")
    return inner if isinstance(inner, dict) else {}


def _stable_unique_strs(vals: Any) -> List[str]:
    """
    Stable sorted unique strings.

    Used for:
      - raw import specs
      - internal resolved targets
      - unresolved specs

    Sorting keeps output deterministic across process/batch ordering.
    """
    out: set[str] = set()
    try:
        iterator = vals or []
    except Exception:
        iterator = []

    for v in iterator:
        s = str(v or "").strip()
        if s:
            out.add(s)

    return sorted(out)


def _resolved_lists_for_file(
    *,
    ff: FileFacts,
    file_id: str,
    resolved_by_src: Optional[Mapping[str, List[ResolvedImport]]],
    repo_root: Optional[Path],
) -> Dict[str, Any]:
    """
    Build resolved/raw import surfaces for one TS/JS file.

    Returns:
      {
        "imports_all_raw": List[str],
        "imports": List[str],              # resolved internal dst node IDs only
        "imports_unresolved": List[str],
        "imports_unresolved_details": List[dict],
        "imports_resolved_details": List[dict],
        "imports_details": List[dict],
        "style_counts": Dict[str, int],
      }

    Field contract:
      - imports:
          graph-facing, internal canonical targets only.
      - imports_all_raw:
          raw import/reexport/require/dynamic import specs extracted from syntax.
      - imports_unresolved:
          raw specs that did not resolve to an in-repo file target.
    """
    raw_imports = _stable_unique_strs(ff.imports or [])

    imports_internal_targets: List[str] = []
    imports_unresolved: List[str] = []

    imports_unresolved_details: List[Dict[str, Any]] = []
    imports_resolved_details: List[Dict[str, Any]] = []
    imports_details: List[Dict[str, Any]] = []

    style_counts = {
        "relative": 0,
        "slash_root": 0,
        "bare": 0,
        "reexport": 0,
        "require": 0,
        "dynamic": 0,
    }

    for spec in raw_imports:
        s = strip_query_and_hash(spec)
        if s.startswith("./") or s.startswith("../"):
            style_counts["relative"] += 1
        elif s.startswith("/"):
            style_counts["slash_root"] += 1
        else:
            style_counts["bare"] += 1

    ris: List[ResolvedImport] = []
    if resolved_by_src:
        ris = (
            resolved_by_src.get(ff.rel_posix)
            or resolved_by_src.get(to_posix(ff.rel_posix))
            or resolved_by_src.get(file_id)
            or []
        )

    for ri in ris:
        if ri.kind == "reexport":
            style_counts["reexport"] += 1
        elif ri.kind == "require":
            style_counts["require"] += 1
        elif ri.kind == "dynamic_import":
            style_counts["dynamic"] += 1

        spec = str(getattr(ri, "spec", "") or "").strip()
        spec_clean = strip_query_and_hash(spec)

        d: Dict[str, Any] = {
            "spec": spec,
            "spec_clean": spec_clean,
            "kind": ri.kind,
        }

        loc = getattr(ri, "loc", None)
        if loc:
            try:
                d["loc"] = {"line": int(loc[0]), "col": int(loc[1])}
            except Exception:
                pass

        resolved_ok = bool(ri.dst_node_id and not ri.unresolved)
        d["resolved"] = resolved_ok

        if resolved_ok:
            dst_id = _canon_id(ri.dst_node_id, repo_root)
            if is_repo_relative(dst_id):
                imports_internal_targets.append(dst_id)
                d["dst"] = dst_id
                imports_resolved_details.append(d)
            else:
                if spec:
                    imports_unresolved.append(spec)
                imports_unresolved_details.append(d)
        else:
            if spec:
                imports_unresolved.append(spec)
            imports_unresolved_details.append(d)

        imports_details.append(d)

    return {
        "imports_all_raw": raw_imports,
        "imports": _stable_unique_strs(imports_internal_targets),
        "imports_unresolved": _stable_unique_strs(imports_unresolved),
        "imports_unresolved_details": imports_unresolved_details,
        "imports_resolved_details": imports_resolved_details,
        "imports_details": imports_details,
        "style_counts": style_counts,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Nodefacts
# ─────────────────────────────────────────────────────────────────────────────

def _stub_node_for_id(node_id: str) -> Dict[str, Any]:
    p = Path(node_id)
    ext = p.suffix.lower()
    return {
        "file": node_id,
        "name": p.name,
        "loc": 0,
        "sloc": 0,
        "comment_lines": 0,
        "blank_lines": 0,
        "comment_pct": None,
        "parse_status": "stub",
        "file_ext": ext,
        "lang": _lang_for_stub_ext(ext),
        "imports": [],
        "imports_all_raw": [],
        "imports_unresolved": [],
        "imports_count": 0,
        "imports_all_raw_count": 0,
        "imports_unresolved_count": 0,
        "exports_count": 0,
        "functions_count": 0,
        "classes_count": 0,
        "globals_count": 0,
        "is_module": False,
        "is_minified": ".min." in p.name,
        "is_stub": True,
        "stub_reason": "referenced_by_edge_not_discovered",
    }


def build_nodefacts(
    *,
    files: List[FileFacts],
    repo_root: Optional[Path] = None,
    extra_node_ids: Optional[List[str]] = None,
    resolved_by_src: Optional[Mapping[str, List[ResolvedImport]]] = None,
) -> Dict[str, Any]:
    """
    Nodes are keyed by canonical web file id (repo-relative POSIX).
    Additive-only: safe for existing consumers.

    Adds stub nodes for extra_node_ids referenced by edges but not in `files`.

    Import field contract:
      - node["imports"]:
          resolved internal dependency targets only.
          This is the graph-facing field used by downstream SCC/edge consumers.
      - node["imports_all_raw"]:
          raw TS/JS import specs extracted from actual import syntax.
      - node["imports_unresolved"]:
          raw specs that did not resolve internally.

    Backward compatibility:
      - resolved_by_src is optional. If omitted, node["imports"] will be empty and
        node["imports_all_raw"] will still preserve ff.imports.
      - Callers should pass resolved_by_src to fully populate graph-facing imports
        in nodefacts.
    """
    t0 = time.perf_counter()

    nodes: Dict[str, Any] = {}
    parsed_nodes = 0
    stub_nodes = 0
    non_repo_ids = 0

    total_imports_internal = 0
    total_imports_raw = 0
    total_imports_unresolved = 0

    for ff in files:
        node_id = _canon_id(ff.rel_posix, repo_root)
        p = Path(node_id)
        ext = p.suffix.lower()

        import_surfaces = _resolved_lists_for_file(
            ff=ff,
            file_id=node_id,
            resolved_by_src=resolved_by_src,
            repo_root=repo_root,
        )

        imports_internal = import_surfaces["imports"]
        imports_all_raw = import_surfaces["imports_all_raw"]
        imports_unresolved = import_surfaces["imports_unresolved"]

        nodes[node_id] = {
            "file": node_id,
            "name": p.name,

            "loc": ff.loc,
            "sloc": ff.sloc,
            "comment_lines": ff.comment_lines,
            "blank_lines": ff.blank_lines,
            "comment_pct": ff.comment_pct,

            "parse_status": ff.parse_status,

            # Always include the import fields so compressed/uncompressed bundles
            # expose a stable contract.
            "imports": imports_internal,
            "imports_all_raw": imports_all_raw,
            "imports_unresolved": imports_unresolved,
        }

        if ff.exports:
            nodes[node_id]["exports"] = ff.exports
        if ff.functions:
            nodes[node_id]["functions"] = ff.functions
        if ff.classes:
            nodes[node_id]["classes"] = ff.classes
        if ff.globals:
            nodes[node_id]["globals"] = ff.globals
        if ff.crosstalk_candidates_ts_v1:
            nodes[node_id]["crosstalk_candidates_ts_v1"] = ff.crosstalk_candidates_ts_v1

        imports_count = len(imports_internal)
        imports_all_raw_count = len(imports_all_raw)
        imports_unresolved_count = len(imports_unresolved)
        exports_count = len(ff.exports or [])
        is_module = bool(imports_all_raw_count or exports_count)

        facts_inner = _inner_facts(ff)
        size_bytes = facts_inner.get("size_bytes")

        nodes[node_id].update(
            {
                "file_ext": ext,
                "lang": _lang_for_ext(ext),
                "imports_count": imports_count,
                "imports_all_raw_count": imports_all_raw_count,
                "imports_unresolved_count": imports_unresolved_count,
                "exports_count": exports_count,
                "functions_count": len(ff.functions or []),
                "classes_count": len(ff.classes or []),
                "globals_count": len(ff.globals or []),
                "is_module": is_module,
                "is_minified": ".min." in p.name,
            }
        )

        if isinstance(size_bytes, int):
            nodes[node_id]["size_bytes"] = size_bytes
            nodes[node_id]["is_minified"] = (
                ".min." in p.name or (ff.loc <= 2 and size_bytes > 50_000)
            )

        if ff.facts:
            # Preserve existing additive facts, but do not allow nested facts.imports
            # to reintroduce raw/noisy imports under the graph-facing import name.
            facts_payload = dict(ff.facts)

            inner = facts_payload.get("facts")
            if isinstance(inner, dict):
                inner2 = dict(inner)
                inner2["imports"] = imports_internal
                inner2["imports_all_raw"] = imports_all_raw
                inner2["imports_unresolved"] = imports_unresolved
                facts_payload["facts"] = inner2

            nodes[node_id].update(facts_payload)
        else:
            nodes[node_id]["facts"] = {
                "imports": imports_internal,
                "imports_all_raw": imports_all_raw,
                "imports_unresolved": imports_unresolved,
            }

        total_imports_internal += imports_count
        total_imports_raw += imports_all_raw_count
        total_imports_unresolved += imports_unresolved_count
        parsed_nodes += 1

    # Stub nodes
    if extra_node_ids:
        for raw in extra_node_ids:
            nid = _canon_id(raw, repo_root)
            if not nid:
                continue
            if not is_repo_relative(nid):
                non_repo_ids += 1
                continue
            if nid not in nodes:
                nodes[nid] = _stub_node_for_id(nid)
                stub_nodes += 1

    out = {"schema_version": "nodefacts@v1.6", "nodes": nodes}

    log_event(
        "TS:build_nodefacts_done",
        ms=int((time.perf_counter() - t0) * 1000),
        files_in=len(files),
        parsed_nodes=parsed_nodes,
        stub_nodes=stub_nodes,
        extra_ids=(len(extra_node_ids) if extra_node_ids else 0),
        non_repo_extra_ids=non_repo_ids,
        nodes_total=len(nodes),
        imports_internal=total_imports_internal,
        imports_all_raw=total_imports_raw,
        imports_unresolved=total_imports_unresolved,
        resolved_by_src_provided=bool(resolved_by_src),
    )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Edges
# ─────────────────────────────────────────────────────────────────────────────

def build_edges(
    *,
    resolved: List[ResolvedImport],
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Emit only canonical, internal edges.
    Edge endpoints always match nodefacts keys.
    """
    t0 = time.perf_counter()

    edges: List[Dict[str, Any]] = []
    dropped_unresolved = 0
    dropped_external = 0

    for ri in resolved:
        if ri.unresolved or not ri.dst_node_id:
            dropped_unresolved += 1
            continue

        src = _canon_id(ri.src_node_id, repo_root)
        dst = _canon_id(ri.dst_node_id, repo_root)

        if not is_repo_relative(src) or not is_repo_relative(dst):
            dropped_external += 1
            continue

        evidence: Dict[str, Any] = {
            "kind": "ts_import",
            "token": ri.kind,
            "confidence": 0.8,
            "reason": f"{ri.kind} '{ri.spec}'",
            "spec_clean": strip_query_and_hash(ri.spec),
        }

        loc = getattr(ri, "loc", None)
        if loc:
            try:
                evidence["loc"] = {"line": int(loc[0]), "col": int(loc[1])}
            except Exception:
                pass

        e: Dict[str, Any] = {
            "src": src,
            "dst": dst,
            "kind": ri.kind,
            "evidence": evidence,
        }

        if ri.symbols:
            e["reasons"] = [{"symbols": ri.symbols, "unresolved": False}]

        edges.append(e)

    out = {"schema_version": "edges@v1", "edges": edges}

    log_event(
        "TS:build_edges_done",
        ms=int((time.perf_counter() - t0) * 1000),
        resolved_in=len(resolved),
        edges_out=len(edges),
        dropped_unresolved=dropped_unresolved,
        dropped_external=dropped_external,
    )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Folder index
# ─────────────────────────────────────────────────────────────────────────────

def build_folder_index(
    *,
    files: List[FileFacts],
    resolved_by_src: Mapping[str, List[ResolvedImport]],
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Compact per-file import index keyed by canonical file id.

    Import field contract:
      - imports_all:
          raw import/reexport/require/dynamic import specs.
      - imports_internal:
          raw specs that resolved internally.
      - resolved_targets:
          canonical internal target node ids.
      - imports_unresolved:
          raw specs that did not resolve internally.

    This keeps backward compatibility with existing folder_index consumers while
    making the nodefacts layer clearer:
      - Nodefacts imports = resolved_targets
      - Nodefacts imports_all_raw = imports_all
    """
    t0 = time.perf_counter()

    out_files: Dict[str, Any] = {}
    sources = 0
    total_imports_all = 0
    total_unresolved = 0
    total_internal = 0
    total_resolved_targets = 0

    for ff in files:
        file_id = _canon_id(ff.rel_posix, repo_root)
        p = Path(file_id)

        import_surfaces = _resolved_lists_for_file(
            ff=ff,
            file_id=file_id,
            resolved_by_src=resolved_by_src,
            repo_root=repo_root,
        )

        imports_all = import_surfaces["imports_all_raw"]
        resolved_targets = import_surfaces["imports"]
        imports_unresolved = import_surfaces["imports_unresolved"]
        imports_unresolved_details = import_surfaces["imports_unresolved_details"]
        imports_resolved_details = import_surfaces["imports_resolved_details"]
        imports_details = import_surfaces["imports_details"]
        style_counts = import_surfaces["style_counts"]

        # Backward-compatible field: raw specs that resolved internally.
        imports_internal: List[str] = []
        for d in imports_resolved_details:
            spec = str(d.get("spec", "") or "").strip()
            if spec:
                imports_internal.append(spec)
        imports_internal = _stable_unique_strs(imports_internal)

        out_files[file_id] = {
            "file": file_id,
            "name": p.name,

            "loc": ff.loc,
            "sloc": ff.sloc,
            "comment_lines": ff.comment_lines,
            "blank_lines": ff.blank_lines,
            "comment_pct": ff.comment_pct,

            "parse_status": ff.parse_status,

            # Raw syntax import surface.
            "imports_all": imports_all,
            "imports_all_raw": imports_all,
            "imports_all_count": len(imports_all),

            # Back-compat: specs that resolved internally.
            "imports_internal": imports_internal,
            "imports_internal_count": len(imports_internal),

            # Canonical graph targets.
            "resolved_targets": resolved_targets,
            "resolved_targets_count": len(resolved_targets),

            # Unresolved/external diagnostics.
            "imports_unresolved": imports_unresolved,
            "imports_unresolved_count": len(imports_unresolved),

            "import_style_counts": style_counts,
            "has_dynamic_imports": style_counts["dynamic"] > 0,
            "has_requires": style_counts["require"] > 0,
            "has_reexports": style_counts["reexport"] > 0,

            "imports_unresolved_details": imports_unresolved_details,
            "imports_resolved_details": imports_resolved_details,
            "imports_details": imports_details,
        }

        sources += 1
        total_imports_all += len(imports_all)
        total_unresolved += len(imports_unresolved)
        total_internal += len(imports_internal)
        total_resolved_targets += len(resolved_targets)

    out = {"schema_version": "folder_index@v1", "files": out_files}

    log_event(
        "TS:build_folder_index_done",
        ms=int((time.perf_counter() - t0) * 1000),
        sources=sources,
        files_out=len(out_files),
        imports_all=total_imports_all,
        imports_internal=total_internal,
        imports_unresolved=total_unresolved,
        resolved_targets=total_resolved_targets,
    )
    return out